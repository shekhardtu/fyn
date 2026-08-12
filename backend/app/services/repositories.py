from __future__ import annotations

from collections.abc import Collection
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session


OwnedModel = TypeVar("OwnedModel")


class UserScopedRepository:
    """Shared dependency boundary for repositories owned by one user."""

    def __init__(self, db: Session, user_id: UUID):
        self.db = db
        self.user_id = user_id

    @staticmethod
    def _identity_columns(model: type[OwnedModel]):
        owner_column = getattr(model, "user_id", None)
        id_column = getattr(model, "id", None)
        if owner_column is None or id_column is None:
            raise TypeError(f"{model.__name__} is not a standard user-owned model")
        return id_column, owner_column

    def get(self, model: type[OwnedModel], object_id: UUID) -> OwnedModel | None:
        """Resolve one row whose mapped model exposes the standard user_id key."""
        id_column, _ = self._identity_columns(model)
        return self.db.scalar(
            self.statement(model).where(id_column == object_id)
        )

    def statement(self, model: type[OwnedModel]):
        """Start a query at the canonical standard-ownership boundary."""
        _, owner_column = self._identity_columns(model)
        return select(model).where(owner_column == self.user_id)

    def by_ids(
        self,
        model: type[OwnedModel],
        object_ids: Collection[UUID],
    ) -> dict[UUID, OwnedModel]:
        """Resolve standard user-owned rows without duplicating tenant filters."""
        if not object_ids:
            return {}
        id_column, _ = self._identity_columns(model)
        return {
            item.id: item
            for item in self.db.scalars(
                self.statement(model).where(id_column.in_(object_ids))
            )
        }
