"""Backfill completed draft-linked taxonomy widget receipts.

Revision ID: 0010_widget_receipts
Revises: 0009_financial_history
"""

from copy import deepcopy

from alembic import op
import sqlalchemy as sa


revision = "0010_widget_receipts"
down_revision = "0009_financial_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    messages = sa.Table("messages", metadata, autoload_with=bind)
    drafts = sa.Table("transaction_drafts", metadata, autoload_with=bind)
    categories = sa.Table("categories", metadata, autoload_with=bind)
    subcategories = sa.Table("subcategories", metadata, autoload_with=bind)

    category_names = {str(row.id): row.name for row in bind.execute(sa.select(categories.c.id, categories.c.name))}
    subcategory_names = {str(row.id): row.name for row in bind.execute(sa.select(subcategories.c.id, subcategories.c.name))}
    draft_rows = {
        str(row.id): row
        for row in bind.execute(sa.select(drafts.c.id, drafts.c.category_id, drafts.c.subcategory_id, drafts.c.state))
    }

    for row in bind.execute(sa.select(messages.c.id, messages.c.widgets)):
        raw_widgets = row.widgets or []
        changed = False
        updated_widgets = deepcopy(raw_widgets)
        for widget in updated_widgets:
            if not isinstance(widget, dict) or widget.get("type") != "taxonomy_editor":
                continue
            data = widget.get("data") if isinstance(widget.get("data"), dict) else {}
            if data.get("lifecycle") in {"completed", "cancelled"}:
                continue
            actions = widget.get("actions") if isinstance(widget.get("actions"), list) else []
            first_payload = actions[0].get("payload", {}) if actions and isinstance(actions[0], dict) else {}
            draft_id = data.get("draftId") or first_payload.get("draftId")
            draft = draft_rows.get(str(draft_id)) if draft_id else None
            if not draft or draft.state in {"NEEDS_CLARIFICATION", "CANCELLED"}:
                continue
            operation = data.get("operation")
            result_id = draft.subcategory_id if operation == "create_subcategory" else draft.category_id
            names = subcategory_names if operation == "create_subcategory" else category_names
            name = names.get(str(result_id)) if result_id else None
            if not name:
                continue
            values = {
                "draftId": str(draft.id),
                "categoryId": str(draft.category_id) if draft.category_id else None,
                "name": name,
            }
            data.update({
                "lifecycle": "completed",
                "name": name,
                "resultId": str(result_id),
                "draftId": str(draft.id),
                "categoryId": str(draft.category_id) if draft.category_id else None,
                "completion": {"action": operation, "values": values},
            })
            widget["data"] = data
            widget["actions"] = []
            changed = True
        if changed:
            bind.execute(sa.update(messages).where(messages.c.id == row.id).values(widgets=updated_widgets))


def downgrade() -> None:
    # Receipts describe actions that already happened. Removing their display
    # metadata during a schema downgrade would make the historical transcript
    # less accurate, so the data repair is intentionally retained.
    pass
