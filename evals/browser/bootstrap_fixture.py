from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from evals.browser.cli import DEFAULT_SUITE, EvalValidationError, load_suite
from evals.browser.fixture_sources import (
    BUDGET_HEADERS,
    BUDGET_SOURCE_NAME,
    VENDOR_SOURCE_NAME,
    VENDOR_TABLE,
    budget_rows,
    budget_totals_minor,
    source_summary,
    vendor_totals_minor,
    write_vendor_database,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_DATABASE_HOSTS = {None, "localhost", "127.0.0.1", "::1"}
# The external database the fixture connects lives beside the suite, so a
# rerun rebuilds a file the repository owns rather than reaching anywhere else.
FIXTURE_SOURCE_DIR = Path(__file__).resolve().parent / "fixture-data"
VENDOR_DATABASE_PATH = FIXTURE_SOURCE_DIR / "vendor_invoices.db"


def _assert_safe_local_settings(settings: Any) -> None:
    from sqlalchemy.engine import make_url

    if settings.environment == "production":
        raise EvalValidationError("Browser fixture bootstrap is disabled in production")
    if not settings.otp_debug_echo:
        raise EvalValidationError("Browser fixture bootstrap requires OTP_DEBUG_ECHO=true")
    database = make_url(settings.database_url)
    if not database.drivername.startswith("sqlite") and database.host not in LOCAL_DATABASE_HOSTS:
        raise EvalValidationError(
            f"Browser fixture bootstrap requires a local database host, got {database.host!r}"
        )


def bootstrap() -> dict[str, Any]:
    backend = REPO_ROOT / "backend"
    previous_cwd = Path.cwd()
    sys.path.insert(0, str(backend))
    try:
        os.chdir(backend)
        # Loading the suite imports the demo oracle and therefore application
        # models. Do it from the backend directory so Settings reads the same
        # backend/.env file as the running API before its cache/engine exist.
        suite = load_suite(DEFAULT_SUITE)
        identifier = suite["fixture"]["account"]["identifier"]
        from sqlalchemy import func, select

        from app.config import get_settings
        from app.database import SessionLocal
        from app.demo_finances import DEMO_EXPENSES, DEMO_INCOMES, DEMO_MARKER, seed_demo_finances
        from app.domain import OtpChannel, OtpPurpose
        from app.models import Transaction, User
        from app.seed import seed_system_taxonomy
        from app.services.auth import complete_login
        from app.services.external_db import connect_external_database
        from app.services.otp import start_challenge
        from app.services.spreadsheet import (
            annotate_source_field,
            ensure_spreadsheet_manifest,
            query_source,
        )

        settings = get_settings()
        _assert_safe_local_settings(settings)
    except EvalValidationError:
        raise
    except Exception as exc:
        raise EvalValidationError(
            f"Could not initialize browser fixture bootstrap: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        os.chdir(previous_cwd)
        sys.path.pop(0)

    expected_demo = len(DEMO_EXPENSES) * len(DEMO_INCOMES) + sum(
        len(items) for items in DEMO_INCOMES.values()
    )
    try:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.phone == identifier))
            if user is not None:
                # Canonical rows only, matching fixture_status and the app's own
                # scope. A row a previous run created and then removed is already
                # gone from every governed read; counting it here would let one
                # cancelled mutation block seeding forever.
                total_before = db.scalar(
                    select(func.count()).select_from(Transaction).where(
                        Transaction.user_id == user.id,
                        Transaction.deleted_at.is_(None),
                    )
                ) or 0
                demo_before = db.scalar(
                    select(func.count()).select_from(Transaction).where(
                        Transaction.user_id == user.id,
                        Transaction.deleted_at.is_(None),
                        Transaction.notes.like(f"{DEMO_MARKER}:%"),
                    )
                ) or 0
                if total_before != demo_before:
                    raise EvalValidationError(
                        "The pinned account contains non-demo transactions; refusing to change or delete them"
                    )

            created = user is None
            if user is None:
                # Exercise the real challenge hash and verification path while
                # omitting deliver_code. The plaintext exists only here and is
                # consumed immediately; the database stores its HMAC.
                issued = start_challenge(
                    db,
                    channel=OtpChannel.PHONE,
                    purpose=OtpPurpose.LOGIN,
                    destination=identifier,
                )
                user = complete_login(db, issued.challenge.id, issued.code)

            seed_system_taxonomy(db)
            added = seed_demo_finances(db, user)
            demo_after = db.scalar(
                select(func.count()).select_from(Transaction).where(
                    Transaction.user_id == user.id,
                    Transaction.deleted_at.is_(None),
                    Transaction.notes.like(f"{DEMO_MARKER}:%"),
                )
            ) or 0

            # The foreign-source lanes get the same treatment as the ledger:
            # fixed content, seeded through the product's own entry points, so
            # a browser case exercises exactly what a customer would.
            budget_source, _drafted = ensure_spreadsheet_manifest(
                db, user, BUDGET_SOURCE_NAME, list(BUDGET_HEADERS), budget_rows()
            )
            # One user_stated annotation is seeded so a case can verify that a
            # stated meaning survives and is quoted; the medium case adds its
            # own on a different column. The manifest reported below is the one
            # that includes it, so a rerun reports the version it leaves behind.
            budget_manifest = annotate_source_field(
                db,
                user,
                budget_source.id,
                "Budget",
                "Budget is the monthly cap in rupees for that category",
                role="money",
            )
            budget_check = query_source(
                db, user.id, budget_source.id,
                metric="sum", value_field="Budget", group_by="Category",
            )
            seeded_budget = {
                str(row["Category"]): int(row["value_minor"]) for row in budget_check["rows"]
            }
            if seeded_budget != budget_totals_minor():
                raise EvalValidationError(
                    "Seeded budget sheet does not match its oracle totals; "
                    f"got {seeded_budget}"
                )

            write_vendor_database(VENDOR_DATABASE_PATH)
            vendor_source, vendor_manifest = connect_external_database(
                db,
                user,
                VENDOR_SOURCE_NAME,
                f"sqlite:///{VENDOR_DATABASE_PATH}",
                [VENDOR_TABLE],
            )
            db.commit()
    except EvalValidationError:
        raise
    except Exception as exc:
        raise EvalValidationError(f"Could not bootstrap the browser fixture: {type(exc).__name__}: {exc}") from exc

    if demo_after != expected_demo:
        raise EvalValidationError(f"Seed produced {demo_after} demo transactions, expected {expected_demo}")
    return {
        "status": "ready",
        "account": identifier,
        "accountCreated": created,
        "demoTransactionsAdded": added,
        "demoTransactions": demo_after,
        "months": len(DEMO_INCOMES),
        "smsDelivered": False,
        "sources": {
            **source_summary(),
            "budgetSheetManifestVersion": budget_manifest.version,
            "vendorDatabaseManifestVersion": vendor_manifest.version,
            "vendorDatabasePath": str(VENDOR_DATABASE_PATH),
            "vendorCategoryTotalsMinor": vendor_totals_minor(),
        },
    }


def main() -> int:
    try:
        print(json.dumps(bootstrap(), indent=2, ensure_ascii=False))
    except EvalValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
