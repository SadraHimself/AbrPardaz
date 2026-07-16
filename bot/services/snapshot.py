"""Snapshot pricing/billing helpers (Hetzner).

هزینه‌ی هتزنر برای اسنپ‌شات ماهانه و per-GB است:
    cost_monthly_eur = image_size(GB) × price_per_gb_month(gross)
ما ساعتی از کاربر می‌گیریم (با سود ساعتیِ اکانت + نرخ روز):
    sell_hourly_eur = (cost_monthly_eur / 720) × (1 + margin_hourly/100)
    debit_toman      = to_toman(sell_hourly_eur, "eur")
۷۲۰ = ۳۰ روز × ۲۴ ساعت (مبنای تبدیل ماهانه→ساعتی، هم‌راستا با cap ماهانه هتزنر).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Snapshot
from bot.services.currency import to_toman

HOURS_PER_MONTH = 720


async def sell_hourly_eur(session: AsyncSession, snapshot: Snapshot) -> float:
    """قیمت فروش ساعتیِ اسنپ‌شات به یورو (با سودِ ساعتیِ سراسری هتزنر)."""
    cost_monthly = float((snapshot.extra_data or {}).get("cost_monthly_eur") or 0)
    if cost_monthly <= 0:
        return 0.0
    from bot.services.hetzner_settings import hourly_margin
    margin = await hourly_margin(session)
    return (cost_monthly / HOURS_PER_MONTH) * (1 + margin / 100)


async def hourly_toman(session: AsyncSession, snapshot: Snapshot) -> float:
    """هزینه‌ی ساعتیِ ریالی — 0 یعنی نرخ ارز در دسترس نیست (باید معوق شود)."""
    eur = await sell_hourly_eur(session, snapshot)
    if eur <= 0:
        return 0.0
    return await to_toman(session, eur, "eur")
