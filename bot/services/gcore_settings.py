"""تنظیمات سراسری جیکور — سود واحد، گروه مقصد، قیمت‌گذاری دیسک (تک-اکانتی).

مدل (تصمیم پروژه 2026-07-21): جیکور فعلاً «تک-اکانتی» است — ماشین چند-اکانتیِ
هتزنر (کاتالوگ مشترک/توزیع متوازن) اینجا لازم نیست و کپی هم نمی‌شود (قانون ۵.۱۰#۱:
اگر روزی چند-اکانتی شد، همان موقع provider_settings.py جنریک ساخته می‌شود).

قیمت‌گذاری (شکاف API جیکور): flavor فقط vCPU/RAM دارد؛ دیسک = volume جدا که
«از ساخت تا حذف» شارژ می‌شود ولی قیمتش در API نمی‌آید. راه‌حل:
- ادمین نرخ «قیمت هر GB دیسک در ماه» را از صفحه قیمت جیکور در پنل وارد می‌کند
  (به ارز اکانت — همان ارز flavorها).
- حجم دیسک هر پلن هنگام ایمپورت از «دیسک پیش‌فرض» (تنظیم ادمین) تعیین و روی
  plan.disk ذخیره می‌شود.
- قیمت خرید کامل = flavor + disk×نرخ (ماهانه) / flavor + disk×نرخ÷720 (ساعتی).
  قیمت فروش = خرید × (1 + سود٪) — الگوی هتزنر.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BotSettings, ProviderAccount, ProviderType, Server, ServerPlan, ServerStatus,
)

logger = logging.getLogger(__name__)

_KEY_MH = "gcore_margin_hourly"
_KEY_MM = "gcore_margin_monthly"
_KEY_GROUP = "gcore_group"
_KEY_VOL_RATE = "gcore_volume_price_gb_month"   # قیمت هر GB دیسک در ماه (ارز اکانت)
_KEY_DISK_GB = "gcore_default_disk_gb"          # دیسک پیش‌فرض پلن‌های جدید (GB)
_DEFAULT_GROUP = "Gcore"
_DEFAULT_DISK_GB = 5    # دیسک پیش‌فرض Cloud VMهای جیکور (تصمیم 2026-07-22)


def is_excluded_flavor(flavor_id: str) -> bool:
    """خانواده‌های عرضه‌نشدنی (تصمیم‌های 2026-07-21/22):
    - Basic VM (CPU اشتراکی): «shared» در ID — بدون SLA/اسنپ‌شات، شبکه محدود
    - memory-optimized: «memory» در ID — ارائه نمی‌شوند
    منبع واحد سیاست — پنل ایمپورت، اعمال سود و سینک همه از همین استفاده می‌کنند."""
    fid = (flavor_id or "").lower()
    return "shared" in fid or "memory" in fid


async def _get(session: AsyncSession, key: str):
    row = await session.get(BotSettings, key)
    return row.value if row else None


async def _set(session: AsyncSession, key: str, value) -> None:
    row = await session.get(BotSettings, key)
    if row:
        row.value = str(value)
    else:
        session.add(BotSettings(key=key, value=str(value)))


async def _get_float(session: AsyncSession, key: str) -> float | None:
    v = await _get(session, key)
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


# ── سود ──────────────────────────────────────────────────────────────────────

async def get_margins(session: AsyncSession) -> tuple[float | None, float | None]:
    return await _get_float(session, _KEY_MH), await _get_float(session, _KEY_MM)


async def set_margin(session: AsyncSession, hourly: bool, value: float) -> None:
    await _set(session, _KEY_MH if hourly else _KEY_MM, value)


# ── دیسک ─────────────────────────────────────────────────────────────────────

async def get_volume_rate(session: AsyncSession) -> float:
    """قیمت هر GB دیسک در ماه (ارز اکانت). 0 = هنوز تنظیم نشده."""
    return await _get_float(session, _KEY_VOL_RATE) or 0.0


async def set_volume_rate(session: AsyncSession, value: float) -> None:
    await _set(session, _KEY_VOL_RATE, value)


async def get_default_disk_gb(session: AsyncSession) -> int:
    v = await _get_float(session, _KEY_DISK_GB)
    return int(v) if v else _DEFAULT_DISK_GB


async def set_default_disk_gb(session: AsyncSession, value: int) -> None:
    await _set(session, _KEY_DISK_GB, int(value))


def full_costs(flavor_hourly: float, flavor_monthly: float,
               disk_gb: int, vol_rate: float) -> tuple[float, float]:
    """قیمت خرید کامل (flavor + دیسک) — ساعتی و ماهانه، به ارز اکانت."""
    disk_monthly = float(disk_gb) * float(vol_rate or 0)
    cost_monthly = round(float(flavor_monthly or 0) + disk_monthly, 4)
    cost_hourly = round(float(flavor_hourly or 0) + disk_monthly / 720.0, 6)
    return cost_hourly, cost_monthly


async def recompute_catalog_costs(session: AsyncSession) -> int:
    """بازمحاسبه‌ی قیمت خرید کامل همه‌ی پلن‌های جیکور از روی قیمت خامِ flavor
    (flavor_cost_*) + دیسکِ خودِ پلن × نرخ فعلی دیسک — بعد از تغییر نرخ دیسک."""
    vol_rate = await get_volume_rate(session)
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.GCORE)
    )).scalars().all()
    count = 0
    for p in plans:
        extra = dict(p.extra_data or {})
        fh = extra.get("flavor_cost_hourly")
        fm = extra.get("flavor_cost_monthly")
        if fh is None and fm is None:
            continue
        ch, cm = full_costs(float(fh or 0), float(fm or 0), int(p.disk or 0), vol_rate)
        extra["cost_hourly"], extra["cost_monthly"] = ch, cm
        p.extra_data = extra
        count += 1
    await session.flush()
    return count


# ── گروه مقصد ────────────────────────────────────────────────────────────────

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


# ── اعمال سود روی کاتالوگ ────────────────────────────────────────────────────

async def apply_margins_to_catalog(session: AsyncSession) -> int:
    """قیمت فروش همه‌ی پلن‌های جیکور = قیمت خرید کامل × (۱ + سود٪) + فعال‌سازی.

    فروش جیکور فقط «ساعتی» است (تصمیم 2026-07-22): price_monthly همیشه None
    می‌ماند تا گزینه‌ی ماهانه در فلوی خرید اصلاً ظاهر نشود. پلن‌های خانواده‌های
    استثناشده (shared/memory) اگر قبلاً ایمپورت شده باشند، غیرفعال می‌شوند."""
    mh, _ = await get_margins(session)
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.GCORE)
    )).scalars().all()
    count = 0
    for p in plans:
        if is_excluded_flavor(p.provider_plan_id or ""):
            if p.is_active:
                p.is_active = False
            continue
        extra = p.extra_data or {}
        ch = extra.get("cost_hourly")
        changed = False
        if mh is not None and ch:
            p.price_hourly = round(float(ch) * (1 + mh / 100), 4)
            changed = True
        if p.price_monthly is not None:
            p.price_monthly = None   # فروش ماهانه ندارد
            changed = True
        if changed:
            if not extra.get("unavailable") and not p.is_active and p.price_hourly:
                p.is_active = True
            count += 1
    await session.flush()
    return count


# ── انتخاب اکانت هنگام خرید ──────────────────────────────────────────────────

async def get_account(session: AsyncSession) -> ProviderAccount | None:
    """تنها اکانت جیکور (فعال یا نه) — برای پنل ادمین."""
    return (await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.GCORE,
        ).order_by(ProviderAccount.id)
    )).scalars().first()


async def pick_account(session: AsyncSession) -> ProviderAccount | None:
    """اکانت فعال جیکور اگر زیر لیمیت VM باشد (تک-اکانتی).

    جیکور شمارش زنده‌ی سبک ندارد (instanceها per-region اند) → مصرف از DB خودمان
    (سرورهای غیرحذف‌شده روی این اکانت) شمرده می‌شود؛ سقف واقعی اکانت را quota
    جیکور هنگام create هم گارد می‌کند (QuotaLimitExceed → پیام شفاف)."""
    account = (await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.GCORE,
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
