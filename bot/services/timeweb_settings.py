"""تنظیمات سراسری تایم‌وب — سود ساعتی/ماهانه، گروه مقصد، انتخاب اکانت (تک-اکانتی).

مدل (تصمیم پروژه 2026-07-24): تایم‌وب تک-اکانتی است (مثل جیکور). قیمت تعرفه‌ها
ماهانه به «روبل» است؛ قیمت خرید ساعتی = ماهانه ÷ ۷۲۰ (در provider محاسبه شده و
در extra_data پلن ذخیره می‌شود: cost_hourly / cost_monthly). فروش = خرید × (۱+سود٪)
— ساعتی و ماهانه جدا (تایم‌وب هر دو را می‌فروشد). نرخ روبل→تومان: خودکار از
Navasan (np_rub_to_irt_rate) + قابل‌تنظیم دستی از پنل ادمین.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BotSettings, ProviderAccount, ProviderType, Server, ServerPlan, ServerStatus,
)

logger = logging.getLogger(__name__)

_KEY_MH = "timeweb_margin_hourly"
_KEY_MM = "timeweb_margin_monthly"
_KEY_GROUP = "timeweb_group"
_DEFAULT_GROUP = "Timeweb"


async def _get(session: AsyncSession, key: str):
    row = await session.get(BotSettings, key)
    return row.value if row else None


async def _set(session: AsyncSession, key: str, value) -> None:
    row = await session.get(BotSettings, key)
    if row:
        row.value = str(value)
    else:
        session.add(BotSettings(key=key, value=str(value)))


async def get_margins(session: AsyncSession) -> tuple[float | None, float | None]:
    async def _f(k):
        v = await _get(session, k)
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None
    return await _f(_KEY_MH), await _f(_KEY_MM)


async def set_margin(session: AsyncSession, hourly: bool, value: float) -> None:
    await _set(session, _KEY_MH if hourly else _KEY_MM, value)


async def get_group_name(session: AsyncSession) -> str:
    from bot.database.models import ProductGroup
    name = await _get(session, _KEY_GROUP)
    target = name or _DEFAULT_GROUP
    grp = (await session.execute(
        select(ProductGroup).where(ProductGroup.name == target)
    )).scalar_one_or_none()
    if not grp:
        grp = ProductGroup(name=target, is_hidden=False)
        session.add(grp)
        await session.flush()
    if not name:
        await _set(session, _KEY_GROUP, target)
    return target


async def set_group_name(session: AsyncSession, name: str) -> None:
    await _set(session, _KEY_GROUP, name)


async def apply_margins_to_catalog(session: AsyncSession) -> int:
    """قیمت فروش پلن‌های تایم‌وب = قیمت خرید × (۱+سود٪) — ساعتی و ماهانه + فعال‌سازی."""
    mh, mm = await get_margins(session)
    if mh is None and mm is None:
        return 0
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.TIMEWEB)
    )).scalars().all()
    count = 0
    for p in plans:
        extra = p.extra_data or {}
        ch, cm = extra.get("cost_hourly"), extra.get("cost_monthly")
        changed = False
        if mh is not None and ch:
            p.price_hourly = round(float(ch) * (1 + mh / 100), 4)
            changed = True
        if mm is not None and cm:
            p.price_monthly = round(float(cm) * (1 + mm / 100), 2)
            changed = True
        if changed:
            if not extra.get("unavailable") and not p.is_active:
                p.is_active = True
            count += 1
    await session.flush()
    return count


async def get_account(session: AsyncSession) -> ProviderAccount | None:
    """تنها اکانت تایم‌وب (فعال یا نه) — برای پنل ادمین."""
    return (await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.TIMEWEB,
        ).order_by(ProviderAccount.id)
    )).scalars().first()


async def pick_account(session: AsyncSession) -> ProviderAccount | None:
    """اکانت فعال تایم‌وب اگر زیر لیمیت VM دستی باشد (تک-اکانتی).
    API سهمیه نمی‌دهد → مصرف از DB خودمان شمرده می‌شود (الگوی جیکور)."""
    account = (await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.TIMEWEB,
            ProviderAccount.is_active == True,
        ).order_by(ProviderAccount.id)
    )).scalars().first()
    if not account:
        return None
    limit = int((account.extra_config or {}).get("vm_limit") or 0)
    if limit:
        count = (await session.execute(
            select(func.count(Server.id)).where(
                Server.provider_account_id == account.id,
                Server.status != ServerStatus.DELETED,
            )
        )).scalar() or 0
        if count >= limit:
            return None
    return account
