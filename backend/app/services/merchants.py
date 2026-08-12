from __future__ import annotations

from sqlalchemy import or_, select

from ..domain import TaxonomyScope
from ..models import Merchant
from .repositories import UserScopedRepository


class MerchantRepository(UserScopedRepository):
    """Single owner-aware lookup and creation boundary for merchant identity."""

    def by_normalized_name(self, normalized_name: str) -> Merchant | None:
        return self.db.scalar(
            select(Merchant)
            .where(
                Merchant.normalized_name == normalized_name,
                or_(
                    Merchant.owner_user_id == self.user_id,
                    (Merchant.scope == TaxonomyScope.SYSTEM.value) & Merchant.owner_user_id.is_(None),
                ),
            )
            .order_by(Merchant.owner_user_id.is_(None))
            .limit(1)
        )

    def get_or_create(self, canonical_name: str, normalized_name: str) -> Merchant:
        merchant = self.by_normalized_name(normalized_name)
        if merchant:
            return merchant
        merchant = Merchant(
            canonical_name=canonical_name,
            normalized_name=normalized_name,
            scope=TaxonomyScope.USER.value,
            owner_user_id=self.user_id,
        )
        self.db.add(merchant)
        self.db.flush()
        return merchant
