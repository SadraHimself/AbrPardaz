from __future__ import annotations

import json
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # ── Telegram ─────────────────────────────────────────────────────────────────
    BOT_TOKEN: str
    ADMIN_IDS: str = "[]"
    WEBAPP_URL: str = ""

    # ── Database ──────────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://telecloud:StrongPassword@localhost:5432/telecloud"

    # ── Redis / Celery ────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── Shahkar KYC (legacy nict.ir) ──────────────────────────────────────────────
    SHAHKAR_BASE_URL: str = ""
    SHAHKAR_SERVICE_ID: str = ""
    SHAHKAR_PASSWORD: str = ""

    # ── Zohal (identity verification → Shahkar) ───────────────────────────────────
    ZOHAL_TOKEN: str = ""
    ZOHAL_BASE_URL: str = "https://service.zohal.io/api/v0"

    # ── Billing ───────────────────────────────────────────────────────────────────
    MIN_BALANCE_THRESHOLD: float = 0.0
    TRAFFIC_GRACE_SECONDS: int = 300

    # ── Navasan (exchange rates) ──────────────────────────────────────────────────
    NAVASAN_API_KEY: str = ""

    # ── NOWPayments ───────────────────────────────────────────────────────────────
    NP_API_KEY: str = ""
    NP_IPN_SECRET: str = ""
    NP_PRICE_CURRENCY: str = "usd"
    NP_OUTCOME_CURRENCY: str = "trx"
    NP_WEBHOOK_PORT: int = 8081

    @property
    def admin_ids(self) -> List[int]:
        return json.loads(self.ADMIN_IDS)


settings = Settings()
