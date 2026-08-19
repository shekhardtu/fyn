"""The Operator's record-listing read.

Analytical reads are not here. Every total, breakdown, comparison, cash
position, and recurring-pattern question executes through the template pool and
the governed harness (``analysis_harness``), so validation, tenancy, template
caching, chart grammar, and the durable audit trail apply to all of them
identically. The hand-written wrappers that used to answer those questions
directly were deleted on 2026-08-18: a second read path meant a second set of
renderings to maintain and a second way for an answer to be right about the
data and wrong about the question.

Listing individual records is the one read the semantic layer cannot express —
it aggregates metrics over dimensions, and a list is neither — so it stays a
typed tool the Operator calls directly and writes the answer from. Adding
anything else here is an architecture decision, not a convenience.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy.orm import Session

from .agent_tools import tool_contract
from . import intelligence
from .tool_models import TransactionListInput, TransactionListResult


@tool_contract(description=(
    "List this user's individual canonical transactions matching exact filters, for questions that "
    "want the records themselves rather than a total. Slugs and the transaction type must match "
    "governed values exactly; an unknown slug returns no rows. Each row carries `amount` already formatted in the user's currency alongside the exact `amount_minor`; show `amount`/`total`, never the raw minor units. "
    "`total_minor` covers every matching record, not only the returned page, and `truncated` is true "
    "when more records matched than `limit` returned — say so instead of implying the list is "
    "complete. Quote returned amounts and dates exactly; never re-derive or re-sum them."
), input_model=TransactionListInput, output_model=TransactionListResult)
def transaction_list(
    db: Session,
    user_id: UUID,
    today: date,
    transaction_type: str | None = None,
    merchant: str | None = None,
    category_slug: str | None = None,
    subcategory_slug: str | None = None,
    account: str | None = None,
    tag: str | None = None,
    min_amount_minor: int | None = None,
    max_amount_minor: int | None = None,
    start: date | None = None,
    end: date | None = None,
    sort_by: str = "transaction_at",
    sort_direction: str = "desc",
    limit: int = 50,
) -> dict:
    return intelligence.transaction_rows(
        db,
        user_id,
        transaction_type=transaction_type,
        merchant=merchant,
        category_slug=category_slug,
        subcategory_slug=subcategory_slug,
        account=account,
        tag=tag,
        min_amount_minor=min_amount_minor,
        max_amount_minor=max_amount_minor,
        start=start,
        # A future end date would read as a period the user has not lived yet;
        # the governed read stops at their own local today.
        end=min(end, today) if end else None,
        sort_by=sort_by,
        sort_direction=sort_direction,
        limit=limit,
    )
