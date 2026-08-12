from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..domain import TaxonomyScope
from ..models import Category, Subcategory, User
from ..taxonomy_catalog import DefaultCategorySlug
from .agent_tools import tool_contract
from .repositories import UserScopedRepository
from .tool_models import EmptyInput, TaxonomyResult


NON_EXPENSE_CATEGORY_SLUGS = frozenset({
    DefaultCategorySlug.INCOME,
    DefaultCategorySlug.INVESTMENT,
})


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
