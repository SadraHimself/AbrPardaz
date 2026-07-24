"""Shared multi-currency pricing helpers.

Plans/servers may be priced in Toman (irt), USD or EUR. The currency lives in
`extra_data["currency"]`; prices stay in that currency and are converted to
Toman at charge time using the auto-updated rates in BotSettings
(np_usd_to_irt_rate / np_eur_to_irt_rate — refreshed every 8h from Navasan).

Shared across providers by design — provider-specific policies can build on it.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CURRENCY_LABELS = {"irt": "تومان", "usd": "دلار", "eur": "یورو", "rub": "روبل"}
RATE_KEYS = {"usd": "np_usd_to_irt_rate", "eur": "np_eur_to_irt_rate",
             "rub": "np_rub_to_irt_rate"}


def obj_currency(obj) -> str:
    """Currency of a ServerPlan/Server via its extra_data. Defaults to irt."""
    cur = ((getattr(obj, "extra_data", None) or {}).get("currency") or "irt").lower()
    return cur if cur in CURRENCY_LABELS else "irt"


async def get_rate(session: AsyncSession, currency: str) -> float:
    """Toman per 1 unit of `currency`. irt → 1. Missing rate → 0 (caller must handle)."""
    cur = (currency or "irt").lower()
    if cur == "irt":
        return 1.0
    from bot.database.models import BotSettings
    row = await session.get(BotSettings, RATE_KEYS.get(cur, ""))
    try:
        return float(row.value) if row and row.value else 0.0
    except (ValueError, TypeError):
        return 0.0


async def to_toman(session: AsyncSession, amount: float, currency: str) -> float:
    """Convert an amount in `currency` to Toman with the current rate.

    Returns 0 when the rate is not configured — callers must treat 0 as
    'rate unavailable' for non-zero amounts."""
    if not amount:
        return 0.0
    rate = await get_rate(session, currency)
    if rate <= 0:
        logger.warning("currency: no rate for %s — conversion unavailable", currency)
        return 0.0
    return float(amount) * rate


async def server_live_price(session: AsyncSession, server, hourly: bool) -> tuple[float, str]:
    """قیمت لحظه‌ایِ یک سرور: اول از پلنِ متصل (extra_data.plan_id) خوانده می‌شود
    تا تغییر قیمت پلن فوراً روی سرورهای موجود مشتری‌ها هم اعمال شود؛
    اگر پلن حذف شده یا لینک نداشت، کپیِ ذخیره‌شده روی خود سرور مبناست.

    سورشارژ per-server: اگر extra_data["price_addon_hourly"/"price_addon_monthly"]
    ست باشد (به ارز پلن)، روی قیمت پلن اضافه می‌شود — برای سرورهایی که گران‌تر از
    پلن پایه‌اند (مثلاً ویندوز/دیسک بزرگ‌تر در جیکور). کپی fallback روی خود سرور
    باید addon-دار ذخیره شده باشد.

    خروجی: (مقدار به ارز پلن، ارز)"""
    extra = getattr(server, "extra_data", None) or {}
    plan_id = extra.get("plan_id")
    if plan_id:
        from bot.database.models import ServerPlan
        plan = await session.get(ServerPlan, plan_id)
        if plan:
            amount = plan.price_hourly if hourly else plan.price_monthly
            if amount:
                addon_key = "price_addon_hourly" if hourly else "price_addon_monthly"
                try:
                    addon = float(extra.get(addon_key) or 0)
                except (TypeError, ValueError):
                    addon = 0.0
                return float(amount) + addon, obj_currency(plan)
    amount = server.price_hourly if hourly else server.price_monthly
    return float(amount or 0), obj_currency(server)


def fmt_price(amount: float, currency: str) -> str:
    """Human price string in the plan's own currency (0.01 یورو / 1,500 تومان)."""
    cur = (currency or "irt").lower()
    if cur == "irt":
        return f"{amount:,.0f} تومان"
    return f"{amount:g} {CURRENCY_LABELS.get(cur, cur)}"
