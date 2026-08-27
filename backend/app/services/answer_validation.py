from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
import ast
import re
from typing import Any, Iterable


class EvidenceKind(str, Enum):
    MONEY = "money"
    PERCENT = "percent"
    COUNT = "count"
    NUMBER = "number"
    TEXT = "text"
    DATE = "date"


@dataclass(frozen=True)
class EvidenceFact:
    """One typed result value with the row context that gives it meaning."""

    fact_id: str
    kind: EvidenceKind
    value: Decimal | str
    field: str
    tool: str
    unit: str | None = None
    currency: str | None = None
    dimensions: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "kind": self.kind.value,
            "value": str(self.value),
            "field": self.field,
            "tool": self.tool,
            "unit": self.unit,
            "currency": self.currency,
            "dimensions": dict(self.dimensions),
        }


@dataclass(frozen=True)
class EvidenceClaim:
    kind: EvidenceKind
    value: Decimal
    text: str
    sentence: str
    decimals: int


@dataclass
class EvidenceValidation:
    facts: list[EvidenceFact]
    claims: list[EvidenceClaim]
    unsupported: list[EvidenceClaim] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.unsupported

    @property
    def error_code(self) -> str | None:
        if not self.unsupported:
            return None
        kinds = {claim.kind for claim in self.unsupported}
        if EvidenceKind.MONEY in kinds:
            return "unsupported_money_claim"
        if EvidenceKind.PERCENT in kinds:
            return "unsupported_percentage_claim"
        if EvidenceKind.COUNT in kinds:
            return "unsupported_count_claim"
        return "unsupported_financial_claim"


class ObligationCode(str, Enum):
    MONTHLY_BREAKDOWN = "monthly_breakdown"
    MERCHANT_DRIVERS = "merchant_drivers"
    HISTORICAL_AVERAGE = "historical_average"
    ABSOLUTE_COMPARISON = "absolute_comparison"
    PERCENTAGE_COMPARISON = "percentage_comparison"
    RANKING = "ranking"
    PARTIAL_PERIOD_ALIGNMENT = "partial_period_alignment"
    FORECAST_ASSUMPTIONS = "forecast_assumptions"
    PROTECTED_CONSTRAINTS = "protected_constraints"
    EPISTEMIC_CALIBRATION = "epistemic_calibration"
    EXPLANATION = "explanation"


@dataclass(frozen=True)
class AnswerObligation:
    code: ObligationCode
    description: str


@dataclass(frozen=True)
class AnswerContract:
    obligations: tuple[AnswerObligation, ...]
    period_markers: tuple[str, ...] = ()

    def prompt(self) -> str:
        if not self.obligations:
            return "Answer the question directly from the final SQL result."
        return "Required answer coverage:\n" + "\n".join(
            f"- {item.description}" for item in self.obligations
        )


@dataclass
class CoverageValidation:
    contract: AnswerContract
    missing_evidence: list[AnswerObligation] = field(default_factory=list)
    missing_answer: list[AnswerObligation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.missing_evidence and not self.missing_answer


_NUMBER = r"[-+]?\d[\d,]*(?:\.\d+)?"
_MONEY_PREFIX = re.compile(
    rf"(?P<full>(?:₹|\bINR\b|\bRs\.?|\brupees?)\s*(?P<number>{_NUMBER})"
    r"(?:\s*(?P<scale>lakhs?|crores?))?)",
    re.I,
)
_MONEY_SUFFIX = re.compile(
    rf"(?P<full>(?P<number>{_NUMBER})(?:\s*(?P<scale>lakhs?|crores?))?\s*"
    r"(?:₹|\bINR\b|\brupees?\b))",
    re.I,
)
_PERCENT = re.compile(rf"(?P<full>(?P<number>{_NUMBER})\s*%)", re.I)
_COUNT = re.compile(
    rf"(?P<full>(?P<number>{_NUMBER})(?:\s+(?:expense|income|financial|recent|recorded|total)){{0,2}}\s+(?:transactions?|purchases?|payments?|"
    r"merchants?|categories|accounts?|months?|days?|records?|items?))",
    re.I,
)
_DATE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?(?:[T ][\d:.+\-Z]+)?$")
_MONEY_FIELD = re.compile(r"(?:^|_)(?:amount|spend|spent|total|value|balance|income|expense|cost|delta|difference|change|average|avg|baseline).*minor$|_minor$", re.I)
_PERCENT_FIELD = re.compile(r"(?:pct|percent|percentage|rate)(?:$|_)", re.I)
_BASIS_POINT_FIELD = re.compile(r"(?:bps|basis_points?)(?:$|_)", re.I)
_COUNT_FIELD = re.compile(
    r"(?:^|_)(?:count|quantity|frequency|rank|returned|tenure_months|"
    r"months_saved|prepayment_after_months)(?:$|_)",
    re.I,
)
_OPERATIONAL_FIELDS = {
    "limit", "offset", "row_count", "duration_ms", "template_saved",
    "dataset_name", "sql", "columns", "tables", "kind", "purpose",
    "fields", "result_schema", "display",
}
_EMPTY_RESULT_FIELD = "authoritative_empty_result"
_EMPTY_RANKING_FIELD = "authoritative_empty_ranking"
_NO_RESULT_ANSWER = re.compile(
    r"\b(?:no\b.{0,50}\b(?:transactions?|records?|expenses?|data|results?|activity)\b|"
    r"no\s+(?:matching|recorded|available)?\s*(?:transactions?|records?|expenses?|data|"
    r"results?|activity|categories|merchants?)|nothing\s+(?:matched|was\s+recorded)|"
    r"zero\s+(?:transactions?|records?|expenses?)|cannot\s+be\s+(?:compared|identified)|"
    r"can(?:not|['’]t)\s+(?:compare|identify)|(?:comparison|ranking|volatility)"
    r".{0,24}\bcan(?:not|['’]t)\s+be\s+(?:calculated|identified))\b",
    re.I,
)
_DATA_DEPENDENT_OBLIGATIONS = frozenset({
    ObligationCode.MONTHLY_BREAKDOWN,
    ObligationCode.MERCHANT_DRIVERS,
    ObligationCode.HISTORICAL_AVERAGE,
    ObligationCode.ABSOLUTE_COMPARISON,
    ObligationCode.PERCENTAGE_COMPARISON,
    ObligationCode.RANKING,
    ObligationCode.PARTIAL_PERIOD_ALIGNMENT,
})

_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_ALIASES = {
    "jan": "january", "feb": "february", "mar": "march", "apr": "april",
    "jun": "june", "jul": "july", "aug": "august", "sep": "september",
    "sept": "september", "oct": "october", "nov": "november", "dec": "december",
}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _decimals(number_text: str) -> int:
    return len(number_text.rsplit(".", 1)[1]) if "." in number_text else 0


def _sentence_at(content: str, start: int, end: int) -> str:
    left = max(content.rfind(".", 0, start), content.rfind("\n", 0, start))
    right_candidates = [
        position for position in (content.find(".", end), content.find("\n", end))
        if position >= 0
    ]
    right = min(right_candidates) if right_candidates else len(content)
    return content[left + 1:right].strip()


def extract_evidence_claims(content: str) -> list[EvidenceClaim]:
    """Extract only explicitly typed financial claims from prose.

    Dates, headings and bare enumeration numbers are deliberately not treated
    as money. A financial answer still requires current-turn grounding, but a
    year or table row number cannot make an otherwise correct reply fail.
    """

    claims: list[EvidenceClaim] = []
    occupied: list[tuple[int, int]] = []
    for kind, pattern in (
        (EvidenceKind.MONEY, _MONEY_PREFIX),
        (EvidenceKind.MONEY, _MONEY_SUFFIX),
        (EvidenceKind.PERCENT, _PERCENT),
        (EvidenceKind.COUNT, _COUNT),
    ):
        for match in pattern.finditer(content):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            value = _decimal(match.group("number"))
            if value is None:
                continue
            scale = (match.groupdict().get("scale") or "").casefold()
            if scale.startswith("lakh"):
                value *= Decimal(100_000)
            elif scale.startswith("crore"):
                value *= Decimal(10_000_000)
            claims.append(EvidenceClaim(
                kind=kind,
                value=value,
                text=match.group("full"),
                sentence=_sentence_at(content, match.start(), match.end()),
                decimals=_decimals(match.group("number")),
            ))
            occupied.append((match.start(), match.end()))
    return claims


def contains_financial_claim(content: str) -> bool:
    if extract_evidence_claims(content):
        return True
    return bool(re.search(
        r"\b(?:spent|spending|income|balance|cost|expense|merchant|category)\b.{0,45}"
        r"\b(?:higher|lower|largest|smallest|increased|decreased|above|below)\b|"
        r"\b(?:higher|lower|largest|smallest|increased|decreased|above|below)\b.{0,45}"
        r"\b(?:spent|spending|income|balance|cost|expense|merchant|category)\b",
        content,
        re.I,
    ))


def _tool_result(item: Any) -> Any:
    result = getattr(getattr(item, "result", None), "data", None)
    if isinstance(result, str):
        try:
            return ast.literal_eval(result)
        except (ValueError, SyntaxError):
            return result
    return result


def _dimensions(row: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for key, value in row.items():
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str) and not _decimal(value):
            values.append((str(key), value))
        elif isinstance(value, str) and _DATE.match(value):
            values.append((str(key), value))
    return tuple(values)


def _count_unit(field_name: str, tool: str) -> str | None:
    """Give explicit count facts a semantic unit when their result supplies one."""

    field = field_name.casefold()
    tool_name = tool.casefold()
    if "month" in field or "tenure" in field:
        return "months"
    if "subcategor" in field:
        return "subcategories"
    if "categor" in field:
        return "categories"
    if "merchant" in field:
        return "merchants"
    if "account" in field:
        return "accounts"
    if "transaction" in field or (
        tool_name in {"transaction_list", "spending_summary"}
        and field in {"count", "returned"}
    ):
        return "transactions"
    return None


def _list_count_fact(
    tool: str,
    tool_index: int,
    path: str,
    value: list[Any],
    dimensions: tuple[tuple[str, str], ...],
) -> EvidenceFact | None:
    """Count only lists whose runtime contract gives the collection a meaning."""

    if tool == "read_user_expense_taxonomy" and path == "result":
        field, unit = "category_count", "categories"
    elif tool == "read_user_expense_taxonomy" and path.endswith(":subcategories"):
        field, unit = "subcategory_count", "subcategories"
    elif tool == "transaction_list" and path.endswith(":rows"):
        field, unit = "transaction_count", "transactions"
    else:
        return None
    return EvidenceFact(
        f"{tool}:{tool_index}:{path}:count",
        EvidenceKind.COUNT,
        Decimal(len(value)),
        field,
        tool,
        unit=unit,
        dimensions=dimensions,
    )


def _fact(
    tool: str,
    path: str,
    field_name: str,
    value: Any,
    dimensions: tuple[tuple[str, str], ...],
    currency: str | None,
) -> EvidenceFact | None:
    field_lower = field_name.casefold()
    if field_lower in _OPERATIONAL_FIELDS or value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and _DATE.match(value):
        return EvidenceFact(path, EvidenceKind.DATE, value, field_name, tool, dimensions=dimensions)
    number = _decimal(value) if isinstance(value, (int, float, Decimal)) else None
    if number is not None:
        if _MONEY_FIELD.search(field_name):
            return EvidenceFact(
                path, EvidenceKind.MONEY, number / 100, field_name, tool,
                unit="major", currency=currency, dimensions=dimensions,
            )
        if _BASIS_POINT_FIELD.search(field_name):
            return EvidenceFact(
                path, EvidenceKind.PERCENT, number / 100, field_name, tool,
                unit="percent", dimensions=dimensions,
            )
        if _PERCENT_FIELD.search(field_name):
            return EvidenceFact(
                path, EvidenceKind.PERCENT, number, field_name, tool,
                unit="percent", dimensions=dimensions,
            )
        if _COUNT_FIELD.search(field_name):
            return EvidenceFact(
                path,
                EvidenceKind.COUNT,
                number,
                field_name,
                tool,
                unit=_count_unit(field_name, tool),
                dimensions=dimensions,
            )
        return EvidenceFact(path, EvidenceKind.NUMBER, number, field_name, tool, dimensions=dimensions)
    if isinstance(value, str) and value.strip():
        return EvidenceFact(path, EvidenceKind.TEXT, value.strip(), field_name, tool, dimensions=dimensions)
    return None


def _walk_result(
    facts: list[EvidenceFact],
    tool: str,
    tool_index: int,
    value: Any,
    path: str,
    inherited_dimensions: tuple[tuple[str, str], ...] = (),
    inherited_currency: str | None = None,
) -> None:
    if isinstance(value, list):
        count_fact = _list_count_fact(
            tool,
            tool_index,
            path,
            value,
            inherited_dimensions,
        )
        if count_fact is not None:
            facts.append(count_fact)
        for index, item in enumerate(value):
            _walk_result(
                facts, tool, tool_index, item, f"{path}:{index}",
                inherited_dimensions, inherited_currency,
            )
        return
    if not isinstance(value, dict):
        return
    local_dimensions = tuple(dict([*inherited_dimensions, *_dimensions(value)]).items())
    currency = next(
        (str(item) for key, item in value.items() if "currency" in str(key).casefold() and item),
        inherited_currency,
    )
    for field_name, item in value.items():
        field_path = f"{path}:{field_name}"
        if isinstance(item, (dict, list)):
            if str(field_name).casefold() in _OPERATIONAL_FIELDS:
                continue
            _walk_result(
                facts, tool, tool_index, item, field_path,
                local_dimensions, currency,
            )
            continue
        fact = _fact(
            tool, f"{tool}:{tool_index}:{field_path}", str(field_name), item,
            local_dimensions, currency,
        )
        if fact is not None:
            facts.append(fact)


def evidence_facts(grounding: Iterable[Any]) -> list[EvidenceFact]:
    """Create facts from successful result values, never prompts or arguments."""

    facts: list[EvidenceFact] = []
    for tool_index, item in enumerate(grounding):
        result = _tool_result(item)
        if not isinstance(result, (dict, list)):
            continue
        tool = str(getattr(item, "name", f"tool_{tool_index}"))
        if isinstance(result, dict) and isinstance(result.get("error"), dict):
            continue
        if isinstance(result, dict) and result.get("empty_result") is True:
            facts.append(EvidenceFact(
                f"{tool}:{tool_index}:result:{_EMPTY_RESULT_FIELD}",
                EvidenceKind.TEXT,
                "true",
                _EMPTY_RESULT_FIELD,
                tool,
            ))
        if isinstance(result, dict) and result.get("kind") == "governed_sql":
            rows = result.get("rows") or []
            # A UNION-style analysis can return summary rows while its ranked
            # subgroup is authentically empty. Treat that as proof only when
            # the result contract contains both an explicit rank and ranked
            # entity column, and both are null across every returned row. This
            # is deliberately narrower than an empty result: it can satisfy a
            # ranking obligation, but cannot stand in for unrelated evidence.
            columns = {
                str(column).casefold()
                for column in result.get("columns", [])
                if isinstance(column, str)
            }
            if not columns and rows:
                columns = {
                    str(column).casefold()
                    for row in rows
                    if isinstance(row, dict)
                    for column in row
                }
            rank_columns = {
                column for column in columns
                if re.search(r"(?:^|_)(?:rank|ranking)(?:$|_)", column, re.I)
            }
            entity_columns = {
                column for column in columns
                if re.search(
                    r"(?:^|_)(?:category|subcategory|merchant|vendor|account|item|driver|name)(?:$|_)",
                    column,
                    re.I,
                )
            }
            normalized_rows = [
                {str(key).casefold(): value for key, value in row.items()}
                for row in rows
                if isinstance(row, dict)
            ]
            explicit_empty_rank = bool(
                normalized_rows
                and rank_columns
                and entity_columns
                and all(
                    all(row.get(column) in {None, ""} for column in rank_columns)
                    and all(row.get(column) in {None, ""} for column in entity_columns)
                    for row in normalized_rows
                )
            )
            row_type_columns = {
                column for column in columns
                if column in {"row_type", "result_type", "record_type"}
            }
            ranked_measure_columns = {
                column for column in columns
                if re.search(
                    r"(?:^|_)(?:reduction|saving|opportunity)(?:$|_).*minor$",
                    column,
                    re.I,
                )
            }
            no_ranked_subgroup_rows = bool(
                normalized_rows
                and row_type_columns
                and ranked_measure_columns
                and ObligationCode.RANKING.value in result.get("answer_contract", [])
                and all(
                    not any(
                        re.search(
                            r"category|subcategory|merchant|vendor|driver|rank",
                            str(row.get(column) or ""),
                            re.I,
                        )
                        for column in row_type_columns
                    )
                    and all(
                        row.get(column) in {None, "", 0}
                        for column in ranked_measure_columns
                    )
                    for row in normalized_rows
                )
            )
            if explicit_empty_rank or no_ranked_subgroup_rows:
                facts.append(EvidenceFact(
                    f"{tool}:{tool_index}:result:{_EMPTY_RANKING_FIELD}",
                    EvidenceKind.TEXT,
                    "true",
                    _EMPTY_RANKING_FIELD,
                    tool,
                ))
            for row_index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                dimensions = _dimensions(row)
                currency = next(
                    (str(value) for key, value in row.items() if "currency" in str(key).casefold() and value),
                    None,
                )
                for field_name, value in row.items():
                    fact = _fact(
                        tool,
                        f"{tool}:{tool_index}:rows:{row_index}:{field_name}",
                        str(field_name),
                        value,
                        dimensions,
                        currency,
                    )
                    if fact is not None:
                        facts.append(fact)
            continue
        _walk_result(facts, tool, tool_index, result, "result")
        if (
            isinstance(result, dict)
            and isinstance(result.get("rows"), list)
            and not result["rows"]
            and result.get("returned") == 0
        ):
            facts.append(EvidenceFact(
                f"{tool}:{tool_index}:result:{_EMPTY_RESULT_FIELD}",
                EvidenceKind.TEXT,
                "true",
                _EMPTY_RESULT_FIELD,
                tool,
            ))
    return facts


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _mentioned_dimensions(sentence: str, facts: list[EvidenceFact]) -> dict[str, str]:
    normalized_sentence = f" {_normalized(sentence)} "
    matches: dict[str, set[str]] = {}
    for fact in facts:
        for key, value in fact.dimensions:
            normalized_value = _normalized(value)
            if len(normalized_value) >= 3 and f" {normalized_value} " in normalized_sentence:
                matches.setdefault(key, set()).add(value)
    return {key: next(iter(values)) for key, values in matches.items() if len(values) == 1}


def _scope_matches(fact: EvidenceFact, mentioned: dict[str, str]) -> bool:
    dimensions = dict(fact.dimensions)
    return all(dimensions.get(key) == value for key, value in mentioned.items())


def _value_matches(claim: EvidenceClaim, fact: EvidenceFact) -> bool:
    if not isinstance(fact.value, Decimal):
        return False
    if claim.kind is EvidenceKind.COUNT:
        claim_unit_match = re.search(
            r"\b(transactions?|purchases?|payments?|merchants?|categories|accounts?|"
            r"months?|days?|records?|items?)\b",
            claim.text,
            re.I,
        )
        claim_unit = claim_unit_match.group(1).casefold() if claim_unit_match else None
        if claim_unit:
            aliases = {
                "transaction": "transactions", "transactions": "transactions",
                "purchase": "transactions", "purchases": "transactions",
                "payment": "transactions", "payments": "transactions",
                "category": "categories", "categories": "categories",
                "merchant": "merchants", "merchants": "merchants",
                "account": "accounts", "accounts": "accounts",
                "month": "months", "months": "months",
                "day": "days", "days": "days",
                "record": "records", "records": "records",
                "item": "items", "items": "items",
            }
            normalized_claim_unit = aliases.get(claim_unit, claim_unit)
            if fact.unit and normalized_claim_unit != fact.unit:
                return False
        return fact.value == claim.value
    quantum = Decimal(1).scaleb(-claim.decimals)
    if fact.value.quantize(quantum, rounding=ROUND_HALF_UP) == claim.value:
        return True
    if re.search(r"\b(?:decrease|decreased|below|lower|less|reduction|down)\b", claim.sentence, re.I):
        return abs(fact.value).quantize(quantum, rounding=ROUND_HALF_UP) == abs(claim.value)
    return False


def validate_evidence(
    content: str,
    grounding: Iterable[Any],
    request_text: str = "",
) -> EvidenceValidation:
    facts = evidence_facts(grounding)
    claims = extract_evidence_claims(content)
    requested_claims = extract_evidence_claims(request_text)
    validation = EvidenceValidation(facts=facts, claims=claims)
    authoritative_empty_result = any(
        fact.field == _EMPTY_RESULT_FIELD and fact.value == "true"
        for fact in facts
    )
    for claim in claims:
        mentioned = _mentioned_dimensions(claim.sentence, facts)
        candidates = [
            fact for fact in facts
            if fact.kind is claim.kind and _scope_matches(fact, mentioned)
        ]
        supported_as_declared_input = bool(
            any(
                requested.kind is claim.kind and requested.value == claim.value
                for requested in requested_claims
            )
            and re.search(
                r"\b(?:assum(?:e|ed|ption)|scenario|target|goal|if|purchase|principal|"
                r"cap|requested|specified|"
                r"can(?:not|['’]t)\s+guarantee|unable\s+to\s+guarantee|"
                r"not\s+(?:a\s+)?guarantee)\b",
                claim.sentence,
                re.I,
            )
        )
        supported_as_empty_zero = bool(
            authoritative_empty_result
            and claim.value == 0
            and claim.kind in {EvidenceKind.MONEY, EvidenceKind.COUNT}
            and re.search(
                r"\b(?:no|none|zero|recorded|result|total|found|matching)\b",
                claim.sentence,
                re.I,
            )
        )
        supported_as_unfulfilled_requested_count = bool(
            claim.kind is EvidenceKind.COUNT
            and any(
                requested.kind is EvidenceKind.COUNT
                and requested.value == claim.value
                for requested in requested_claims
            )
            and re.search(
                r"\b(?:no|none|not|cannot|can['’]t|couldn['’]t|unable|nothing)\b"
                r".{0,80}\b(?:available|found|identify|rank|show|recorded|returned|drivers?)\b"
                r"|\b(?:available|found|identify|rank|show|recorded|returned|drivers?)\b"
                r".{0,80}\b(?:no|none|not|cannot|can['’]t|couldn['’]t|unable|nothing)\b",
                claim.sentence,
                re.I,
            )
        )
        if (
            not supported_as_declared_input
            and not supported_as_empty_zero
            and not supported_as_unfulfilled_requested_count
            and not any(
            _value_matches(claim, fact) for fact in candidates
            )
        ):
            validation.unsupported.append(claim)
    return validation


def _month_marker(value: str) -> str | None:
    normalized = value.casefold().strip(". ")
    if normalized in _MONTHS:
        return normalized
    return _MONTH_ALIASES.get(normalized[:4].rstrip(".")) or _MONTH_ALIASES.get(normalized[:3].rstrip("."))


def _period_markers(question: str) -> tuple[str, ...]:
    found: list[tuple[int, str]] = []
    month_pattern = re.compile(
        r"\b(" + "|".join([*_MONTHS, *_MONTH_ALIASES]) + r")[a-z.]*\b",
        re.I,
    )
    for match in month_pattern.finditer(question):
        marker = _month_marker(match.group(1))
        if marker and marker not in [item[1] for item in found]:
            found.append((match.start(), marker))
    if len(found) < 2 or not re.search(r"\b(?:through|to|until|-)\b", question, re.I):
        return tuple(item[1] for item in found)
    first = _MONTHS.index(found[0][1])
    last = _MONTHS.index(found[-1][1])
    count = ((last - first) % 12) + 1
    return tuple(_MONTHS[(first + offset) % 12] for offset in range(count))


def compile_answer_contract(question: str) -> AnswerContract:
    """Compile explicit user obligations; style preferences remain advisory."""

    obligations: list[AnswerObligation] = []

    def require(code: ObligationCode, description: str) -> None:
        if code not in {item.code for item in obligations}:
            obligations.append(AnswerObligation(code, description))

    if re.search(r"\b(?:group(?:ed)?\s+(?:it\s+)?by\s+month|month[- ]by[- ]month|each\s+month)\b", question, re.I):
        require(ObligationCode.MONTHLY_BREAKDOWN, "Show the requested month-by-month values.")
    if re.search(r"\b(?:merchant|vendor)s?\b", question, re.I):
        require(ObligationCode.MERCHANT_DRIVERS, "Name the requested merchant drivers from the result.")
    if re.search(r"\b(?:average|baseline|usual|normal)\b", question, re.I):
        require(ObligationCode.HISTORICAL_AVERAGE, "Show and explain the requested historical average or baseline.")
    if re.search(r"\b(?:compare|versus|vs\.?|difference|delta|higher|lower|more|less)\b", question, re.I):
        require(ObligationCode.ABSOLUTE_COMPARISON, "Put the compared values and their absolute difference together.")
    if re.search(
        r"\b(?:percent(?:age)?\s+(?:change|difference|increase|decrease)|"
        r"rate\s+of\s+change|how\s+much\s+(?:higher|lower)\s+in\s+percent)\b|"
        r"(?:%|\bpercent(?:age)?\b).{0,24}\b(?:change|difference|increase|decrease|"
        r"higher|lower)\b|\b(?:change|difference|increase|decrease|higher|lower)\b"
        r".{0,24}(?:%|\bpercent(?:age)?\b)",
        question,
        re.I,
    ):
        require(ObligationCode.PERCENTAGE_COMPARISON, "Include the requested percentage comparison.")
    if re.search(r"\b(?:largest|biggest|highest|lowest|top|rank)\b", question, re.I):
        require(ObligationCode.RANKING, "Identify the requested largest, top, or ranked result.")
    if (
        re.search(r"\b(?:through|until|to)\s+(?:[a-z]+\s+)?\d{1,2}\b|\b(?:mtd|month[- ]to[- ]date|so far)\b", question, re.I)
        and re.search(r"\b(?:compare|average|baseline|previous|prior)\b", question, re.I)
    ):
        require(ObligationCode.PARTIAL_PERIOD_ALIGNMENT, "Explain the like-for-like elapsed-day comparison.")
    if re.search(r"\b(?:forecast|predict|project(?:ion|ed)?|scenario)\b", question, re.I):
        require(ObligationCode.FORECAST_ASSUMPTIONS, "Separate recorded facts from forecast assumptions.")
    if re.search(r"\bwithout\s+(?:changing|cutting|reducing|touching|affecting)\b|\b(?:must\s+not|do\s+not|don't)\b", question, re.I):
        require(ObligationCode.PROTECTED_CONSTRAINTS, "Address every protected constraint explicitly.")
    if re.search(r"\b(?:guarantee|guaranteed|ensure|certain(?:ty)?|promise)\b", question, re.I):
        require(ObligationCode.EPISTEMIC_CALIBRATION, "Do not present a forecast or historical pattern as a guarantee.")
    if re.search(r"\b(?:why|explain|what\s+drove|drivers?)\b", question, re.I):
        require(ObligationCode.EXPLANATION, "Explain the result and its evidence-backed drivers.")
    return AnswerContract(tuple(obligations), _period_markers(question))


def _field_names(facts: list[EvidenceFact]) -> set[str]:
    return {fact.field.casefold() for fact in facts}


def _has_field(fields: set[str], pattern: str) -> bool:
    expression = re.compile(pattern, re.I)
    return any(expression.search(field) for field in fields)


def _answer_has_period(content: str, marker: str) -> bool:
    month_number = _MONTHS.index(marker) + 1
    aliases = [marker, marker[:3], f"-{month_number:02d}"]
    return any(re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", content, re.I) for alias in aliases)


def validate_coverage(
    content: str,
    contract: AnswerContract,
    facts: list[EvidenceFact],
) -> CoverageValidation:
    validation = CoverageValidation(contract=contract)
    fields = _field_names(facts)
    text_facts = [fact for fact in facts if fact.kind in {EvidenceKind.TEXT, EvidenceKind.DATE}]
    lowered = content.casefold()
    authoritative_empty_result = any(
        fact.field == _EMPTY_RESULT_FIELD and fact.value == "true"
        for fact in facts
    )
    authoritative_empty_ranking = any(
        fact.field == _EMPTY_RANKING_FIELD and fact.value == "true"
        for fact in facts
    )
    acknowledges_empty_result = bool(_NO_RESULT_ANSWER.search(content))

    for obligation in contract.obligations:
        code = obligation.code
        evidence_ok = True
        answer_ok = True
        if (
            authoritative_empty_result
            and code in _DATA_DEPENDENT_OBLIGATIONS
        ):
            # An authenticated successful query that returned no rows is a
            # complete answer for that exact scope. Require the prose to say
            # so, rather than pretending the requested comparison exists.
            evidence_ok = True
            answer_ok = acknowledges_empty_result
        elif code is ObligationCode.MONTHLY_BREAKDOWN:
            if contract.period_markers:
                evidence_text = " ".join(
                    [fact.field for fact in facts] + [str(fact.value) for fact in text_facts]
                )
                evidence_ok = all(_answer_has_period(evidence_text, marker) for marker in contract.period_markers)
                answer_ok = all(_answer_has_period(content, marker) for marker in contract.period_markers)
            else:
                evidence_ok = _has_field(fields, r"month|period|date")
                answer_ok = bool(re.search(r"\b(?:month|monthly)\b|\b\d{4}-\d{2}\b", content, re.I))
        elif code is ObligationCode.MERCHANT_DRIVERS:
            merchants = [
                str(fact.value) for fact in text_facts if "merchant" in fact.field.casefold()
            ]
            evidence_ok = bool(merchants)
            answer_ok = any(value.casefold() in lowered for value in merchants)
        elif code is ObligationCode.HISTORICAL_AVERAGE:
            evidence_ok = _has_field(fields, r"avg|average|baseline")
            answer_ok = bool(re.search(r"\b(?:average|baseline|usual|normal)\b", content, re.I))
        elif code is ObligationCode.ABSOLUTE_COMPARISON:
            evidence_ok = _has_field(
                fields,
                r"delta|difference|change.*minor|variance|range|saved|reduction",
            )
            answer_ok = acknowledges_empty_result or bool(re.search(
                r"\b(?:difference|delta|above|below|higher|lower|more|less|"
                r"increase|decrease|reduc(?:e|ed|es|tion)|sav(?:e|ed|es|ing|ings))\b",
                content,
                re.I,
            ))
        elif code is ObligationCode.PERCENTAGE_COMPARISON:
            evidence_ok = any(fact.kind is EvidenceKind.PERCENT for fact in facts)
            answer_ok = bool(_PERCENT.search(content))
        elif code is ObligationCode.RANKING:
            if authoritative_empty_ranking:
                evidence_ok = True
                answer_ok = acknowledges_empty_result or bool(re.search(
                    r"\b(?:nothing|none)\s+to\s+rank\b",
                    content,
                    re.I,
                ))
            else:
                evidence_ok = _has_field(fields, r"rank|merchant|top|largest|highest|lowest")
                answer_ok = bool(re.search(r"\b(?:largest|biggest|highest|lowest|top|main)\b", content, re.I))
        elif code is ObligationCode.PARTIAL_PERIOD_ALIGNMENT:
            evidence_ok = _has_field(fields, r"comparable|same_day|through_day|baseline|avg|average")
            answer_ok = bool(re.search(
                r"\b(?:like[- ]for[- ]like|same\s+(?:day|elapsed)|first\s+\d+\s+days?|"
                r"through\s+(?:[a-z]+\s+)?\d{1,2}|1\s*[–-]\s*\d{1,2})\b",
                content,
                re.I,
            ))
        elif code is ObligationCode.FORECAST_ASSUMPTIONS:
            evidence_ok = _has_field(fields, r"forecast|project|scenario|assumption|recorded")
            answer_ok = bool(re.search(r"\b(?:forecast|projected|assumption|recorded|scenario)\b", content, re.I))
        elif code is ObligationCode.PROTECTED_CONSTRAINTS:
            evidence_ok = True
            answer_ok = bool(re.search(r"\b(?:protected|preserved|unchanged|without\s+(?:changing|cutting|reducing|touching))\b", content, re.I))
        elif code is ObligationCode.EPISTEMIC_CALIBRATION:
            evidence_ok = True
            answer_ok = bool(re.search(
                r"\b(?:can(?:not|['’]t)|unable\s+to|not\s+possible\s+to|no\s+(?:honest\s+)?way\s+to)\s+"
                r"(?:honestly\s+)?(?:guarantee|ensure|promise)\b|"
                r"\b(?:guarantee|certainty|promise)\b.{0,45}\b(?:is(?:n't|\s+not)|cannot|can't|unsupported|impossible)\b|"
                r"\b(?:insufficient|not\s+enough)\s+(?:history|data|evidence)\b",
                content,
                re.I,
            ))
        elif code is ObligationCode.EXPLANATION:
            evidence_ok = bool(facts)
            answer_ok = not bool(re.search(
                r"\b(?:i\s+ran\s+\d+\s+validated\s+analys(?:is|es)|"
                r"validated\s+analys(?:is|es)\s+(?:completed|ran)|"
                r"governed\s+analysis\s+completed)\b",
                content,
                re.I,
            ))
        if not evidence_ok:
            validation.missing_evidence.append(obligation)
        elif not answer_ok:
            validation.missing_answer.append(obligation)
    return validation
