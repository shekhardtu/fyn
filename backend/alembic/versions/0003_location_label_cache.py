"""Cache one place name per ~150m cell.

Reverse geocoding turns the coordinates a device reports into something a
person recognises — "Indiranagar, Karnataka" rather than 12.97, 77.59. The
lookup is a network call to a third party, so it happens once per cell and is
kept, never once per transaction.

The table is not user-scoped. A cell's name is a fact about the world, not
about anyone who went there; scoping it per user would multiply identical
lookups against a provider whose terms ask for caching, and would copy the same
coordinates into a row per person.

Revision ID: 0003_location_label_cache
Revises: 0002_merge_transport_into_travel
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_location_label_cache"
down_revision: Union[str, None] = "0002_merge_transport_into_travel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "location_labels",
        # A UUID primary key like every other entity here; the cell keeps its
        # uniqueness as a constraint so identity stays uniform across the schema.
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        # Uniqueness lives in the index below, not here as well: the model
        # declares `unique=True, index=True`, which SQLAlchemy renders as a
        # unique index alone. Both would leave the schema one constraint ahead
        # of the models.
        sa.Column("geohash", sa.String(length=12), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("country", sa.String(length=120), nullable=True),
        # Null means the provider answered and had no name for this cell —
        # a highway, open water, a new development. That is a real answer and
        # is cached, so it is not asked again on every save.
        sa.Column("display", sa.String(length=160), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_location_labels_geohash", "location_labels", ["geohash"], unique=True)


def downgrade() -> None:
    op.drop_table("location_labels")
