"""Make the Databricks target reachable at localhost inside the worker.

WHY THIS EXISTS, and why it is the platform's problem rather than the product's.

`databricks-connect`'s channel builder skips TLS only when the host is literally
`localhost`. Any other name -- including a compose service name -- gets a TLS
handshake regardless of `use_ssl=false` in the connection string. Measured, not
inferred: connecting to `sc://databricks:8447/;use_ssl=false` from the worker
reaches the right address (`ipv4:192.168.158.13:8447`) and fails with
`WRONG_VERSION_NUMBER`, which is a plaintext server being sent a TLS hello.

The product must not care. `spark_session.connect()` is byte-identical to the
Jobs leaf's, where `localhost:<published port>` is simply true; asking the
product to special-case a network topology would put deployment knowledge in a
data product, which is the one thing the split was for.

So the platform makes it true here too: a plain TCP forwarder on 127.0.0.1:8447
inside the worker's own network namespace, pointing at the emulator. Nothing is
translated or inspected -- bytes in, bytes out.

Stdlib only, deliberately. The alternative was `socat`, which is not in the
airflow image and would need a root layer to install.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("BRIDGE_LISTEN_PORT", "8447"))
TARGET_HOST = os.environ.get("BRIDGE_TARGET_HOST", "databricks")
TARGET_PORT = int(os.environ.get("BRIDGE_TARGET_PORT", "8447"))


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        # Half-close so the peer sees EOF rather than hanging on a stream that
        # will never produce another byte.
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _serve(client: socket.socket) -> None:
    try:
        upstream = socket.create_connection((TARGET_HOST, TARGET_PORT))
    except OSError as exc:
        # Closing without a reply is the honest signal: the bridge is up and the
        # emulator is not. Pretending otherwise would surface as a Spark error
        # about a session rather than a missing service.
        print(f"bridge: cannot reach {TARGET_HOST}:{TARGET_PORT} ({exc})", flush=True)
        client.close()
        return
    threading.Thread(target=_pump, args=(client, upstream), daemon=True).start()
    threading.Thread(target=_pump, args=(upstream, client), daemon=True).start()


def main() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((LISTEN_HOST, LISTEN_PORT))
    listener.listen(128)
    print(
        f"bridge: {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}",
        flush=True,
    )
    while True:
        client, _ = listener.accept()
        _serve(client)


if __name__ == "__main__":
    sys.exit(main())
