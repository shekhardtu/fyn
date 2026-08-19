from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import or_, select

from app.database import SessionLocal
from app.demo_finances import seed_demo_finances
from app.models import User
from app.seed import seed_system_taxonomy


def user_filter(identifier: str):
    try:
        user_id = UUID(identifier)
    except ValueError:
        user_id = None
    clauses = [User.email == identifier, User.phone == identifier]
    if user_id:
        clauses.append(User.id == user_id)
    return or_(*clauses)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a realistic three-month finance ledger for one existing account.")
    parser.add_argument("--user", required=True, help="Exact email, phone number, or user UUID")
    args = parser.parse_args()

    with SessionLocal() as db:
        user = db.scalar(select(User).where(user_filter(args.user)))
        if not user:
            raise SystemExit(f"No user matched {args.user!r}.")
        seed_system_taxonomy(db)
        added = seed_demo_finances(db, user)
        print(f"Seeded {added} finance records for {user.display_name} ({args.user}).")


if __name__ == "__main__":
    main()
