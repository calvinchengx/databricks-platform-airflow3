"""Repo-boundary tests. No Docker, no emulator.

This cell had none. `databricks-platform-jobs` has `test_the_platform_holds_no
_product`, and its absence here is why three Contoso identifiers sat in the
platform: a hard-coded Unity catalog, the delta volume's default path, and the
vendor database fallbacks.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent

# contoso-sources is the VENDOR declaration every cell consumes, and the vendor
# services it generates are named for the company that owns them. Naming it is
# not naming a product, so these are the documented exceptions.
# THE VENDOR NAMES THE GENERATOR ACTUALLY EMITS. `scripts/sources.py` builds a
# cdc vendor's stack as `{name}-db`, `{name}-broker`, `{name}-connect` and
# `{name}-seed`; this list carried the first two and not the other two, so
# naming either of them anywhere in a config file was reported as the platform
# naming a PRODUCT. It never came up because nothing had written them down
# until now. Checked against the generator, not against the file that tripped it.
VENDOR_OK = re.compile(
    r"contoso-(sources|pos|web|reference|erp-db|erp-broker|erp-connect|erp-seed)")

# KNOWN AND NOT YET FIXED. The platform hands the product four environment
# variables whose NAMES carry the product's identity: CONTOSO_DELTA,
# CONTOSO_PRODUCT_DIR, CONTOSO_STATE, CONTOSO_SNAPSHOT. They are the
# platform-to-product contract, and the product reads them by these exact names
# (`contoso_dbx_airflow/target.py`, `landing.py`, and the DAG), so renaming them
# to something neutral -- PRODUCT_DELTA and friends -- has to happen in BOTH
# repositories in one go or the stack breaks in the middle.
#
# Listed rather than silently permitted: a second product mounted here today
# must read variables named for the first, which is exactly what this test
# exists to forbid. Delete this exemption with the rename.
PENDING_RENAME = re.compile(r"CONTOSO_(DELTA|PRODUCT_DIR|STATE|SNAPSHOT)")

# THE ONE PLACE THIS CELL IS ASSEMBLED, and the only place its product may be
# named. `_config_files`' own docstring draws the line: what must not name a
# product is anything that CONFIGURES the stack, "because that is what a second
# product would inherit". A second product inherits the Makefile, the compose
# and the scripts -- it does not inherit this repository's CI. The acceptance
# workflow is not the platform being coupled to a product; it is the family
# verifying THIS cell, which is a platform plus one specific leaf, and it
# cannot do that without naming it.
#
# THREE NAMES, each one a fact about the cell that the platform refuses to
# know, and each one enumerated rather than matched by pattern:
#
#   the leaf     -- which product this cell runs, checked out by the workflow
#   the DAG      -- `make verify DAG=<id>` takes it as a parameter precisely so
#                   the Makefile does not have to know it
#   the catalog  -- compose declares `${UC_CATALOG:?}` with NO default, because
#                   "a default here would hand a second product the first one's
#                   catalog"; the value therefore belongs to whoever assembles
#                   the cell
#
# The guard stays strong: a DIFFERENT product named in a workflow still fails,
# and so does the Makefile naming any of these -- which is where coupling would
# actually live. Verified both ways.
# The catalog is a BARE word, so it is matched as an assignment rather than
# stripped as a substring. Listing plain "contoso" here stripped it out of
# every other identifier too -- `contoso-data-product-fabric-airflow3` became
# `-data-product-fabric-airflow3` and passed. Caught by the check below that
# names a DIFFERENT product and expects a failure; without that check this
# would have shipped as a guard that permits anything.
ACCEPTANCE_NAMES = (
    "contoso-data-product-databricks-airflow3",
    "contoso_daily",
)
ACCEPTANCE_CATALOG = re.compile(r"UC_CATALOG\s*[:=]\s*contoso\b")
ACCEPTANCE_ONLY = ".github/workflows/"


def _config_files() -> list[pathlib.Path]:
    """The files that CONFIGURE this platform, from git rather than the disk.

    Two exclusions, both deliberate. Generated files -- `.sources.generated.yml`
    is rendered from contoso-sources on every run -- are not tracked, so asking
    git rather than walking the tree skips them without a name-based rule that
    would rot. And PROSE is excluded: a README may say which product this cell
    was built against; what must not name a product is anything that CONFIGURES
    the stack, because that is what a second product would inherit.
    """
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout.split()
    keep = []
    for rel in out:
        if rel.endswith((".md", ".txt")):
            continue
        # `tests/` is excluded because a checker must NAME what it forbids:
        # the patterns below contain the very identifier they look for, so
        # scanning this file makes the guard fail on itself. Found the hard
        # way -- it passed locally while still untracked, and `git ls-files`
        # only picked it up once committed.
        if rel.split("/")[0] in {"product", "data", "tests"}:
            continue
        p = ROOT / rel
        if p.is_file():
            keep.append(p)
    return keep


def test_the_platform_holds_no_product():
    """A platform holds compose, pins, vendors and scripts. Nothing Contoso.

    PRODUCT is a path and the product carries its own task code, so a product
    IDENTIFIER in this repository is the thing that makes "a second product can
    use this platform unchanged" false.
    """
    assert not (ROOT / "platform").exists(), (
        "a platform/ directory is back — the product's steps belong in the leaf"
    )

    offenders: list[str] = []
    for path in _config_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # as_posix(), not str(): on Windows the latter gives
        # `.github\\workflows\\acceptance.yml` and the prefix below never
        # matches, so the guard fired on the exempted file there and passed
        # on Linux -- green on two runners and red on the third.
        rel = path.relative_to(ROOT).as_posix()
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if "contoso" not in code.lower():
                continue
            # Strip the allowed vendor names, then see if any mention survives.
            stripped = PENDING_RENAME.sub("", VENDOR_OK.sub("", code))
            if rel.startswith(ACCEPTANCE_ONLY):
                stripped = ACCEPTANCE_CATALOG.sub("", stripped)
                for name in ACCEPTANCE_NAMES:
                    stripped = stripped.replace(name, "")
            if "contoso" in stripped.lower():
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    assert not offenders, (
        "the platform names a product:\n  " + "\n  ".join(offenders)
    )


def test_the_product_is_supplied_as_a_path():
    """PRODUCT is how the platform learns what to run, and it is a PATH."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^PRODUCT \?= \./product$", makefile, re.M), (
        "PRODUCT must default to the ./product mount point"
    )


def test_the_catalog_is_not_guessed():
    """The Unity catalog belongs to the product, so compose must demand it.

    A default would silently give a second product the first one's catalog,
    which is the same failure as naming the product outright but harder to see.
    """
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "UC_CATALOG:?" in compose, (
        "UC_CATALOG must be REQUIRED (`:?`), like PRODUCT and SOURCES"
    )
    assert not re.search(r"UC_CATALOG:-", compose), (
        "UC_CATALOG has a default; the platform is guessing the product's catalog"
    )
