"""Rial wallet top-up via Zarinpal — restricted to fully KYC-verified users."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import BotSettings, PaymentOrder, User
from bot.services.zarinpal import ZarinpalClient, ZarinpalError

from bot.utils.loading import ERR

router = Router(name="zarinpal_payment")
logger = logging.getLogger(__name__)

_AMOUNTS = [300_000, 500_000, 1_000_000, 5_000_000, 10_000_000]
_KYC_MSG = "برای پرداخت ریالی ابتدا باید احراز هویت کنید."

_BACK = InlineKeyboardButton(text="بازگشت", callback_data="wallet",
                             **{"icon_custom_emoji_id": "5258236805890710909"})


async def _callback_url(session: AsyncSession) -> str:
    if settings.ZARINPAL_CALLBACK_URL:
        return settings.ZARINPAL_CALLBACK_URL
    row = await session.get(BotSettings, "zarinpal_callback_url")
    return (row.value if row and row.value else "")


def _amount_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{a:,} تومان", callback_data=f"zp_amount:{a}")] for a in _AMOUNTS]
    rows.append([_BACK])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "zarinpal_pay")
async def cb_zarinpal_pay(cb: CallbackQuery, user: User, session: AsyncSession):
    if not settings.ZARINPAL_MERCHANT_ID:
        await cb.answer("درگاه ریالی فعال نیست.", show_alert=True)
        return
    if not user.is_kyc_verified:
        await cb.answer(_KYC_MSG, show_alert=True)
        return
    await cb.message.edit_text(
        "<b>شارژ کیف پول — درگاه ریالی</b>\n\nمبلغ مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=_amount_kb(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("zp_amount:"))
async def cb_zp_amount(cb: CallbackQuery, user: User, session: AsyncSession):
    if not user.is_kyc_verified:
        await cb.answer(_KYC_MSG, show_alert=True)
        return

    amount = int(cb.data.split(":")[1])
    callback_url = await _callback_url(session)
    if not callback_url:
        await cb.answer("آدرس بازگشت درگاه تنظیم نشده. با پشتیبانی تماس بگیرید.", show_alert=True)
        return

    await cb.answer("⏳ در حال اتصال به درگاه...")

    order = PaymentOrder(user_id=user.id, amount=float(amount), gateway="zarinpal", status="pending")
    session.add(order)
    await session.flush()

    sep = "&" if "?" in callback_url else "?"
    cb_url = f"{callback_url}{sep}order={order.id}"

    client = ZarinpalClient()
    try:
        authority = await client.request_payment(
            amount_toman=amount,
            callback_url=cb_url,
            description=f"شارژ کیف پول — کاربر {user.telegram_id}",
            mobile=user.phone_number or None,
            email=user.email or None,
            auto_verify=False,                   # we call verify ourselves in the callback
            card_pan=(user.extra_data or {}).get("card_pan"),  # lock to the verified card
        )
    except ZarinpalError as exc:
        await cb.message.edit_text(
            f"{ERR} خطا در ساخت پرداخت:\n<code>{exc}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_BACK]]),
        )
        return

    order.authority = authority
    await session.flush()

    pay_url = client.startpay_url(authority)
    await cb.message.edit_text(
        f"<b>پرداخت ریالی</b>\n\n"
        f"مبلغ: <b>{amount:,} تومان</b>\n\n"
        "برای پرداخت روی دکمه زیر بزنید.\n"
        "پس از پرداخت موفق، موجودی به‌صورت خودکار شارژ می‌شود.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="پرداخت", url=pay_url)],
            [_BACK],
        ]),
    )
