"""برنامه ریسلر (فقط ویرچولایزور).

ریسلر کاربر رباتی است که با دسترسی API ادمینِ خودش VMهایش را مستقیم روی پنل
ویرچولایزور می‌سازد؛ همه زیر یک ایمیل (کاربر پنل) مشخص. نقش ربات فقط:
کشف/ثبت VMها، کسر ساعتی به نرخ «قیمت خریدِ پلن × (۱ + کارمزد٪)» و فاکتور.
هیچ عملیات مدیریتی یا
مخربی (حذف/ساسپند/…) علیه VMهای ریسلر انجام نمی‌شود — «سرور رفت که رفت» فقط
با غیب‌شدن از خود پنل تشخیص داده می‌شود و صرفاً رکورد DELETED می‌شود.

قراردادها:
- کانفیگ ریسلر در User.extra_data["reseller"] (بدون جدول/DDL جدید).
- هر VM ریسلر یک رکورد Server معمولی با extra_data["reseller"]=True است تا
  بیلینگ ساعتی/لنگر/نرخ ارز/فاکتور موجود بدون کد جدید رویش کار کند.
- JSON column همیشه با dict جدید reassign می‌شود (الگوی سراسری پروژه).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BillingType, ProviderType, Server, ServerPlan, ServerStatus, User,
)

logger = logging.getLogger(__name__)

# دو اجرای متوالیِ ناموفقِ تسک ۱۰دقیقه‌ای باید بگذرد تا رکورد DELETED شود —
# «لیست ناقص/خطای گذرا نباید باعث حذف اشتباهی شود»
MISSING_GRACE_MINUTES = 25


# ── کانفیگ ریسلر روی User ────────────────────────────────────────────────────

def get_reseller_cfg(user: User) -> dict:
    return dict(((user.extra_data or {}).get("reseller")) or {})


def set_reseller_cfg(user: User, cfg: dict) -> None:
    ex = dict(user.extra_data or {})
    ex["reseller"] = cfg
    user.extra_data = ex


def is_reseller_user(user: User) -> bool:
    return bool(get_reseller_cfg(user))


def is_active_reseller(user: User) -> bool:
    return bool(get_reseller_cfg(user).get("active"))


def is_reseller_server(server: Server) -> bool:
    return bool((server.extra_data or {}).get("reseller"))


def reseller_markup_percent(user: User) -> float:
    """درصد کارمزد معتبر (۰ تا ۳۰۰)؛ مقدار خراب = صفر.

    مدل قیمت (تصمیم 2026-08-20): نرخ ریسلر = قیمت خریدِ پلن × (۱ + کارمزد٪) —
    مستقل از قیمت فروش ربات. کلید قدیمی discount_percent (مدل اولیه‌ی «تخفیف
    روی قیمت فروش») به‌عنوان fallback خوانده می‌شود چون عددِ توافق همان است.
    """
    cfg = get_reseller_cfg(user)
    raw = cfg.get("markup_percent", cfg.get("discount_percent"))
    try:
        m = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    return m if 0 <= m <= 300 else 0.0


def parse_panel_time(val) -> Optional[str]:
    """فیلد time پنل (epoch) → ISO UTC؛ مقدار نامعتبر/غایب → None."""
    try:
        ts = int(float(str(val).strip()))
    except (TypeError, ValueError):
        return None
    if ts < 1_000_000_000:  # epoch نامعقول (قبل از 2001) = داده‌ی خراب
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ── مچ کردن VM به پلن ────────────────────────────────────────────────────────

async def match_plan(session: AsyncSession, account_id: int, row: dict) -> Optional[ServerPlan]:
    """مچ VM پنل به یکی از پلن‌های ویرچولایزورِ همان اکانت.

    اول با plid (اگر پنل داده باشد)، وگرنه مچِ «یکتا»ی ram/cpu/disk.
    ابهام (چند پلن هم‌مشخصات) = مچ نشد — بیلینگ اشتباه بدتر از بیلینگ‌نشدن است.
    """
    plans = list((await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.VIRTUALIZOR,
            ServerPlan.provider_account_id == account_id,
        )
    )).scalars().all())
    # بیلینگ ریسلر بر مبنای «قیمت خرید» پلن است — پلن بدون cost_monthly نباید
    # مچ شود، وگرنه VM با نرخ صفر «مچ‌شده» ثبت می‌شود و شبکه‌ی امنیتی
    # plan_unmatched (هشدار ادمین + rematch خودکار) بی‌صدا دور زده می‌شود.
    # نکته: پلنِ «فقط-ریسلری» ممکن است (plid + قیمت خرید، بدون قیمت فروش و
    # غیرفعال) — در فروشگاه دیده نمی‌شود ولی برای مچ ریسلر معتبر است.
    def _has_cost(p) -> bool:
        try:
            return float(((p.extra_data or {}).get("cost_monthly")) or 0) > 0
        except (TypeError, ValueError):
            return False
    plans = [p for p in plans if _has_cost(p)]

    plid = str(row.get("plid") or "").strip()
    if plid and plid != "0":
        hits = [p for p in plans if str(p.provider_plan_id or "").strip() == plid]
        for pool in ([p for p in hits if p.is_active], hits):
            if pool:
                return pool[0]

    try:
        ram = int(float(row.get("ram") or 0))
        cpu = int(float(row.get("cores") or 0))
        disk = int(float(row.get("space") or 0))
    except (TypeError, ValueError):
        return None
    if not (ram and cpu and disk):
        return None
    hits = [p for p in plans if p.ram == ram and p.cpu == cpu and p.disk == disk]
    pool = [p for p in hits if p.is_active] or hits
    return pool[0] if len(pool) == 1 else None


def _apply_plan_to_server(server: Server, plan: ServerPlan) -> None:
    """اتصال پلن مچ‌شده به رکورد (کپی قیمت/ارز + plan_id برای قیمت زنده)."""
    ex = dict(server.extra_data or {})
    ex["plan_id"] = plan.id
    ex["currency"] = (plan.extra_data or {}).get("currency", "irt")
    ex.pop("plan_unmatched", None)
    ex.pop("unmatched_warned", None)
    server.extra_data = ex
    server.price_hourly = plan.price_hourly
    if plan.location and not server.location:
        server.location = plan.location


# ── کشف/ثبت/حذف (هسته‌ی تسک دوره‌ای) ─────────────────────────────────────────

async def sync_user_reseller_servers(
    session: AsyncSession, user: User, account, panel_rows: list[dict], uid: str,
    expected_email: Optional[str] = None,
) -> Optional[dict]:
    """کشف و ثبت VMهای یک ریسلر بر اساس لیستِ «کامل و موفق» پنل.

    ⚠️ فقط باید با خروجی موفقِ list_all_vps صدا زده شود (آن متد روی لیست
    ناقص/خالی خودش RuntimeError می‌دهد) — این تابع هرگز نباید با لیستِ مشکوک
    اجرا شود وگرنه two-strike به‌اشتباه جلو می‌رود.

    خروجی: {"registered": [نام‌ها], "deleted": [نام‌ها],
            "unmatched": [نام‌ها], "rematched": [نام‌ها]}
    یا None اگر کار لغو شد (کانفیگ هم‌زمان تغییر کرده/غیرفعال شده بود).
    """
    now = datetime.now(timezone.utc)
    uid = str(uid)

    # ۰) قفل ردیف کاربر + رفرش. سریال‌سازیِ تسک دوره‌ای/«سینک فوری»/ویرایش
    # کانفیگ: بدون این، دو سینک هم‌زمان هر دو «رکوردی موجود نیست» می‌دیدند و یک
    # VM دوبار ثبت (= دوبار بیل) می‌شد. فراخوانی‌های کُند پنل قبل از این تابع و
    # بدون قفل انجام شده‌اند؛ ادامه‌ی کار DB-only و سریع است. caller باید بعد از
    # این تابع (و قبل از هر I/O تلگرام) commit کند تا قفل آزاد شود.
    locked = (await session.execute(
        select(User).where(User.id == user.id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked is None:
        return None
    cfg = get_reseller_cfg(locked)
    if not cfg.get("active"):
        return None
    if expected_email is not None and \
            str(cfg.get("email") or "").lower() != expected_email.lower():
        return None  # ایمیل وسط کار عوض شده — نتیجه‌ی resolve این دور کهنه است
    if str(cfg.get("uid") or "") not in ("", uid):
        return None  # uid کانفیگ با uid این سینک نمی‌خواند
    if not cfg.get("uid"):
        # کشِ uid فقط زیر قفل نوشته می‌شود (ضد lost-update با ویرایش هم‌زمان ادمین)
        cfg["uid"] = uid
        set_reseller_cfg(locked, cfg)

    mine = [r for r in panel_rows if str(r.get("uid") or "") == uid]
    present_vpsids = {str(r.get("vpsid")) for r in mine}

    # همه‌ی vpsidهای زنده‌ی این اکانت (هر کاربری) — ضد دابل‌بیل: VMی که ربات
    # خودش برای همین کاربر ساخته (خرید عادی) دوباره به‌عنوان ریسلری ثبت نشود
    known_rows = list((await session.execute(
        select(Server).where(
            Server.provider_account_id == account.id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalars().all())
    known_vpsids = {str(s.provider_server_id or "") for s in known_rows}

    my_rows = [s for s in known_rows if s.user_id == user.id and is_reseller_server(s)]

    registered: list[str] = []
    deleted: list[str] = []
    unmatched: list[str] = []
    rematched: list[str] = []

    # ۱) ثبت VMهای جدید
    for r in mine:
        vid = str(r.get("vpsid"))
        if not vid or vid in known_vpsids:
            continue
        panel_created = parse_panel_time(r.get("time"))
        if panel_created:
            # ضد ریس با خریدِ در جریان از خود ربات (VM روی پنل ساخته شده ولی
            # رکورد Serverش هنوز commit نشده و در known_vpsids دیده نمی‌شود):
            # VM تازه این دور ثبت نمی‌شود؛ دور بعد تعیین تکلیف می‌شود. ۲۰ دقیقه =
            # پوشش کامل ساخت کُند/تایم‌اوت ۱۵دقیقه‌ای خرید. بیلینگ از لحظه‌ی ثبت
            # شروع می‌شود، پس این تأخیر ضرری ندارد.
            try:
                if (now - datetime.fromisoformat(panel_created)) < timedelta(minutes=20):
                    continue
            except (TypeError, ValueError):
                pass
        name = (r.get("hostname") or r.get("name") or f"vps-{vid}").strip() or f"vps-{vid}"
        plan = await match_plan(session, account.id, r)

        def _i(v) -> int:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0

        ips = r.get("ips") or []
        extra: dict = {
            "reseller": True,
            "vpsid": vid,
            "uid": uid,
            "panel_created_at": panel_created,
            # پیام per-server بیلینگ ساعتی خاموش — اطلاع‌رسانی batch سه‌ساعته است
            "hourly_notify": False,
        }
        srv = Server(
            user_id=user.id,
            provider_type=ProviderType.VIRTUALIZOR,
            provider_account_id=account.id,
            provider_server_id=vid,
            name=name,
            hostname=(r.get("hostname") or "").strip() or None,
            ip_address=(ips[0] if ips else None),
            ram=_i(r.get("ram")),
            cpu=_i(r.get("cores")),
            disk=_i(r.get("space")),
            bandwidth=_i(r.get("bandwidth")),
            os_name=(r.get("os_name") or "").strip() or None,
            status=ServerStatus.ACTIVE,
            billing_type=BillingType.HOURLY,
            # ترافیک ریسلر اصلاً بیل/سینک نمی‌شود؛ None = نامحدود (هرگز 0.0)
            traffic_limit_gb=None,
            # شروع بیلینگ = لحظه‌ی ثبت؛ اولین کسر یک ساعت بعد
            last_billed_at=now,
            extra_data=extra,
        )
        if plan is not None:
            extra["plan_id"] = plan.id
            extra["currency"] = (plan.extra_data or {}).get("currency", "irt")
            srv.extra_data = extra
            srv.price_hourly = plan.price_hourly
            srv.location = plan.location
        else:
            extra["plan_unmatched"] = True
            extra["unmatched_warned"] = True
            srv.extra_data = extra
            # قیمت None → charge_hourly مبلغ صفر می‌بیند و رد می‌شود (بدون
            # advance لنگر) = بیلینگ عملاً شروع نشده تا ادمین تعیین تکلیف کند
            srv.price_hourly = None
            unmatched.append(name)
        session.add(srv)
        known_vpsids.add(vid)
        registered.append(name)

    # ۲) تلاش دوباره برای مچِ VMهای بدون پلن (ادمین پلن ساخته → خودکار وصل شود)
    for s in my_rows:
        ex = s.extra_data or {}
        if ex.get("plan_id") or not ex.get("plan_unmatched"):
            continue
        row = next((r for r in mine if str(r.get("vpsid")) == str(s.provider_server_id)), None)
        if row is None:
            continue
        plan = await match_plan(session, account.id, row)
        if plan is None:
            continue
        _apply_plan_to_server(s, plan)
        # بیلینگ از لحظه‌ی مچ شروع شود، نه backdate تا زمان ثبت
        s.last_billed_at = now
        rematched.append(s.name)

    # ۳) حذف two-strike: غایب در دو اجرای متوالیِ «موفق» → DELETED (فقط رکورد)
    for s in my_rows:
        vid = str(s.provider_server_id or "")
        ex = dict(s.extra_data or {})
        if vid in present_vpsids:
            if ex.get("missing_since"):
                ex.pop("missing_since", None)
                s.extra_data = ex
            continue
        ms = ex.get("missing_since")
        if not ms:
            ex["missing_since"] = now.isoformat()
            s.extra_data = ex
            continue
        try:
            ms_dt = datetime.fromisoformat(ms)
            if ms_dt.tzinfo is None:
                ms_dt = ms_dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            ex["missing_since"] = now.isoformat()
            s.extra_data = ex
            continue
        if (now - ms_dt) >= timedelta(minutes=MISSING_GRACE_MINUTES):
            # ⚠️ هرگز provider.delete_server صدا زده نمی‌شود — VM مال ریسلر است
            s.status = ServerStatus.DELETED
            deleted.append(s.name)

    await session.flush()
    return {"registered": registered, "deleted": deleted,
            "unmatched": unmatched, "rematched": rematched}


# ── قیمت/بدهی ساعتی ──────────────────────────────────────────────────────────

async def plan_cost_hourly_toman(session: AsyncSession, plan) -> float:
    """قیمت خریدِ ساعتی یک پلن به تومان (cost_monthly ÷ ۷۲۰ با نرخ روز).

    قیمت خرید در extra_data پلن: `cost_monthly` + `cost_currency` (پیش‌فرض irt؛
    ارزِ خرید مستقل از ارزِ فروش پلن است). 0 = تعریف‌نشده یا نرخ ارز ناموجود.
    """
    from bot.services.currency import to_toman
    ex = getattr(plan, "extra_data", None) or {}
    try:
        monthly = float(ex.get("cost_monthly") or 0)
    except (TypeError, ValueError):
        return 0.0
    if monthly <= 0:
        return 0.0
    hourly = monthly / 720.0
    cur = str(ex.get("cost_currency") or "irt").lower()
    if cur == "irt":
        return hourly
    try:
        return float(await to_toman(session, hourly, cur) or 0)
    except Exception:
        return 0.0


async def reseller_hourly_toman(session: AsyncSession, user: User, server: Server) -> float:
    """نرخ ساعتی ریسلر به تومان: قیمت خریدِ پلن × (۱ + کارمزد٪) — زنده؛ 0 = نامشخص.

    مستقل از قیمت فروش ربات (server_live_price اینجا نقشی ندارد). پلن حذف‌شده
    یا بدون قیمت خرید → 0 → لنگر جلو نمی‌رود تا ادمین تعیین تکلیف کند.
    """
    plan_id = (server.extra_data or {}).get("plan_id")
    if not plan_id:
        return 0.0
    try:
        plan = await session.get(ServerPlan, plan_id)
    except Exception:
        return 0.0
    if plan is None:
        return 0.0
    cost = await plan_cost_hourly_toman(session, plan)
    if cost <= 0:
        return 0.0
    return cost * (1 + reseller_markup_percent(user) / 100.0)


async def get_reseller_servers(session: AsyncSession, user_id: int,
                               include_deleted: bool = False) -> list[Server]:
    q = select(Server).where(Server.user_id == user_id)
    if not include_deleted:
        q = q.where(Server.status != ServerStatus.DELETED)
    rows = list((await session.execute(q.order_by(Server.created_at))).scalars().all())
    return [s for s in rows if is_reseller_server(s)]


async def compute_reseller_owed(session: AsyncSession, user: User) -> tuple[int, float]:
    """(مجموع ساعت‌های عقب‌افتاده، مبلغ تقریبی تومان با کارمزد) سرورهای ریسلر فعال."""
    now = datetime.now(timezone.utc)
    hours_total, toman_total = 0, 0.0
    for s in await get_reseller_servers(session, user.id):
        if s.status != ServerStatus.ACTIVE or s.billing_type != BillingType.HOURLY:
            continue
        last = s.last_billed_at or s.created_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        behind = int((now - last).total_seconds() // 3600)
        if behind <= 0:
            continue
        rate = await reseller_hourly_toman(session, user, s)
        if rate <= 0:
            # سرور بدون نرخ (پلن مچ‌نشده / نرخ ارز نامشخص) اصلاً بیل نمی‌شود و
            # لنگرش عمداً جلو نمی‌رود — ساعت‌هایش «بدهی» نیست؛ اگر شمرده شود
            # hours هیچ‌وقت صفر نمی‌شود و وضعیت بدهی (debt_since/…) هرگز پاک نمی‌شود
            continue
        hours_total += behind
        toman_total += behind * rate
    return hours_total, toman_total


# ── بدهی «محاسبه از تاریخ ساخت» (backfill) ───────────────────────────────────

def get_backfill_debt(user: User) -> list[dict]:
    """آیتم‌های بدهی وصول‌نشده: [{"server_id", "name", "remaining"}]"""
    items = get_reseller_cfg(user).get("backfill_debt") or []
    return [dict(i) for i in items if isinstance(i, dict)]


def set_backfill_debt(user: User, items: list[dict]) -> None:
    cfg = get_reseller_cfg(user)
    items = [i for i in items if float(i.get("remaining") or 0) >= 1]
    if items:
        cfg["backfill_debt"] = items
    else:
        cfg.pop("backfill_debt", None)
    set_reseller_cfg(user, cfg)


async def collect_backfill_debt(session: AsyncSession, user: User) -> list[tuple[str, float]]:
    """وصول بدهی backfill تا سقف موجودی فعلی. خروجی: [(نام سرور، مبلغ وصول‌شده)].

    caller مسئول commit و پیام/لاگ است. کل عملیات زیر قفل ردیف کاربر انجام
    می‌شود (debit خودش هم همان قفل را در همین تراکنش می‌گیرد — بدون بن‌بست).
    """
    from bot.services.billing import BillingService

    # نگاه سریع بدون قفل (پرهیز از قفل بی‌دلیل روی همه‌ی کاربران)…
    if not get_backfill_debt(user):
        return []
    locked = (await session.execute(
        select(User).where(User.id == user.id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked is None or locked.balance < 1:
        return []
    # …ولی لیست آیتم‌ها حتماً «زیر قفل» خوانده می‌شود: نسخه‌ی قبل-از-قفل ممکن
    # است توسط اجرای هم‌زمان دیگری وصول شده باشد → وصول دوباره از کاربر
    items = get_backfill_debt(locked)
    if not items:
        return []

    billing = BillingService(session)
    collected: list[tuple[str, float]] = []
    for item in items:
        remaining = float(item.get("remaining") or 0)
        if remaining < 1:
            item["remaining"] = 0
            continue
        pay = min(remaining, float(locked.balance))
        if pay < 1:
            break
        ok = await billing.debit(
            user.id, pay,
            server_id=item.get("server_id"),
            description=f"محاسبه گذشته — {item.get('name') or '—'}",
        )
        if not ok:
            break
        item["remaining"] = remaining - pay
        collected.append((str(item.get("name") or "—"), pay))
    if collected:
        set_backfill_debt(locked, [i for i in items])
        await session.flush()
    return collected


async def compute_backfill_rows(session: AsyncSession, user: User) -> list[dict]:
    """ردیف‌های ابزار «محاسبه از تاریخ ساخت».

    هر ردیف: {server_id, name, created_at (iso|None), hours, rate_toman,
              amount_toman, selectable, note}
    VMهای قبلاً محاسبه‌شده (backfill_billed_at) اصلاً برنمی‌گردند (idempotent).
    """
    rows: list[dict] = []
    servers = await get_reseller_servers(session, user.id)
    # رکوردهای حذف‌شده‌ی قبلی برای هشدار «سابقه‌ی قبلی» (مهاجرت نود = vpsid نو)
    old_deleted = [s for s in await get_reseller_servers(session, user.id, include_deleted=True)
                   if s.status == ServerStatus.DELETED]
    old_names = {(s.hostname or s.name or "").strip().lower() for s in old_deleted}
    # پنجره‌ی backfill نباید دوره‌ای را بشمارد که رکورد قبلیِ همین VM (همین
    # vpsid — چرخه‌ی غیرفعال/فعال یا two-strike و بازگشت) قبلاً ساعتی/گذشته بیل
    # شده: last_billed_at رکورد قبلی هم بیلینگ ساعتی و هم backfill قبلی را
    # پوشش می‌دهد (backfill قبلی بازه‌ی «ساخت تا ثبتِ» همان رکورد بود که
    # همیشه ≤ last_billed_at است).
    prev_billed_until: dict[str, datetime] = {}
    for s in old_deleted:
        vid = str(s.provider_server_id or "")
        if not vid:
            continue
        t = s.last_billed_at or s.created_at
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if vid not in prev_billed_until or t > prev_billed_until[vid]:
            prev_billed_until[vid] = t

    for s in servers:
        ex = s.extra_data or {}
        if ex.get("backfill_billed_at"):
            continue
        created_iso = ex.get("panel_created_at")
        registered_at = s.created_at
        if registered_at.tzinfo is None:
            registered_at = registered_at.replace(tzinfo=timezone.utc)

        note = ""
        selectable = True
        hours = 0
        rate = 0.0

        if not created_iso:
            selectable, note = False, "تاریخ نامشخص"
        elif not ex.get("plan_id"):
            selectable, note = False, "بدون پلن"
        else:
            try:
                created_dt = datetime.fromisoformat(created_iso)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                # کلمپ به دوره‌ی بیل‌شده‌ی رکورد قبلیِ همین vpsid — ضد کسر دوباره‌ی
                # ماه‌هایی که قبلاً (ساعتی یا backfill) پرداخت شده‌اند
                prev = prev_billed_until.get(str(s.provider_server_id or ""))
                if prev is not None and prev > created_dt:
                    created_dt = prev
                hours = int((registered_at - created_dt).total_seconds() // 3600)
            except (TypeError, ValueError):
                selectable, note = False, "تاریخ نامشخص"
            if selectable and hours <= 0:
                continue  # گذشته‌ای برای محاسبه ندارد
            if selectable:
                rate = await reseller_hourly_toman(session, user, s)
                if rate <= 0:
                    selectable, note = False, "نرخ نامشخص"
        if selectable and (s.hostname or s.name or "").strip().lower() in old_names:
            note = "سابقه قبلی"

        rows.append({
            "server_id": s.id,
            "name": s.name,
            "created_at": created_iso,
            "hours": hours,
            "rate_toman": rate,
            "amount_toman": hours * rate,
            "selectable": selectable,
            "note": note,
        })
    return rows
