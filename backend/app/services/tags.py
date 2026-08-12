from __future__ import annotations

import re
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select

from ..models import Tag, TransactionTag
from .repositories import UserScopedRepository


class TagRepository(UserScopedRepository):
    """Canonical normalization, ownership, and transaction-link boundary."""

    @staticmethod
    def normalize(raw_tag: object) -> tuple[str, str] | None:
        name = str(raw_tag).strip()[:80]
        normalized = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        return (name, normalized) if name and normalized else None

    def get_or_create(self, raw_tag: object) -> Tag | None:
        normalized = self.normalize(raw_tag)
        if not normalized:
            return None
        name, normalized_name = normalized
        tag = self.db.scalar(select(Tag).where(
            Tag.user_id == self.user_id,
            Tag.normalized_name == normalized_name,
        ))
        if tag:
            return tag
        tag = Tag(
            user_id=self.user_id,
            name=name.replace("_", " ").title(),
            normalized_name=normalized_name,
        )
        self.db.add(tag)
        self.db.flush()
        return tag

    def replace_transaction_tags(
        self,
        transaction_id: UUID,
        raw_tags: list[object],
        *,
        source: str,
        confidence: Decimal,
    ) -> list[str]:
        self.db.execute(delete(TransactionTag).where(
            TransactionTag.transaction_id == transaction_id,
        ))
        normalized_names: list[str] = []
        for raw_tag in raw_tags[:8]:
            tag = self.get_or_create(raw_tag)
            if not tag or tag.normalized_name in normalized_names:
                continue
            normalized_names.append(tag.normalized_name)
            self.db.add(TransactionTag(
                transaction_id=transaction_id,
                tag_id=tag.id,
                source=source,
                confidence=confidence,
            ))
        return sorted(normalized_names)
