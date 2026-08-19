#!/usr/bin/env python3
"""Point versions.env at a fabric-emulator release, digests and all.

WHY A SCRIPT. Every fabric-emulator release rebuilds and overwrites the tags it
publishes, so three pins move at once and a tag alone names a moving target.
Bumping the emulator by hand and forgetting the compute is the failure this
prevents: the stack then runs a new emulator against the previous release's
Sail and statement agent, which is a combination nobody tested.

Measured on v0.29.0: all three digests moved while two of the three tags stayed
exactly where they were (`emulator-sail:0.7.0`, `emulator-spark-agent:4.2.0`).

Usage:  python3 scripts/set_release.py 0.29.0
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.env"

# var-prefix -> (image, which version var supplies its tag)
PINS = {
    "FABRIC_EMULATOR": ("ghcr.io/calvinchengx/fabric-emulator", "release"),
    "SAIL_ENGINE": ("ghcr.io/calvinchengx/emulator-sail", "SAIL_ENGINE_VERSION"),
    "SPARK_CLIENT": ("ghcr.io/calvinchengx/emulator-spark-agent", "SPARK_CLIENT_VERSION"),
}


def digest_of(image: str, tag: str) -> str:
    """Ask the registry what this tag points at RIGHT NOW."""
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", f"{image}:{tag}",
         "--format", "{{.Manifest.Digest}}"],
        capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip().startswith("sha256:"):
        raise SystemExit(f"cannot read digest for {image}:{tag}: "
                         f"{(out.stderr or out.stdout).strip()[:200]}")
    return out.stdout.strip()


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: set_release.py <emulator-version>   e.g. 0.29.0")
    release = sys.argv[1].lstrip("v")
    text = VERSIONS.read_text()

    def current(var: str) -> str:
        m = re.search(rf"^{var}=(.+)$", text, re.M)
        if not m:
            raise SystemExit(f"{var} not found in versions.env")
        return m.group(1).strip()

    for prefix, (image, tag_source) in PINS.items():
        tag = release if tag_source == "release" else current(tag_source)
        digest = digest_of(image, tag)
        before = current(f"{prefix}_DIGEST")
        text = re.sub(rf"^{prefix}_DIGEST=.*$", f"{prefix}_DIGEST={digest}", text, flags=re.M)
        moved = "moved" if before != digest else "unchanged"
        print(f"{image}:{tag}\n  {before[:19]}… -> {digest[:19]}…  ({moved})")

    text = re.sub(r"^FABRIC_EMULATOR_VERSION=.*$",
                  f"FABRIC_EMULATOR_VERSION={release}", text, flags=re.M)
    VERSIONS.write_text(text)
    print(f"\nversions.env now pins fabric-emulator {release}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
