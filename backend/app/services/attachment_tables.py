"""Complete-source CSV/TSV arithmetic, separate from model document reading."""
from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation, localcontext

from .attachment_validation import decode_text

MAX_TABLE_ROWS = 50_000


def summarize_table(content: bytes, media_type: str, column: str, filter_column: str = "", filter_value: str = "") -> dict:
    if media_type not in {"text/csv", "text/tab-separated-values"}:
        raise ValueError("Exact table calculations support CSV and TSV. Export the sheet in one of these formats.")
    reader = csv.reader(io.StringIO(decode_text(content), newline=""),
                        delimiter="\t" if media_type == "text/tab-separated-values" else ",", strict=True)
    header = next(reader, [])
    if not header or len(header) > 100 or len(set(header)) != len(header) or any(not name or len(name) > 128 for name in header):
        raise ValueError("Use a table with distinct column names and at most 100 columns.")
    if column not in header or (filter_column and filter_column not in header):
        raise ValueError(f"Choose a column from: {', '.join(header)}")
    target = header.index(column)
    filter_index = header.index(filter_column) if filter_column else None
    matched = numeric = invalid = rows_read = 0
    minimum = maximum = None
    with localcontext() as context:
        context.prec = 50
        total = Decimal(0)
        for number, row in enumerate(reader, 1):
            if not row:
                continue
            rows_read += 1
            if rows_read > MAX_TABLE_ROWS:
                raise ValueError("This calculation supports up to 50,000 rows. Split the table; no partial total was returned.")
            if len(row) != len(header):
                raise ValueError(f"Row {number} has a different number of columns. No partial total was returned.")
            if filter_index is not None and row[filter_index] != filter_value:
                continue
            matched += 1
            try:
                value = Decimal(row[target].strip())
                if not value.is_finite() or abs(value) > Decimal("1e18") or int(value.as_tuple().exponent) < -8:
                    raise InvalidOperation
            except InvalidOperation:
                invalid += 1
                continue
            numeric += 1
            total += value
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
        return {"columns": header, "column": column, "rowsRead": rows_read, "matchedRows": matched,
                "numericRows": numeric, "invalidOrEmptyRows": invalid, "sum": str(total),
                "minimum": str(minimum) if minimum is not None else None,
                "maximum": str(maximum) if maximum is not None else None,
                "average": str(total / numeric) if numeric else None,
                "scope": "All matching rows in the original file; amounts use the units written there. No ledger records were changed."}
