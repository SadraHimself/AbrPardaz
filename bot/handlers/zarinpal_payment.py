"""Rial wallet top-up via Zarinpal — restricted to fully KYC-verified users."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import BotSettings, PaymentOrder, User
from bot.services.zarinpal import ZarinpalClient, ZarinpalError

from bot.utils.loading import ERR

router = Router(name="zarinpal_payment")
logger = logging.getLogger(__name__)

_AMOUNTS = [300_000, 500_000, 1_000_000, 5_000_000, 10_000_000]
# حداقلِ پذیرشِ زرین‌پال برای شارژ کیف پول؛ سقف = حد خودِ درگاه (خطای -41)
_MIN_AMOUNT = 300_000
_MAX_AMOUNT = 100_000_000
_CUSTOM_ICON = "6021858463288663100"
_KYC_MSG = "برای پرداخت ریالی ابتدا باید احراز هویت کنید."

_BACK = InlineKeyboardButton(text="بازگشت", callback_data="wallet",
                             **{"icon_custom_emoji_id": "5258236805890710909"})

# ارقام فارسی/عربی + جداکننده‌های هزارگان → عددِ خام
_DIGIT_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_STRIP_CHARS = (",", "٬", "،", ".", "٫", " ", "‏", "‎", "_", "-")


class ZarinpalFSM(StatesGroup):
    custom_amount = State()


def _parse_amount(text: str) -> int | None:
    """مبلغِ تایپ‌شده را به عدد صحیح تبدیل می‌کند (None = ورودی نامعتبر)."""
    raw = (text or "").strip().translate(_DIGIT_TRANS)
    raw = raw.replace("تومان", "").replace("تومن", "")
    for ch in _STRIP_CHARS:
        raw = raw.replace(ch, "")
    return int(raw) if raw.isdigit() and raw else None


async def _callback_url(session: AsyncSession) -> str:
    if settings.ZARINPAL_CALLBACK_URL:
        return settings.ZARINPAL_CALLBACK_URL
    row = await session.get(BotSettings, "zarinpal_callback_url")
    return (row.value if row and row.value else "")


def _amount_kb() -> InlineKeyboardMarkup:
    """مبالغ آماده دو-ستونی، «مبلغ دلخواه» سبز بالای بازگشت."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for a in _AMOUNTS:
        row.append(InlineKeyboardButton(text=f"{a:,} تومان", callback_data=f"zp_amount:{a}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        text="مبلغ دلخواه",
        callback_data="zp_custom",
        **{"style": "success", "icon_custom_emoji_id": _CUSTOM_ICON},
    )])
    rows.append([_BACK])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _checkout(user: User, session: AsyncSession, amount: int) -> tuple[str, InlineKeyboardMarkup]:
    """سفارش را می‌سازد و به درگاه وصل می‌شود؛ (متن، کیبورد) آماده‌ی نمایش برمی‌گردد."""
    callback_url = await _callback_url(session)
    if not callback_url:
        return (
            f"{ERR} آدرس بازگشت درگاه تنظیم نشده. با پشتیبانی تماس بگیرید.",
            InlineKeyboardMarkup(inline_keyboard=[[_BACK]]),
        )

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
        return (
            f"{ERR} خطا در ساخت پرداخت:\n<code>{html.escape(str(exc))}</code>",
            InlineKeyboardMarkup(inline_keyboard=[[_BACK]]),
        )

    order.authority = authority
    await session.flush()

    return (
        f"<b>پرداخت ریالی</b>\n\n"
        f"مبلغ: <b>{amount:,} تومان</b>\n\n"
        "برای پرداخت روی دکمه زیر بزنید.\n"
        "پس از پرداخت موفق، موجودی به‌صورت خودکار شارژ می‌شود.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="پرداخت", url=client.startpay_url(authority))],
            [_BACK],
        ]),
    )


@router.callback_query(F.data == "zarinpal_pay")
async def cb_zarinpal_pay(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    if not settings.ZARINPAL_MERCHANT_ID:
        await cb.answer("درگاه ریالی فعال نیست.", show_alert=True)
        return
    if not user.is_kyc_verified:
        await cb.answer(_KYC_MSG, show_alert=True)
        return
    await state.clear()
    await cb.message.edit_text(
        "<b>شارژ کیف پول — درگاه ریالی</b>\n\nمبلغ مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=_amount_kb(),
    )
    await cb.answer()


# ── مبلغ دلخواه ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "zp_custom")
async def cb_zp_custom(cb: CallbackQuery, user: User, state: FSMContext):
    if not user.is_kyc_verified:
        await cb.answer(_KYC_MSG, show_alert=True)
        return
    await state.set_state(ZarinpalFSM.custom_amount)
    await cb.message.edit_text(
        "<b>شارژ کیف پول — مبلغ دلخواه</b>\n\n"
        "مبلغ مورد نظر را به <b>تومان</b> بفرستید.\n\n"
        f"حداقل: <b>{_MIN_AMOUNT:,} تومان</b>\n"
        f"حداکثر: <b>{_MAX_AMOUNT:,} تومان</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="انصراف", callback_data="zp_custom_cancel",
                                 **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"}),
        ]]),
    )
    await cb.answer()


# بدون فیلتر state — اگر state به هر دلیلی از دست رفته باشد دکمه نباید بی‌اثر شود
@router.callback_query(F.data == "zp_custom_cancel")
async def cb_zp_custom_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "<b>شارژ کیف پول — درگاه ریالی</b>\n\nمبلغ مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=_amount_kb(),
    )
    await cb.answer()


@router.message(ZarinpalFSM.custom_amount)
async def msg_zp_custom_amount(message: Message, user: User, state: FSMContext, session: AsyncSession):
    if not user.is_kyc_verified:
        await state.clear()
        await message.answer(f"{ERR} {_KYC_MSG}", parse_mode="HTML")
        return

    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer(f"{ERR} مبلغ نامعتبر است. فقط عدد بفرستید.", parse_mode="HTML")
        return
    if amount < _MIN_AMOUNT:
        await message.answer(
            f"{ERR} حداقل مبلغ شارژ <b>{_MIN_AMOUNT:,} تومان</b> است.",
            parse_mode="HTML",
        )
        return
    if amount > _MAX_AMOUNT:
        await message.answer(
            f"{ERR} حداکثر مبلغ هر پرداخت <b>{_MAX_AMOUNT:,} تومان</b> است.",
            parse_mode="HTML",
        )
        return

    await state.clear()
    wait = await message.answer(
        '‏<tg-emoji emoji-id="5386367538735104399">⌛️</tg-emoji> در حال اتصال به درگاه...',
        parse_mode="HTML",
    )
    text, kb = await _checkout(user, session, amount)
    await wait.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── مبالغ آماده ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("zp_amount:"))
async def cb_zp_amount(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    if not user.is_kyc_verified:
        await cb.answer(_KYC_MSG, show_alert=True)
        return

    amount = int(cb.data.split(":")[1])
    await state.clear()
    await cb.answer("⏳ در حال اتصال به درگاه...")

    text, kb = await _checkout(user, session, amount)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
