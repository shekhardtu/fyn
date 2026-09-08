from __future__ import annotations

from sqlalchemy import select

from ..models import Account
from .repositories import UserScopedRepository


def account_name_key(name: str | None) -> str:
    """Use the same identity comparison for named input and saved choices."""
    return " ".join((name or "").split()).casefold()


class AccountRepository(UserScopedRepository):
    """Canonical owner-aware account lookup and creation boundary."""

    def find_by_name(self, name: str) -> Account | None:
        key = account_name_key(name)
        return next((
            account for account in self.db.scalars(select(Account).where(Account.user_id == self.user_id).order_by(Account.id))
            if account_name_key(account.name) == key
        ), None)

    def get_or_create(self, name: str, currency: str) -> Account:
        normalized_name = " ".join(name.split())
        if not normalized_name:
            raise ValueError("Account name is required")
        account = self.find_by_name(normalized_name)
        if account:
            return account
        account = Account(
            user_id=self.user_id,
            name=normalized_name,
            currency=currency,
        )
        self.db.add(account)
        self.db.flush()
        return account
