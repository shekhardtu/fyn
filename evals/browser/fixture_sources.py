"""Deterministic foreign sources for the browser fixture.

The demo ledger exercises the native lane. These two sources exercise the rest
of the analyst platform through the same visible UI: an uploaded spreadsheet
(profile → drafted semantics → user_stated annotation → governed query) and a
read-only external database (connect → profile → query → federated join).

Everything here is fixed and value-stable, so the suite's oracle can assert
exact rupee amounts. Budgets are deliberately chosen to straddle the demo
ledger's own totals: some categories sit under budget and some over, which is
what makes a joined answer checkable rather than merely well-formed.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

BUDGET_SOURCE_NAME = "Monthly budget sheet"
BUDGET_HEADERS = ["Category", "Owner", "Budget"]
# Category slugs match the demo taxonomy so a join on category resolves.
BUDGET_ROWS: tuple[tuple[str, str, str], ...] = (
    ("food", "Hari", "15000.00"),
    ("transport", "Hari", "15000.00"),
    ("housing", "Shared", "45000.00"),
    ("shopping", "Hari", "12000.00"),
    ("travel", "Shared", "20000.00"),
    ("health", "Hari", "10000.00"),
)
# The column a browser case corrects: "Budget" reads as money by heuristic,
# which is right — "Owner" is the one a person must explain.
BUDGET_ANNOTATION_FIELD = "Owner"

VENDOR_SOURCE_NAME = "Vendor invoices"
VENDOR_TABLE = "vendor_invoices"
VENDOR_ROWS: tuple[tuple[int, str, str, int, str], ...] = (
    (1, "Blue Tokai Coffee", "food", 180000, "2026-08-04"),
    (2, "Urban Company", "personal_care", 220000, "2026-08-06"),
    (3, "Blue Tokai Coffee", "food", 96000, "2026-08-11"),
    (4, "Indigo Airlines", "travel", 1450000, "2026-08-12"),
    (5, "Apollo Pharmacy", "health", 74000, "2026-08-15"),
)


def budget_rows() -> list[list[str]]:
    return [list(row) for row in BUDGET_ROWS]


def budget_totals_minor() -> dict[str, int]:
    """What a governed sum over the sheet must return, per category."""
    return {
        category: int(round(float(amount) * 100))
        for category, _owner, amount in BUDGET_ROWS
    }


def vendor_totals_minor() -> dict[str, int]:
    totals: dict[str, int] = {}
    for _id, _vendor, category, amount_minor, _invoiced_on in VENDOR_ROWS:
        totals[category] = totals.get(category, 0) + amount_minor
    return totals


def vendor_merchant_totals_minor() -> dict[str, int]:
    totals: dict[str, int] = {}
    for _id, vendor, _category, amount_minor, _invoiced_on in VENDOR_ROWS:
        totals[vendor] = totals.get(vendor, 0) + amount_minor
    return totals


def write_vendor_database(path: Path) -> Path:
    """Rebuild the external database from scratch, so a rerun is identical."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"CREATE TABLE {VENDOR_TABLE} ("
            "id INTEGER PRIMARY KEY, vendor TEXT NOT NULL, category TEXT NOT NULL, "
            "amount_minor INTEGER NOT NULL, invoiced_on TEXT NOT NULL)"
        )
        connection.executemany(
            f"INSERT INTO {VENDOR_TABLE} (id, vendor, category, amount_minor, invoiced_on) "
            "VALUES (?, ?, ?, ?, ?)",
            VENDOR_ROWS,
        )
        connection.commit()
    finally:
        connection.close()
    # Read-only for the process too: the connector opens mode=ro, and a fixture
    # that cannot be written cannot drift between a seed and a run.
    path.chmod(0o444)
    return path


def source_summary() -> dict[str, Any]:
    return {
        "budgetSheet": {
            "name": BUDGET_SOURCE_NAME,
            "rows": len(BUDGET_ROWS),
            "totalsMinor": budget_totals_minor(),
        },
        "vendorDatabase": {
            "name": VENDOR_SOURCE_NAME,
            "table": VENDOR_TABLE,
            "rows": len(VENDOR_ROWS),
            "categoryTotalsMinor": vendor_totals_minor(),
            "vendorTotalsMinor": vendor_merchant_totals_minor(),
        },
    }
