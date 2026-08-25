"""Repo-boundary tests. No Docker, no emulator.

This cell had none. `databricks-platform-jobs` has `test_the_platform_holds_no
_product`, and its absence here is why three Contoso identifiers sat in the
platform: a hard-coded Unity catalog, the delta volume's default path, and the
vendor database fallbacks.
"""

from __future__ import annotations

import pathlib
import re
import sys
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
# CORE, which is not a product. `contoso-data-product` holds the transforms,
# the contracts and the figures every cell must produce; the acceptance run
# checks it out to assert this run's numbers against them (G50). A second
# product on this platform would check out the same repository, so naming it
# couples this platform to nothing.
#
# THE NEGATIVE LOOKAHEAD IS THE WHOLE POINT. Listed in ACCEPTANCE_NAMES as a
# plain substring it would also strip the leading two thirds of every LEAF
# name, so `contoso-data-product-fabric-airflow3` would become
# `-fabric-airflow3` and pass -- the same defect the comment above records
# against listing bare "contoso", one level less obvious. Followed by a hyphen
# it is a leaf, and a leaf that is not this cell's must still fail.
ACCEPTANCE_CORE = re.compile(r"contoso-data-product(?!-)")
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


def names_a_product(rel: str, line: str) -> bool:
    """Whether this line couples the platform to a particular product.

    EXTRACTED so the guard can be tested rather than only run. Its exemptions
    have twice been written in a form that permitted everything -- bare
    "contoso" once, and `contoso-data-product` as a plain substring would do it
    again by eating the front of every leaf name -- and an inline loop cannot
    be handed a line that ought to fail.
    """
    code = line.split("#", 1)[0]
    if "contoso" not in code.lower():
        return False
    # Strip the allowed vendor names, then see if any mention survives.
    stripped = PENDING_RENAME.sub("", VENDOR_OK.sub("", code))
    if rel.startswith(ACCEPTANCE_ONLY):
        stripped = ACCEPTANCE_CATALOG.sub("", stripped)
        for name in ACCEPTANCE_NAMES:
            stripped = stripped.replace(name, "")
        # AFTER the exact leaf names, never before: this cell's own leaf has to
        # be consumed as a whole before a prefix rule sees it.
        stripped = ACCEPTANCE_CORE.sub("", stripped)
    return "contoso" in stripped.lower()


def test_the_guard_still_catches_a_product_that_is_not_this_one():
    """THE GUARD FOR THE GUARD. Every exemption above is a hole by construction.

    A workflow line naming a DIFFERENT cell's leaf must still fail, and the
    Makefile -- where coupling would actually live -- must fail on names the
    workflow is allowed to carry.
    """
    wf = ".github/workflows/acceptance.yml"
    must_fail = [
        (wf, "          repository: calvinchengx/contoso-data-product-fabric-airflow3"),
        (wf, "          path: contoso-data-product-snowflake-tasks"),
        (wf, "      - run: make verify DAG=contoso_hourly"),
        ("Makefile", "PRODUCT ?= ../contoso-data-product-databricks-airflow3"),
        ("Makefile", "DAG ?= contoso_daily"),
    ]
    for rel, line in must_fail:
        assert names_a_product(rel, line), f"the guard permits {line.strip()!r} in {rel}"

    must_pass = [
        (wf, "          repository: calvinchengx/contoso-data-product"),
        (wf, "          path: contoso-data-product-databricks-airflow3"),
        (wf, "      - run: make verify DAG=contoso_daily"),
        (wf, "      UC_CATALOG: contoso"),
    ]
    for rel, line in must_pass:
        assert not names_a_product(rel, line), f"the guard rejects {line.strip()!r} in {rel}"


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
            if names_a_product(rel, line):
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


# --- digest pins ---------------------------------------------------------------
#
# Docker IGNORES the tag in `repo:tag@sha256:...` — the digest wins, silently.
# So a version moved without its digest is not a stale pin, it is the wrong
# image running under the right name.

def _scripts():
    sys.path.insert(0, str(ROOT / "scripts"))


def test_every_pinned_image_has_both_a_version_and_a_digest():
    _scripts()
    from digests import PINS

    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    for prefix in PINS:
        assert re.search(rf"^{prefix}_VERSION=.+$", text, re.M), prefix
        assert re.search(rf"^{prefix}_DIGEST=sha256:[0-9a-f]{{64}}$", text, re.M), prefix


def test_the_compose_file_fetches_every_image_by_digest():
    _scripts()
    from digests import PINS

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for prefix, image in PINS.items():
        for line in compose.splitlines():
            if f"image: {image}:" in line:
                assert f"@${{{prefix}_DIGEST" in line, f"pulled by tag alone: {line.strip()}"
                break
        else:
            raise AssertionError(f"{image} is not referenced in docker-compose.yml")


def test_no_image_falls_back_to_a_default_version():
    """`postgres:${POSTGRES_VERSION:-16}` is how this stack ran on whatever
    Postgres 16 point release was current, with versions.env not required to
    have an opinion. A default is a floating pin wearing a fixed one's clothes."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for line in compose.splitlines():
        if line.strip().startswith("image:"):
            assert ":-" not in line, f"image has a default version: {line.strip()}"


def test_a_release_records_the_digest_it_verified(tmp_path):
    """set_release already resolved the digest to check the tag exists; the
    bug it prevents is resolving it, printing it, and writing only the tag."""
    _scripts()
    import set_release

    versions = tmp_path / "versions.env"
    versions.write_text((ROOT / "versions.env").read_text(encoding="utf-8"),
                        encoding="utf-8")
    fake = "sha256:" + "f" * 64
    saved = (set_release.VERSIONS, set_release.tag_exists)
    try:
        set_release.VERSIONS = versions
        set_release.tag_exists = lambda image, tag: (True, fake)
        set_release.main(["--fabric", "9.9.9"])
    finally:
        set_release.VERSIONS, set_release.tag_exists = saved

    written = versions.read_text(encoding="utf-8")
    for prefix in ("SAIL_ENGINE", "SPARK_CLIENT"):
        # What a fabric release moves for these two is the RELEASE and the
        # digest. Same invariant as before -- never record one without the
        # other -- one field along.
        assert re.search(rf"^{prefix}_RELEASE=9\.9\.9$", written, re.M), prefix
        assert re.search(rf"^{prefix}_DIGEST={fake}$", written, re.M), (
            f"{prefix} moved its release and kept the old digest")
        # And the TAG must NOT move. It names the dependency the image carries,
        # so retagging it onto the release is how the pin stops saying which
        # Sail is inside -- which is what this scheme exists to prevent.
        assert not re.search(rf"^{prefix}_VERSION=9\.9\.9$", written, re.M), (
            f"{prefix}_VERSION was retagged onto the release; it names the "
            f"dependency, not the release that built it")
