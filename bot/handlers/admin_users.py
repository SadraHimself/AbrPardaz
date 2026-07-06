"""Admin user management — ban, wallet, discount, KYC, messages."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import (
    DiscountCode, PaymentOrder, Server, ServerStatus, Transaction, User, UserStatus,
)
from bot.keyboards.admin import (
    back_to_admin_kb, cancel_admin_kb, confirm_kb, user_detail_kb, users_list_kb,
)
from bot.services.billing import BillingService
from bot.services.log_service import LogService

router = Router(name="admin_users")


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, user: User) -> bool:
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class UserManageFSM(StatesGroup):
    credit_amount = State()
    debit_amount = State()
    send_message = State()
    search_user = State()
    disc_code = State()
    disc_percent = State()
    disc_expires = State()
    disc_max_uses = State()
    server_limit = State()
    ban_days = State()
    ban_reason = State()
    edit_national_id = State()
    edit_phone = State()


# ── User list ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:users")
async def cb_admin_users(cb: CallbackQuery, session: AsyncSession):
    await _show_users_page(cb, session, 0)


@router.callback_query(F.data.startswith("admin:users_page:"))
async def cb_users_page(cb: CallbackQuery, session: AsyncSession):
    page = int(cb.data.split(":")[2])
    await _show_users_page(cb, session, page)


async def _show_users_page(cb: CallbackQuery, session: AsyncSession, page: int):
    total = (await session.execute(select(func.count(User.id)))).scalar() or 0
    result = await session.execute(
        select(User).order_by(User.created_at.desc()).offset(page * 20).limit(20)
    )
    users = list(result.scalars().all())
    await cb.message.edit_text(
        f"<b>کاربران</b> (صفحه {page + 1} — {total} نفر کل):",
        parse_mode="HTML",
        reply_markup=users_list_kb(users, page, total),
    )
    await cb.answer()


@router.callback_query(F.data == "admin:user_search")
async def cb_user_search_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(UserManageFSM.search_user)
    await cb.message.edit_text(
        "<b>جستجوی کاربر</b>\n\nیوزرنیم، آیدی تلگرام یا شماره تلفن وارد کنید:",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(UserManageFSM.search_user)
async def msg_user_search(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    q = message.text.strip().lstrip("@")
    # Try numeric (telegram_id)
    try:
        tg_id = int(q)
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
    except ValueError:
        # Try username or phone
        result = await session.execute(
            select(User).where(
                (User.username == q) | (User.phone_number == q)
            ).limit(1)
        )
        user = result.scalar_one_or_none()

    if not user:
        await message.answer("کاربری یافت نشد.", reply_markup=back_to_admin_kb("admin:users"))
        return

    await _show_user_detail(message, session, user, edit=False)


# ── User detail ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user:"))
async def cb_admin_user_detail(cb: CallbackQuery, session: AsyncSession):
    user_id = int(cb.data.split(":")[2])
    user = await session.get(User, user_id)
    if not user:
        await cb.answer("کاربر یافت نشد.", show_alert=True)
        return
    await _show_user_detail(cb.message, session, user, edit=True)
    await cb.answer()


async def _show_user_detail(msg, session: AsyncSession, user: User, edit: bool = True):
    srv_count = (await session.execute(
        select(func.count(Server.id)).where(
            Server.user_id == user.id,
            Server.status == ServerStatus.ACTIVE,
        )
    )).scalar() or 0
    tx_count = (await session.execute(
        select(func.count(Transaction.id)).where(Transaction.user_id == user.id)
    )).scalar() or 0

    is_banned = user.status == UserStatus.BANNED
    if is_banned:
        extra_u = user.extra_data or {}
        ban_reason = extra_u.get("ban_reason", "—")
        ban_until_raw = extra_u.get("ban_until")
        if ban_until_raw:
            try:
                bt = datetime.fromisoformat(ban_until_raw).strftime("%Y/%m/%d %H:%M")
                status_text = f"🚫 بن تا {bt}\n(علت: {ban_reason})"
            except (ValueError, TypeError):
                status_text = f"🚫 بن دائمی\n(علت: {ban_reason})"
        else:
            status_text = f"🚫 بن دائمی\n(علت: {ban_reason})"
    elif user.status == UserStatus.ACTIVE:
        status_text = "✅ فعال"
    else:
        status_text = "معلق"
    kyc_text = "✅ تأیید شده" if user.is_kyc_verified else "❌ تأیید نشده"
    phone_text = user.phone_number or "—"
    hourly_limit = (user.extra_data or {}).get("max_hourly_servers", 5)

    text = (
        f"<b>کاربر #{user.id}</b>\n\n"
        f"نام: {user.first_name or '—'} {user.last_name or ''}\n"
        f"یوزرنیم: @{user.username or '—'}\n"
        f"آیدی عددی تلگرام: <code>{user.telegram_id}</code>\n"
        f"موبایل: <code>{phone_text}</code>\n"
        f"کد ملی: <code>{user.national_id or '—'}</code>\n"
        f"احراز هویت: {kyc_text}\n"
        f"وضعیت: {status_text}\n\n"
        f"موجودی: <b>{user.balance:,.0f} تومان</b>\n"
        f"سرور فعال: {srv_count}\n"
        f"تراکنش: {tx_count}\n"
        f"لیمیت سرور ساعتی: {hourly_limit}\n"
        f"عضو از: {user.created_at.strftime('%Y/%m/%d')}"
    )
    kb = user_detail_kb(user.id, is_banned, user.is_kyc_verified, hourly_limit)
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Ban / Unban ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user_ban:"))
async def cb_user_ban(cb: CallbackQuery, user: User, session: AsyncSession, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    target = await session.get(User, user_id)
    if not target:
        await cb.answer("کاربر یافت نشد.", show_alert=True)
        return

    if target.status == UserStatus.BANNED:
        # Unban immediately
        target.status = UserStatus.ACTIVE
        extra = dict(target.extra_data or {})
        extra.pop("ban_until", None)
        extra.pop("ban_reason", None)
        target.extra_data = extra
        await session.flush()
        await cb.answer("کاربر آنبن شد.")
        try:
            await cb.bot.send_message(target.telegram_id, "حساب شما از حالت بن خارج شد.")
        except Exception:
            pass
        await LogService(cb.bot, session).log_unban_user(target, cb.from_user.id)
        await _show_user_detail(cb.message, session, target, edit=True)
    else:
        # Self-ban check
        if target.telegram_id == cb.from_user.id:
            await cb.answer("شما نمی‌توانید خودتان را بن کنید!", show_alert=True)
            return
        # Start ban FSM — ask duration
        await state.update_data(target_user_id=user_id)
        await state.set_state(UserManageFSM.ban_days)
        await cb.message.edit_text(
            f"<b>بن کاربر #{user_id}</b>\n\n"
            "چند روز بن شود؟\n"
            "<i>(عدد وارد کنید — ۰ = بن دائمی)</i>",
            parse_mode="HTML",
            reply_markup=cancel_admin_kb(),
        )
        await cb.answer()


@router.message(UserManageFSM.ban_days, F.text.regexp(r"^\d+$"))
async def msg_ban_days(message: Message, state: FSMContext):
    await state.update_data(ban_days=int(message.text))
    await state.set_state(UserManageFSM.ban_reason)
    await message.answer("علت بن را بنویسید:", reply_markup=cancel_admin_kb())


@router.message(UserManageFSM.ban_reason)
async def msg_ban_reason(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()

    reason = message.text.strip()
    days = data.get("ban_days", 0)
    user_id = data["target_user_id"]

    target = await session.get(User, user_id)
    if not target:
        await message.answer("کاربر یافت نشد.")
        return

    target.status = UserStatus.BANNED
    extra = dict(target.extra_data or {})
    extra["ban_reason"] = reason

    if days > 0:
        ban_until = datetime.now(timezone.utc) + timedelta(days=days)
        extra["ban_until"] = ban_until.isoformat()
        duration_text = f"{days} روز"
    else:
        extra.pop("ban_until", None)
        duration_text = "دائمی"

    target.extra_data = extra
    await session.flush()

    await message.answer(
        f"کاربر برای <b>{duration_text}</b> بن شد.\nعلت: {reason}",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:user:{user_id}"),
    )
    try:
        notify = f"حساب شما توسط مدیریت بن شده است.\nعلت: {reason}"
        if days > 0:
            notify += f"\nمدت: {days} روز"
        await message.bot.send_message(target.telegram_id, notify)
    except Exception:
        pass
    await LogService(message.bot, session).log_ban_user(target, reason, days, message.from_user.id)


# ── Credit / Debit ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user_credit:"))
async def cb_user_credit_start(cb: CallbackQuery, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await state.update_data(target_user_id=user_id)
    await state.set_state(UserManageFSM.credit_amount)
    await cb.message.edit_text(
        "<b>افزایش موجودی</b>\n\nمبلغ (تومان) را وارد کنید:",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(UserManageFSM.credit_amount, F.text.regexp(r"^\d+(\.\d+)?$"))
async def msg_user_credit(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    amount = float(message.text)
    user = await session.get(User, data["target_user_id"])
    if not user:
        await message.answer("کاربر یافت نشد.")
        return
    billing = BillingService(session)
    await billing.credit(user.id, amount, description="شارژ توسط ادمین")
    await message.answer(
        f"{amount:,.0f} تومان به <b>{user.first_name or user.telegram_id}</b> اضافه شد.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:user:{user.id}"),
    )
    await LogService(message.bot, session).log_admin_wallet_change(
        user, amount, is_credit=True,
        admin_tg_id=message.from_user.id,
        admin_name=message.from_user.first_name or "ادمین",
    )
    try:
        await message.bot.send_message(
            user.telegram_id,
            f"<b>{amount:,.0f} تومان</b> توسط مدیریت به کیف‌پول شما اضافه شد.",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:user_debit:"))
async def cb_user_debit_start(cb: CallbackQuery, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await state.update_data(target_user_id=user_id)
    await state.set_state(UserManageFSM.debit_amount)
    await cb.message.edit_text(
        "<b>کاهش موجودی</b>\n\nمبلغ (تومان) را وارد کنید:",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(UserManageFSM.debit_amount, F.text.regexp(r"^\d+(\.\d+)?$"))
async def msg_user_debit(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    amount = float(message.text)
    user = await session.get(User, data["target_user_id"])
    if not user:
        await message.answer("کاربر یافت نشد.")
        return
    billing = BillingService(session)
    ok = await billing.debit(user.id, amount, description="کسر توسط ادمین")
    if ok:
        await message.answer(
            f"{amount:,.0f} تومان از <b>{user.first_name or user.telegram_id}</b> کسر شد.",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb(f"admin:user:{user.id}"),
        )
        await LogService(message.bot, session).log_admin_wallet_change(
            user, amount, is_credit=False,
            admin_tg_id=message.from_user.id,
            admin_name=message.from_user.first_name or "ادمین",
        )
    else:
        await message.answer(
            "موجودی کافی نیست.",
            reply_markup=back_to_admin_kb(f"admin:user:{user.id}"),
        )


# ── KYC management ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user_verify:"))
async def cb_user_verify(cb: CallbackQuery, session: AsyncSession):
    user_id = int(cb.data.split(":")[2])
    user = await session.get(User, user_id)
    if not user:
        await cb.answer("کاربر یافت نشد.", show_alert=True)
        return
    user.is_kyc_verified = True
    user.is_phone_verified = True
    await session.flush()
    await cb.answer("کاربر احراز هویت شد.")
    try:
        await cb.bot.send_message(
            user.telegram_id,
            "احراز هویت شما توسط مدیریت تأیید شد.",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        pass
    await _show_user_detail(cb.message, session, user, edit=True)


@router.callback_query(F.data.startswith("admin:user_unverify:"))
async def cb_user_unverify(cb: CallbackQuery, session: AsyncSession):
    user_id = int(cb.data.split(":")[2])
    user = await session.get(User, user_id)
    if not user:
        await cb.answer("کاربر یافت نشد.", show_alert=True)
        return
    user.is_kyc_verified = False
    user.national_id = None
    await session.flush()
    await cb.answer("احراز هویت حذف شد.")
    try:
        await cb.bot.send_message(
            user.telegram_id,
            '<tg-emoji emoji-id="6008233706039284019">⚠️</tg-emoji> '
            "احراز هویت شما توسط مدیریت حذف شد. لطفاً مجدداً احراز هویت کنید.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await _show_user_detail(cb.message, session, user, edit=True)


@router.callback_query(F.data.startswith("admin:user_edit_nid:"))
async def cb_user_edit_nid_start(cb: CallbackQuery, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await state.update_data(target_user_id=user_id)
    await state.set_state(UserManageFSM.edit_national_id)
    await cb.message.edit_text(
        "<b>ویرایش کد ملی</b>\n\nکد ملی جدید (۱۰ رقم) را وارد کنید:",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(UserManageFSM.edit_national_id, F.text.regexp(r"^\d{10}$"))
async def msg_user_edit_nid(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    user = await session.get(User, data["target_user_id"])
    if not user:
        await message.answer("کاربر یافت نشد.")
        return
    user.national_id = message.text.strip()
    await session.flush()
    await message.answer(
        f"کد ملی به <code>{user.national_id}</code> تغییر یافت.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:user:{user.id}"),
    )


@router.callback_query(F.data.startswith("admin:user_edit_phone:"))
async def cb_user_edit_phone_start(cb: CallbackQuery, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await state.update_data(target_user_id=user_id)
    await state.set_state(UserManageFSM.edit_phone)
    await cb.message.edit_text(
        "<b>ویرایش شماره موبایل</b>\n\nشماره موبایل جدید را وارد کنید:",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(UserManageFSM.edit_phone)
async def msg_user_edit_phone(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    phone = message.text.strip()
    user = await session.get(User, data["target_user_id"])
    if not user:
        await message.answer("کاربر یافت نشد.")
        return
    user.phone_number = phone
    user.is_phone_verified = True
    await session.flush()
    await message.answer(
        f"شماره موبایل به <code>{phone}</code> تغییر یافت.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:user:{user.id}"),
    )


# ── Send message ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user_msg:"))
async def cb_user_msg_start(cb: CallbackQuery, state: FSMContext):
    user_id = int(cb.data.split(":")[2])
    await state.update_data(target_user_id=user_id)
    await state.set_state(UserManageFSM.send_message)
    await cb.message.edit_text(
        "<b>ارسال پیام به کاربر</b>\n\nمتن پیام را بنویسید:",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(UserManageFSM.send_message)
async def msg_send_to_user(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    user = await session.get(User, data["target_user_id"])
    if not user:
        await message.answer("کاربر یافت نشد.")
        return
    try:
        await message.bot.send_message(
            user.telegram_id,
            f"<b>پیام از مدیریت:</b>\n\n{message.text}",
            parse_mode="HTML",
        )
        await message.answer(
            "پیام ارسال شد.",
            reply_markup=back_to_admin_kb(f"admin:user:{user.id}"),
        )
    except Exception as e:
        await message.answer(
            f"ارسال ناموفق: {e}",
            reply_markup=back_to_admin_kb(f"admin:user:{user.id}"),
        )


# ── Payment history ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user_payments:"))
async def cb_user_payments(cb: CallbackQuery, session: AsyncSession):
    user_id = int(cb.data.split(":")[2])
    result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.desc())
        .limit(10)
    )
    txs = list(result.scalars().all())
    if not txs:
        await cb.answer("هیچ تراکنشی یافت نشد.", show_alert=True)
        return
    lines = []
    for tx in txs:
        sign = "+" if tx.type.value == "credit" else "-"
        lines.append(
            f"{sign}{tx.amount:,.0f}T | {tx.description or '—'} | {tx.created_at.strftime('%m/%d %H:%M')}"
        )
    await cb.message.edit_text(
        f"<b>آخرین ۱۰ تراکنش:</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:user:{user_id}"),
    )
    await cb.answer()


# ── Active servers ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user_servers:"))
async def cb_user_servers(cb: CallbackQuery, session: AsyncSession):
    user_id = int(cb.data.split(":")[2])
    result = await session.execute(
        select(Server).where(
            Server.user_id == user_id,
            Server.status != ServerStatus.DELETED,
        ).order_by(Server.created_at.desc())
    )
    servers = list(result.scalars().all())
    if not servers:
        await cb.answer("هیچ سروری یافت نشد.", show_alert=True)
        return
    lines = []
    for s in servers:
        lines.append(
            f"{s.name} | {s.ip_address or '—'} | {s.status.value}"
        )
    await cb.message.edit_text(
        f"<b>سرویس‌های کاربر ({len(servers)}):</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:user:{user_id}"),
    )
    await cb.answer()


# ── User-specific discount code ───────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user_disc:"))
async def cb_user_disc_start(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    user_id = int(cb.data.split(":")[2])
    # Show existing personal discount for this user
    result = await session.execute(
        select(DiscountCode).where(DiscountCode.user_id == user_id, DiscountCode.is_active == True)
    )
    existing = result.scalar_one_or_none()

    if existing:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="حذف کد موجود", callback_data=f"admin:user_disc_del:{user_id}:{existing.id}")],
            [InlineKeyboardButton(text="بازگشت", callback_data=f"admin:user:{user_id}")],
        ])
        await cb.message.edit_text(
            f"کد تخفیف اختصاصی این کاربر:\n\n"
            f"کد: <code>{existing.code}</code>\n"
            f"تخفیف: {existing.discount_percent:.0f}%\n"
            f"انقضا: {existing.expires_at.strftime('%Y-%m-%d') if existing.expires_at else 'بدون انقضا'}",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await state.update_data(disc_user_id=user_id)
        await state.set_state(UserManageFSM.disc_code)
        await cb.message.edit_text(
            "<b>کد تخفیف اختصاصی</b>\n\nکد تخفیف را وارد کنید:",
            parse_mode="HTML",
            reply_markup=cancel_admin_kb(),
        )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:user_disc_del:"))
async def cb_user_disc_del(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    user_id, disc_id = int(parts[2]), int(parts[3])
    disc = await session.get(DiscountCode, disc_id)
    if disc:
        await session.delete(disc)
        await session.flush()
    await cb.answer("کد تخفیف حذف شد.")
    user = await session.get(User, user_id)
    if user:
        await _show_user_detail(cb.message, session, user, edit=True)


@router.message(UserManageFSM.disc_code)
async def msg_disc_code(message: Message, state: FSMContext, session: AsyncSession):
    code = message.text.strip().upper()
    exists = await session.execute(select(DiscountCode).where(DiscountCode.code == code))
    if exists.scalar_one_or_none():
        await message.answer("این کد قبلاً وجود دارد.")
        return
    await state.update_data(disc_code=code)
    await state.set_state(UserManageFSM.disc_percent)
    await message.answer("درصد تخفیف (۱-۱۰۰):", reply_markup=cancel_admin_kb())


@router.message(UserManageFSM.disc_percent, F.text.regexp(r"^\d+(\.\d+)?$"))
async def msg_disc_percent(message: Message, state: FSMContext):
    pct = float(message.text)
    if not 1 <= pct <= 100:
        await message.answer("باید بین ۱ تا ۱۰۰ باشد.")
        return
    await state.update_data(disc_percent=pct)
    await state.set_state(UserManageFSM.disc_expires)
    from bot.keyboards.admin import skip_or_cancel_kb
    await message.answer("تاریخ انقضا (YYYY-MM-DD) یا /skip:", reply_markup=skip_or_cancel_kb())


@router.message(UserManageFSM.disc_expires)
async def msg_disc_expires(message: Message, state: FSMContext):
    raw = message.text.strip()
    if raw.lower() in ("/skip", "skip"):
        await state.update_data(disc_expires=None)
    else:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            await state.update_data(disc_expires=dt.isoformat())
        except ValueError:
            await message.answer("فرمت نادرست. مثال: 2025-12-31")
            return
    await state.set_state(UserManageFSM.disc_max_uses)
    from bot.keyboards.admin import skip_or_cancel_kb
    await message.answer("حداکثر استفاده (عدد) یا /skip:", reply_markup=skip_or_cancel_kb())


@router.callback_query(UserManageFSM.disc_expires, F.data == "admin:skip")
async def cb_disc_expires_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(disc_expires=None)
    await state.set_state(UserManageFSM.disc_max_uses)
    from bot.keyboards.admin import skip_or_cancel_kb
    await cb.message.edit_text("حداکثر استفاده یا /skip:", reply_markup=skip_or_cancel_kb())
    await cb.answer()


@router.message(UserManageFSM.disc_max_uses)
async def msg_disc_max_uses(message: Message, state: FSMContext, session: AsyncSession):
    raw = message.text.strip()
    max_uses = None
    if raw.lower() not in ("/skip", "skip"):
        try:
            max_uses = int(raw)
        except ValueError:
            await message.answer("عدد صحیح وارد کنید.")
            return

    data = await state.get_data()
    await state.clear()
    expires_at = datetime.fromisoformat(data["disc_expires"]) if data.get("disc_expires") else None
    disc = DiscountCode(
        code=data["disc_code"],
        discount_percent=data["disc_percent"],
        expires_at=expires_at,
        max_uses=max_uses,
        user_id=data["disc_user_id"],
        is_active=True,
    )
    session.add(disc)
    await session.flush()
    await message.answer(
        f"کد تخفیف اختصاصی <code>{disc.code}</code> با {disc.discount_percent:.0f}% برای کاربر ساخته شد.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:user:{data['disc_user_id']}"),
    )


# ── Hourly server limit ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:user_limit:"))
async def cb_user_limit_start(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    user_id = int(cb.data.split(":")[2])
    user = await session.get(User, user_id)
    if not user:
        await cb.answer("کاربر یافت نشد.", show_alert=True)
        return
    current = (user.extra_data or {}).get("max_hourly_servers", 5)
    await state.update_data(target_user_id=user_id)
    await state.set_state(UserManageFSM.server_limit)
    await cb.message.edit_text(
        f"<b>لیمیت سرور ساعتی</b>\n\n"
        f"لیمیت فعلی: <b>{current}</b>\n\n"
        f"مقدار جدید را وارد کنید (۵ تا ۵۰):",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(UserManageFSM.server_limit, F.text.regexp(r"^\d+$"))
async def msg_user_server_limit(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    value = max(5, min(50, int(message.text)))
    user = await session.get(User, data["target_user_id"])
    if not user:
        await message.answer("کاربر یافت نشد.")
        return
    extra = dict(user.extra_data or {})
    extra["max_hourly_servers"] = value
    user.extra_data = extra
    await session.flush()
    await message.answer(
        f"لیمیت سرور ساعتی کاربر به <b>{value}</b> تغییر یافت.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:user:{user.id}"),
    )
