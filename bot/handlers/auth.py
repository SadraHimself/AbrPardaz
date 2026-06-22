"""Phone number verification + Shahkar KYC."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import phonenumbers
from aiogram import F, Router
from bot.config import settings as _settings
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import OTP, User
from bot.keyboards.main import (
    back_kb, cancel_kb, main_menu_kb, remove_kb, request_phone_kb,
)
from bot.services.shahkar import ShahkarService

router = Router(name="auth")


class KYCStates(StatesGroup):
    waiting_national_id = State()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_ir_phone(raw: str) -> str | None:
    """Returns +98xxxxxxxxxx format or None if invalid/non-Iranian."""
    try:
        parsed = phonenumbers.parse(raw, "IR")
        if not phonenumbers.is_valid_number(parsed):
            return None
        if parsed.country_code != 98:
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        return None


# ── Phone contact handler ─────────────────────────────────────────────────────

@router.message(F.contact)
async def handle_contact(message: Message, user: User, session: AsyncSession):
    contact = message.contact

    # Telegram enforces that contact.user_id == message.from_user.id for own contacts
    if contact.user_id != message.from_user.id:
        await message.answer("❌ لطفاً شماره خودتان را ارسال کنید.", reply_markup=request_phone_kb())
        return

    phone = _normalize_ir_phone(contact.phone_number)
    if not phone:
        await message.answer(
            "❌ فقط شماره موبایل ایرانی (۰۹xxxxxxxxx) قابل قبول است.\n"
            "لطفاً دوباره تلاش کنید.",
            reply_markup=request_phone_kb(),
        )
        return

    user.phone_number = phone
    user.is_phone_verified = True
    await session.flush()

    await message.answer(
        f"✅ شماره موبایل شما با موفقیت تأیید شد!\n📱 {phone}",
        reply_markup=remove_kb(),
    )
    await message.answer(
        "به ربات خوش آمدید! از منوی زیر استفاده کنید:",
        reply_markup=main_menu_kb(is_admin=user.is_admin or user.telegram_id in _settings.admin_ids),
    )


# ── Shahkar KYC ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "start_kyc")
async def cb_start_kyc(cb: CallbackQuery, user: User, state: FSMContext):
    if not user.is_phone_verified:
        await cb.answer("ابتدا شماره موبایل خود را تأیید کنید.", show_alert=True)
        return
    if user.is_kyc_verified:
        await cb.answer("احراز هویت شما قبلاً تأیید شده است.", show_alert=True)
        return

    await state.set_state(KYCStates.waiting_national_id)
    await cb.message.edit_text(
        "🪪 <b>احراز هویت (شاهکار)</b>\n\n"
        "برای تهیه سرور ایران، احراز هویت الزامی است.\n"
        "لطفاً کد ملی ۱۰ رقمی خود را وارد کنید:",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    await cb.answer()


@router.message(KYCStates.waiting_national_id, F.text.regexp(r"^\d{10}$"))
async def handle_national_id(message: Message, user: User, state: FSMContext, session: AsyncSession):
    national_id = message.text.strip()
    await state.clear()

    await message.answer("⏳ در حال بررسی اطلاعات در سامانه شاهکار...")

    try:
        shahkar = ShahkarService()
        matched = await shahkar.verify(user.phone_number, national_id)
    except RuntimeError as e:
        if "not configured" in str(e):
            await message.answer(
                "⚠️ سرویس شاهکار در حال حاضر پیکربندی نشده است. با پشتیبانی تماس بگیرید.",
                reply_markup=main_menu_kb(is_admin=user.is_admin or user.telegram_id in _settings.admin_ids),
            )
            return
        matched = False

    if matched:
        user.national_id = national_id
        user.is_kyc_verified = True
        await session.flush()
        await message.answer(
            "✅ احراز هویت با موفقیت انجام شد!\nاکنون می‌توانید سرور ایران تهیه کنید.",
            reply_markup=main_menu_kb(is_admin=user.is_admin or user.telegram_id in _settings.admin_ids),
        )
    else:
        await message.answer(
            "❌ اطلاعات وارد شده با سامانه شاهکار مطابقت ندارد.\n"
            "مطمئن شوید کد ملی با شماره موبایل ثبت شده در سامانه مخابرات یکی باشد.",
            reply_markup=main_menu_kb(is_admin=user.is_admin or user.telegram_id in _settings.admin_ids),
        )


@router.message(KYCStates.waiting_national_id)
async def handle_national_id_invalid(message: Message):
    await message.answer("❌ کد ملی باید دقیقاً ۱۰ رقم باشد. دوباره وارد کنید:")
