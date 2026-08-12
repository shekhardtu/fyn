"""Make learned merchant identity explicitly user-owned.

Revision ID: 0012_merchant_ownership
Revises: 0011_taxonomy_ownership
"""

from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "0012_merchant_ownership"
down_revision = "0011_taxonomy_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("merchants", sa.Column("scope", sa.String(length=20), server_default="system", nullable=False))
    op.add_column("merchants", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_merchants_owner_user", "merchants", "users", ["owner_user_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_merchants_scope", "merchants", ["scope"])
    op.create_index("ix_merchants_owner_user_id", "merchants", ["owner_user_id"])

    bind = op.get_bind()
    metadata = sa.MetaData()
    merchants = sa.Table("merchants", metadata, autoload_with=bind)
    aliases = sa.Table("merchant_aliases", metadata, autoload_with=bind)
    transactions = sa.Table("transactions", metadata, autoload_with=bind)
    recurring = sa.Table("recurring_transactions", metadata, autoload_with=bind)

    merchant_rows = list(bind.execute(sa.select(merchants)))
    for merchant in merchant_rows:
        transaction_users = bind.execute(
            sa.select(transactions.c.user_id)
            .where(transactions.c.merchant_id == merchant.id)
            .distinct()
        ).scalars()
        recurring_users = bind.execute(
            sa.select(recurring.c.user_id)
            .where(recurring.c.merchant_id == merchant.id)
            .distinct()
        ).scalars()
        user_ids = sorted(set(transaction_users) | set(recurring_users), key=str)
        if not user_ids:
            continue

        alias_rows = list(bind.execute(sa.select(aliases).where(aliases.c.merchant_id == merchant.id)))
        merchant_for_user = {user_ids[0]: merchant.id}
        bind.execute(
            sa.update(merchants)
            .where(merchants.c.id == merchant.id)
            .values(scope="user", owner_user_id=user_ids[0])
        )
        for user_id in user_ids[1:]:
            duplicate_id = uuid4()
            merchant_for_user[user_id] = duplicate_id
            bind.execute(sa.insert(merchants).values(
                id=duplicate_id,
                canonical_name=merchant.canonical_name,
                normalized_name=merchant.normalized_name,
                scope="user",
                owner_user_id=user_id,
                created_at=merchant.created_at,
                updated_at=merchant.updated_at,
            ))
            for alias in alias_rows:
                bind.execute(sa.insert(aliases).values(
                    id=uuid4(),
                    merchant_id=duplicate_id,
                    raw_alias=alias.raw_alias,
                    normalized_alias=alias.normalized_alias,
                    context=alias.context,
                    created_at=alias.created_at,
                    updated_at=alias.updated_at,
                ))
        for user_id, owned_merchant_id in merchant_for_user.items():
            bind.execute(
                sa.update(transactions)
                .where(transactions.c.merchant_id == merchant.id, transactions.c.user_id == user_id)
                .values(merchant_id=owned_merchant_id)
            )
            bind.execute(
                sa.update(recurring)
                .where(recurring.c.merchant_id == merchant.id, recurring.c.user_id == user_id)
                .values(merchant_id=owned_merchant_id)
            )

    op.create_check_constraint(
        "ck_merchant_scope_owner",
        "merchants",
        "(scope = 'system' AND owner_user_id IS NULL) OR (scope = 'user' AND owner_user_id IS NOT NULL)",
    )
    op.alter_column("merchants", "scope", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_merchant_scope_owner", "merchants", type_="check")
    op.drop_index("ix_merchants_owner_user_id", table_name="merchants")
    op.drop_index("ix_merchants_scope", table_name="merchants")
    op.drop_constraint("fk_merchants_owner_user", "merchants", type_="foreignkey")
    op.drop_column("merchants", "owner_user_id")
    op.drop_column("merchants", "scope")
