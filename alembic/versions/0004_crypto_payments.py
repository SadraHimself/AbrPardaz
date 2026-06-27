"""crypto_payments: new table + direct payment columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-27 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    def table_exists(name: str) -> bool:
        return conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = :t)"),
            {"t": name},
        ).scalar()

    def column_exists(table: str, column: str) -> bool:
        return conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c)"
            ),
            {"t": table, "c": column},
        ).scalar()

    def index_exists(name: str) -> bool:
        return conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :i)"),
            {"i": name},
        ).scalar()

    # ── crypto_payments table ─────────────────────────────────────────────────
    if not table_exists("crypto_payments"):
        op.create_table(
            "crypto_payments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("order_id", sa.String(100), unique=True, nullable=False),
            sa.Column("payment_id", sa.String(100), nullable=True),
            sa.Column("invoice_id", sa.String(100), nullable=True),
            sa.Column("amount_usd", sa.Float(), nullable=False),
            sa.Column("amount_irt", sa.Float(), nullable=False),
            sa.Column("pay_address", sa.String(255), nullable=True),
            sa.Column("pay_amount", sa.Float(), nullable=True),
            sa.Column("pay_currency", sa.String(20), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="waiting"),
            sa.Column("activated", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        if not index_exists("ix_crypto_payments_order_id"):
            op.create_index("ix_crypto_payments_order_id", "crypto_payments", ["order_id"])
        if not index_exists("ix_crypto_payments_payment_id"):
            op.create_index("ix_crypto_payments_payment_id", "crypto_payments", ["payment_id"])
    else:
        # Table already exists (manually created) — add missing columns
        for col, ddl in [
            ("pay_address",  "ALTER TABLE crypto_payments ADD COLUMN pay_address VARCHAR(255)"),
            ("pay_amount",   "ALTER TABLE crypto_payments ADD COLUMN pay_amount FLOAT"),
            ("pay_currency", "ALTER TABLE crypto_payments ADD COLUMN pay_currency VARCHAR(20)"),
            ("expires_at",   "ALTER TABLE crypto_payments ADD COLUMN expires_at TIMESTAMPTZ"),
        ]:
            if not column_exists("crypto_payments", col):
                conn.execute(sa.text(ddl))


def downgrade() -> None:
    op.drop_table("crypto_payments")
