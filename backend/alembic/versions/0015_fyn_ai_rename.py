"""Carry the seeded local account across the rename to fyn AI.

`default_user` finds the pre-authentication account by its placeholder address,
so changing that constant without moving the existing row would strand it: the
row would no longer be found, and no replacement would be seeded either, because
seeding only runs against an empty `users` table. The first real sign-in would
then create an empty account beside the demo data instead of adopting it.

Only the placeholder is moved. An account that has already been claimed holds a
real address and is left alone.

Revision ID: 0015_fyn_ai_rename
Revises: 0014_user_authentication
"""

from alembic import op


revision = "0015_fyn_ai_rename"
down_revision = "0014_user_authentication"
branch_labels = None
depends_on = None


OLD_PLACEHOLDER = "demo@financialcopilot.local"
NEW_PLACEHOLDER = "demo@fynai.local"


def _move(source: str, target: str) -> None:
    # Skipped rather than failed if the target address already exists, which is
    # the case on a database seeded after the rename.
    op.execute(
        f"UPDATE users SET email = '{target}' WHERE email = '{source}' "
        f"AND NOT EXISTS (SELECT 1 FROM users WHERE email = '{target}')"
    )


def upgrade() -> None:
    _move(OLD_PLACEHOLDER, NEW_PLACEHOLDER)


def downgrade() -> None:
    _move(NEW_PLACEHOLDER, OLD_PLACEHOLDER)
