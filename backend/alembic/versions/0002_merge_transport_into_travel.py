"""Replace the Transport category with Travel.

Getting somewhere is one activity whether it is a commute or a holiday, and the
split put `flights` under both roots — so the same expense could be filed in two
places depending on which one the user reached for first.

Transport is removed outright rather than folded in: its subcategories do not
survive, and Travel keeps its own. Rows filed under Transport are repointed at
Travel and lose their subcategory, because the subcategory they named no longer
exists and inventing a nearest match would put spending under a label the user
never chose. Anyone who wants Fuel or Tolls back can add them — custom
categories are a first-class feature, which is what makes this safe to delete.

The category is preserved on every row, so nothing becomes uncategorised.

Revision ID: 0002_merge_transport_into_travel
Revises: 0001_baseline
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_merge_transport_into_travel"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every table that points at a category or a subcategory. Missing one would
# leave rows referencing a row this migration then deletes.
CATEGORY_REFERENCES = ("transactions", "transaction_drafts", "transaction_category_hints", "budgets")
SUBCATEGORY_REFERENCES = ("transactions", "transaction_drafts", "transaction_category_hints")

TRANSPORT_SUBCATEGORIES = [
    ("cab", "Cab"),
    ("fuel", "Fuel"),
    ("public_transit", "Public transit"),
    ("flights", "Flights"),
    ("parking", "Parking"),
    ("tolls", "Tolls"),
    ("other", "Other"),
]


def _category_id(bind, slug: str):
    return bind.execute(sa.text("SELECT id FROM categories WHERE slug = :slug"), {"slug": slug}).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    transport_id = _category_id(bind, "transport")
    travel_id = _category_id(bind, "travel")
    if transport_id is None or travel_id is None:
        # A database seeded after this change has no Transport root to replace.
        return

    # Clear the subcategory first: it is about to be deleted, and the columns
    # that reference it are ON DELETE SET NULL only on some tables.
    for table in SUBCATEGORY_REFERENCES:
        bind.execute(
            sa.text(
                f"UPDATE {table} SET subcategory_id = NULL WHERE subcategory_id IN "
                "(SELECT id FROM subcategories WHERE category_id = :cid)"
            ),
            {"cid": transport_id},
        )
    for table in CATEGORY_REFERENCES:
        bind.execute(
            sa.text(f"UPDATE {table} SET category_id = :new WHERE category_id = :old"),
            {"new": travel_id, "old": transport_id},
        )

    bind.execute(sa.text("DELETE FROM subcategories WHERE category_id = :cid"), {"cid": transport_id})
    bind.execute(sa.text("DELETE FROM categories WHERE id = :cid"), {"cid": transport_id})


def downgrade() -> None:
    """Restore the Transport root and its subcategories, empty.

    Which rows were Transport before the merge is not recoverable — they were
    repointed at Travel and are indistinguishable from rows that were always
    Travel. So this rebuilds the taxonomy and leaves the data where it is,
    rather than guessing.
    """
    bind = op.get_bind()
    if _category_id(bind, "transport") is not None:
        return
    travel_id = _category_id(bind, "travel")
    if travel_id is None:
        return

    transport_id = bind.execute(
        sa.text(
            "INSERT INTO categories (id, slug, name, icon, scope, user_id, created_at, updated_at) "
            "SELECT gen_random_uuid(), 'transport', 'Transport', 'car', scope, user_id, now(), now() "
            "FROM categories WHERE id = :cid RETURNING id"
        ),
        {"cid": travel_id},
    ).scalar()
    for slug, name in TRANSPORT_SUBCATEGORIES:
        bind.execute(
            sa.text(
                "INSERT INTO subcategories (id, category_id, slug, name, scope, user_id, created_at, updated_at) "
                "SELECT gen_random_uuid(), :cid, :slug, :name, scope, user_id, now(), now() "
                "FROM categories WHERE id = :cid"
            ),
            {"cid": transport_id, "slug": slug, "name": name},
        )
