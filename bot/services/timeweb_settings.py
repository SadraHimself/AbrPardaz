"""تنظیمات سراسری تایم‌وب — سود ساعتی/ماهانه، گروه مقصد، انتخاب اکانت (تک-اکانتی).

مدل (تصمیم پروژه 2026-07-24): تایم‌وب تک-اکانتی است (مثل جیکور). قیمت تعرفه‌ها
ماهانه به «روبل» است؛ قیمت خرید ساعتی = ماهانه ÷ ۷۲۰ (در provider محاسبه شده و
در extra_data پلن ذخیره می‌شود: cost_hourly / cost_monthly). فروش = خرید × (۱+سود٪)
— ساعتی و ماهانه جدا (تایم‌وب هر دو را می‌فروشد). نرخ روبل→تومان: خودکار از
Navasan (np_rub_to_irt_rate) + قابل‌تنظیم دستی از پنل ادمین.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BotSettings, ProviderAccount, ProviderType, Server, ServerPlan, ServerStatus,
)

logger = logging.getLogger(__name__)

# چون API تایم‌وب فیلد موجودی ندارد، ناموجودی را از «شکست ساخت به‌خاطر ظرفیت»
# یاد می‌گیریم و پلن را برای این مدت مخفی می‌کنیم؛ بعدش خودکار دوباره برای فروش
# باز می‌شود (auto-retry). اگر باز شکست خورد، دوباره کول‌داون می‌گیرد.
_STOCK_COOLDOWN_S = 6 * 3600

_KEY_MH = "timeweb_margin_hourly"
_KEY_MM = "timeweb_margin_monthly"
_KEY_GROUP = "timeweb_group"
_DEFAULT_GROUP = "Timeweb"

# هزینه‌ی IPv4 عمومی (₽/ماه) — تایم‌وب آن را سرویس جدا کنار سرور بیل می‌کند
# (E2E 2026-07-24: فاکتور = سرور 611₽ + Public IP 180₽). قیمتش در API نمی‌آید.
IP_RUB_MONTH = 180.0

# آستانه‌ی هشدارِ «موجودی اکانت تایم‌وب کم است» (₽). زیر این مقدار، سینکِ
# ۳۰دقیقه‌ای یک‌بار به لاگ ادمین هشدار می‌دهد (edge-triggered؛ با شارژِ دوباره
# بالای آستانه، برای دفعه‌ی بعد ری‌آرم می‌شود). ⚠️ توجه: پیش‌چکِ ساخت وقتی
# موجودی < هزینه‌ی یک‌ماهِ سرور (~۵۰۰–۸۰۰₽) باشد سفارش را رد می‌کند، پس این
# آستانه‌ی پایین یعنی «حساب تقریباً خالی»؛ برای هشدارِ زودهنگام‌ترش بالا ببرید.
LOW_BALANCE_RUB = 50.0


def full_costs(preset_monthly_rub: float) -> tuple[float, float]:
    """قیمت خرید کاملِ یک سرور تایم‌وب = تعرفه + IPv4 عمومی.
    خروجی: (ساعتی، ماهانه) به روبل — ساعتی = ماهانه ÷ ۷۲۰."""
    cm = round(float(preset_monthly_rub or 0) + IP_RUB_MONTH, 2)
    ch = round(cm / 720.0, 6)
    return ch, cm


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
    """قیمت فروش پلن‌های تایم‌وب = قیمت خرید × (۱+سود ماهانه٪) + فعال‌سازی.

    ⚠️ تایم‌وب **فقط ماهانه** فروخته می‌شود (تصمیم 2026-07-26: سقفِ روزانه‌ی IP
    تایم‌وب برای گردشِ فروش ساعتی مناسب نیست؛ فروش ساعتی به RootVDS منتقل شد).
    price_hourly همیشه None می‌شود تا گزینه‌ی ساعتی در خرید ظاهر نشود."""
    mh, mm = await get_margins(session)
    if mm is None:
        return 0
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.TIMEWEB)
    )).scalars().all()
    count = 0
    for p in plans:
        extra = p.extra_data or {}
        cm = extra.get("cost_monthly")
        changed = False
        if p.price_hourly is not None:      # فقط-ماهانه: ساعتی همیشه پاک
            p.price_hourly = None
            changed = True
        if cm:
            p.price_monthly = round(float(cm) * (1 + mm / 100), 2)
            changed = True
        if changed:
            if not extra.get("unavailable") and not p.is_active:
                p.is_active = True
            count += 1
    await session.flush()
    return count


def is_capacity_error(err: str) -> bool:
    """آیا شکست ساخت به‌خاطر نبود ظرفیت/صف‌شدن است (نه خطای دیگر)."""
    e = (err or "")
    return ("ظرفیت سرویس‌دهنده" in e or "مهلت انتظار" in e
            or "no available resources" in e.lower())


async def mark_plan_out_of_stock(session: AsyncSession, plan: ServerPlan) -> None:
    """پلن تایم‌وب را «ناموجود» علامت بزن و از فروش مخفی کن (کول‌داون ۶ ساعته).
    was_active نگه داشته می‌شود تا بعد از کول‌داون خودکار برگردد."""
    extra = dict(plan.extra_data or {})
    if not extra.get("out_of_stock"):
        extra["was_active"] = plan.is_active
    extra["out_of_stock"] = True
    extra["stock_retry_at"] = int(time.time()) + _STOCK_COOLDOWN_S
    plan.extra_data = extra
    plan.is_active = False
    await session.flush()
    logger.info("timeweb plan %s marked out-of-stock (retry in 6h)", plan.provider_plan_id)


async def mark_group_out_of_stock(session: AsyncSession, plan: ServerPlan) -> int:
    """کل «بخش» (همان لوکیشن + همان نوع سرور) را ناموجود کن — تایم‌وب ظرفیت را
    per (location, type) مدیریت می‌کند («no resources in this location»)، پس اگر
    یک پلن پر شد، هم‌گروه‌هایش هم پرند. خروجی: تعداد پلن‌های علامت‌خورده."""
    cpu_type = (plan.extra_data or {}).get("cpu_type") or "standard"
    siblings = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.TIMEWEB,
            ServerPlan.location == plan.location,
        )
    )).scalars().all()
    n = 0
    for p in siblings:
        if ((p.extra_data or {}).get("cpu_type") or "standard") != cpu_type:
            continue
        await mark_plan_out_of_stock(session, p)
        n += 1
    return n


async def mark_plan_in_stock(session: AsyncSession, plan: ServerPlan) -> None:
    """پلن دوباره موجود شد (ساخت موفق یا فعال‌سازی دستی ادمین) — کول‌داون پاک."""
    extra = dict(plan.extra_data or {})
    if not extra.get("out_of_stock"):
        return
    extra.pop("out_of_stock", None)
    extra.pop("stock_retry_at", None)
    was = extra.pop("was_active", True)
    plan.extra_data = extra
    # فقط اگر ناموجودشدنِ سود/سینک آن را غیرفعال نکرده باشد
    if not extra.get("unavailable"):
        plan.is_active = bool(was) if plan.price_hourly or plan.price_monthly else False
    await session.flush()


async def clear_all_out_of_stock(session: AsyncSession) -> int:
    """همه‌ی پلن‌های «ناموجود» تایم‌وب را آزاد و به فروش برمی‌گرداند — بازیابی
    از علامت‌گذاری‌های اشتباهِ خودکار (API تایم‌وب سیگنال ظرفیت نمی‌دهد و
    تایم‌اوتِ setup اشتباهی ناموجود می‌زد). موقع استارت ربات صدا زده می‌شود."""
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.TIMEWEB)
    )).scalars().all()
    n = 0
    for p in plans:
        if (p.extra_data or {}).get("out_of_stock"):
            await mark_plan_in_stock(session, p)
            n += 1
    return n


async def restore_cooldown_passed(session: AsyncSession) -> int:
    """پلن‌هایی که کول‌داونِ ناموجودی‌شان تمام شده را دوباره برای فروش باز کن
    (auto-retry). در سینک دوره‌ای صدا زده می‌شود."""
    now = int(time.time())
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.TIMEWEB)
    )).scalars().all()
    n = 0
    for p in plans:
        extra = p.extra_data or {}
        if extra.get("out_of_stock") and int(extra.get("stock_retry_at") or 0) <= now:
            await mark_plan_in_stock(session, p)
            n += 1
    return n


async def resync_plan_labels(session: AsyncSession, provider) -> int:
    """اسم کوتاه/شهر/نوعِ همه‌ی پلن‌های تایم‌وب را از تعرفه‌ی زنده تصحیح می‌کند
    (بدون منتظرماندنِ سینک ۳۰دقیقه‌ای). موقع باز کردن پنل و استارت ربات صدا
    زده می‌شود. خروجی: تعداد پلن‌های اصلاح‌شده."""
    from bot.providers.timeweb import tw_build_labels
    try:
        raw = await provider._request("GET", "/api/v1/presets/servers")
        by_id = {str(p.get("id")): p for p in raw.get("server_presets") or []}
        labels = tw_build_labels(list(by_id.values()))
    except Exception as e:
        logger.warning("timeweb resync_plan_labels: fetch failed: %s", e)
        return 0
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.TIMEWEB)
    )).scalars().all()
    n = 0
    for plan in plans:
        lbl = labels.get(plan.provider_plan_id)
        if not lbl:
            continue
        extra = dict(plan.extra_data or {})
        dn = lbl["display_name"]
        if (plan.display_name != dn
                or extra.get("region_name") != lbl["city"]
                or extra.get("cpu_type") != lbl["cpu_type"]
                or extra.get("city_code") != lbl["city_code"]):
            plan.display_name = dn
            extra["region_name"] = lbl["city"]
            extra["city_code"] = lbl["city_code"]
            extra["cpu_type"] = lbl["cpu_type"]
            extra["cpu_type_label"] = lbl["cpu_label"]
            plan.extra_data = extra
            n += 1
    await session.flush()
    return n


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
