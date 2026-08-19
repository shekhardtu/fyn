from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ..config import DEFAULT_CURRENCY
from ..domain import FinancialSourceType, SpendNature, TAXONOMY_FIELD_NAMES, TransactionType
from ..event_time import local_date, now_utc
from ..taxonomy_catalog import DefaultCategorySlug, taxonomy_path
from .finance_time import resolve_finance_period

@dataclass
class ExtractedTransaction:
    transaction_type: TransactionType
    amount_minor: int | None
    currency: str = DEFAULT_CURRENCY
    merchant: str | None = None
    source_account: str | None = None
    destination_account: str | None = None
    transaction_date: date | None = None
    category_slug: str | None = None
    subcategory_slug: str | None = None
    transaction_time: str | None = None
    timezone: str | None = None
    location_label: str | None = None
    tags: list[str] = field(default_factory=list)
    spend_nature: SpendNature = SpendNature.UNKNOWN
    explicit_fields: list[str] = field(default_factory=list)
    confidence: Decimal = Decimal("0.50")
    inferred_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    source: FinancialSourceType = FinancialSourceType.MANUAL


AMOUNT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:₹|\$|€|£|rs\.?|inr|usd|eur|gbp)?\s*"
    r"([0-9][0-9,]*(?:\.\d+)?)\s*"
    r"(crores?|cr|lakhs?|lacs?|lakh|lac|k)?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
MERCHANT_PATTERNS = [
    re.compile(r"\b(?:at|to|from)\s+([A-Za-z][A-Za-z0-9&.' -]{1,50}?)(?=\s+(?:for|in|on|today|yesterday|last\s+night|was\s+successful)\b|[.!?,]|$)", re.I),
]

MERCHANT_RULES = {
    "toit": taxonomy_path(DefaultCategorySlug.FOOD, "dining"),
    "swiggy instamart": taxonomy_path(DefaultCategorySlug.FOOD, "groceries"),
    "swiggy": taxonomy_path(DefaultCategorySlug.FOOD, "delivery"),
    "zomato": taxonomy_path(DefaultCategorySlug.FOOD, "delivery"),
    "starbucks": taxonomy_path(DefaultCategorySlug.FOOD, "coffee"),
    "uber": taxonomy_path(DefaultCategorySlug.TRAVEL, "local_transport"),
    "ola": taxonomy_path(DefaultCategorySlug.TRAVEL, "local_transport"),
    "netflix": taxonomy_path(DefaultCategorySlug.BILLS, "subscriptions"),
}

EXPENSE_TEXT_RULES = [
    (("ice cream", "icecream", "gelato", "kulfi"), taxonomy_path(DefaultCategorySlug.FOOD, "ice_cream")),
    (("grocery", "groceries", "supermarket"), taxonomy_path(DefaultCategorySlug.FOOD, "groceries")),
    (("coffee", "cafe", "tea"), taxonomy_path(DefaultCategorySlug.FOOD, "coffee")),
    (("food delivery", "delivery", "swiggy", "zomato"), taxonomy_path(DefaultCategorySlug.FOOD, "delivery")),
    (("dinner", "lunch", "breakfast", "restaurant", "dining", "meal"), taxonomy_path(DefaultCategorySlug.FOOD, "dining")),
    (("cab", "taxi", "uber", "ola"), taxonomy_path(DefaultCategorySlug.TRAVEL, "local_transport")),
    (("petrol", "diesel", "fuel"), taxonomy_path(DefaultCategorySlug.TRAVEL, "other")),
    (("metro", "bus", "train", "public transit"), taxonomy_path(DefaultCategorySlug.TRAVEL, "local_transport")),
    (("flight", "flights", "airfare"), taxonomy_path(DefaultCategorySlug.TRAVEL, "flights")),
    (("travelling", "traveling", "travel", "transport", "commute"), taxonomy_path(DefaultCategorySlug.TRAVEL, "other")),
    (("clothing", "clothes", "apparel"), taxonomy_path(DefaultCategorySlug.SHOPPING, "clothing")),
    (("electronics", "gadget", "laptop", "phone purchase"), taxonomy_path(DefaultCategorySlug.SHOPPING, "electronics")),
    (("household", "furniture"), taxonomy_path(DefaultCategorySlug.SHOPPING, "household")),
    (("movie", "movies", "cinema"), taxonomy_path(DefaultCategorySlug.ENTERTAINMENT, "movies")),
    (("concert", "event", "events"), taxonomy_path(DefaultCategorySlug.ENTERTAINMENT, "events")),
    (("game", "gaming"), taxonomy_path(DefaultCategorySlug.ENTERTAINMENT, "games")),
    (("electricity", "water bill", "gas bill", "utilities"), taxonomy_path(DefaultCategorySlug.BILLS, "utilities")),
    (("internet", "broadband"), taxonomy_path(DefaultCategorySlug.BILLS, "internet")),
    (("mobile bill", "phone bill", "recharge"), taxonomy_path(DefaultCategorySlug.BILLS, "phone")),
    (("subscription", "subscriptions"), taxonomy_path(DefaultCategorySlug.BILLS, "subscriptions")),
    (("doctor", "hospital", "clinic"), taxonomy_path(DefaultCategorySlug.HEALTH, "doctor")),
    (("medicine", "medicines", "pharmacy"), taxonomy_path(DefaultCategorySlug.HEALTH, "pharmacy")),
    (("gym", "fitness"), taxonomy_path(DefaultCategorySlug.HEALTH, "fitness")),
]


def infer_expense_category(text: str) -> tuple[str | None, str | None]:
    lowered = text.lower()
    for tokens, mapping in EXPENSE_TEXT_RULES:
        if any(re.search(rf"\b{re.escape(token)}\b", lowered) for token in tokens):
            return mapping
    return None, None


def parse_amount_minor(text: str) -> int | None:
    candidates = list(AMOUNT_PATTERN.finditer(text))
    if not candidates:
        return None
    # Prefer a currency-marked or magnitude-bearing number over dates/times.
    match = next((m for m in candidates if "₹" in m.group(0) or re.search(r"\b(?:rs|inr|lakh|lac|crore|cr|k)\b", m.group(0), re.I)), candidates[0])
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    suffix = (match.group(2) or "").lower()
    if suffix in {"k"}:
        value *= 1_000
    elif suffix in {"lakh", "lakhs", "lac", "lacs"}:
        value *= 100_000
    elif suffix in {"crore", "crores", "cr"}:
        value *= 10_000_000
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def classify_type(text: str) -> tuple[TransactionType, bool]:
    lowered = text.lower()
    if "loan" in lowered and any(token in lowered for token in ("paid", "payment", "toward", "towards")):
        return TransactionType.LOAN_PAYMENT, False
    rules = [
        (TransactionType.REFUND, ("refund", "refunded")),
        (TransactionType.REIMBURSEMENT, ("reimburs",)),
        (TransactionType.INVESTMENT, ("invested", "mutual fund", "sip ", "stocks")),
        (TransactionType.TRANSFER, ("moved ", "transferred", "transfer ")),
        (TransactionType.CASH_WITHDRAWAL, ("withdrew", "withdrawal", "atm")),
        (TransactionType.CASH_DEPOSIT, ("cash deposit", "deposited cash")),
        (TransactionType.INCOME, ("salary", "credited", "received", "got paid", "freelance", "income")),
        (TransactionType.EXPENSE, ("spent", "paid", "bought", "debited", "purchase", "expense", "expenses")),
    ]
    for transaction_type, tokens in rules:
        # Financial event classification is a write boundary. Substring
        # matching is unsafe here (for example, ``heatmap`` contains ``atm``).
        # Require complete lexical tokens/phrases before declaring an explicit
        # transaction direction.
        if any(re.search(rf"(?<![a-z0-9]){re.escape(token.strip())}(?![a-z0-9])", lowered) for token in tokens):
            return transaction_type, False
    if parse_amount_minor(text) is not None:
        return TransactionType.EXPENSE, True
    return TransactionType.UNKNOWN, False


def extract_transaction(text: str, today: date | None = None, default_currency: str = DEFAULT_CURRENCY) -> ExtractedTransaction:
    today = today or local_date(now_utc(), None)
    lowered = text.lower().strip()
    transaction_type, type_inferred = classify_type(text)
    amount_minor = parse_amount_minor(text)
    currency_match = re.search(r"(?<![A-Za-z])(?:INR|USD|EUR|GBP)(?![A-Za-z])|[₹$€£]|\brs\.?(?![A-Za-z])", text, re.I)
    currency = default_currency.upper()
    if currency_match:
        token = currency_match.group(0).casefold()
        currency = "INR" if token in {"₹", "inr", "rs", "rs."} else "USD" if token in {"$", "usd"} else "EUR" if token in {"€", "eur"} else "GBP"
    tags = list(dict.fromkeys(match.group(1).strip().casefold() for match in re.finditer(r"#([A-Za-z][A-Za-z0-9_-]{1,39})", text)))[:8]

    transaction_date = today
    date_inferred = True
    if "day before yesterday" in lowered:
        transaction_date = today - timedelta(days=2)
        date_inferred = False
    elif "yesterday" in lowered or "last night" in lowered:
        transaction_date = today - timedelta(days=1)
        date_inferred = False
    elif "today" in lowered:
        date_inferred = False

    merchant = None
    for pattern in MERCHANT_PATTERNS:
        match = pattern.search(text)
        if match:
            merchant = match.group(1).strip(" .,-")
            if merchant.lower() in {
                "expense",
                "an expense",
                "my expense",
                "income",
                "my income",
                "transaction",
                "a transaction",
                "my salary",
                "salary",
                "a freelance project",
                "freelance work",
                "my home loan",
                "mutual funds",
            }:
                merchant = None
            break

    source_account = destination_account = None
    if transaction_type == TransactionType.TRANSFER:
        transfer_match = re.search(r"\bfrom\s+([A-Za-z][A-Za-z0-9 &.-]{1,40}?)\s+to\s+([A-Za-z][A-Za-z0-9 &.-]{1,40}?)(?=\s+(?:today|yesterday|on)\b|[.!?,]|$)", text, re.I)
        if transfer_match:
            source_account = transfer_match.group(1).strip()
            destination_account = transfer_match.group(2).strip()
            merchant = None

    category_slug = subcategory_slug = None
    if transaction_type == TransactionType.INCOME:
        category_slug, subcategory_slug = taxonomy_path(
            DefaultCategorySlug.INCOME,
            "salary" if "salary" in lowered else "freelance" if "freelance" in lowered else "other",
        )
    elif transaction_type == TransactionType.INVESTMENT:
        category_slug, subcategory_slug = taxonomy_path(
            DefaultCategorySlug.INVESTMENT,
            "mutual_fund" if "mutual fund" in lowered or "sip" in lowered else "stocks" if "stock" in lowered else "other",
        )
    elif merchant:
        normalized = normalize_merchant(merchant)
        for alias, mapping in MERCHANT_RULES.items():
            if normalized == alias or normalized.startswith(alias + " "):
                category_slug, subcategory_slug = mapping
                break
    if transaction_type == TransactionType.EXPENSE and not category_slug:
        category_slug, subcategory_slug = infer_expense_category(text)

    inferred_fields: list[str] = []
    if type_inferred:
        inferred_fields.append("transaction_type")
    if date_inferred:
        inferred_fields.append("transaction_date")
    if category_slug and transaction_type == TransactionType.EXPENSE:
        inferred_fields.extend(TAXONOMY_FIELD_NAMES)

    spend_nature = SpendNature.UNKNOWN
    if re.search(r"\b(?:essential|necessary)\b", lowered):
        spend_nature = SpendNature.ESSENTIAL
    elif re.search(r"\bdiscretionary\b", lowered):
        spend_nature = SpendNature.DISCRETIONARY
    elif re.search(r"\b(?:avoidable|unnecessary)\b", lowered):
        spend_nature = SpendNature.POTENTIALLY_AVOIDABLE

    location_match = re.search(r"\bin\s+([A-Za-z][A-Za-z .'-]{1,60})(?=\s+(?:today|yesterday|last night|for)\b|[.!?,]|$)", text, re.I)
    location_label = location_match.group(1).strip() if location_match else None
    explicit_fields = []
    if amount_minor is not None:
        explicit_fields.append("amount")
    if currency_match:
        explicit_fields.append("currency")
    if not type_inferred and transaction_type != TransactionType.UNKNOWN:
        explicit_fields.append("transaction_type")
    if not date_inferred:
        explicit_fields.append("transaction_date")
    if merchant:
        explicit_fields.append("merchant")
    if source_account:
        explicit_fields.append("source_account")
    if destination_account:
        explicit_fields.append("destination_account")
    if location_label:
        explicit_fields.append("location")
    if tags:
        explicit_fields.append("tags")
    if spend_nature != SpendNature.UNKNOWN:
        explicit_fields.append("spend_nature")

    missing_fields: list[str] = []
    if amount_minor is None:
        missing_fields.append("amount")
    if transaction_type == TransactionType.UNKNOWN:
        missing_fields.append("transaction_type")
    if transaction_type == TransactionType.EXPENSE and not category_slug:
        missing_fields.append("category")
    if transaction_type == TransactionType.TRANSFER:
        if not source_account:
            missing_fields.append("source_account")
        if not destination_account:
            missing_fields.append("destination_account")

    known = sum(value is not None for value in (amount_minor, transaction_date, category_slug, merchant))
    confidence = Decimal("0.55") + Decimal("0.1") * known
    if type_inferred:
        confidence -= Decimal("0.05")
    confidence = min(confidence, Decimal("0.96"))
    return ExtractedTransaction(
        transaction_type=transaction_type,
        amount_minor=amount_minor,
        currency=currency,
        merchant=merchant,
        source_account=source_account,
        destination_account=destination_account,
        transaction_date=transaction_date,
        category_slug=category_slug,
        subcategory_slug=subcategory_slug,
        location_label=location_label,
        tags=tags,
        spend_nature=spend_nature,
        explicit_fields=explicit_fields,
        confidence=confidence,
        inferred_fields=inferred_fields,
        missing_fields=missing_fields,
    )


def normalize_merchant(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^a-z0-9 ]+", " ", value.lower())
    normalized = re.sub(r"\b(?:online|payment|txn|purchase|pos)\b", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def looks_like_financial_query(text: str) -> bool:
    lowered = text.lower()
    financial_subject = re.search(
        # A question about a connected source — an uploaded sheet, an invoice
        # table, a vendor — is a financial question even when it never says
        # "spend": the words a person uses for their own data are the subject.
        r"\b(?:spend|spent|spending|savings?|breakdown|expenses?|rupees?|money|"
        r"transactions?|income|salary|cash\s+flow|recurring|subscription|afford|"
        r"emi|interest|sip|investment|budget|loan|invoices?|vendors?|merchants?|"
        r"sheet|spreadsheet|upload(?:ed)?|chart|graph|plot|dashboard|category|categories)\b",
        lowered,
    )
    request_signal = re.search(
        r"^\s*(?:how|what|why|show|list|compare|can|could|which|give|tell|total|"
        r"using|project|forecast|analy[sz]e|review|estimate|summarize)\b"
        r"|\b(?:project|forecast|analy[sz]e|compare|calculate|estimate|summarize)\b"
        r"|\?\s*$",
        lowered,
    )
    return bool(financial_subject and request_signal) or any(
        token in lowered
        for token in (
            "how much", "why did", "compare", "breakdown", "biggest expense",
            "recurring", "subscription", "afford", "spending", "duplicate",
            "reconciliation", "need review", "prepay", "interest save", "emi",
            "increase my sip", "investment projection",
            "add up to", "invoices", "budget sheet", "uploaded", "chart", "graph",
        )
    )


def parse_spending_period(text: str, today: date | None = None) -> tuple[date, date, str] | None:
    """Compatibility-free public adapter to the centralized finance resolver."""
    today = today or local_date(now_utc(), None)
    resolution = resolve_finance_period(text, today)
    if resolution.status != "resolved" or not resolution.start_date or not resolution.end_date:
        return None
    return resolution.start_date, resolution.end_date, resolution.label or "Selected period"
