"""تنظیمات سراسری Scaleway — سود، گروه مقصد، قیمت‌گذاری دیسک/IP (تک-اکانتی).

مدل (تصمیم پروژه 2026-08-20): Scaleway مثل جیکور/تایم‌وب/روت **تک-اکانتی** است
— ماشین چند-اکانتیِ هتزنر اینجا کپی نمی‌شود (قانون ۵.۱۰#۱).

قیمت‌گذاری (شکاف API):
- API فقط قیمت ساعتیِ خودِ instance را می‌دهد (یورو). **دیسک و IPv4 جدا شارژ
  می‌شوند و Scaleway هیچ endpoint قیمتی ندارد** → نرخشان تنظیمِ ادمین است با
  پیش‌فرضِ صفحه‌ی قیمت رسمی (research §۴ و §۶، به‌روز ۱ ژوئن ۲۰۲۶):
    · Block Storage 5K … €0.095 / GB / ماه
    · Flexible IPv4  … €3.65 / ماه  (0.005/ساعت × ۷۳۰)
- حجم دیسکِ هر پلن هنگام ایمپورت از «دیسک پیش‌فرض» گرفته می‌شود (حداقل ۱۰GB).
- ⚠️ تبدیل ماه→ساعت برای این دو نرخ **÷۷۳۰** است (همان فرمولی که خودِ Scaleway
  با آن عدد ماهانه‌اش را می‌سازد)، نه ÷۷۲۰.
- قیمت خرید ماهانه = ساعتیِ کامل × **۷۴۴** (ماهِ ۳۱روزه). فروشِ ماهانه با ۷۲۰
  یعنی هر ماهِ بلند از جیب ما (research §۲: «حدود ۱.۹٪ بیشتر»).
- فروش = خرید × (۱ + سود٪) — ساعتی و ماهانه جدا (الگوی RootVDS).

نرخ یورو→تومان: همان `np_eur_to_irt_rate` خودکارِ نوسان (مشترک با هتزنر).
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BotSettings, ProviderAccount, ProviderType, Server, ServerPlan, ServerStatus,
)
from bot.providers.scaleway import MIN_DISK_GB

logger = logging.getLogger(__name__)

_KEY_MH = "scaleway_margin_hourly"
_KEY_MM = "scaleway_margin_monthly"
_KEY_GROUP = "scaleway_group"
_KEY_VOL_RATE = "scaleway_volume_price_gb_month"   # €/GB/ماه دیسک بلاک
_KEY_IP_MONTH = "scaleway_ip_price_month"          # €/ماه IPv4 قابل انعطاف
_KEY_DISK_GB = "scaleway_default_disk_gb"          # دیسک پیش‌فرض پلن‌های جدید

_DEFAULT_GROUP = "Scaleway"
DEFAULT_VOL_RATE = 0.095    # Block Storage 5K — €/GB/ماه (صفحه‌ی قیمت رسمی)
DEFAULT_IP_MONTH = 3.65     # Flexible IPv4 — €0.005/ساعت × ۷۳۰
DEFAULT_DISK_GB = 20        # دیسک پیش‌فرضِ فروش (حداقل مجاز ۱۰GB)

HOURS_PER_MONTH_PRICE = 730.0   # فرمول خودِ Scaleway برای عددِ ماهانه
HOURS_PER_MONTH_COST = 744.0    # ماهِ ۳۱روزه — مبنای محافظه‌کارانه‌ی فروش ماهانه


# ── key/value helpers ────────────────────────────────────────────────────────

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


# ── نرخ دیسک / IP / دیسک پیش‌فرض ─────────────────────────────────────────────

async def get_volume_rate(session: AsyncSession) -> float:
    """€ به‌ازای هر GB دیسک بلاک در ماه. تنظیم‌نشده → پیش‌فرض رسمی."""
    v = await _get_float(session, _KEY_VOL_RATE)
    return v if v and v > 0 else DEFAULT_VOL_RATE


async def set_volume_rate(session: AsyncSession, value: float) -> None:
    await _set(session, _KEY_VOL_RATE, value)


async def get_ip_month(session: AsyncSession) -> float:
    """€ ماهانه‌ی IPv4 قابل انعطاف. سرور بدون IPv4 به مشتری فروخته نمی‌شود،
    پس همیشه در قیمت خرید لحاظ می‌شود."""
    v = await _get_float(session, _KEY_IP_MONTH)
    return v if v is not None and v >= 0 else DEFAULT_IP_MONTH


async def set_ip_month(session: AsyncSession, value: float) -> None:
    await _set(session, _KEY_IP_MONTH, value)


async def get_default_disk_gb(session: AsyncSession) -> int:
    v = await _get_float(session, _KEY_DISK_GB)
    return max(int(v), MIN_DISK_GB) if v else DEFAULT_DISK_GB


async def set_default_disk_gb(session: AsyncSession, value: int) -> None:
    await _set(session, _KEY_DISK_GB, max(int(value), MIN_DISK_GB))


# ── قیمت خرید کامل ───────────────────────────────────────────────────────────

def full_costs(type_hourly: float, disk_gb: int, vol_rate: float,
               ip_month: float) -> tuple[float, float]:
    """قیمت خرید کاملِ یک سرور Scaleway (instance + دیسک + IPv4).

    خروجی: (€/ساعت، €/ماه). ماهانه محافظه‌کارانه با ۷۴۴ ساعت حساب می‌شود."""
    addons_month = float(disk_gb or 0) * float(vol_rate or 0) + float(ip_month or 0)
    hourly = float(type_hourly or 0) + addons_month / HOURS_PER_MONTH_PRICE
    return round(hourly, 6), round(hourly * HOURS_PER_MONTH_COST, 4)


async def recompute_catalog_costs(session: AsyncSession) -> int:
    """بازمحاسبه‌ی قیمت خرید همه‌ی پلن‌های Scaleway از روی قیمت خامِ تایپ
    (`type_cost_hourly`) + نرخ‌های جاریِ دیسک/IP. بعد از تغییر نرخ‌ها یا دیسکِ
    پیش‌فرض صدا زده می‌شود؛ قیمت فروش را `apply_margins_to_catalog` می‌سازد."""
    vol_rate = await get_volume_rate(session)
    ip_month = await get_ip_month(session)
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.SCALEWAY)
    )).scalars().all()
    count = 0
    for p in plans:
        extra = dict(p.extra_data or {})
        th = extra.get("type_cost_hourly")
        if th is None:
            continue
        ch, cm = full_costs(float(th), int(p.disk or 0), vol_rate, ip_month)
        if extra.get("cost_hourly") == ch and extra.get("cost_monthly") == cm:
            continue
        extra["cost_hourly"], extra["cost_monthly"] = ch, cm
        extra["disk_cost_monthly"] = round(float(p.disk or 0) * vol_rate, 4)
        extra["ip_cost_monthly"] = round(float(ip_month), 4)
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
    """قیمت فروش همه‌ی پلن‌های Scaleway = قیمت خرید × (۱ + سود٪) + فعال‌سازی.

    پلن‌های خانواده‌های استثناشده (ویندوز/GPU) اگر قبلاً ایمپورت شده باشند
    غیرفعال می‌شوند — منبع واحد سیاست `is_excluded_type` است."""
    from bot.providers.scaleway import is_excluded_type
    mh, mm = await get_margins(session)
    if mh is None and mm is None:
        return 0
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.SCALEWAY)
    )).scalars().all()
    count = 0
    for p in plans:
        if is_excluded_type(p.provider_plan_id or ""):
            if p.is_active:
                p.is_active = False
            continue
        extra = p.extra_data or {}
        ch, cm = extra.get("cost_hourly"), extra.get("cost_monthly")
        changed = False
        if mh is not None and ch:
            p.price_hourly = round(float(ch) * (1 + mh / 100), 6)
            changed = True
        if mm is not None and cm:
            p.price_monthly = round(float(cm) * (1 + mm / 100), 4)
            changed = True
        if changed:
            if not extra.get("unavailable") and not p.is_active:
                p.is_active = True
            count += 1
    await session.flush()
    return count


# ── انتخاب اکانت ─────────────────────────────────────────────────────────────

async def get_account(session: AsyncSession) -> ProviderAccount | None:
    """تنها اکانت Scaleway (فعال یا نه) — برای پنل ادمین."""
    return (await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.SCALEWAY,
        ).order_by(ProviderAccount.id)
    )).scalars().first()


async def pick_account(session: AsyncSession) -> ProviderAccount | None:
    """اکانت فعال Scaleway اگر زیر لیمیت VM دستی باشد (تک-اکانتی).

    ⚠️ کوتاهای Scaleway per-Organization و **بسیار پایین**اند (research §۸ —
    مثلاً فقط ۵ عدد POP2-2C-8G با KYC) و API آن‌ها را برنمی‌گرداند؛ پس لیمیت
    دستی اینجا واقعاً لازم است. گاردِ نهایی هم خطای `quota` هنگام ساخت است."""
    account = (await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.SCALEWAY,
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
