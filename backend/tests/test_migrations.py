"""Guard the frozen migration baseline against the live ORM schema.

The ordinary test fixtures build SQLite tables directly from ``Base.metadata``.
These tests instead create a throwaway PostgreSQL database and prove that the
explicit Alembic baseline is single-headed, replayable, schema-equivalent to
the models, and reversible.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.command import downgrade, upgrade
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.config import get_settings
from app.database import Base

BACKEND_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _alembic_config(url: str | None = None) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    if url:
        config.attributes["sqlalchemy.url"] = url
    return config


def _server_url() -> sa.engine.URL:
    return sa.make_url(get_settings().database_url)


@pytest.fixture()
def migrated_database() -> str:
    """Create an empty database, hand back its URL, and drop it afterwards."""
    base = _server_url()
    name = f"alembic_check_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except sa.exc.OperationalError as error:
        admin.dispose()
        pytest.skip(f"PostgreSQL is not reachable for migration tests: {error}")

    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ), {"name": name})
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_migration_chain_is_linear_with_a_single_head():
    script = ScriptDirectory.from_config(_alembic_config())

    heads = script.get_heads()
    assert len(heads) == 1, f"expected one head, found {sorted(heads)}"

    revisions = list(script.walk_revisions())
    parents = [revision.down_revision for revision in revisions if revision.down_revision]
    duplicated = {parent for parent in parents if parents.count(parent) > 1}
    assert not duplicated, f"revisions branch at {sorted(duplicated)}"

    bases = script.get_bases()
    assert len(bases) == 1, f"expected one base revision, found {sorted(bases)}"
    assert heads == bases == ["0001_baseline"]


def test_baseline_is_frozen_instead_of_importing_live_metadata():
    source = (BACKEND_ROOT / "alembic" / "versions" / "0001_baseline.py").read_text()
    assert "Base.metadata" not in source
    assert "create_all" not in source


def test_migrations_build_the_schema_the_models_declare(migrated_database):
    upgrade(_alembic_config(migrated_database), "head")

    engine = sa.create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"migrations and models disagree: {diff}"


def test_the_baseline_can_be_rolled_back(migrated_database):
    config = _alembic_config(migrated_database)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    previous = script.get_revision(head).down_revision

    upgrade(config, "head")
    downgrade(config, previous or "base")

    engine = sa.create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    assert current is None
