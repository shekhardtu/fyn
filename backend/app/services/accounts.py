from __future__ import annotations

from sqlalchemy import func, select

from ..models import Account
from .repositories import UserScopedRepository


class AccountRepository(UserScopedRepository):
    """Canonical owner-aware account lookup and creation boundary."""

    def get_or_create(self, name: str, currency: str) -> Account:
        normalized_name = name.strip()
        account = self.db.scalar(select(Account).where(
            Account.user_id == self.user_id,
            func.lower(Account.name) == normalized_name.casefold(),
        ))
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
