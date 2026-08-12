"""Backfill persisted transaction widgets to canonical UTC timestamps.

Conversation messages are durable records too. Older widgets embedded a local
``date`` (and sometimes split ``time``/``timezone``) even after their linked
transaction row had a precise event instant. Rewriting that JSON keeps old
threads readable by the new contracts and prevents a refresh from losing time.

Revision ID: 0017_widget_timestamps
Revises: 0016_utc_timestamps
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alembic import op
import sqlalchemy as sa


revision = "0017_widget_timestamps"
down_revision = "0016_utc_timestamps"
branch_labels = None
depends_on = None


UTC = timezone.utc
TRANSACTION_WIDGETS = {"confirmation_card", "transaction_preview", "transaction_edit"}


def _utc_string(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _legacy_instant(data: dict, owner_timezone: str) -> str | None:
    raw_day = data.get("date")
    if not raw_day:
        return None
    try:
        day = date.fromisoformat(str(raw_day))
        clock = time.fromisoformat(str(data.get("time") or "00:00:00"))
        try:
            source_zone = ZoneInfo(str(data.get("timezone") or owner_timezone or "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            source_zone = ZoneInfo("UTC")
        return _utc_string(datetime.combine(day, clock, tzinfo=source_zone))
    except (TypeError, ValueError):
        return None


def _linked_instant(data: dict, transactions: dict[str, str], drafts: dict[str, str], owner_timezone: str) -> str | None:
    return (
        transactions.get(str(data.get("transactionId")))
        or drafts.get(str(data.get("draftId")))
        or _legacy_instant(data, owner_timezone)
    )


def _canonicalize_record(data: dict, instant: str | None) -> bool:
    if not instant:
        return False
    changed = data.get("transactionAt") != instant or any(key in data for key in ("date", "time", "timezone"))
    data["transactionAt"] = instant
    for key in ("date", "time", "timezone"):
        data.pop(key, None)
    completion = data.get("completion")
    values = completion.get("values") if isinstance(completion, dict) else None
    if isinstance(values, dict) and (
        values.get("transactionAt") is not None
        or any(key in values for key in ("date", "time", "timezone"))
    ):
        changed |= values.get("transactionAt") != instant or any(key in values for key in ("date", "time", "timezone"))
        values["transactionAt"] = instant
        for key in ("date", "time", "timezone"):
            values.pop(key, None)
    return changed


def _upgrade_widget(widget: dict, transactions: dict[str, str], drafts: dict[str, str], owner_timezone: str) -> bool:
    widget_type = widget.get("type")
    data = widget.get("data")
    if not isinstance(data, dict):
        return False
    changed = False
    if widget_type in TRANSACTION_WIDGETS:
        changed |= _canonicalize_record(data, _linked_instant(data, transactions, drafts, owner_timezone))
        if widget_type == "transaction_edit" and isinstance(data.get("fields"), list):
            fields = ["transaction_at" if field == "date" else field for field in data["fields"]]
            changed |= fields != data["fields"]
            data["fields"] = fields
    elif widget_type == "transaction_list" and isinstance(data.get("transactions"), list):
        for row in data["transactions"]:
            if isinstance(row, dict):
                changed |= _canonicalize_record(row, transactions.get(str(row.get("id"))) or _legacy_instant(row, owner_timezone))
    elif widget_type == "data_table" and isinstance(data.get("rows"), list):
        rows_changed = False
        for row in data["rows"]:
            if isinstance(row, dict) and str(row.get("id")) in transactions:
                rows_changed |= _canonicalize_record(row, transactions[str(row["id"])])
        if rows_changed and isinstance(data.get("columns"), list):
            for column in data["columns"]:
                if isinstance(column, dict) and column.get("key") == "date":
                    column.update({"key": "transactionAt", "label": "Transaction time", "type": "datetime"})
        changed |= rows_changed
    return changed


def _local_day(value: str, owner_timezone: str) -> str:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    try:
        target = ZoneInfo(owner_timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        target = ZoneInfo("UTC")
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(target).date().isoformat()


def _downgrade_record(data: dict, owner_timezone: str) -> bool:
    value = data.get("transactionAt")
    if not value:
        return False
    data["date"] = _local_day(str(value), owner_timezone)
    data.pop("transactionAt", None)
    completion = data.get("completion")
    values = completion.get("values") if isinstance(completion, dict) else None
    if isinstance(values, dict) and values.get("transactionAt"):
        values["date"] = _local_day(str(values["transactionAt"]), owner_timezone)
        values.pop("transactionAt", None)
    return True


def _downgrade_widget(widget: dict, owner_timezone: str) -> bool:
    widget_type = widget.get("type")
    data = widget.get("data")
    if not isinstance(data, dict):
        return False
    changed = False
    if widget_type in TRANSACTION_WIDGETS:
        changed |= _downgrade_record(data, owner_timezone)
        if widget_type == "transaction_edit" and isinstance(data.get("fields"), list):
            fields = ["date" if field == "transaction_at" else field for field in data["fields"]]
            changed |= fields != data["fields"]
            data["fields"] = fields
    elif widget_type == "transaction_list" and isinstance(data.get("transactions"), list):
        for row in data["transactions"]:
            if isinstance(row, dict):
                changed |= _downgrade_record(row, owner_timezone)
    elif widget_type == "data_table" and isinstance(data.get("rows"), list):
        rows_changed = False
        for row in data["rows"]:
            if isinstance(row, dict):
                rows_changed |= _downgrade_record(row, owner_timezone)
        if rows_changed and isinstance(data.get("columns"), list):
            for column in data["columns"]:
                if isinstance(column, dict) and column.get("key") == "transactionAt":
                    column.update({"key": "date", "label": "Date", "type": "date"})
        changed |= rows_changed
    return changed


def _instant_map(connection, table: str, column: str) -> dict[str, str]:
    rows = connection.execute(sa.text(f"SELECT id::text, {column} FROM {table}"))
    return {row[0]: _utc_string(row[1]) for row in rows if row[1] is not None}


def _messages(connection):
    return connection.execute(sa.text("""
        SELECT message.id, message.widgets, owner.timezone
        FROM messages AS message
        JOIN conversations AS conversation ON conversation.id = message.conversation_id
        JOIN users AS owner ON owner.id = conversation.user_id
        WHERE json_array_length(message.widgets) > 0
    """))


def _write_widgets(connection, message_id, widgets: list) -> None:
    messages = sa.table("messages", sa.column("id", sa.Uuid()), sa.column("widgets", sa.JSON()))
    connection.execute(sa.update(messages).where(messages.c.id == message_id).values(widgets=widgets))


def upgrade() -> None:
    connection = op.get_bind()
    transactions = _instant_map(connection, "transactions", "transaction_at")
    drafts = _instant_map(connection, "transaction_drafts", "transaction_at")
    for message_id, widgets, owner_timezone in _messages(connection):
        if not isinstance(widgets, list):
            continue
        changed = False
        for widget in widgets:
            if isinstance(widget, dict):
                changed |= _upgrade_widget(widget, transactions, drafts, owner_timezone)
        if changed:
            _write_widgets(connection, message_id, widgets)


def downgrade() -> None:
    connection = op.get_bind()
    for message_id, widgets, owner_timezone in _messages(connection):
        if not isinstance(widgets, list):
            continue
        changed = False
        for widget in widgets:
            if isinstance(widget, dict):
                changed |= _downgrade_widget(widget, owner_timezone)
        if changed:
            _write_widgets(connection, message_id, widgets)
