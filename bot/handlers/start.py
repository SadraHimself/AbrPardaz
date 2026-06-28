"""Handler for /start command — welcome, terms, force-join, main menu."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import BotSettings, Server, ServerStatus, User
from bot.keyboards.main import back_kb, main_menu_kb, request_phone_kb
from bot.services.log_service import LogService
from bot.utils.loading import answer_loading, edit_loading

router = Router(name="start")


def _is_admin(user: User) -> bool:
    return user.is_admin or user.telegram_id in settings.admin_ids


# ── BotSettings helpers ───────────────────────────────────────────────────────

async def _get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(BotSettings, key)
    return row.value if row else default


async def _get_setting_opt(session: AsyncSession, key: str) -> Optional[str]:
    row = await session.get(BotSettings, key)
    return row.value if row else None


# ── Force-join helpers ────────────────────────────────────────────────────────

async def _get_force_channels(session: AsyncSession) -> list[str]:
    raw = await _get_setting_opt(session, "force_channels")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


async def _check_membership(bot, tg_id: int, channels: list[str]) -> list[str]:
    """Return list of channels the user has NOT joined."""
    not_joined = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, tg_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                not_joined.append(ch)
        except TelegramForbiddenError:
            # Bot not admin in channel — skip, don't block user
            pass
        except Exception as e:
            err = str(e).lower()
            if "administrator" in err or "admin" in err:
                pass  # Bot needs admin rights — skip
            else:
                not_joined.append(ch)
    return not_joined


def _join_channels_kb(channels: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        display = ch.lstrip("@") if ch.startswith("@") else ch
        buttons.append([InlineKeyboardButton(text=f"📢 عضویت در {display}", url=f"https://t.me/{ch.lstrip('@')}")])
    buttons.append([InlineKeyboardButton(text="✅ بررسی عضویت", callback_data="check_join")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Terms helpers ─────────────────────────────────────────────────────────────

async def _get_terms_text(session: AsyncSession) -> str:
    return await _get_setting(
        session, "terms_text",
        default=(
            "📋 <b>قوانین و مقررات استفاده از سرویس</b>\n\n"
            "• استفاده از سرویس برای فعالیت‌های غیرقانونی ممنوع است.\n"
            "• ربات هر زمان می‌تواند سرویس را مطابق قوانین تعلیق کند.\n"
            "• با ادامه، موافقت خود را اعلام می‌کنید."
        ),
    )


def _terms_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ قبول می‌کنم", callback_data="accept_terms")],
        [InlineKeyboardButton(text="❌ رد کردن", callback_data="decline_terms")],
    ])


# ── Main entry flow ───────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, user: User, session: AsyncSession,
                    state: FSMContext, is_new_user: bool = False):
    await state.clear()

    if is_new_user:
        await LogService(message.bot, session).log_new_user(user)

    # 1. Maintenance check
    maintenance = await _get_setting(session, "maintenance_mode", "0")
    if maintenance == "1" and not _is_admin(user):
        maint_text = await _get_setting(session, "maintenance_text", "🔧 ربات در حال بروزرسانی است. لطفاً چند دقیقه دیگر تلاش کنید.")
        await message.answer(maint_text)
        return

    # 2. Phone verification
    if not user.is_phone_verified:
        await message.answer(
            "👋 سلام!\n\n"
            "برای استفاده از ربات، ابتدا باید شماره موبایل خود را وارد کنید.\n"
            "دکمه زیر را بزنید تا شماره‌تان به اشتراک گذاشته شود:",
            reply_markup=request_phone_kb(),
        )
        return

    # 3. Terms acceptance
    if not user.terms_accepted_at:
        terms_text = await _get_terms_text(session)
        await message.answer(terms_text, parse_mode="HTML", reply_markup=_terms_kb())
        return

    # 4. Force-join channels
    channels = await _get_force_channels(session)
    if channels:
        not_joined = await _check_membership(message.bot, message.from_user.id, channels)
        if not_joined:
            await message.answer(
                "📢 <b>عضویت اجباری</b>\n\n"
                "برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شوید:",
                parse_mode="HTML",
                reply_markup=_join_channels_kb(not_joined),
            )
            return

    await _send_welcome(message, user, session)


@router.callback_query(F.data == "accept_terms")
async def cb_accept_terms(cb: CallbackQuery, user: User, session: AsyncSession):
    user.terms_accepted_at = datetime.now(timezone.utc)
    await session.flush()

    # Check force-join after terms
    channels = await _get_force_channels(session)
    if channels:
        not_joined = await _check_membership(cb.bot, cb.from_user.id, channels)
        if not_joined:
            await cb.message.edit_text(
                "📢 <b>عضویت اجباری</b>\n\n"
                "برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شوید:",
                parse_mode="HTML",
                reply_markup=_join_channels_kb(not_joined),
            )
            await cb.answer()
            return

    await cb.message.delete()
    await _send_welcome(cb.message, user, session, is_cb=True, bot=cb.bot, chat_id=cb.from_user.id)
    await cb.answer("✅ قوانین قبول شد!")


@router.callback_query(F.data == "decline_terms")
async def cb_decline_terms(cb: CallbackQuery):
    await cb.message.edit_text(
        "❌ متأسفانه بدون قبول قوانین امکان استفاده از ربات وجود ندارد.\n"
        "هرگاه آماده بودید /start را بزنید."
    )
    await cb.answer()


@router.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery, user: User, session: AsyncSession):
    channels = await _get_force_channels(session)
    not_joined = await _check_membership(cb.bot, cb.from_user.id, channels)
    if not_joined:
        await cb.answer("هنوز در همه کانال‌ها عضو نشدید.", show_alert=True)
        return
    await cb.message.delete()
    await _send_welcome(cb.message, user, session, is_cb=True, bot=cb.bot, chat_id=cb.from_user.id)
    await cb.answer("✅ عضویت تأیید شد!")


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, user: User, session: AsyncSession):
    maintenance = await _get_setting(session, "maintenance_mode", "0")
    if maintenance == "1" and not _is_admin(user):
        maint_text = await _get_setting(session, "maintenance_text", "🔧 ربات در حال بروزرسانی است.")
        await cb.message.edit_text(maint_text)
        await cb.answer()
        return

    welcome_text = await _build_welcome_text(user, session)
    await cb.message.edit_text(welcome_text, parse_mode="HTML")
    await cb.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, user: User, session: AsyncSession, state=None):
    if state:
        await state.clear()
    welcome_text = await _build_welcome_text(user, session)
    await cb.message.edit_text(welcome_text, parse_mode="HTML")
    await cb.answer("لغو شد.")


async def _render_support(target_msg, session: AsyncSession):
    support_text = "برای ارتباط با پشتیبانی میتونید با ایدی زیر در ارتباط باشید @AskAfagh"
    support_id = await _get_setting_opt(session, "support_id")
    buttons = []
    if support_id:
        buttons.append([InlineKeyboardButton(text="💬 پشتیبانی", url=f"https://t.me/{support_id.lstrip('@')}")])
    buttons.append([InlineKeyboardButton(text="بازگشت", callback_data="main_menu", **{"icon_custom_emoji_id": "5933748020960038714"})])
    await target_msg.edit_text(support_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, session: AsyncSession):
    await edit_loading(cb.message)
    await cb.answer()
    await _render_support(cb.message, session)


@router.message(F.text == "پشتیبانی")
async def msg_support(message: Message, session: AsyncSession):
    loading = await answer_loading(message)
    await _render_support(loading, session)


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _build_welcome_text(user: User, session: AsyncSession) -> str:
    name = user.first_name or "کاربر"
    return (
        f'سلام {name} عزیز! <tg-emoji emoji-id="5472055112702629499">👋</tg-emoji>\n\n'
        "به ربات ابر پرداز خوش اومدی، از منوی زیر استفاده کن:"
    )


async def _send_welcome(msg: Message, user: User, session: AsyncSession,
                        is_cb: bool = False, bot=None, chat_id: int = None):
    welcome_text = await _build_welcome_text(user, session)
    target_chat = chat_id or msg.chat.id
    target_bot = bot or msg.bot
    await target_bot.send_message(
        target_chat,
        welcome_text,
        parse_mode="HTML",
        reply_markup=main_menu_kb(is_admin=_is_admin(user)),
    )


# ── User profile ──────────────────────────────────────────────────────────────

async def _render_profile(target_msg, user: User, session: AsyncSession):
    active_count = (await session.execute(
        select(func.count(Server.id)).where(
            Server.user_id == user.id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalar() or 0
    hourly_limit = (user.extra_data or {}).get("max_hourly_servers", 5)
    name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "—"
    phone = user.phone_number or "ثبت نشده"
    kyc = "✅ تأیید شده" if user.is_kyc_verified else "❌ تأیید نشده"
    text = (
        f'<tg-emoji emoji-id="6030393645637570247">👤</tg-emoji> <b>{name}</b>\n\n'
        f"آیدی عددی: <code>{user.telegram_id}</code>\n"
        f"شماره تلفن: <code>{phone}</code>\n"
        f"احراز هویت: {kyc}\n\n"
        f'<tg-emoji emoji-id="5348263477943222701">📑</tg-emoji> سرور‌های فعال: <b>{active_count}</b>\n'
        f'<tg-emoji emoji-id="5258477770735885832">📄</tg-emoji> لیمیت سرور ساعتی: <b>{hourly_limit}</b>\n\n'
        f"موجودی کیف پول: <b>{user.balance:,.0f} تومان</b>"
    )
    await target_msg.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="شارژ کیف پول", callback_data="wallet", **{"icon_custom_emoji_id": "5987880246865565644"})],
            [InlineKeyboardButton(text="تاریخچه تراکنش‌ها", callback_data="tx_history", **{"icon_custom_emoji_id": "5956561916573782596"})],
            [InlineKeyboardButton(text="بازگشت", callback_data="main_menu", **{"icon_custom_emoji_id": "5933748020960038714"})],
        ]),
    )


@router.callback_query(F.data == "user_profile")
async def cb_user_profile(cb: CallbackQuery, user: User, session: AsyncSession):
    await edit_loading(cb.message)
    await cb.answer()
    await _render_profile(cb.message, user, session)


@router.message(F.text == "مشخصات کاربری")
async def msg_user_profile(message: Message, user: User, session: AsyncSession):
    loading = await answer_loading(message)
    await _render_profile(loading, user, session)


async def _render_rules(target_msg, session: AsyncSession):
    rules_text = (
        "📃 <b>قوانین استفاده از سرویس</b>\n\n"
        "• استفاده از سرویس برای فعالیت‌های غیرقانونی ممنوع است.\n"
        "• ربات هر زمان می‌تواند سرویس را مطابق قوانین تعلیق کند.\n"
        "• با استفاده از سرویس، موافقت خود را با این قوانین اعلام می‌کنید."
    )
    await target_msg.edit_text(
        rules_text,
        parse_mode="HTML",
        reply_markup=back_kb("main_menu"),
    )


@router.callback_query(F.data == "rules")
async def cb_rules(cb: CallbackQuery, session: AsyncSession):
    await edit_loading(cb.message)
    await cb.answer()
    await _render_rules(cb.message, session)


@router.message(F.text == "قوانین")
async def msg_rules(message: Message, session: AsyncSession):
    loading = await answer_loading(message)
    await _render_rules(loading, session)
