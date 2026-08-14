from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..domain import TaxonomyScope
from ..models import Budget, Category, Subcategory, Transaction, TransactionCategoryHint, TransactionDraft, User
from ..taxonomy_catalog import NON_EXPENSE_CATEGORY_SLUGS
from .agent_tools import tool_contract
from .extraction import normalize_merchant
from .repositories import UserScopedRepository
from .tool_models import EmptyInput, TaxonomyResult


def _owned_or_system(model, user_id: UUID):
    """The one visibility rule for canonical taxonomy rows."""
    return or_(
        model.scope == TaxonomyScope.SYSTEM.value,
        and_(
            model.scope == TaxonomyScope.USER.value,
            model.owner_user_id == user_id,
        ),
    )


class TaxonomyRepository(UserScopedRepository):
    """User-scoped access to taxonomy truth.

    Callers never infer ownership from slugs or preference JSON. System rows are
    visible to everyone; user rows are visible only to their explicit owner.
    """

    def _category_statement(self, *, expense_only: bool = False):
        statement = select(Category).where(
            _owned_or_system(Category, self.user_id),
        )
        if expense_only:
            statement = statement.where(
                Category.slug.not_in(NON_EXPENSE_CATEGORY_SLUGS),
            )
        return statement

    def _subcategory_statement(self):
        return (
            select(Subcategory)
            .join(Category, Category.id == Subcategory.category_id)
            .where(
                _owned_or_system(Subcategory, self.user_id),
                _owned_or_system(Category, self.user_id),
            )
        )

    def expense_categories(self) -> list[Category]:
        return list(self.db.scalars(
            self._category_statement(expense_only=True)
            .order_by(Category.name, Category.id)
        ))

    def subcategories(self, category_id: UUID) -> list[Subcategory]:
        return list(self.db.scalars(
            self._subcategory_statement()
            .where(Subcategory.category_id == category_id)
            .order_by(Subcategory.name, Subcategory.id)
        ))

    def category(self, category_id: UUID | None, *, expense_only: bool = False) -> Category | None:
        if not category_id:
            return None
        statement = self._category_statement(expense_only=expense_only).where(
            Category.id == category_id,
        )
        return self.db.scalar(statement)

    def category_by_slug(self, slug: str | None, *, expense_only: bool = False) -> Category | None:
        if not slug:
            return None
        statement = self._category_statement(expense_only=expense_only).where(
            Category.slug == slug,
        )
        return self.db.scalar(statement)

    def subcategory(self, subcategory_id: UUID | None, *, category_id: UUID | None = None) -> Subcategory | None:
        if not subcategory_id:
            return None
        statement = self._subcategory_statement().where(
            Subcategory.id == subcategory_id,
        )
        if category_id:
            statement = statement.where(Subcategory.category_id == category_id)
        return self.db.scalar(statement)

    def path(
        self,
        category_id: UUID | None,
        subcategory_id: UUID | None,
    ) -> tuple[Category | None, Subcategory | None]:
        """Resolve one visible category path through the canonical ownership rules."""
        return (
            self.category(category_id),
            self.subcategory(subcategory_id, category_id=category_id),
        )

    def categories_by_id(self, category_ids: set[UUID]) -> dict[UUID, Category]:
        if not category_ids:
            return {}
        return {
            item.id: item
            for item in self.db.scalars(
                self._category_statement().where(Category.id.in_(category_ids))
            )
        }

    def subcategories_by_id(self, subcategory_ids: set[UUID]) -> dict[UUID, Subcategory]:
        if not subcategory_ids:
            return {}
        return {
            item.id: item
            for item in self.db.scalars(
                self._subcategory_statement().where(
                    Subcategory.id.in_(subcategory_ids),
                )
            )
        }

    def subcategory_by_slug(self, category_id: UUID, slug: str | None) -> Subcategory | None:
        if not slug:
            return None
        return self.db.scalar(
            self._subcategory_statement().where(
                Subcategory.category_id == category_id,
                Subcategory.slug == slug,
            )
        )

    def create_category(self, name: str, icon: str, slug: str) -> Category:
        category = Category(
            slug=slug,
            name=name,
            icon=icon,
            scope=TaxonomyScope.USER.value,
            owner_user_id=self.user_id,
        )
        self.db.add(category)
        self.db.flush()
        return category

    def create_subcategory(self, category: Category, name: str, slug: str) -> Subcategory:
        if not self.category(category.id):
            raise ValueError("Unknown parent category")
        subcategory = Subcategory(
            category_id=category.id,
            slug=slug,
            name=name,
            scope=TaxonomyScope.USER.value,
            owner_user_id=self.user_id,
        )
        self.db.add(subcategory)
        self.db.flush()
        return subcategory

    def can_edit(self, item: Category | Subcategory) -> bool:
        return item.scope == TaxonomyScope.USER.value and item.owner_user_id == self.user_id

    def rename_category(self, category_id: UUID, name: str) -> Category:
        category = self.category(category_id, expense_only=True)
        if not category:
            raise ValueError("Unknown category")
        if not self.can_edit(category):
            raise ValueError("Built-in categories cannot be renamed")
        normalized = name.strip()
        if not normalized:
            raise ValueError("Category name is required")
        if any(item.id != category.id and item.name.casefold() == normalized.casefold() for item in self.expense_categories()):
            raise ValueError("A category with this name already exists")
        category.name = normalized[:80]
        self.db.flush()
        return category

    def rename_subcategory(self, category_id: UUID, subcategory_id: UUID, name: str) -> Subcategory:
        category = self.category(category_id, expense_only=True)
        subcategory = self.subcategory(subcategory_id, category_id=category_id)
        if not category or not subcategory:
            raise ValueError("Unknown subcategory")
        if not self.can_edit(subcategory):
            raise ValueError("Built-in subcategories cannot be renamed")
        normalized = name.strip()
        if not normalized:
            raise ValueError("Subcategory name is required")
        if any(item.id != subcategory.id and item.name.casefold() == normalized.casefold() for item in self.subcategories(category.id)):
            raise ValueError("A subcategory with this name already exists")
        subcategory.name = normalized[:80]
        self.db.flush()
        return subcategory

    def delete_category(self, category_id: UUID) -> None:
        category = self.category(category_id, expense_only=True)
        if not category:
            raise ValueError("Unknown category")
        if not self.can_edit(category):
            raise ValueError("Built-in categories cannot be deleted")
        references = sum((
            self.db.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == self.user_id, Transaction.category_id == category.id)) or 0,
            self.db.scalar(select(func.count()).select_from(TransactionDraft).where(TransactionDraft.user_id == self.user_id, TransactionDraft.category_id == category.id)) or 0,
            self.db.scalar(select(func.count()).select_from(Budget).where(Budget.user_id == self.user_id, Budget.category_id == category.id)) or 0,
        ))
        if references:
            raise ValueError(f"This category is used by {references} financial record{'s' if references != 1 else ''}; reassign them before deleting it")
        self.db.delete(category)
        self.db.flush()

    def delete_subcategory(self, category_id: UUID, subcategory_id: UUID) -> None:
        subcategory = self.subcategory(subcategory_id, category_id=category_id)
        if not subcategory:
            raise ValueError("Unknown subcategory")
        if not self.can_edit(subcategory):
            raise ValueError("Built-in subcategories cannot be deleted")
        references = sum((
            self.db.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == self.user_id, Transaction.subcategory_id == subcategory.id)) or 0,
            self.db.scalar(select(func.count()).select_from(TransactionDraft).where(TransactionDraft.user_id == self.user_id, TransactionDraft.subcategory_id == subcategory.id)) or 0,
        ))
        if references:
            raise ValueError(f"This subcategory is used by {references} transaction{'s' if references != 1 else ''}; reassign them before deleting it")
        self.db.delete(subcategory)
        self.db.flush()

    def hints(self, category_id: UUID | None = None) -> list[TransactionCategoryHint]:
        statement = select(TransactionCategoryHint).where(TransactionCategoryHint.user_id == self.user_id)
        if category_id:
            statement = statement.where(TransactionCategoryHint.category_id == category_id)
        return list(self.db.scalars(statement.order_by(TransactionCategoryHint.merchant_pattern, TransactionCategoryHint.id)))

    def hint(self, hint_id: UUID) -> TransactionCategoryHint | None:
        return self.db.scalar(select(TransactionCategoryHint).where(
            TransactionCategoryHint.id == hint_id,
            TransactionCategoryHint.user_id == self.user_id,
        ))

    def save_hint(
        self,
        category_id: UUID,
        merchant_pattern: str,
        subcategory_id: UUID | None = None,
        *,
        hint_id: UUID | None = None,
    ) -> TransactionCategoryHint:
        category = self.category(category_id, expense_only=True)
        subcategory = self.subcategory(subcategory_id, category_id=category_id) if subcategory_id else None
        if not category:
            raise ValueError("Unknown category")
        if subcategory_id and not subcategory:
            raise ValueError("Unknown subcategory")
        display = merchant_pattern.strip()[:160]
        normalized = normalize_merchant(display)
        if not normalized:
            raise ValueError("Merchant hint is required")
        duplicate_statement = select(TransactionCategoryHint).where(
            TransactionCategoryHint.user_id == self.user_id,
            TransactionCategoryHint.normalized_pattern == normalized,
        )
        if hint_id:
            duplicate_statement = duplicate_statement.where(TransactionCategoryHint.id != hint_id)
        duplicate = self.db.scalar(duplicate_statement)
        if duplicate:
            raise ValueError("A hint for this merchant already exists")
        hint = self.hint(hint_id) if hint_id else None
        if hint_id and not hint:
            raise ValueError("Unknown transaction hint")
        if not hint:
            hint = TransactionCategoryHint(user_id=self.user_id)
            self.db.add(hint)
        hint.merchant_pattern = display
        hint.normalized_pattern = normalized
        hint.category_id = category.id
        hint.subcategory_id = subcategory.id if subcategory else None
        self.db.flush()
        return hint

    def delete_hint(self, hint_id: UUID) -> None:
        hint = self.hint(hint_id)
        if not hint:
            raise ValueError("Unknown transaction hint")
        self.db.delete(hint)
        self.db.flush()


@tool_contract(
    name="read_user_expense_taxonomy",
    description=(
        "Read this user's complete visible expense category and subcategory taxonomy. "
        "Use for category/subcategory inventory, names, membership, and exact counts."
    ),
    input_model=EmptyInput,
    output_model=TaxonomyResult,
)
def agent_taxonomy(db: Session, user: User) -> list[dict]:
    repository = TaxonomyRepository(db, user.id)
    return [
        {
            "slug": category.slug,
            "name": category.name,
            "subcategories": [
                {"slug": item.slug, "name": item.name}
                for item in repository.subcategories(category.id)
            ],
        }
        for category in repository.expense_categories()
    ]
