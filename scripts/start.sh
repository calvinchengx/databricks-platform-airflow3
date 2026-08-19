#!/usr/bin/env bash
# Bring up an Airflow 3 stack and provision the connections the product asks
# for by name. Everything here is platform business; the product never sees it.
set -euo pipefail

# A COMBINED trust bundle: the system CAs PLUS the emulator's certificate.
#
# Pointing SSL_CERT_FILE at the emulator cert alone REPLACES the system store
# for every Python client in this container, so public HTTPS breaks -- measured:
# `dbt deps` could not reach hub.getdbt.com with CERTIFICATE_VERIFY_FAILED. The
# emulator has to be ADDED to what is already trusted, not substituted for it.
if [ -f /emu-data/tls/cert.pem ]; then
  python3 - <<'CAEOF'
import pathlib
import ssl

out = pathlib.Path("/tmp/ca-bundle.pem")
emulator = pathlib.Path("/emu-data/tls/cert.pem").read_text()
# certifi if present, else the interpreter's own default store. Either way the
# emulator is APPENDED to what is already trusted, never substituted for it.
try:
    import certifi
    system = pathlib.Path(certifi.where()).read_text()
except Exception:  # noqa: BLE001 -- no certifi in this image
    default = ssl.get_default_verify_paths().cafile
    system = pathlib.Path(default).read_text() if default else ""
out.write_text(system + "\n" + emulator)
print(f"platform: trust bundle = system CAs + emulator cert ({out})", flush=True)
CAEOF
fi

airflow db migrate >/dev/null

# api-server FIRST: in Airflow 3 the scheduler hands tasks to it over HTTP and
# a worker with nothing listening fails every task with `Connection refused`.
# ORDER MATTERS, and getting it wrong is a race a restart loses every time.
# The scheduler must not start until the connections exist: this DAG ships
# unpaused with a daily schedule, so the moment the dag-processor finds it the
# scheduler fires a run -- measured at FOUR SECONDS after container start,
# against a platform that had provisioned nothing. The task then failed with
# `The conn_id 'fabric' isn't defined`, which reads like a fault in the product
# rather than a platform that was not ready yet.
#
# api-server first because tasks execute through it and the CLI below wants it
# live; scheduler and dag-processor last, once there is something to run against.
airflow api-server &

for _ in $(seq 1 60); do
  curl -sf http://localhost:8080/api/v2/monitor/health >/dev/null 2>&1 && break
  sleep 2
done

# THE AUDIENCES THIS PLATFORM CAN MINT. A PLATFORM CONCERN, not a product one.
#
# Real Entra can already issue for Azure SQL in every tenant: it is a
# first-party resource and no registration exists to perform. This emulator
# mints only for audiences it knows, so the non-default one is registered here
# -- a setup difference, resolved by the platform exactly like the connections
# below, so no product code learns that a registration was ever needed.
#
# Without it a Warehouse token request fails with a bare HTTP 400 from the
# token endpoint, and the first thing that notices is dbt-fabric reporting
# `Invalid authorization specification` -- which reads like a bad credential
# rather than an audience the issuer was never told about.
python3 - <<'PY' || echo "platform: WARNING -- could not register the Azure SQL audience"
import json, os, urllib.error, urllib.request

# The token URL is per-tenant; the admin API sits at the origin.
root = "/".join(os.environ["ENTRA_TOKEN_URL"].split("/")[:3])
body = json.dumps({"displayName": "Azure SQL",
                   "appIdUri": "https://database.windows.net",
                   "isConfidential": False}).encode()
req = urllib.request.Request(f"{root}/admin/api/apps", data=body,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"platform: audience https://database.windows.net registered ({r.status})")
except urllib.error.HTTPError as e:
    # 409 is the normal case on a restart against a warm emulator.
    if e.code != 409:
        raise
    print("platform: audience https://database.windows.net already registered")
PY

# THE SEAM. The product's DAGs say conn_id="fabric" and nothing else -- no
# host, no tenant, no grant type. Here that resolves to the emulator; in
# production the same conn_id is provisioned against real Fabric and not one
# line of product code changes.
airflow connections delete fabric >/dev/null 2>&1 || true
airflow connections add fabric \
  --conn-type generic \
  --conn-host "${FABRIC_API_ROOT}" \
  --conn-extra "$(python3 - <<'PY'
import json, os
print(json.dumps({
    "api_root": os.environ["FABRIC_API_ROOT"],
    "onelake_url": os.environ["FABRIC_ONELAKE_URL"],
    # The emulator serves OneLake on the Fabric port and routes by Host header,
    # the way `curl --resolve` does. Real Fabric has its own hostname, so this
    # is empty there -- the one target difference, and it lives in the
    # connection rather than in the product.
    "onelake_host_header": os.environ.get("FABRIC_ONELAKE_HOST_HEADER", ""),
    "token_url": os.environ["ENTRA_TOKEN_URL"],
    "client_id": os.environ["ENTRA_CLIENT_ID"],
    "client_secret": os.environ["ENTRA_CLIENT_SECRET"],
    # WHAT delta-rs NEEDS TO REACH THIS OneLake, stated by the platform
    # because it is a property of THIS deployment. The product merges these
    # into its storage options without inspecting them, so the same product
    # code runs against real Fabric -- which supplies none of this and gets
    # the default Azure endpoint and ordinary certificate validation.
    #
    # `azure_allow_invalid_certificates` is here and nowhere else: the
    # emulator serves a self-signed certificate and object_store has no
    # CA-bundle option, so this is a narrow allowance for one client rather
    # than verification being turned off for the worker -- which would also
    # silence a genuine failure.
    "storage_options": {
        "azure_endpoint": os.environ["FABRIC_ONELAKE_URL"].rstrip("/") + "/onelake",
        "azure_allow_invalid_certificates": "true",
    },
    "target": os.environ.get("FABRIC_TARGET", "emulator"),
}))
PY
)" >/dev/null
echo "platform: connection 'fabric' provisioned -> ${FABRIC_API_ROOT}"

# The WAREHOUSE, as its own connection. Separate from `fabric` because it is a
# different protocol to a different surface: TDS to a SQL endpoint, not REST to
# a control plane. A product asking for `fabric_warehouse` gets a host and a
# port; the token it authenticates with is minted from the same credential, so
# production points this at a real Warehouse and no dbt profile changes.
airflow connections delete fabric_warehouse >/dev/null 2>&1 || true
airflow connections add fabric_warehouse \
  --conn-type generic \
  --conn-host "${FABRIC_TDS_HOST:-fabric-emulator}" \
  --conn-port "${FABRIC_TDS_PORT:-1433}" \
  --conn-extra "{\"token_url\": \"${ENTRA_TOKEN_URL}\", \"client_id\": \"${ENTRA_CLIENT_ID}\", \"client_secret\": \"${ENTRA_CLIENT_SECRET}\"}" >/dev/null
echo "platform: connection 'fabric_warehouse' provisioned -> ${FABRIC_TDS_HOST:-fabric-emulator}:${FABRIC_TDS_PORT:-1433}"

# ONE CONNECTION PER DECLARED VENDOR. The product's DAG asks for these by the
# name the declaration gives them and learns nothing else -- so in production
# the same names are provisioned against the real vendors and no DAG changes.
if [ -f "${SOURCES_DECL:-/nonexistent}" ]; then
  python3 - <<'PYEOF'
import json, os, pathlib, subprocess
decl = pathlib.Path(os.environ["SOURCES_DECL"])
root = decl.parent
vendors, cur = [], None
for raw in decl.read_text().splitlines():
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip() or line.strip() == "vendors:" or line.startswith("version:"):
        continue
    t = line.strip()
    if t.startswith("- "):
        cur = {}; vendors.append(cur); t = t[2:]
    if cur is None or ":" not in t:
        continue
    k, _, v = t.partition(":")
    cur[k.strip()] = v.strip()
for v in vendors:
    if v.get("kind") == "cdc":
        # A stream vendor has no base URL. Its broker and topic ride in the
        # connection extra the way an HTTP vendor's URL rides in its host --
        # same seam, so in production these point at the real ERP's stream and
        # no DAG changes.
        name = v["name"].replace("_", "-")
        subprocess.run(["airflow", "connections", "delete", v["conn"]],
                       capture_output=True)
        r = subprocess.run(["airflow", "connections", "add", v["conn"],
                            "--conn-type", "generic",
                            "--conn-extra", json.dumps({
                                "bootstrap": f"{name}-broker:9092",
                                "topic": v.get("topic", ""),
                            })], capture_output=True, text=True)
        print(f"platform: connection {v['conn']!r} -> {name}-broker:9092 "
              f"({v.get('topic')})" if r.returncode == 0 else
              f"platform: WARNING could not provision {v['conn']!r}: "
              f"{(r.stderr or r.stdout).strip()[:200]}", flush=True)
        continue
    if v.get("kind") != "openapi":
        continue
    host = f"http://{v['name'].replace('_','-')}:{v['port']}"
    # The vendor's own credential, from its fixture directory. Each vendor has
    # its own key that rotates separately -- that is the point of there being
    # more than one vendor, and sharing one here would erase it.
    key_file = root / v["data"] / ".api-key"
    key = key_file.read_text().strip() if key_file.exists() else ""
    # IDEMPOTENT, and LOUD when it fails. Provisioning runs on every start
    # against a metadata DB that may already carry these -- an existing
    # connection is the normal case on restart, not an error. It used to be
    # both: `check=True` under `set -e` meant a second `make up` killed the
    # entire platform, and `capture_output` hid the reason.
    subprocess.run(["airflow", "connections", "delete", v["conn"]],
                   capture_output=True)
    r = subprocess.run(["airflow", "connections", "add", v["conn"],
                        "--conn-type", "http", "--conn-host", host,
                        "--conn-password", key],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # Report and carry on: one vendor that cannot be provisioned should
        # fail ITS OWN tasks with a missing-connection error, not prevent the
        # platform from starting at all.
        print(f"platform: WARNING could not provision {v['conn']!r}: "
              f"{(r.stderr or r.stdout).strip()[:300]}", flush=True)
    else:
        print(f"platform: connection {v['conn']!r} provisioned -> {host}", flush=True)
PYEOF
fi

# Only now is it safe to let anything run.
airflow scheduler &
airflow dag-processor &

echo "platform: ready. Airflow UI on :8080 (published as ${AIRFLOW_PORT:-18080})"
wait -n
