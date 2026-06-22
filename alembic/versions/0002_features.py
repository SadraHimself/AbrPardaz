"""features: BotSettings, SubProduct, DailyStat, new columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-22 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
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
            sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c)"),
            {"t": table, "c": column},
        ).scalar()

    def index_exists(name: str) -> bool:
        return conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :i)"),
            {"i": name},
        ).scalar()

    # ── New enum types ────────────────────────────────────────────────────────
    op.execute("DO $$ BEGIN CREATE TYPE subproducttype AS ENUM ('traffic', 'extra_ip'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")

    # ── New columns on existing tables ────────────────────────────────────────
    if not column_exists("users", "terms_accepted_at"):
        conn.execute(sa.text("ALTER TABLE users ADD COLUMN terms_accepted_at TIMESTAMPTZ"))

    if not column_exists("provider_accounts", "strict_kyc"):
        conn.execute(sa.text("ALTER TABLE provider_accounts ADD COLUMN strict_kyc BOOLEAN NOT NULL DEFAULT false"))

    if not column_exists("discount_codes", "user_id"):
        conn.execute(sa.text("ALTER TABLE discount_codes ADD COLUMN user_id INTEGER REFERENCES users(id)"))

    # ── sub_products ──────────────────────────────────────────────────────────
    if not table_exists("sub_products"):
        op.create_table(
            "sub_products",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("server_plans.id"), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("type", sa.Enum("traffic", "extra_ip", name="subproducttype", create_type=False), nullable=False),
            sa.Column("price", sa.Float(), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # ── bot_settings ──────────────────────────────────────────────────────────
    if not table_exists("bot_settings"):
        op.create_table(
            "bot_settings",
            sa.Column("key", sa.String(100), primary_key=True),
            sa.Column("value", sa.Text()),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        # Seed default settings
        conn.execute(sa.text("""
            INSERT INTO bot_settings (key, value) VALUES
            ('welcome_text', 'سلام {name} عزیز! 👋\n\nبه ربات <b>Abr Pardaz</b> خوش آمدید.\nبا این ربات می‌توانید سرور مجازی ایران و خارج را تهیه کنید.\n\n💰 موجودی: {balance} تومان'),
            ('support_text', '🆘 <b>پشتیبانی</b>\n\nبرای ارتباط با پشتیبانی از آیدی زیر استفاده کنید:'),
            ('support_id', '@support'),
            ('website_url', ''),
            ('terms_text', ''),
            ('welcome_sticker_id', ''),
            ('force_channels', '[]'),
            ('maintenance_mode', '0'),
            ('maintenance_text', 'ربات در حال تعمیر است. لطفاً بعداً مراجعه کنید.')
            ON CONFLICT (key) DO NOTHING
        """))

    # ── daily_stats ───────────────────────────────────────────────────────────
    if not table_exists("daily_stats"):
        op.create_table(
            "daily_stats",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("date", sa.DateTime(timezone=True), nullable=False),
            sa.Column("new_users", sa.Integer(), server_default="0"),
            sa.Column("new_servers", sa.Integer(), server_default="0"),
            sa.Column("revenue", sa.Float(), server_default="0"),
            sa.Column("active_users", sa.Integer(), server_default="0"),
            sa.Column("total_wallet", sa.Float(), server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        if not index_exists("ix_daily_stats_date"):
            op.create_index("ix_daily_stats_date", "daily_stats", ["date"], unique=True)


def downgrade() -> None:
    op.drop_table("daily_stats")
    op.drop_table("bot_settings")
    op.drop_table("sub_products")
    op.execute("DROP TYPE IF EXISTS subproducttype")
