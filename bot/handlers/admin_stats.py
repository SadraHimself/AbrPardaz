"""Admin stats, bot settings, and finance handlers."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Sticker
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import (
    BotSettings, DailyStat, DiscountCode, Server, ServerPlan,
    ServerStatus, Transaction, TransactionType, User, UserStatus,
)
from bot.keyboards.admin import (
    back_to_admin_kb, cancel_admin_kb, channels_kb, confirm_kb,
    finance_kb, np_gateway_kb, price_adj_categories_kb, settings_menu_kb, stats_kb,
)
from bot.services.billing import BillingService
from bot.utils.loading import edit_loading

router = Router(name="admin_stats")


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, user: User) -> bool:
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class SettingsFSM(StatesGroup):
    edit_value = State()
    channel_add = State()


class FinanceFSM(StatesGroup):
    bulk_credit_amount = State()
    bulk_credit_confirm = State()
    price_adj_percent = State()
    price_adj_confirm = State()


class StatsFSM(StatesGroup):
    range_start = State()
    range_end = State()


class LogGroupFSM(StatesGroup):
    waiting_group_id = State()


class NPSettingsFSM(StatesGroup):
    waiting_rate = State()
    waiting_webhook_url = State()


# ── Helper ────────────────────────────────────────────────────────────────────

async def _get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(BotSettings, key)
    return row.value if row and row.value is not None else default


async def _set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(BotSettings, key)
    if row:
        row.value = value
    else:
        session.add(BotSettings(key=key, value=value))
    await session.flush()


# ══════════════════════════════════════════════════════════════════════════════
#  STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        "📊 <b>آمار</b>\n\nنوع گزارش را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=stats_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "admin:stats_today")
async def cb_stats_today(cb: CallbackQuery, session: AsyncSession):
    await edit_loading(cb.message)
    await cb.answer()
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await _show_stats(cb, session, start, now, "امروز")


@router.callback_query(F.data == "admin:stats_month")
async def cb_stats_month(cb: CallbackQuery, session: AsyncSession):
    await edit_loading(cb.message)
    await cb.answer()
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await _show_stats(cb, session, start, now, "این ماه")


@router.callback_query(F.data == "admin:stats_range")
async def cb_stats_range_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(StatsFSM.range_start)
    await cb.message.edit_text(
        "📅 <b>بازه تاریخی</b>\n\nتاریخ شروع (YYYY-MM-DD):",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(StatsFSM.range_start)
async def msg_stats_range_start(message: Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        await state.update_data(range_start=dt.isoformat())
        await state.set_state(StatsFSM.range_end)
        await message.answer("تاریخ پایان (YYYY-MM-DD):", reply_markup=cancel_admin_kb())
    except ValueError:
        await message.answer("❌ فرمت نادرست. مثال: 2025-01-01")


@router.message(StatsFSM.range_end)
async def msg_stats_range_end(message: Message, state: FSMContext, session: AsyncSession):
    try:
        end = datetime.strptime(message.text.strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc, hour=23, minute=59, second=59
        )
    except ValueError:
        await message.answer("❌ فرمت نادرست. مثال: 2025-01-31")
        return
    data = await state.get_data()
    await state.clear()
    start = datetime.fromisoformat(data["range_start"])
    label = f"{start.strftime('%Y/%m/%d')} تا {end.strftime('%Y/%m/%d')}"

    # Build a fake CallbackQuery-like object — just send as message
    total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    new_users = (await session.execute(
        select(func.count(User.id)).where(User.created_at.between(start, end))
    )).scalar() or 0
    new_servers = (await session.execute(
        select(func.count(Server.id)).where(Server.created_at.between(start, end))
    )).scalar() or 0
    revenue = (await session.execute(
        select(func.sum(Transaction.amount)).where(
            Transaction.type == TransactionType.DEBIT,
            Transaction.created_at.between(start, end),
        )
    )).scalar() or 0
    active_srv = (await session.execute(
        select(func.count(Server.id)).where(Server.status == ServerStatus.ACTIVE)
    )).scalar() or 0
    total_wallet = (await session.execute(
        select(func.sum(User.balance)).where(User.status == UserStatus.ACTIVE)
    )).scalar() or 0

    await message.answer(
        f"📊 <b>آمار: {label}</b>\n\n"
        f"👥 <b>کاربران</b>\n"
        f"کاربران کل: <b>{total_users}</b>\n"
        f"کاربر جدید: <b>{new_users}</b>\n\n"
        f"🖥 <b>سرور‌ها</b>\n"
        f"سرور جدید: <b>{new_servers}</b>\n"
        f"سرور فعال: <b>{active_srv}</b>\n\n"
        f"💰 <b>مالی</b>\n"
        f"درآمد: <b>{revenue:,.0f} تومان</b>\n"
        f"موجودی کیف‌پول‌ها: <b>{total_wallet:,.0f} تومان</b>",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:stats"),
    )


async def _show_stats(cb: CallbackQuery, session: AsyncSession, start: datetime, end: datetime, label: str):
    total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    new_users = (await session.execute(
        select(func.count(User.id)).where(User.created_at.between(start, end))
    )).scalar() or 0
    new_servers = (await session.execute(
        select(func.count(Server.id)).where(Server.created_at.between(start, end))
    )).scalar() or 0
    revenue = (await session.execute(
        select(func.sum(Transaction.amount)).where(
            Transaction.type == TransactionType.DEBIT,
            Transaction.created_at.between(start, end),
        )
    )).scalar() or 0
    active_srv = (await session.execute(
        select(func.count(Server.id)).where(Server.status == ServerStatus.ACTIVE)
    )).scalar() or 0
    suspended_srv = (await session.execute(
        select(func.count(Server.id)).where(Server.status == ServerStatus.SUSPENDED)
    )).scalar() or 0
    total_wallet = (await session.execute(
        select(func.sum(User.balance)).where(User.status == UserStatus.ACTIVE)
    )).scalar() or 0
    active_disc = (await session.execute(
        select(func.count(DiscountCode.id)).where(DiscountCode.is_active == True)
    )).scalar() or 0

    await cb.message.edit_text(
        f"📊 <b>آمار — {label}</b>\n\n"
        f"👥 <b>کاربران</b>\n"
        f"کاربران کل: <b>{total_users}</b>\n"
        f"کاربر جدید: <b>{new_users}</b>\n\n"
        f"🖥 <b>سرور‌ها</b>\n"
        f"سرور جدید: <b>{new_servers}</b>\n"
        f"سرور فعال: <b>{active_srv}</b>\n"
        f"سرور ساسپند: <b>{suspended_srv}</b>\n\n"
        f"💰 <b>مالی</b>\n"
        f"درآمد: <b>{revenue:,.0f} تومان</b>\n"
        f"موجودی کیف‌پول‌ها: <b>{total_wallet:,.0f} تومان</b>\n"
        f"کد تخفیف فعال: <b>{active_disc}</b>",
        parse_mode="HTML",
        reply_markup=stats_kb(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  BOT SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

_SETTING_LABELS = {
    "welcome_text": "متن خوش‌آمدگویی\n<i>متغیرها: {name}، {balance}</i>",
    "welcome_sticker_id": "آیدی استیکر خوش‌آمدگویی\n<i>استیکر موردنظر را برای من ارسال کنید</i>",
    "support_text": "متن پشتیبانی",
    "support_id": "آیدی تلگرام پشتیبان\n<i>مثال: @support_user</i>",
    "website_url": "لینک سایت\n<i>مثال: https://example.ir</i>",
    "terms_text": "متن شرایط پذیرش (هنگام ثبت‌نام)\n<i>HTML مجاز است</i>",
    "rules_text": "متن قوانین (دکمه منوی اصلی)\n<i>HTML مجاز است</i>",
}


@router.callback_query(F.data == "admin:settings")
async def cb_admin_settings(cb: CallbackQuery):
    await cb.message.edit_text(
        "⚙️ <b>تنظیمات ربات</b>\n\nیک گزینه را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=settings_menu_kb(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:setting:"))
async def cb_setting_edit(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    key = cb.data[len("admin:setting:"):]
    current = await _get_setting(session, key)
    label = _SETTING_LABELS.get(key, key)
    await state.update_data(setting_key=key)
    await state.set_state(SettingsFSM.edit_value)
    preview = f"\n\n<b>مقدار فعلی:</b>\n<code>{current[:200]}</code>" if current else ""
    await cb.message.edit_text(
        f"✏️ <b>{label}</b>{preview}\n\nمقدار جدید را وارد کنید:",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(SettingsFSM.edit_value)
async def msg_setting_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    key = data["setting_key"]

    # Handle sticker file_id specially
    if key == "welcome_sticker_id" and message.sticker:
        value = message.sticker.file_id
    elif key == "welcome_sticker_id" and not message.text:
        await message.answer("❌ لطفاً یک استیکر ارسال کنید.")
        return
    else:
        value = message.text.strip() if message.text else ""

    await state.clear()
    await _set_setting(session, key, value)
    await message.answer(
        "✅ تنظیم ذخیره شد.",
        reply_markup=settings_menu_kb(),
    )


# ── Channel lock ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:channels")
async def cb_channels(cb: CallbackQuery, session: AsyncSession):
    raw = await _get_setting(session, "force_channels", "[]")
    try:
        channels = json.loads(raw)
    except Exception:
        channels = []
    await cb.message.edit_text(
        f"📢 <b>کانال‌های اجباری</b>\n\n"
        f"کاربران باید قبل از استفاده عضو این کانال‌ها باشند:\n"
        f"{'بدون کانال' if not channels else chr(10).join(channels)}",
        parse_mode="HTML",
        reply_markup=channels_kb(channels),
    )
    await cb.answer()


@router.callback_query(F.data == "admin:ch_add")
async def cb_ch_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsFSM.channel_add)
    await cb.message.edit_text(
        "📢 <b>افزودن کانال</b>\n\n"
        "آیدی کانال را وارد کنید:\n<i>مثال: @mychannel یا -1001234567890</i>",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(SettingsFSM.channel_add)
async def msg_ch_add(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    ch = message.text.strip()
    if not ch.startswith("@") and not ch.lstrip("-").isdigit():
        await message.answer("❌ آیدی نامعتبر. باید با @ شروع شود یا عدد باشد.")
        return
    raw = await _get_setting(session, "force_channels", "[]")
    try:
        channels = json.loads(raw)
    except Exception:
        channels = []
    if ch not in channels:
        channels.append(ch)
        await _set_setting(session, "force_channels", json.dumps(channels))
    await message.answer(f"✅ کانال {ch} اضافه شد.", reply_markup=back_to_admin_kb("admin:channels"))


@router.callback_query(F.data.startswith("admin:ch_del:"))
async def cb_ch_del(cb: CallbackQuery, session: AsyncSession):
    ch = cb.data[len("admin:ch_del:"):].replace("_", ":")
    # Restore colon for numeric IDs
    raw = await _get_setting(session, "force_channels", "[]")
    try:
        channels = json.loads(raw)
    except Exception:
        channels = []
    # Find and remove
    to_remove = None
    for c in channels:
        if c.replace(":", "_") == cb.data[len("admin:ch_del:"):]:
            to_remove = c
            break
    if to_remove:
        channels.remove(to_remove)
        await _set_setting(session, "force_channels", json.dumps(channels))
    await cb.answer("✅ کانال حذف شد.")
    await cb_channels(cb, session)


# ══════════════════════════════════════════════════════════════════════════════
#  FINANCE
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:finance")
async def cb_admin_finance(cb: CallbackQuery):
    await cb.message.edit_text(
        "💰 <b>بخش مالی</b>\n\nعملیات مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=finance_kb(),
    )
    await cb.answer()


# ── Bulk credit ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:finance_bulk_credit")
async def cb_bulk_credit_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(FinanceFSM.bulk_credit_amount)
    await cb.message.edit_text(
        "💰 <b>شارژ همه کاربران</b>\n\nمبلغ (تومان) برای شارژ همه کاربران فعال:",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(FinanceFSM.bulk_credit_amount, F.text.regexp(r"^\d+(\.\d+)?$"))
async def msg_bulk_credit_amount(message: Message, state: FSMContext, session: AsyncSession):
    amount = float(message.text)
    total_users = (await session.execute(
        select(func.count(User.id)).where(User.status == UserStatus.ACTIVE)
    )).scalar() or 0
    await state.update_data(bulk_amount=amount)
    await state.set_state(FinanceFSM.bulk_credit_confirm)
    await message.answer(
        f"⚠️ <b>تأیید شارژ همگانی</b>\n\n"
        f"مبلغ: {amount:,.0f} تومان\n"
        f"تعداد کاربران: {total_users}\n"
        f"جمع کل: {amount * total_users:,.0f} تومان\n\n"
        "آیا مطمئنید؟",
        parse_mode="HTML",
        reply_markup=confirm_kb("admin:bulk_credit_do", "admin:finance"),
    )


@router.callback_query(F.data == "admin:bulk_credit_do", FinanceFSM.bulk_credit_confirm)
async def cb_bulk_credit_do(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    amount = data["bulk_amount"]
    await cb.answer("⏳ در حال پردازش...")

    result = await session.execute(select(User).where(User.status == UserStatus.ACTIVE))
    users = list(result.scalars().all())
    billing = BillingService(session)
    count = 0
    for u in users:
        await billing.credit(u.id, amount, description="شارژ همگانی توسط ادمین")
        count += 1

    await cb.message.edit_text(
        f"✅ <b>شارژ همگانی انجام شد!</b>\n\n"
        f"{count} کاربر — {amount:,.0f} تومان هر نفر",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:finance"),
    )


# ── Group price adjustment ────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:finance_price_adj")
async def cb_price_adj_start(cb: CallbackQuery, session: AsyncSession):
    result = await session.execute(select(ServerPlan.category).distinct())
    categories = sorted({row[0] for row in result.all() if row[0]})
    await cb.message.edit_text(
        "📈 <b>تغییر قیمت محصولات</b>\n\nدسته‌بندی را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=price_adj_categories_kb(categories),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:price_cat:"))
async def cb_price_adj_category(cb: CallbackQuery, state: FSMContext):
    category = cb.data[len("admin:price_cat:"):]
    await state.update_data(price_category=category)
    await state.set_state(FinanceFSM.price_adj_percent)
    cat_label = "همه محصولات" if category == "__all__" else category
    await cb.message.edit_text(
        f"📈 <b>{cat_label}</b>\n\n"
        "درصد تغییر را وارد کنید:\n"
        "<i>مثبت = افزایش، منفی = کاهش\nمثال: 25 یا -10</i>",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(FinanceFSM.price_adj_percent, F.text.regexp(r"^-?\d+(\.\d+)?$"))
async def msg_price_adj_percent(message: Message, state: FSMContext, session: AsyncSession):
    pct = float(message.text)
    data = await state.get_data()
    category = data["price_category"]

    # Count affected plans
    query = select(func.count(ServerPlan.id)).where(ServerPlan.is_active == True)
    if category != "__all__":
        query = query.where(ServerPlan.category == category)
    count = (await session.execute(query)).scalar() or 0

    await state.update_data(price_pct=pct)
    await state.set_state(FinanceFSM.price_adj_confirm)
    cat_label = "همه محصولات" if category == "__all__" else category
    await message.answer(
        f"⚠️ <b>تأیید تغییر قیمت</b>\n\n"
        f"دسته: {cat_label}\n"
        f"تغییر: {'+'if pct > 0 else ''}{pct:.1f}%\n"
        f"تعداد محصولات: {count}\n\n"
        "آیا مطمئنید؟",
        parse_mode="HTML",
        reply_markup=confirm_kb("admin:price_adj_do", "admin:finance"),
    )


@router.callback_query(F.data == "admin:price_adj_do", FinanceFSM.price_adj_confirm)
async def cb_price_adj_do(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    pct = data["price_pct"]
    category = data["price_category"]
    multiplier = 1 + pct / 100

    query = select(ServerPlan).where(ServerPlan.is_active == True)
    if category != "__all__":
        query = query.where(ServerPlan.category == category)
    result = await session.execute(query)
    plans = list(result.scalars().all())

    count = 0
    for plan in plans:
        if plan.price_hourly:
            plan.price_hourly = round(plan.price_hourly * multiplier, 0)
        if plan.price_monthly:
            plan.price_monthly = round(plan.price_monthly * multiplier, 0)
        count += 1
    await session.flush()

    sign = "+" if pct > 0 else ""
    await cb.message.edit_text(
        f"✅ <b>قیمت‌ها تغییر کرد!</b>\n\n"
        f"{count} محصول — {sign}{pct:.1f}%",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:finance"),
    )
    await cb.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  LOG GROUP (FORUM TOPICS)
# ══════════════════════════════════════════════════════════════════════════════

_LOG_TOPIC_NAMES = {
    "log_topic_finance":      "💰 گزارش مالی",
    "log_topic_new_user":     "👤 کاربران جدید",
    "log_topic_purchase":     "🛒 گزارش خرید",
    "log_topic_server":       "🖥 لاگ سرور",
    "log_topic_backup":       "💾 بکاپ",
    "log_topic_moderation":   "🔨 مودریشن",
}


@router.callback_query(F.data == "admin:log_group")
async def cb_admin_log_group(cb: CallbackQuery, session: AsyncSession):
    group_id = await _get_setting(session, "log_group_id")
    if group_id:
        topics = []
        for key, label in _LOG_TOPIC_NAMES.items():
            tid = await _get_setting(session, key)
            topics.append(f"  {label}: {'✅' if tid else '❌'}")
        topics_text = "\n".join(topics)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ ساخت تاپیک‌های جدید", callback_data="admin:log_sync")],
            [InlineKeyboardButton(text="❌ قطع اتصال", callback_data="admin:log_disconnect")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel")],
        ])
        await cb.message.edit_text(
            f"📋 <b>تاپیک اطلاعات</b>\n\n"
            f"✅ متصل به گروه: <code>{group_id}</code>\n\n"
            f"<b>تاپیک‌ها:</b>\n{topics_text}",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 اتصال گروه", callback_data="admin:log_setup")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel")],
        ])
        await cb.message.edit_text(
            "📋 <b>تاپیک اطلاعات</b>\n\n"
            "هنوز گروهی متصل نشده.\n\n"
            "<b>راهنما:</b>\n"
            "۱. ربات را به یک سوپرگروه تاپیک‌دار اضافه کنید\n"
            "۲. به ربات دسترسی <b>ادمین کامل</b> بدهید\n"
            "۳. دکمه اتصال را بزنید و Chat ID گروه را وارد کنید",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await cb.answer()


@router.callback_query(F.data == "admin:log_setup")
async def cb_admin_log_setup(cb: CallbackQuery, state: FSMContext):
    await state.set_state(LogGroupFSM.waiting_group_id)
    await cb.message.edit_text(
        "🔗 <b>اتصال گروه لاگ</b>\n\n"
        "Chat ID گروه را وارد کنید:\n"
        "<i>مثال: -1001234567890</i>",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(LogGroupFSM.waiting_group_id)
async def msg_log_group_id(message: Message, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip()
    try:
        group_id = int(raw)
    except ValueError:
        await message.answer("❌ آیدی نامعتبر. یک عدد صحیح وارد کنید:")
        return

    await state.clear()

    # Test connection
    try:
        await message.bot.send_message(
            group_id,
            "✅ ربات با موفقیت متصل شد! در حال ساخت تاپیک‌ها...",
        )
    except Exception as e:
        await message.answer(
            f"❌ اتصال به گروه ناموفق بود:\n<code>{e}</code>\n\n"
            "مطمئن شوید ربات ادمین گروه است.",
            parse_mode="HTML",
        )
        return

    # Create forum topics
    topics_to_create = list(_LOG_TOPIC_NAMES.items())
    failed = []
    for key, name in topics_to_create:
        try:
            ft = await message.bot.create_forum_topic(group_id, name)
            await _set_setting(session, key, str(ft.message_thread_id))
        except Exception as e:
            failed.append(f"{name}: {e}")

    await _set_setting(session, "log_group_id", str(group_id))

    # Trigger an immediate backup now that the group is connected
    try:
        from bot.tasks.backup import run_database_backup
        run_database_backup.apply_async(countdown=5)
    except Exception:
        pass

    if failed:
        fail_text = "\n".join(failed)
        await message.answer(
            f"⚠️ گروه متصل شد ولی برخی تاپیک‌ها ساخته نشدند:\n<code>{fail_text}</code>",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb("admin:log_group"),
        )
    else:
        await message.answer(
            "✅ <b>اتصال برقرار شد!</b>\n\n"
            "تمام تاپیک‌ها با موفقیت ساخته شدند.\n"
            "اولین بکاپ در چند ثانیه ارسال می‌شود.",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb("admin:log_group"),
        )


@router.callback_query(F.data == "admin:log_sync")
async def cb_admin_log_sync(cb: CallbackQuery, session: AsyncSession):
    group_id = await _get_setting(session, "log_group_id")
    if not group_id:
        await cb.answer("گروهی متصل نیست.", show_alert=True)
        return
    created, failed = [], []
    for key, name in _LOG_TOPIC_NAMES.items():
        existing = await _get_setting(session, key)
        if existing:
            continue
        try:
            ft = await cb.bot.create_forum_topic(int(group_id), name)
            await _set_setting(session, key, str(ft.message_thread_id))
            created.append(name)
        except Exception as e:
            failed.append(f"{name}: {e}")
    if not created and not failed:
        await cb.answer("همه تاپیک‌ها قبلاً موجودند.", show_alert=True)
    elif failed:
        await cb.answer(f"❌ خطا در ساخت: {', '.join(failed[:2])}", show_alert=True)
    else:
        await cb.answer(f"✅ ساخته شد: {', '.join(created)}", show_alert=True)
    await cb_admin_log_group(cb, session)


@router.callback_query(F.data == "admin:log_disconnect")
async def cb_admin_log_disconnect(cb: CallbackQuery, session: AsyncSession):
    keys = ["log_group_id"] + list(_LOG_TOPIC_NAMES.keys())
    for key in keys:
        row = await session.get(BotSettings, key)
        if row:
            await session.delete(row)
    await session.flush()
    await cb_admin_log_group(cb, session)  # handles cb.answer() internally


# ══════════════════════════════════════════════════════════════════════════════
#  NOWPAYMENTS GATEWAY MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:np")
async def cb_admin_np_gateway(cb: CallbackQuery, session: AsyncSession):
    from bot.config import settings as _s

    rate = await _get_setting(session, "np_usd_to_irt_rate", "تنظیم نشده")
    wh_url = await _get_setting(session, "np_webhook_url", "تنظیم نشده")

    api_status = "✅ تنظیم شده" if _s.NP_API_KEY else "❌ تنظیم نشده"
    if wh_url and wh_url != "تنظیم نشده":
        wh_short = wh_url[:40] + "…" if len(wh_url) > 40 else wh_url
    else:
        wh_short = "تنظیم نشده"

    await cb.message.edit_text(
        f"💎 <b>مدیریت درگاه NOWPayments</b>\n\n"
        f"🔑 API Key: {api_status}\n"
        f"💱 نرخ دلار: <b>{rate}</b> تومان\n"
        f"🌐 Webhook URL: <code>{wh_short}</code>\n\n"
        f"<i>برای تغییر هر مورد دکمه مربوطه را بزنید.</i>",
        parse_mode="HTML",
        reply_markup=np_gateway_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "admin:np_rate")
async def cb_admin_np_rate(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NPSettingsFSM.waiting_rate)
    await cb.message.edit_text(
        "💱 <b>تنظیم نرخ دلار به تومان</b>\n\n"
        "نرخ تبدیل هر دلار به تومان را وارد کنید:\n"
        "<i>مثال: 75000</i>",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(NPSettingsFSM.waiting_rate)
async def msg_np_rate(message: Message, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip().replace(",", "")
    try:
        rate = float(raw)
        if rate <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ مقدار نامعتبر. یک عدد مثبت وارد کنید:")
        return

    await state.clear()
    await _set_setting(session, "np_usd_to_irt_rate", str(rate))
    await message.answer(
        f"✅ نرخ دلار به <b>{rate:,.0f} تومان</b> تنظیم شد.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:np"),
    )


@router.callback_query(F.data == "admin:np_wh")
async def cb_admin_np_wh(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NPSettingsFSM.waiting_webhook_url)
    await cb.message.edit_text(
        "🌐 <b>تنظیم Webhook URL</b>\n\n"
        "آدرس IPN webhook را وارد کنید:\n"
        "<i>مثال: https://example.com/np-webhook</i>",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(NPSettingsFSM.waiting_webhook_url)
async def msg_np_wh(message: Message, state: FSMContext, session: AsyncSession):
    url = (message.text or "").strip()
    if not url.startswith("http"):
        await message.answer("❌ آدرس باید با http یا https شروع شود:")
        return

    await state.clear()
    await _set_setting(session, "np_webhook_url", url)
    await message.answer(
        f"✅ Webhook URL تنظیم شد:\n<code>{url}</code>",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:np"),
    )


@router.callback_query(F.data == "admin:np_test")
async def cb_admin_np_test(cb: CallbackQuery):
    from bot.config import settings as _s
    from bot.services.nowpayments import NOWPaymentsClient, NOWPaymentsError

    await cb.answer("⏳ در حال تست...")

    if not _s.NP_API_KEY:
        await cb.message.edit_text(
            "❌ <b>NP_API_KEY</b> در .env تنظیم نشده.",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb("admin:np"),
        )
        return

    client = NOWPaymentsClient()
    lines = ["💎 <b>تست اتصال NOWPayments</b>\n"]

    try:
        status = await client.check_status()
        api_ok = status.get("message") == "OK"
        lines.append(f"🔌 وضعیت API: {'✅ آنلاین' if api_ok else '❌ مشکل'}")
    except NOWPaymentsError as e:
        lines.append(f"🔌 وضعیت API: ❌ خطا — {e}")
        await cb.message.edit_text("\n".join(lines), parse_mode="HTML",
                                   reply_markup=back_to_admin_kb("admin:np"))
        return

    try:
        coins = await client.get_merchant_coins()
        outcome = _s.NP_OUTCOME_CURRENCY.lower()
        trx_active = any(c.lower() == outcome for c in coins)
        lines.append(f"🪙 ارز خروجی ({outcome.upper()}): {'✅ فعال' if trx_active else '❌ غیرفعال'}")
        active_list = ", ".join(coins[:10]) + ("…" if len(coins) > 10 else "")
        lines.append(f"📋 ارزهای فعال: {active_list}")
    except NOWPaymentsError as e:
        lines.append(f"🪙 دریافت لیست ارزها: ❌ خطا — {e}")

    lines.append(f"\n🔑 API Key: <code>***{_s.NP_API_KEY[-6:]}</code>")
    lines.append(f"💱 قیمت‌گذاری: {_s.NP_PRICE_CURRENCY.upper()}")
    lines.append(f"🎯 پرداخت به: {_s.NP_OUTCOME_CURRENCY.upper()}")

    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:np"),
    )
