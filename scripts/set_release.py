#!/usr/bin/env python3
"""Point versions.env at a release. TWO cadences, because this cell has two.

WHAT WAS HERE BEFORE. A copy of fabric-platform-airflow3's script, which pins a
`fabric-emulator` release and three DIGESTS. This repository runs no fabric
emulator and pins by TAG, so that script could not work with any argument:

    $ python3 scripts/set_release.py 0.2.7
    cannot read digest for ghcr.io/calvinchengx/fabric-emulator:0.2.7: not found

It would have failed on the fabric lookup, and then again on `SAIL_ENGINE_VERSION`
and `SPARK_CLIENT_VERSION`, which do not exist here. A release tool that cannot
run is worse than no tool: it looks like the bump is covered. This repository
drifted twice while it sat there — to 0.2.5 against a family on 0.2.6, and to
0.30.0 compute against a family on 0.32.0, the release that stopped the
statement agent sharing `sys.argv` between concurrent tasks.

THE TWO CADENCES. `DATABRICKS_EMULATOR_VERSION` moves with a databricks-emulator
release. `SAIL_ENGINE_RELEASE` and `SPARK_CLIENT_RELEASE` do not: Sail and the statement
agent are built and published by **fabric-emulator**, tagged with ITS release
number in an OCI LABEL rather than in their tag, so what moves when fabric
releases is their digest and that label. Bumping one and forgetting the other
is what leaves a new workspace binary running last month's compute, which is the
drift this script exists to prevent — so each cadence is a separate, explicit
invocation rather than a guess.

    python3 scripts/set_release.py --databricks 0.2.7
    python3 scripts/set_release.py --fabric 0.32.0

THE SIDECARS' _VERSION FOLLOWS THE RELEASE'S DEPENDENCY PINS. It names what
the image carries (pysail, pyspark-client), and this script used to leave it
alone on the belief a fabric release never changes that. v0.36.0 moved pysail
0.7.0 -> 0.7.1. `--fabric` now reads both from fabric-emulator's pyproject.toml
at the release tag, the same pins that chose the images' tags.

THE TAGS ARE CHECKED. Both cadences publish tags that are REBUILT rather than
moved, so a tag naming nothing is a live possibility and a pin to it fails only
later, in CI, as a pull error. The registry is asked whether the tag resolves
before anything is written. `--no-verify` skips that for offline use, and says
so loudly.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.env"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")

# cadence -> {variable: image that must carry the tag}
CADENCES = {
    "databricks": {
        "DATABRICKS_EMULATOR_VERSION": "ghcr.io/calvinchengx/databricks-emulator",
    },
    # _RELEASE, not _VERSION. These two are TAGGED for the dependency they
    # carry (pysail, pyspark-client), so the release number never goes into
    # their _VERSION. That moves only to the dependency the release shipped;
    # see TAGGED_BY.
    "fabric": {
        "SAIL_ENGINE_RELEASE": "ghcr.io/calvinchengx/emulator-sail",
        "SPARK_CLIENT_RELEASE": "ghcr.io/calvinchengx/emulator-spark-agent",
    },
}

# prefix -> the pin in fabric-emulator's pyproject.toml the sidecar is tagged
# with. The same map as fabric-emulator's scripts/image_tags.py.
TAGGED_BY = {"SAIL_ENGINE": "pysail", "SPARK_CLIENT": "pyspark-client"}

# A TAG, not a branch: what the release was built from, and it cannot move.
FABRIC_PYPROJECT = ("https://raw.githubusercontent.com/calvinchengx/"
                    "fabric-emulator/v{release}/pyproject.toml")


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def carried_versions(release: str) -> dict[str, str]:
    """The dependency version each sidecar carries in fabric `release`.

    Read with image_tags.py's own rule: exactly one `==` pin per package.
    """
    url = FABRIC_PYPROJECT.format(release=release)
    try:
        text = fetch(url)
    except OSError as err:
        raise SystemExit(f"cannot read {url}: {err}") from None
    carried = {}
    for prefix, package in TAGGED_BY.items():
        found = set(re.findall(rf'"{re.escape(package)}==([0-9][^"]*)"', text))
        if len(found) != 1:
            raise SystemExit(f"v{release} pins {package} as {sorted(found) or 'nothing'}; "
                             f"expected exactly one == version")
        carried[prefix] = found.pop()
    return carried


def tag_exists(image: str, tag: str) -> tuple[bool, str]:
    """Ask the registry whether image:tag resolves RIGHT NOW."""
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", f"{image}:{tag}",
         "--format", "{{.Manifest.Digest}}"],
        capture_output=True, text=True,
    )
    if out.returncode == 0 and out.stdout.strip().startswith("sha256:"):
        return True, out.stdout.strip()
    return False, (out.stderr or out.stdout).strip().splitlines()[-1:][0] if (out.stderr or out.stdout).strip() else "no such tag"


def set_vars(text: str, updates: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Rewrite only the named assignments, leaving comments and layout alone."""
    moved: dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, old = stripped.partition("=")
        key, old = key.strip(), old.strip()
        if key in updates:
            moved[key] = old
            lines[i] = f"{key}={updates[key]}\n"
    return "".join(lines), moved


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--databricks", metavar="VERSION",
                       help="a databricks-emulator release, e.g. 0.2.7")
    group.add_argument("--fabric", metavar="VERSION",
                       help="a fabric-emulator release: moves Sail and the spark agent")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the registry check (offline); the pin is then unproven")
    args = ap.parse_args(argv)

    cadence = "databricks" if args.databricks else "fabric"
    version = (args.databricks or args.fabric).lstrip("v")
    if not SEMVER.match(version):
        sys.exit(f"not a version: {version!r} — expected something like 0.2.7")

    wanted = CADENCES[cadence]

    # The registry check already resolves the digest; RECORD it rather than
    # printing and discarding it. Docker ignores the tag in
    # `repo:tag@sha256:...`, so a version moved without its digest would leave
    # the stack pulling the previous image while versions.env named the new one.
    resolved = {}
    if args.no_verify:
        print("  !! --no-verify: the tags below were NOT checked against the registry")
        print("     and their digests are therefore left as they are — the pin now")
        print("     names a version this run did not confirm the digest for.")
    else:
        for var, image in wanted.items():
            ok, detail = tag_exists(image, version)
            if not ok:
                sys.exit(f"{image}:{version} does not resolve ({detail}).\n"
                         f"Nothing was written. Check the release published its images "
                         f"before pinning to it.")
            resolved[var.rsplit("_", 1)[0]] = detail
            print(f"  {image}:{version} -> {detail[:19]}…")

    updates = {v: version for v in wanted}
    if cadence == "fabric":
        if args.no_verify:
            print("  !! --no-verify: SAIL_ENGINE_VERSION and SPARK_CLIENT_VERSION were")
            print("     NOT read from the release and are left as they are.")
        else:
            updates.update({f"{prefix}_VERSION": dep
                            for prefix, dep in carried_versions(version).items()})

    text = VERSIONS.read_text(encoding="utf-8")
    new, moved = set_vars(text, updates)
    if resolved:
        new, _ = set_vars(new, {f"{prefix}_DIGEST": digest
                                for prefix, digest in resolved.items()})
    missing = [v for v in updates if v not in moved]
    if missing:
        sys.exit(f"{VERSIONS.name} has no {', '.join(missing)} to set — this script "
                 f"and the file have drifted apart.")
    VERSIONS.write_text(new, encoding="utf-8")
    for var, old in moved.items():
        note = "  (unchanged)" if old == updates[var] else ""
        print(f"  {var}: {old} -> {updates[var]}{note}")

    if cadence == "databricks":
        print("  Sail and the spark agent are NOT moved: they ship on fabric's "
              "cadence. Use --fabric for those.")
    else:
        print("  DATABRICKS_EMULATOR_VERSION is NOT moved: it ships on the "
              "databricks-emulator cadence. Use --databricks for that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
