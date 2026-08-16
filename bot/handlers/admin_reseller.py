"""پنل ادمین — بخش «ریسلر» هر کاربر (فقط ویرچولایزور).

فعال‌سازی/ویرایش کانفیگ ریسلر (اکانت + ایمیل پنل + درصد تخفیف)، نمای
جدول‌بندی‌شده‌ی سرورهای ثبت‌شده، سینک فوری، و ابزار «محاسبه از تاریخ ساخت»
با پیش‌نمایش و انتخاب per-VM. ثبت‌نام ریسلر آزاد نیست — فقط از همین‌جا.
"""
from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, text as _sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import ProviderAccount, ProviderType, ServerStatus, User
from bot.handlers.admin_users import AdminFilter, _safe_edit
from bot.providers import get_provider
from bot.services import reseller as rs
from bot.services.billing import BillingService
from bot.services.log_service import LogService

logger = logging.getLogger(__name__)
router = Router(name="admin_reseller")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# ایزوله‌ی جهت برای متن لاتین/عدد داخل خط فارسی: RLM در ابتدای خط +
# LTR-isolate دور بخش لاتین (الگوی سراسری ربات)
_RLM = "‏"


def _iso(s) -> str:
    return f"⁦{s}⁩"


def _cap_msg(text: str, limit: int = 3800) -> str:
    """سقف طول پیام تلگرام (۴۰۹۶) — لیست بلند نباید ارسال را بشکند."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut + "\n\n<i>… فهرست طولانی بود و بریده شد.</i>"


def _fmt_dt(val) -> str:
    if not val:
        return "—"
    try:
        dt = datetime.fromisoformat(str(val)) if isinstance(val, str) else val
        return dt.strftime("%Y/%m/%d %H:%M")
    except (TypeError, ValueError):
        return "—"


class ResellerFSM(StatesGroup):
    email = State()
    discount = State()


def _cancel_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="انصراف", callback_data=f"admin:ursl:{user_id}")]
    ])


async def _conflicting_reseller(session: AsyncSession, exclude_user_id: int,
                                account_id: int, email: str, uid: str = "") -> User | None:
    """ریسلر فعالِ دیگری با همین ایمیل/uid روی همین اکانت؟ (ضد دابل‌بیل حیاتی)"""
    rows = (await session.execute(
        select(User).where(User.extra_data.isnot(None))
    )).scalars().all()
    for u in rows:
        if u.id == exclude_user_id:
            continue
        c = rs.get_reseller_cfg(u)
        if not c.get("active"):
            continue
        try:
            if int(c.get("account_id") or 0) != int(account_id):
                continue
        except (TypeError, ValueError):
            continue
        if str(c.get("email") or "").lower() == email.lower():
            return u
        if uid and str(c.get("uid") or "") == str(uid):
            return u
    return None


# ── نمای اصلی ────────────────────────────────────────────────────────────────

async def _show_reseller_view(msg, session: AsyncSession, user: User, edit: bool = True):
    cfg = rs.get_reseller_cfg(user)
    if not cfg:
        text = (
            f"<b>ریسلر — کاربر #{user.id}</b>\n\n"
            "برای این کاربر برنامه ریسلر تنظیم نشده است.\n\n"
            "ریسلر با API ادمینِ خودش VM را مستقیم روی پنل ویرچولایزور می‌سازد "
            "(همه زیر یک ایمیل مشخص). ربات VMها را ثبت می‌کند و ساعتی با تخفیف "
            "از کیف پول همین کاربر کسر می‌کند. هیچ کنترلی روی سرورها به کاربر "
            "داده نمی‌شود و ترافیک محاسبه نمی‌شود."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="فعال‌سازی ریسلر",
                                  callback_data=f"admin:ursl_setup:{user.id}")],
            [InlineKeyboardButton(text="بازگشت", callback_data=f"admin:user:{user.id}")],
        ])
    else:
        account = None
        try:
            account = await session.get(ProviderAccount, int(cfg.get("account_id") or 0))
        except (TypeError, ValueError):
            pass
        servers = await rs.get_reseller_servers(session, user.id)
        unmatched = sum(1 for s in servers if (s.extra_data or {}).get("plan_unmatched"))
        rate_sum = 0.0
        for s in servers:
            rate_sum += await rs.reseller_hourly_toman(session, user, s)
        backfill = sum(float(i.get("remaining") or 0) for i in rs.get_backfill_debt(user))
        active = bool(cfg.get("active"))
        status = "✅ فعال" if active else "🚫 غیرفعال"
        lines = [
            f"<b>ریسلر — کاربر #{user.id}</b>",
            "",
            f"وضعیت: {status}",
            f"اکانت: {account.name if account else '—'}",
            f"{_RLM}ایمیل پنل: <code>{_iso(_html.escape(str(cfg.get('email') or '—')))}</code>",
            f"{_RLM}شناسه پنل (uid): {_iso(cfg.get('uid') or '—')}",
            f"تخفیف: {rs.reseller_discount_percent(user):g}٪",
            "",
            f"سرورهای ثبت‌شده: {len(servers)}"
            + (f" (⚠️ {unmatched} بدون پلن)" if unmatched else ""),
            f"جمع نرخ ساعتی با تخفیف: {rate_sum:,.0f} تومان/ساعت",
            f"موجودی کاربر: {user.balance:,.0f} تومان",
        ]
        if backfill >= 1:
            lines.append(f"بدهی «محاسبه گذشته» (وصول خودکار): {backfill:,.0f} تومان")
        if cfg.get("debt_since"):
            lines.append(f"⚠️ بدهی ساعتی از: {_fmt_dt(cfg.get('debt_since'))}")
        text = "\n".join(lines)

        builder = InlineKeyboardBuilder()
        builder.button(text="لیست سرورها", callback_data=f"admin:ursl_srv:{user.id}")
        builder.button(text="سینک فوری", callback_data=f"admin:ursl_sync:{user.id}")
        builder.button(text="محاسبه از تاریخ ساخت", callback_data=f"admin:ursl_bf:{user.id}")
        builder.button(text="ویرایش تخفیف", callback_data=f"admin:ursl_disc:{user.id}")
        builder.button(text="ویرایش ایمیل", callback_data=f"admin:ursl_email:{user.id}")
        builder.button(text="غیرفعال‌سازی" if active else "فعال‌سازی مجدد",
                       callback_data=f"admin:ursl_toggle:{user.id}")
        builder.button(text="بازگشت", callback_data=f"admin:user:{user.id}")
        builder.adjust(2)
        kb = builder.as_markup()

    if edit:
        await _safe_edit(msg, _cap_msg(text), kb)
    else:
        await msg.answer(_cap_msg(text), parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("admin:ursl:"))
async def cb_reseller_view(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    await cb.answer()
    # بازگشت از هر فلوی نیمه‌کاره: وضعیت FSM پاک شود تا پیام بعدی ادمین
    # به‌اشتباه ایمیل/تخفیف تلقی نشود
    await state.clear()
    user = await session.get(User, int(cb.data.split(":")[2]))
    if not user:
        await cb.answer("کاربر یافت نشد.", show_alert=True)
        return
    await _show_reseller_view(cb.message, session, user)


# ── فعال‌سازی (انتخاب اکانت ← ایمیل ← تخفیف) ─────────────────────────────────

@router.callback_query(F.data.startswith("admin:ursl_setup:"))
async def cb_reseller_setup(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    accounts = (await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.VIRTUALIZOR,
            ProviderAccount.is_active == True,  # noqa: E712
        )
    )).scalars().all()
    if not accounts:
        await cb.answer("هیچ اکانت ویرچولایزور فعالی وجود ندارد.", show_alert=True)
        return
    await cb.answer()
    if len(accounts) == 1:
        await _ask_email(cb.message, state, user_id, accounts[0].id, mode="setup")
        return
    builder = InlineKeyboardBuilder()
    for a in accounts:
        builder.button(text=a.name, callback_data=f"admin:ursl_acct:{user_id}:{a.id}")
    builder.button(text="انصراف", callback_data=f"admin:ursl:{user_id}")
    builder.adjust(1)
    await _safe_edit(cb.message,
                     "<b>فعال‌سازی ریسلر</b>\n\nاکانت ویرچولایزور را انتخاب کنید:",
                     builder.as_markup())


@router.callback_query(F.data.startswith("admin:ursl_acct:"))
async def cb_reseller_account(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    await cb.answer()
    await _ask_email(cb.message, state, int(parts[2]), int(parts[3]), mode="setup")


async def _ask_email(msg, state: FSMContext, user_id: int, account_id: int, mode: str):
    await state.set_state(ResellerFSM.email)
    await state.update_data(target_user_id=user_id, account_id=account_id, mode=mode)
    await _safe_edit(
        msg,
        "<b>ایمیل ویرچولایزور ریسلر</b>\n\n"
        "ایمیلِ کاربر پنل که VMهای ریسلر زیر آن ساخته می‌شوند را بفرستید.\n"
        "ایمیل باید از قبل در پنل وجود داشته باشد (به‌صورت زنده بررسی می‌شود).",
        _cancel_kb(user_id),
    )


@router.callback_query(F.data.startswith("admin:ursl_email:"))
async def cb_reseller_edit_email(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    user = await session.get(User, user_id)
    if not user or not rs.get_reseller_cfg(user):
        await cb.answer("کانفیگ ریسلر یافت نشد.", show_alert=True)
        return
    await cb.answer()
    account_id = int(rs.get_reseller_cfg(user).get("account_id") or 0)
    await state.set_state(ResellerFSM.email)
    await state.update_data(target_user_id=user_id, account_id=account_id, mode="email")
    await _safe_edit(
        cb.message,
        "<b>ویرایش ایمیل ریسلر</b>\n\n"
        "ایمیل جدید را بفرستید (باید در پنل موجود باشد).\n\n"
        "⚠️ با تغییر ایمیل، VMهای ایمیل قبلی در سینک‌های بعدی به‌صورت خودکار از "
        "ربات حذف (و بیلینگ‌شان قطع) می‌شوند و VMهای ایمیل جدید ثبت می‌شوند.",
        _cancel_kb(user_id),
    )


@router.message(ResellerFSM.email, F.text)
async def msg_reseller_email(message: Message, session: AsyncSession, state: FSMContext):
    data = await state.get_data()
    user_id = int(data.get("target_user_id") or 0)
    account_id = int(data.get("account_id") or 0)
    mode = data.get("mode") or "setup"
    email = (message.text or "").strip()

    if not _EMAIL_RE.match(email):
        await message.answer("فرمت ایمیل معتبر نیست — دوباره بفرستید.",
                             reply_markup=_cancel_kb(user_id))
        return

    user = await session.get(User, user_id)
    account = await session.get(ProviderAccount, account_id)
    if not user or not account:
        await state.clear()
        await message.answer("کاربر یا اکانت یافت نشد.")
        return

    # بررسی زنده: ایمیل باید در پنل موجود باشد (خطای پنل ≠ یافت‌نشدن)
    provider = get_provider(account)
    try:
        panel_users = await provider.list_users()
    except Exception as e:
        await message.answer(
            f"خطا در اتصال به پنل:\n<code>{_html.escape(str(e)[:300])}</code>\n\n"
            "دوباره تلاش کنید یا انصراف دهید.",
            parse_mode="HTML", reply_markup=_cancel_kb(user_id))
        return
    uid = next((str(u["uid"]) for u in panel_users
                if str(u.get("email") or "").lower() == email.lower()), "")
    if not uid:
        await message.answer(
            "کاربری با این ایمیل در پنل یافت نشد. اول کاربر را در پنل بسازید، "
            "بعد دوباره ایمیل را بفرستید.",
            reply_markup=_cancel_kb(user_id))
        return

    clash = await _conflicting_reseller(session, user_id, account_id, email, uid)
    if clash:
        await message.answer(
            f"⛔ این ایمیل/کاربر پنل قبلاً برای ریسلر دیگری (کاربر #{clash.id}) "
            "فعال است — دو ریسلر با یک ایمیل یعنی دوبار بیل شدن همان VMها.",
            reply_markup=_cancel_kb(user_id))
        return

    # هشدار نمایشی: ایمیل متعلق به کاربر دیگری از ربات
    other = (await session.execute(
        select(User).where(User.email.ilike(email), User.id != user_id)
    )).scalars().first()
    warn_line = (f"\n\n⚠️ توجه: این ایمیل، ایمیلِ ثبت‌شده‌ی کاربر دیگری از ربات "
                 f"(#{other.id}) هم هست." if other else "")

    if mode == "setup":
        await state.set_state(ResellerFSM.discount)
        # فقط کلیدهای در حال ویرایش پارک می‌شوند نه کل cfg — snapshot کهنه‌ی کامل
        # موقع ذخیره، بدهی/وضعیت‌هایی را که هم‌زمان تغییر کرده‌اند پاک می‌کرد
        await state.update_data(pending_cfg={"account_id": account_id,
                                             "email": email, "uid": uid})
        await message.answer(
            f"{_RLM}✅ کاربر پنل پیدا شد (uid: {_iso(uid)}).{warn_line}\n\n"
            "حالا <b>درصد تخفیف</b> ریسلر را بفرستید (عدد ۰ تا ۹۹):",
            parse_mode="HTML", reply_markup=_cancel_kb(user_id))
        return

    # mode == "email" — فقط ایمیل عوض می‌شود؛ merge زیر قفل روی cfg «تازه» تا
    # backfill_debt/debt_*/uid که هم‌زمان توسط سینک/بیلینگ نوشته می‌شوند حفظ شوند
    await session.execute(_sql_text("SET LOCAL statement_timeout = '15s'"))
    user = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not user or not rs.get_reseller_cfg(user):
        await state.clear()
        await message.answer("کانفیگ ریسلر یافت نشد.")
        return
    cfg = rs.get_reseller_cfg(user)
    cfg["account_id"] = account_id
    cfg["email"] = email
    cfg["uid"] = uid
    rs.set_reseller_cfg(user, cfg)
    # قفل ردیف کاربر قبل از ارسال‌های تلگرامی آزاد شود
    await session.commit()
    await state.clear()
    log = LogService(message.bot, session)
    await log.log_reseller_config(
        user, f"ایمیل ریسلر به {email} (uid {uid}) تغییر کرد.",
        admin_tg_id=message.from_user.id)
    await message.answer(f"{_RLM}✅ ایمیل ریسلر به‌روز شد (uid: {_iso(uid)}).{warn_line}",
                         parse_mode="HTML")
    await _show_reseller_view(message, session, user, edit=False)


@router.callback_query(F.data.startswith("admin:ursl_disc:"))
async def cb_reseller_edit_discount(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    user = await session.get(User, user_id)
    if not user or not rs.get_reseller_cfg(user):
        await cb.answer("کانفیگ ریسلر یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await state.set_state(ResellerFSM.discount)
    await state.update_data(target_user_id=user_id, mode="disc")
    await _safe_edit(
        cb.message,
        f"<b>ویرایش تخفیف ریسلر</b>\n\n"
        f"تخفیف فعلی: {rs.reseller_discount_percent(user):g}٪\n"
        "درصد جدید را بفرستید (عدد ۰ تا ۹۹). از کسر ساعتیِ بعدی اعمال می‌شود.",
        _cancel_kb(user_id),
    )


@router.message(ResellerFSM.discount, F.text.regexp(r"^\d+(\.\d+)?$"))
async def msg_reseller_discount(message: Message, session: AsyncSession, state: FSMContext):
    data = await state.get_data()
    user_id = int(data.get("target_user_id") or 0)
    mode = data.get("mode") or "disc"
    value = float(message.text)
    if not (0 <= value <= 99):
        await message.answer("درصد باید بین ۰ تا ۹۹ باشد — دوباره بفرستید.",
                             reply_markup=_cancel_kb(user_id))
        return
    await state.clear()
    # نوشتنِ cfg همیشه زیر قفل و روی نسخه‌ی تازه (merge) — ضد بازنویسی snapshot کهنه
    await session.execute(_sql_text("SET LOCAL statement_timeout = '15s'"))
    user = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not user:
        await message.answer("کاربر یافت نشد.")
        return

    if mode == "setup":
        pend = dict(data.get("pending_cfg") or {})
        if not pend.get("email"):
            await session.rollback()
            await message.answer("فلو ناقص شد — دوباره از «فعال‌سازی ریسلر» شروع کنید.")
            return
        # بازچک تداخل در لحظه‌ی فعال‌سازی (بین مرحله‌ی ایمیل و تخفیف ممکن است
        # ریسلر دیگری با همین ایمیل فعال شده باشد)
        clash = await _conflicting_reseller(
            session, user_id, int(pend.get("account_id") or 0),
            str(pend.get("email") or ""), str(pend.get("uid") or ""))
        if clash:
            await session.rollback()
            await message.answer(
                f"⛔ این ایمیل/کاربر پنل هم‌اکنون برای ریسلر دیگری (کاربر "
                f"#{clash.id}) فعال است — فعال‌سازی انجام نشد.")
            return
        cfg = rs.get_reseller_cfg(user)
        for k in ("account_id", "email", "uid"):
            if pend.get(k) is not None:
                cfg[k] = pend[k]
        cfg["discount_percent"] = value
        cfg["active"] = True
        cfg["activated_at"] = datetime.now(timezone.utc).isoformat()
        rs.set_reseller_cfg(user, cfg)
        log_text = f"ریسلر فعال شد — ایمیل {cfg.get('email')} | تخفیف {value:g}٪"
        ok_msg = (
            "✅ ریسلر فعال شد.\n\n"
            "VMهای موجود و جدیدِ این ایمیل در سینک بعدی (حداکثر ۱۰ دقیقه، یا با "
            "دکمه «سینک فوری») ثبت می‌شوند و بیلینگ از لحظه ثبت شروع می‌شود.\n"
            "برای VMهای از-قبل-موجود، محاسبه‌ی گذشته فقط با ابزار «محاسبه از "
            "تاریخ ساخت» و تأیید شماست.")
    else:
        cfg = rs.get_reseller_cfg(user)
        if not cfg:
            await session.rollback()
            await message.answer("کانفیگ ریسلر یافت نشد.")
            return
        old = rs.reseller_discount_percent(user)
        cfg["discount_percent"] = value
        rs.set_reseller_cfg(user, cfg)
        log_text = f"تخفیف ریسلر از {old:g}٪ به {value:g}٪ تغییر کرد."
        ok_msg = f"✅ تخفیف ریسلر: {value:g}٪"

    # قفل ردیف کاربر قبل از ارسال‌های تلگرامی آزاد شود
    await session.commit()
    await LogService(message.bot, session).log_reseller_config(
        user, log_text, admin_tg_id=message.from_user.id)
    await message.answer(ok_msg)
    await _show_reseller_view(message, session, user, edit=False)


# ── فعال/غیرفعال ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:ursl_toggle:"))
async def cb_reseller_toggle(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await state.clear()  # فلوی نیمه‌کاره‌ی ایمیل/تخفیف خنثی شود
    user = await session.get(User, user_id)
    if not user or not rs.get_reseller_cfg(user):
        await cb.answer("کانفیگ ریسلر یافت نشد.", show_alert=True)
        return
    await cb.answer()
    active = bool(rs.get_reseller_cfg(user).get("active"))
    if active:
        n = len(await rs.get_reseller_servers(session, user_id))
        text = (
            "<b>غیرفعال‌سازی ریسلر</b>\n\n"
            f"- {n} رکورد سرور ریسلر در ربات DELETED می‌شود (بیلینگ فوراً قطع).\n"
            "- بدهی ساعتی جبران‌نشده و بدهی «محاسبه گذشته» پاک می‌شوند.\n"
            "- خود VMها در پنل دست نمی‌خورند.\n\nادامه می‌دهید؟"
        )
    else:
        text = (
            "<b>فعال‌سازی مجدد ریسلر</b>\n\n"
            "VMهای ایمیل ریسلر در سینک بعدی دوباره ثبت می‌شوند و بیلینگ از لحظه "
            "ثبت شروع می‌شود.\n\nادامه می‌دهید؟"
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="بله، انجام بده",
                             callback_data=f"admin:ursl_toggle_do:{user_id}"),
        InlineKeyboardButton(text="انصراف", callback_data=f"admin:ursl:{user_id}"),
    ]])
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("admin:ursl_toggle_do:"))
async def cb_reseller_toggle_do(cb: CallbackQuery, session: AsyncSession):
    user_id = int(cb.data.split(":")[2])
    await cb.answer()
    await session.execute(_sql_text("SET LOCAL statement_timeout = '15s'"))
    user = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not user or not rs.get_reseller_cfg(user):
        await _safe_edit(cb.message, "کانفیگ ریسلر یافت نشد.", _cancel_kb(user_id))
        return
    cfg = rs.get_reseller_cfg(user)
    if cfg.get("active"):
        cfg["active"] = False
        for k in ("debt_since", "debt_warned_at", "debt_admin_warned_at", "backfill_debt"):
            cfg.pop(k, None)
        rs.set_reseller_cfg(user, cfg)
        deleted = 0
        for s in await rs.get_reseller_servers(session, user_id):
            # فقط رکورد — هرگز provider.delete_server (VM مال ریسلر است)
            s.status = ServerStatus.DELETED
            deleted += 1
        log_text = f"ریسلر غیرفعال شد — {deleted} رکورد سرور DELETED شد."
    else:
        # فعال‌سازی مجدد هم مثل فعال‌سازی اول باید چک تداخل بدهد — وگرنه با
        # چرخه‌ی «غیرفعال A ← ثبت B با همان ایمیل ← فعال‌سازی مجدد A» دو ریسلر
        # فعال با یک ایمیل/uid روی یک اکانت می‌مانند
        clash = await _conflicting_reseller(
            session, user_id, int(cfg.get("account_id") or 0),
            str(cfg.get("email") or ""), str(cfg.get("uid") or ""))
        if clash:
            await session.rollback()
            await _safe_edit(
                cb.message,
                f"⛔ فعال‌سازی ممکن نیست — این ایمیل/کاربر پنل اکنون برای ریسلر "
                f"دیگری (کاربر #{clash.id}) فعال است. اول ریسلر مقابل را "
                "غیرفعال کنید یا ایمیلش را عوض کنید.",
                _cancel_kb(user_id))
            return
        cfg["active"] = True
        rs.set_reseller_cfg(user, cfg)
        log_text = "ریسلر دوباره فعال شد."
    await session.flush()
    # قفل ردیف کاربر قبل از ارسال‌های تلگرامی آزاد شود
    await session.commit()
    await LogService(cb.bot, session).log_reseller_config(
        user, log_text, admin_tg_id=cb.from_user.id)
    await _show_reseller_view(cb.message, session, user)


# ── لیست سرورها ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:ursl_srv:"))
async def cb_reseller_servers(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await state.clear()  # فلوی نیمه‌کاره‌ی ایمیل/تخفیف خنثی شود
    user = await session.get(User, user_id)
    if not user:
        await cb.answer("کاربر یافت نشد.", show_alert=True)
        return
    await cb.answer()
    servers = await rs.get_reseller_servers(session, user_id)
    if not servers:
        await _safe_edit(cb.message,
                         "<b>سرورهای ریسلر</b>\n\nهنوز سروری ثبت نشده است.",
                         _cancel_kb(user_id))
        return
    lines = [f"<b>سرورهای ریسلر — کاربر #{user_id}</b>", ""]
    total = 0.0
    for s in servers:
        ex = s.extra_data or {}
        rate = await rs.reseller_hourly_toman(session, user, s)
        total += rate
        if ex.get("plan_unmatched"):
            status = "⚠️ بدون پلن"
        elif ex.get("missing_since"):
            status = "🚫 غایب از پنل"
        else:
            status = "✅ فعال"
        spec = _iso(f"{s.cpu}C/{s.ram}M/{s.disk}G")
        lines.append(
            f"{_RLM}▫️ {_iso(_html.escape(s.name or '—'))} │ "
            f"{_iso('#' + str(s.provider_server_id or '—'))} │ {spec} │ "
            f"{rate:,.0f} ت/س │ {status}"
        )
        lines.append(
            f"{_RLM}   شروع بیلینگ: {_fmt_dt(s.created_at)} │ "
            f"ساخت در پنل: {_fmt_dt(ex.get('panel_created_at'))}"
        )
    lines += ["", f"جمع نرخ ساعتی (با تخفیف): <b>{total:,.0f} تومان/ساعت</b>"]
    await _safe_edit(cb.message, _cap_msg("\n".join(lines)), _cancel_kb(user_id))


# ── سینک فوری ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:ursl_sync:"))
async def cb_reseller_sync(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await state.clear()  # فلوی نیمه‌کاره‌ی ایمیل/تخفیف خنثی شود
    await cb.answer("در حال سینک با پنل…")
    user = await session.get(User, user_id)
    if not user:
        return
    cfg = rs.get_reseller_cfg(user)
    if not cfg.get("active"):
        await _safe_edit(cb.message, "<b>سینک ریسلر</b>\n\nریسلر فعال نیست.",
                         _cancel_kb(user_id))
        return
    account = await session.get(ProviderAccount, int(cfg.get("account_id") or 0))
    if not account or not account.is_active:
        await _safe_edit(cb.message,
                         "<b>سینک ریسلر</b>\n\nاکانت ویرچولایزور در دسترس نیست.",
                         _cancel_kb(user_id))
        return
    provider = get_provider(account)
    email0 = str(cfg.get("email") or "")
    try:
        # فراخوانی‌های کُند پنل قبل از هر قفلی
        panel_rows = await provider.list_all_vps()
        uid = str(cfg.get("uid") or "")
        if not uid:
            found = await provider.find_user_by_email(email0)
            if not found:
                raise RuntimeError("کاربر پنل با ایمیل ریسلر پیدا نشد.")
            uid = str(found)
        # قفل ردیف کاربر داخل سرویس (کشِ uid هم زیر همان قفل نوشته می‌شود)
        summary = await rs.sync_user_reseller_servers(
            session, user, account, panel_rows, uid, expected_email=email0)
        if summary is None:
            raise RuntimeError("کانفیگ ریسلر هم‌زمان تغییر کرد — دوباره تلاش کنید.")
        collected = await rs.collect_backfill_debt(session, user)
        # قفل با commit آزاد شود، بعد ارسال‌های تلگرامی (وگرنه بیلینگ دقیقه‌ای
        # پشت این کاربر می‌ماند و پیامِ قبل از commit هم قابل دروغ‌شدن است)
        await session.commit()
    except Exception as e:
        try:
            await session.rollback()
        except Exception:
            pass
        await _safe_edit(cb.message,
                         f"<b>سینک ریسلر</b>\n\n❌ خطا:\n"
                         f"<code>{_html.escape(str(e)[:500])}</code>",
                         _cancel_kb(user_id))
        return
    if any(summary.values()):
        await LogService(cb.bot, session).log_reseller_sync(user, account.name, summary)

    def _n(key):
        return len(summary.get(key) or [])
    text = (
        "<b>سینک ریسلر انجام شد</b>\n\n"
        f"ثبت جدید: {_n('registered')}\n"
        f"حذف‌شده از پنل: {_n('deleted')}\n"
        f"پلن مچ‌شده: {_n('rematched')}\n"
        f"بدون پلن: {_n('unmatched')}"
    )
    if collected:
        text += f"\nوصول بدهی گذشته: {sum(p for _, p in collected):,.0f} تومان"
    await _safe_edit(cb.message, text, _cancel_kb(user_id))


# ── محاسبه از تاریخ ساخت ─────────────────────────────────────────────────────

def _bf_text_and_kb(user_id: int, rows: list[dict], selected: set[int]):
    lines = ["<b>محاسبه از تاریخ ساخت</b>", "",
             "ساعت‌های «تاریخ ساخت واقعی ← لحظه ثبت در ربات» برای VMهای "
             "از-قبل-موجود. هر VM فقط یک بار قابل محاسبه است.", ""]
    total_sel = 0.0
    for r in rows:
        if r["selectable"]:
            mark = "✅" if r["server_id"] in selected else "⬜"
        else:
            mark = "⛔"
        line = (
            f"{_RLM}{mark} {_iso(_html.escape(r['name'] or '—'))} │ "
            f"ساخت: {_fmt_dt(r['created_at'])} │ "
        )
        if r["selectable"]:
            line += (f"{r['hours']} ساعت × {r['rate_toman']:,.0f} = "
                     f"<b>{r['amount_toman']:,.0f} ت</b>")
            if r["server_id"] in selected:
                total_sel += r["amount_toman"]
        else:
            line += r["note"] or "—"
        if r["selectable"] and r["note"]:
            line += f" (⚠️ {r['note']})"
        lines.append(line)
    lines += ["", f"جمع انتخاب‌شده: <b>{total_sel:,.0f} تومان</b>",
              "",
              "اگر موجودی کافی نباشد: تا سقف موجودی کسر و باقی به‌عنوان بدهی ثبت "
              "می‌شود که بعد از هر شارژ به‌صورت خودکار وصول خواهد شد."]
    builder = InlineKeyboardBuilder()
    for r in rows:
        if not r["selectable"]:
            continue
        mark = "✅" if r["server_id"] in selected else "⬜"
        builder.button(text=f"{mark} {r['name'][:24]}",
                       callback_data=f"admin:ursl_bft:{user_id}:{r['server_id']}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(
        text=f"کسر موارد انتخاب‌شده ({total_sel:,.0f} ت)",
        callback_data=f"admin:ursl_bfdo:{user_id}"))
    builder.row(InlineKeyboardButton(text="بازگشت",
                                     callback_data=f"admin:ursl:{user_id}"))
    return _cap_msg("\n".join(lines)), builder.as_markup()


@router.callback_query(F.data.startswith("admin:ursl_bf:"))
async def cb_reseller_backfill(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()  # فلوی نیمه‌کاره‌ی ایمیل/تخفیف خنثی شود (bf_sel پایین‌تر ست می‌شود)
    user = await session.get(User, user_id)
    if not user:
        return
    rows = await rs.compute_backfill_rows(session, user)
    if not rows:
        await _safe_edit(cb.message,
                         "<b>محاسبه از تاریخ ساخت</b>\n\n"
                         "VM واجد شرایطی وجود ندارد (یا همه قبلاً محاسبه شده‌اند).",
                         _cancel_kb(user_id))
        return
    # ردیف‌های دارای یادداشت (مثل «سابقه قبلی») پیش‌فرض انتخاب «نمی‌شوند» —
    # کسرِ خطرناک باید opt-in صریح ادمین باشد، نه پیش‌فرضِ یک کلیک
    selected = {r["server_id"] for r in rows if r["selectable"] and not r["note"]}
    await state.update_data(bf_sel=sorted(selected))
    text, kb = _bf_text_and_kb(user_id, rows, selected)
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("admin:ursl_bft:"))
async def cb_reseller_backfill_toggle(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    parts = cb.data.split(":")
    user_id, server_id = int(parts[2]), int(parts[3])
    await cb.answer()
    user = await session.get(User, user_id)
    if not user:
        return
    rows = await rs.compute_backfill_rows(session, user)
    data = await state.get_data()
    selected = set(data.get("bf_sel") or [])
    valid_ids = {r["server_id"] for r in rows if r["selectable"]}
    selected &= valid_ids
    if server_id in valid_ids:
        if server_id in selected:
            selected.discard(server_id)
        else:
            selected.add(server_id)
    await state.update_data(bf_sel=sorted(selected))
    text, kb = _bf_text_and_kb(user_id, rows, selected)
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("admin:ursl_bfdo:"))
async def cb_reseller_backfill_do(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await cb.answer()
    data = await state.get_data()
    selected = set(data.get("bf_sel") or [])
    if not selected:
        await _safe_edit(cb.message,
                         "<b>محاسبه از تاریخ ساخت</b>\n\nهیچ موردی انتخاب نشده است.",
                         _cancel_kb(user_id))
        return

    # ضد دابل‌کلیک: قفل ردیف کاربر + بازمحاسبه‌ی موارد واجد شرایط زیر قفل —
    # VMهای قبلاً محاسبه‌شده (backfill_billed_at) اینجا دیگر برنمی‌گردند
    await session.execute(_sql_text("SET LOCAL statement_timeout = '20s'"))
    user = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not user:
        await cb.answer("کاربر یافت نشد.", show_alert=True)
        return
    rows = [r for r in await rs.compute_backfill_rows(session, user)
            if r["selectable"] and r["server_id"] in selected]
    if not rows:
        await state.update_data(bf_sel=[])
        await _safe_edit(cb.message,
                         "<b>محاسبه از تاریخ ساخت</b>\n\n"
                         "موردی باقی نمانده — احتمالاً قبلاً محاسبه شده است.",
                         _cancel_kb(user_id))
        return

    billing = BillingService(session)
    now_iso = datetime.now(timezone.utc).isoformat()
    paid_total, debt_total = 0.0, 0.0
    debt_items = rs.get_backfill_debt(user)
    report_lines = []
    from bot.database.models import Server
    for r in rows:
        amount = float(r["amount_toman"])
        if amount < 1:
            continue
        pay = min(amount, float(user.balance))
        paid = 0.0
        if pay >= 1:
            ok = await billing.debit(
                user.id, pay, server_id=r["server_id"],
                description=f"محاسبه گذشته — {r['name']}")
            if ok:
                paid = pay
        debt = amount - paid
        paid_total += paid
        if debt >= 1:
            debt_total += debt
            debt_items.append({"server_id": r["server_id"],
                               "name": r["name"], "remaining": debt})
        srv = await session.get(Server, r["server_id"])
        if srv is not None:
            ex = dict(srv.extra_data or {})
            ex["backfill_billed_at"] = now_iso
            srv.extra_data = ex
        report_lines.append(
            f"{_RLM}▫️ {_iso(_html.escape(r['name'] or '—'))} │ {r['hours']} ساعت │ "
            f"کسر: {paid:,.0f} ت"
            + (f" │ بدهی: {debt:,.0f} ت" if debt >= 1 else "")
        )
    rs.set_backfill_debt(user, debt_items)
    await session.flush()
    await state.update_data(bf_sel=[])
    # کسر قطعی شود و قفل ردیف کاربر آزاد شود، بعد پیام‌ها (الگوی «کسر قطعی قبل
    # از عملیات طولانی» در خرید): پیام موفقیتِ قبل از commit با شکستِ commit به
    # «کسر شد»ِ دروغین تبدیل می‌شود و قفلِ نگه‌داشته حین I/O تلگرام، بیلینگ
    # دقیقه‌ایِ همه‌ی کاربران را پشت این ردیف معطل می‌کند.
    await session.commit()

    log = LogService(cb.bot, session)
    await log.log_reseller_backfill(user, report_lines, paid_total, debt_total,
                                    admin_tg_id=cb.from_user.id)
    try:
        note = (f"\nباقی‌مانده به‌عنوان بدهی ثبت شد و پس از شارژ خودکار کسر می‌شود: "
                f"{debt_total:,.0f} تومان" if debt_total >= 1 else "")
        await cb.bot.send_message(
            user.telegram_id,
            f"🧮 بابت محاسبه‌ی گذشته‌ی سرورهای ریسلری، مبلغ "
            f"{paid_total:,.0f} تومان از کیف پول شما کسر شد.{note}",
            parse_mode="HTML")
    except Exception:
        pass

    text = ("<b>محاسبه از تاریخ ساخت — انجام شد</b>\n\n"
            + "\n".join(report_lines)
            + f"\n\nکسرشده: <b>{paid_total:,.0f} تومان</b>")
    if debt_total >= 1:
        text += (f"\nثبت‌شده به‌عنوان بدهی (وصول خودکار پس از شارژ): "
                 f"<b>{debt_total:,.0f} تومان</b>")
    text += f"\nموجودی جدید کاربر: {user.balance:,.0f} تومان"
    await _safe_edit(cb.message, _cap_msg(text), _cancel_kb(user_id))
