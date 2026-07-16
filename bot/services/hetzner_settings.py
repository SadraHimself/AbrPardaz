"""تنظیمات سراسری هتزنر — کاتالوگ مشترک، سود واحد، انتخاب اکانتِ متوازن.

مدل: همه‌ی اکانت‌های هتزنر یک کاتالوگ مشترک دارند (هر محصول یک‌بار ایمپورت می‌شود،
مستقل از اکانت)، یک درصد سود واحد (ساعتی/ماهانه) در BotSettings، و موقع خرید اکانت
با «توزیع متوازن» (کمترین سرورِ زنده، زیر لیمیت VM) انتخاب می‌شود.

هزینه/فاکتور per-server است — هر سرور provider_account_id خودش را دارد → گزارش هر
اکانت جدا. پلن‌ها `provider_account_id` را فقط به‌عنوان «اکانت مرجع» برای خواندن
قیمت/کاتالوگ نگه می‌دارند (نه تعیین اکانتِ ساخت).
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import BotSettings, ProviderAccount, ProviderType, ServerPlan

logger = logging.getLogger(__name__)

_KEY_MH = "hetzner_margin_hourly"
_KEY_MM = "hetzner_margin_monthly"
_KEY_GROUP = "hetzner_group"
_DEFAULT_GROUP = "Hetzner"


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


async def hourly_margin(session: AsyncSession) -> float:
    mh, _ = await get_margins(session)
    return mh or 0.0


async def get_group_name(session: AsyncSession) -> str:
    """گروه مقصد مشترک — در صورت نبود، گروه پیش‌فرض ساخته می‌شود."""
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
    """قیمت فروش همه‌ی پلن‌های هتزنر = قیمت خرید × (۱ + سود٪) + فعال‌سازی خودکار."""
    mh, mm = await get_margins(session)
    if mh is None and mm is None:
        return 0
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.HETZNER)
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


async def pick_account(session: AsyncSession) -> ProviderAccount | None:
    """اکانت هتزنرِ فعال با کمترین سرورِ زنده که زیر لیمیت VM است (توزیع متوازن).
    None اگر همه پر یا هیچ اکانتی نباشد."""
    from bot.providers.hetzner import HetznerProvider
    accounts = (await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.HETZNER,
            ProviderAccount.is_active == True,
        ).order_by(ProviderAccount.id)
    )).scalars().all()
    best = None
    best_count = None
    for acc in accounts:
        try:
            count = await asyncio.wait_for(
                HetznerProvider(acc.api_key or "").count_servers(), timeout=15)
        except Exception as e:
            logger.warning("pick_account: count failed for account %s: %s", acc.id, e)
            continue
        limit = int((acc.extra_config or {}).get("vm_limit") or 0)
        if limit and count >= limit:
            continue  # این اکانت پر است
        if best_count is None or count < best_count:
            best, best_count = acc, count
    return best
