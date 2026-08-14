"""Repair direction, taxonomy and spend-nature inconsistencies.

Revision ID: 0023_tx_classification_invariant
Revises: 0022_agent_run_delivery_mode
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0023_tx_classification_invariant"
down_revision = "0022_agent_run_delivery_mode"
branch_labels = None
depends_on = None


categories = sa.table(
    "categories",
    sa.column("id", sa.Uuid()),
    sa.column("slug", sa.String()),
)
subcategories = sa.table(
    "subcategories",
    sa.column("id", sa.Uuid()),
    sa.column("category_id", sa.Uuid()),
    sa.column("slug", sa.String()),
)
transactions = sa.table(
    "transactions",
    sa.column("id", sa.Uuid()),
    sa.column("transaction_type", sa.String()),
    sa.column("category_id", sa.Uuid()),
    sa.column("subcategory_id", sa.Uuid()),
    sa.column("spend_nature", sa.String()),
    sa.column("description", sa.Text()),
)
drafts = sa.table(
    "transaction_drafts",
    sa.column("id", sa.Uuid()),
    sa.column("transaction_type", sa.String()),
    sa.column("category_id", sa.Uuid()),
    sa.column("subcategory_id", sa.Uuid()),
    sa.column("spend_nature", sa.String()),
    sa.column("raw_text", sa.Text()),
)


def _preferred_leaf(kind: str, text: str, fallback: str | None) -> str:
    lowered = text.casefold()
    if kind == "income":
        if "salary" in lowered or "payroll" in lowered:
            return "salary"
        if "freelance" in lowered or "consulting" in lowered:
            return "freelance"
        if "interest" in lowered:
            return "interest"
    if kind == "investment":
        if "mutual fund" in lowered or "sip" in lowered:
            return "mutual_fund"
        if "stock" in lowered or "share" in lowered:
            return "stocks"
        if "fixed deposit" in lowered or " fd " in f" {lowered} ":
            return "fixed_deposit"
    return fallback or "other"


def upgrade() -> None:
    connection = op.get_bind()
    category_rows = connection.execute(sa.select(categories.c.id, categories.c.slug)).mappings().all()
    if not category_rows:
        return
    category_by_slug = {row["slug"]: row["id"] for row in category_rows}
    slug_by_category = {row["id"]: row["slug"] for row in category_rows}
    subcategory_rows = connection.execute(sa.select(
        subcategories.c.id,
        subcategories.c.category_id,
        subcategories.c.slug,
    )).mappings().all()
    subcategory_by_path = {(row["category_id"], row["slug"]): row["id"] for row in subcategory_rows}
    subcategory_path_by_id = {row["id"]: (row["category_id"], row["slug"]) for row in subcategory_rows}
    non_expense_roots = {"income", "investment"}
    required_root = {"income": "income", "investment": "investment"}

    def normalized(kind: str, category_id, subcategory_id, nature: str, text: str, *, persisted: bool):
        source_path = subcategory_path_by_id.get(subcategory_id)
        source_leaf = source_path[1] if source_path else None
        if kind in required_root:
            target_category = category_by_slug.get(required_root[kind])
            preferred = _preferred_leaf(kind, text or "", source_leaf)
            target_subcategory = subcategory_by_path.get((target_category, preferred)) or subcategory_by_path.get((target_category, "other"))
            return target_category, target_subcategory, "unknown"
        if kind != "expense":
            return None, None, "unknown"

        category_slug = slug_by_category.get(category_id)
        if category_slug in non_expense_roots:
            target_category = category_by_slug.get("other") if persisted else None
        elif persisted and category_id is None:
            target_category = category_by_slug.get("other")
        else:
            target_category = category_id
        if target_category is None:
            return None, None, nature
        if source_path and source_path[0] == target_category:
            target_subcategory = subcategory_id
        elif persisted:
            target_subcategory = subcategory_by_path.get((target_category, source_leaf or "other")) or subcategory_by_path.get((target_category, "other"))
        else:
            # An unresolved draft must remain unresolved so its state machine
            # asks the person instead of silently categorizing it.
            target_subcategory = None
        return target_category, target_subcategory, nature

    for row in connection.execute(sa.select(
        transactions.c.id,
        transactions.c.transaction_type,
        transactions.c.category_id,
        transactions.c.subcategory_id,
        transactions.c.spend_nature,
        transactions.c.description,
    )).mappings():
        category_id, subcategory_id, nature = normalized(
            row["transaction_type"], row["category_id"], row["subcategory_id"], row["spend_nature"], row["description"] or "", persisted=True,
        )
        if (category_id, subcategory_id, nature) != (row["category_id"], row["subcategory_id"], row["spend_nature"]):
            connection.execute(
                transactions.update().where(transactions.c.id == row["id"]).values(
                    category_id=category_id,
                    subcategory_id=subcategory_id,
                    spend_nature=nature,
                )
            )

    for row in connection.execute(sa.select(
        drafts.c.id,
        drafts.c.transaction_type,
        drafts.c.category_id,
        drafts.c.subcategory_id,
        drafts.c.spend_nature,
        drafts.c.raw_text,
    )).mappings():
        category_id, subcategory_id, nature = normalized(
            row["transaction_type"], row["category_id"], row["subcategory_id"], row["spend_nature"], row["raw_text"] or "", persisted=False,
        )
        if (category_id, subcategory_id, nature) != (row["category_id"], row["subcategory_id"], row["spend_nature"]):
            connection.execute(
                drafts.update().where(drafts.c.id == row["id"]).values(
                    category_id=category_id,
                    subcategory_id=subcategory_id,
                    spend_nature=nature,
                )
            )


def downgrade() -> None:
    # This migration removes contradictory classifications. Re-introducing the
    # invalid combinations would be data corruption, so the repair is retained
    # when the revision marker is rolled back.
    pass
