"""initial

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types only if they don't exist (works on all PostgreSQL versions)
    op.execute("DO $$ BEGIN CREATE TYPE userstatus AS ENUM ('active', 'banned', 'suspended'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE providertype AS ENUM ('virtualizor'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE serverstatus AS ENUM ('pending', 'building', 'active', 'suspended', 'deleted', 'rebuilding', 'rebooting'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE suspendreason AS ENUM ('low_balance', 'traffic_exceeded', 'expired', 'admin'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE billingtype AS ENUM ('hourly', 'monthly'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE transactiontype AS ENUM ('credit', 'debit', 'refund'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")

    conn = op.get_bind()

    def table_exists(name: str) -> bool:
        return conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = :t)"),
            {"t": name},
        ).scalar()

    def index_exists(name: str) -> bool:
        return conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :i)"),
            {"i": name},
        ).scalar()

    def column_exists(table: str, column: str) -> bool:
        return conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c)"),
            {"t": table, "c": column},
        ).scalar()

    # ── users ────────────────────────────────────────────────────────────────
    if not table_exists("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
            sa.Column("username", sa.String(255)),
            sa.Column("first_name", sa.String(255)),
            sa.Column("last_name", sa.String(255)),
            sa.Column("phone_number", sa.String(20)),
            sa.Column("national_id", sa.String(10)),
            sa.Column("is_phone_verified", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("is_kyc_verified", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("balance", sa.Float(), server_default="0", nullable=False),
            sa.Column("status", sa.Enum("active", "banned", "suspended", name="userstatus", create_type=False), server_default="active", nullable=False),
            sa.Column("is_admin", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        if not index_exists("ix_users_telegram_id"):
            op.create_index("ix_users_telegram_id", "users", ["telegram_id"])
    else:
        # Add missing columns to existing table
        for col, stmt in [
            ("national_id", "ALTER TABLE users ADD COLUMN IF NOT EXISTS national_id VARCHAR(10)"),
            ("is_kyc_verified", "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_kyc_verified BOOLEAN NOT NULL DEFAULT false"),
        ]:
            if not column_exists("users", col):
                conn.execute(sa.text(stmt))

    # ── provider_accounts ─────────────────────────────────────────────────────
    if not table_exists("provider_accounts"):
        op.create_table(
            "provider_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider_type", sa.Enum("virtualizor", name="providertype", create_type=False), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("api_key", sa.String(1000)),
            sa.Column("api_secret", sa.String(1000)),
            sa.Column("api_endpoint", sa.String(500)),
            sa.Column("extra_config", sa.JSON()),
            sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # ── servers ───────────────────────────────────────────────────────────────
    if not table_exists("servers"):
        op.create_table(
            "servers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("provider_type", sa.Enum("virtualizor", name="providertype", create_type=False), nullable=False),
            sa.Column("provider_account_id", sa.Integer(), sa.ForeignKey("provider_accounts.id")),
            sa.Column("provider_server_id", sa.String(255)),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("hostname", sa.String(255)),
            sa.Column("ip_address", sa.String(45)),
            sa.Column("ipv6_address", sa.String(100)),
            sa.Column("ram", sa.Integer(), nullable=False),
            sa.Column("cpu", sa.Integer(), nullable=False),
            sa.Column("disk", sa.Integer(), nullable=False),
            sa.Column("bandwidth", sa.Integer(), nullable=False),
            sa.Column("os_name", sa.String(255)),
            sa.Column("location", sa.String(100)),
            sa.Column("datacenter", sa.String(100)),
            sa.Column("status", sa.Enum("pending", "building", "active", "suspended", "deleted", "rebuilding", "rebooting", name="serverstatus", create_type=False), server_default="pending", nullable=False),
            sa.Column("suspend_reason", sa.Enum("low_balance", "traffic_exceeded", "expired", "admin", name="suspendreason", create_type=False)),
            sa.Column("billing_type", sa.Enum("hourly", "monthly", name="billingtype", create_type=False), nullable=False),
            sa.Column("price_hourly", sa.Float()),
            sa.Column("price_monthly", sa.Float()),
            sa.Column("traffic_used_gb", sa.Float(), server_default="0", nullable=False),
            sa.Column("traffic_limit_gb", sa.Float()),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("last_billed_at", sa.DateTime(timezone=True)),
            sa.Column("suspended_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("extra_data", sa.JSON()),
        )

    # ── transactions ──────────────────────────────────────────────────────────
    if not table_exists("transactions"):
        op.create_table(
            "transactions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("server_id", sa.Integer(), sa.ForeignKey("servers.id")),
            sa.Column("amount", sa.Float(), nullable=False),
            sa.Column("type", sa.Enum("credit", "debit", "refund", name="transactiontype", create_type=False), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("reference_id", sa.String(255)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # ── server_plans ──────────────────────────────────────────────────────────
    if not table_exists("server_plans"):
        op.create_table(
            "server_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider_type", sa.Enum("virtualizor", name="providertype", create_type=False), nullable=False),
            sa.Column("provider_account_id", sa.Integer(), sa.ForeignKey("provider_accounts.id")),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("display_name", sa.String(255)),
            sa.Column("description", sa.Text()),
            sa.Column("ram", sa.Integer(), nullable=False),
            sa.Column("cpu", sa.Integer(), nullable=False),
            sa.Column("disk", sa.Integer(), nullable=False),
            sa.Column("bandwidth", sa.Integer(), nullable=False),
            sa.Column("price_hourly", sa.Float()),
            sa.Column("price_monthly", sa.Float()),
            sa.Column("location", sa.String(100)),
            sa.Column("datacenter", sa.String(100)),
            sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
            sa.Column("category", sa.String(100)),
            sa.Column("provider_plan_id", sa.String(255)),
            sa.Column("extra_data", sa.JSON()),
        )
    else:
        # Add category column if missing (from older installs)
        if not column_exists("server_plans", "category"):
            conn.execute(sa.text("ALTER TABLE server_plans ADD COLUMN IF NOT EXISTS category VARCHAR(100)"))
        if not column_exists("server_plans", "display_name"):
            conn.execute(sa.text("ALTER TABLE server_plans ADD COLUMN IF NOT EXISTS display_name VARCHAR(255)"))
        if not column_exists("server_plans", "provider_plan_id"):
            conn.execute(sa.text("ALTER TABLE server_plans ADD COLUMN IF NOT EXISTS provider_plan_id VARCHAR(255)"))

    # ── otps ──────────────────────────────────────────────────────────────────
    if not table_exists("otps"):
        op.create_table(
            "otps",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("phone_number", sa.String(20), nullable=False),
            sa.Column("code", sa.String(10), nullable=False),
            sa.Column("is_used", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        if not index_exists("ix_otps_telegram_id"):
            op.create_index("ix_otps_telegram_id", "otps", ["telegram_id"])

    # ── discount_codes ────────────────────────────────────────────────────────
    if not table_exists("discount_codes"):
        op.create_table(
            "discount_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(50), nullable=False, unique=True),
            sa.Column("discount_percent", sa.Float(), nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("max_uses", sa.Integer()),
            sa.Column("use_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        if not index_exists("ix_discount_codes_code"):
            op.create_index("ix_discount_codes_code", "discount_codes", ["code"])

    # ── payment_orders ────────────────────────────────────────────────────────
    if not table_exists("payment_orders"):
        op.create_table(
            "payment_orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("amount", sa.Float(), nullable=False),
            sa.Column("gateway", sa.String(50), nullable=False),
            sa.Column("authority", sa.String(255)),
            sa.Column("ref_id", sa.String(255)),
            sa.Column("status", sa.String(20), server_default="pending", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("paid_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    op.drop_table("payment_orders")
    op.drop_table("discount_codes")
    op.drop_table("otps")
    op.drop_table("server_plans")
    op.drop_table("transactions")
    op.drop_table("servers")
    op.drop_table("provider_accounts")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS userstatus")
    op.execute("DROP TYPE IF EXISTS providertype")
    op.execute("DROP TYPE IF EXISTS serverstatus")
    op.execute("DROP TYPE IF EXISTS suspendreason")
    op.execute("DROP TYPE IF EXISTS billingtype")
    op.execute("DROP TYPE IF EXISTS transactiontype")
