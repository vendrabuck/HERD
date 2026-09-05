"""The cabling Alembic migration chain is linear (issue #710).

No prior test walked this service's real migrations/versions directory: a branch
point (two files sharing a down_revision, or a revision with no path to head) would
otherwise only surface at `make migrate` time against a real database. Reads the
actual files via Alembic's own ScriptDirectory, the same mechanism `make migrate`
uses, so this is authoritative rather than a regex over the file headers.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_CABLING_DIR = Path(__file__).resolve().parent.parent


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(_CABLING_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_CABLING_DIR / "migrations"))
    # alembic.ini's prepend_sys_path has no path_separator set; Alembic 1.13+ warns
    # (DeprecationWarning) about the legacy split fallback, and this repo's
    # filterwarnings = ["error"] would turn that into a test failure. Setting it
    # here (rather than editing the checked-in alembic.ini, which make migrate
    # also reads) keeps the fix scoped to this test's own Config instance.
    cfg.set_main_option("path_separator", "os")
    return ScriptDirectory.from_config(cfg)


def test_single_head():
    """Exactly one head: no two migrations branch off the same down_revision."""
    script = _script_directory()
    assert script.get_heads() == script.get_heads()  # sanity: call is stable
    heads = script.get_heads()
    assert len(heads) == 1, f"expected exactly one head, got {heads}"


def test_chain_is_linear_and_walks_to_base():
    """walk_revisions from head reaches every migration file exactly once, ending
    at a single root (down_revision=None)."""
    script = _script_directory()
    revisions = list(script.walk_revisions())
    revision_ids = {r.revision for r in revisions}

    on_disk = {
        p.stem.split("_", 1)[0] for p in (_CABLING_DIR / "migrations" / "versions").glob("*.py")
    }
    assert revision_ids == on_disk, (
        f"walk_revisions found {revision_ids} but the versions directory has {on_disk}; "
        "a mismatch means a branch, a gap, or an orphaned file"
    )

    roots = [r for r in revisions if r.down_revision is None]
    assert len(roots) == 1, f"expected exactly one root revision, got {[r.revision for r in roots]}"

    down_revisions = [r.down_revision for r in revisions if r.down_revision is not None]
    assert len(down_revisions) == len(set(down_revisions)), (
        "two migrations share a down_revision: the chain branches instead of staying linear"
    )


def test_head_is_0010_active_fork_listing_index():
    """The current head is 0010 (issue #710's partial-index migration)."""
    script = _script_directory()
    assert script.get_heads() == ["0010"]
    head = script.get_revision("0010")
    assert head.down_revision == "0009"
