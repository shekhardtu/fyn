"""Make system and user taxonomy ownership explicit.

Revision ID: 0011_taxonomy_ownership
Revises: 0010_widget_receipts
"""

from alembic import op
from app.migration_guards import (
    add_column_if_absent,
    create_check_constraint_if_absent,
    create_foreign_key_if_absent,
    create_index_if_absent,
)
import sqlalchemy as sa
from uuid import UUID


revision = "0011_taxonomy_ownership"
down_revision = "0010_widget_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_absent("categories", sa.Column("scope", sa.String(length=20), server_default="system", nullable=False))
    add_column_if_absent("categories", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    create_foreign_key_if_absent("fk_categories_owner_user", "categories", "users", ["owner_user_id"], ["id"], ondelete="CASCADE")
    create_index_if_absent("ix_categories_scope", "categories", ["scope"])
    create_index_if_absent("ix_categories_owner_user_id", "categories", ["owner_user_id"])

    add_column_if_absent("subcategories", sa.Column("scope", sa.String(length=20), server_default="system", nullable=False))
    add_column_if_absent("subcategories", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    create_foreign_key_if_absent("fk_subcategories_owner_user", "subcategories", "users", ["owner_user_id"], ["id"], ondelete="CASCADE")
    create_index_if_absent("ix_subcategories_scope", "subcategories", ["scope"])
    create_index_if_absent("ix_subcategories_owner_user_id", "subcategories", ["owner_user_id"])

    bind = op.get_bind()
    metadata = sa.MetaData()
    preferences = sa.Table("user_preferences", metadata, autoload_with=bind)
    users = sa.Table("users", metadata, autoload_with=bind)
    categories = sa.Table("categories", metadata, autoload_with=bind)
    subcategories = sa.Table("subcategories", metadata, autoload_with=bind)

    for preference in bind.execute(sa.select(preferences.c.user_id, preferences.c.key, preferences.c.value)):
        value = preference.value if isinstance(preference.value, dict) else {}
        if preference.key.startswith("custom_category:") and value.get("categoryId"):
            bind.execute(
                sa.update(categories)
                .where(categories.c.id == UUID(str(value["categoryId"])))
                .values(scope="user", owner_user_id=preference.user_id)
            )
        if preference.key.startswith("custom_subcategory:") and value.get("subcategoryId"):
            bind.execute(
                sa.update(subcategories)
                .where(subcategories.c.id == UUID(str(value["subcategoryId"])))
                .values(scope="user", owner_user_id=preference.user_id)
            )

    # Very old custom rows may predate preference ownership. Local installations
    # historically had one seeded identity, so use it only as a compatibility
    # backfill; all new writes require an explicit authenticated owner.
    default_user_id = bind.execute(
        sa.select(users.c.id).where(users.c.email == "demo@financialcopilot.local")
    ).scalar_one_or_none()
    if default_user_id:
        bind.execute(
            sa.update(categories)
            .where(categories.c.scope == "system", categories.c.slug.like("custom-%"))
            .values(scope="user", owner_user_id=default_user_id)
        )
        bind.execute(
            sa.update(subcategories)
            .where(subcategories.c.scope == "system", subcategories.c.slug.like("custom-%"))
            .values(scope="user", owner_user_id=default_user_id)
        )
    # The default "Other" child of a custom category predates a dedicated
    # subcategory preference, so inherit explicit ownership from its parent.
    owned_categories = bind.execute(
        sa.select(categories.c.id, categories.c.owner_user_id).where(categories.c.scope == "user")
    )
    for category in owned_categories:
        bind.execute(
            sa.update(subcategories)
            .where(subcategories.c.category_id == category.id)
            .values(scope="user", owner_user_id=category.owner_user_id)
        )

    create_check_constraint_if_absent(
        "ck_category_scope_owner",
        "categories",
        "(scope = 'system' AND owner_user_id IS NULL) OR (scope = 'user' AND owner_user_id IS NOT NULL)",
    )
    create_check_constraint_if_absent(
        "ck_subcategory_scope_owner",
        "subcategories",
        "(scope = 'system' AND owner_user_id IS NULL) OR (scope = 'user' AND owner_user_id IS NOT NULL)",
    )
    op.alter_column("categories", "scope", server_default=None)
    op.alter_column("subcategories", "scope", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_subcategory_scope_owner", "subcategories", type_="check")
    op.drop_index("ix_subcategories_owner_user_id", table_name="subcategories")
    op.drop_index("ix_subcategories_scope", table_name="subcategories")
    op.drop_constraint("fk_subcategories_owner_user", "subcategories", type_="foreignkey")
    op.drop_column("subcategories", "owner_user_id")
    op.drop_column("subcategories", "scope")

    op.drop_constraint("ck_category_scope_owner", "categories", type_="check")
    op.drop_index("ix_categories_owner_user_id", table_name="categories")
    op.drop_index("ix_categories_scope", table_name="categories")
    op.drop_constraint("fk_categories_owner_user", "categories", type_="foreignkey")
    op.drop_column("categories", "owner_user_id")
    op.drop_column("categories", "scope")
