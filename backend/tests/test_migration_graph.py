"""Validate the migration graph without requiring a database connection."""

import warnings
from pathlib import Path

from alembic.script import ScriptDirectory


def test_migrations_have_unique_revisions_and_one_connected_head():
    migration_dir = Path(__file__).resolve().parents[1] / "alembic"
    scripts = ScriptDirectory(dir=str(migration_dir))
    # Alembic otherwise only warns about duplicate IDs before overwriting them.
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        assert len(scripts.get_heads()) == 1
        revisions = list(scripts.walk_revisions())
    assert len(scripts.get_bases()) == 1
    assert len(revisions) == len(list((migration_dir / "versions").glob("*.py")))


def test_historical_revision_ids_still_resolve_to_original_migrations():
    migration_dir = Path(__file__).resolve().parents[1] / "alembic"
    scripts = ScriptDirectory(dir=str(migration_dir))
    for revision, filename in {
        "a1b2c3d4e5f6": "a1b2c3d4e5f6_add_pet_agent_and_actions.py",
        "b2c3d4e5f6a7": "b2c3d4e5f6a7_add_graph_knowledge_base.py",
    }.items():
        assert Path(scripts.get_revision(revision).path).name == filename
