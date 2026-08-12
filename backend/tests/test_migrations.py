"""Guard the migration chain, which the rest of the suite never exercises.

``conftest`` builds its schema with ``Base.metadata.create_all``, so every
other test would keep passing with a completely broken revision. These tests
replay the migrations against a throwaway PostgreSQL database instead.

What this catches, and what it cannot
-------------------------------------
``0001_initial`` also uses ``create_all``, so a fresh database reaches
revision 0001 already holding every table and index the *current* models
declare. Later revisions therefore find their objects already present and
skip them, which means this suite proves:

* the chain is linear, single-headed and replayable end to end;
* no revision raises — a bad ``op.execute``, a data migration against a
  missing column, or an operation on an object outside the models is caught;
* the newest revision can be rolled back.

It cannot check that a ``create_table`` or ``create_index`` body is correct,
because those never execute on a fresh database. Closing that gap means
replacing ``0001``'s ``create_all`` with a real baseline, at which point
these tests become total rather than partial.
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
    """Create an empty database, hand back its URL, and drop it afterwards.

    Creating a database cannot run inside a transaction, hence AUTOCOMMIT. The
    name is unique per run so a crashed test can never collide with, or be
    mistaken for, the developer's own database.
    """
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
        # str(URL) masks the password as "***", which would be handed to the
        # driver verbatim. The credentials have to be rendered in full.
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
    """A second head means two migrations claim the same parent."""
    script = ScriptDirectory.from_config(_alembic_config())

    heads = script.get_heads()
    assert len(heads) == 1, f"expected one head, found {sorted(heads)}"

    revisions = list(script.walk_revisions())
    parents = [revision.down_revision for revision in revisions if revision.down_revision]
    duplicated = {parent for parent in parents if parents.count(parent) > 1}
    assert not duplicated, f"revisions branch at {sorted(duplicated)}"

    bases = script.get_bases()
    assert len(bases) == 1, f"expected one base revision, found {sorted(bases)}"


def test_migrations_build_the_schema_the_models_declare(migrated_database):
    """Replay every migration onto an empty database and diff against the models.

    The diff is a weak assertion while 0001 seeds from ``create_all`` — see the
    module docstring. The strong assertion here is that the replay completes at
    all, which is what was broken: 0009 through 0014 each tried to create
    objects 0001 had already made, so no fresh database could be built.
    """
    upgrade(_alembic_config(migrated_database), "head")

    engine = sa.create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"migrations and models disagree: {diff}"


def test_the_newest_migration_can_be_rolled_back(migrated_database):
    """A downgrade nobody has run is a downgrade nobody knows is broken."""
    config = _alembic_config(migrated_database)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    previous = script.get_revision(head).down_revision

    upgrade(config, "head")
    downgrade(config, previous)

    engine = sa.create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    assert current == previous
