"""user: add email and extra_data columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-23 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    def column_exists(table: str, column: str) -> bool:
        return conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c)"
            ),
            {"t": table, "c": column},
        ).scalar()

    if not column_exists("users", "email"):
        op.add_column("users", sa.Column("email", sa.String(255), nullable=True))

    if not column_exists("users", "extra_data"):
        op.add_column("users", sa.Column("extra_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "extra_data")
    op.drop_column("users", "email")
