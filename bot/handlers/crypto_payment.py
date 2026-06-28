"""Crypto wallet top-up via NOWPayments Direct Payment flow."""
from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import BotSettings, CryptoPayment, User
from bot.services.nowpayments import NOWPaymentsClient, NOWPaymentsError

router = Router(name="crypto_payment")

_USD_AMOUNTS = [3, 10, 20, 50, 100]
_AMOUNT_ICONS = {
    3:   "5803336218099848042",
    10:  "5800901723262293755",
    20:  "5453902265922376865",
    50:  "5447203607294265305",
    100: "5440539497383087970",
}
_BACK_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="بازگشت به کیف پول", callback_data="wallet", **{"icon_custom_emoji_id": "5933748020960038714"})
]])


async def _get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(BotSettings, key)
    return row.value if row else None


def _amount_kb() -> InlineKeyboardMarkup:
    # largest amount at top, smallest at bottom
    rows = [
        [InlineKeyboardButton(text=f"{a}$", callback_data=f"np_amount:{a}", **{"icon_custom_emoji_id": _AMOUNT_ICONS[a]})]
        for a in reversed(_USD_AMOUNTS)
    ]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="wallet", **{"icon_custom_emoji_id": "5933748020960038714"})])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _make_qr_bytes(data: str) -> bytes:
    import qrcode  # lazy import — only used when payment is created
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.callback_query(F.data == "crypto_pay")
async def cb_crypto_pay(cb: CallbackQuery, session: AsyncSession):
    if not settings.NP_API_KEY:
        await cb.answer("درگاه کریپتو فعال نیست.", show_alert=True)
        return

    rate_str = await _get_setting(session, "np_usd_to_irt_rate") or "60000"
    rate = float(rate_str)

    lines = [
        '<tg-emoji emoji-id="5769403330761593044">👛</tg-emoji> <b>شارژ کیف پول با کریپتو</b>\n',
        "مبلغ دلاری را انتخاب کنید:",
    ]
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_amount_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("np_amount:"))
async def cb_np_amount(cb: CallbackQuery, user: User, session: AsyncSession):
    amount_usd = int(cb.data.split(":")[1])

    rate_str = await _get_setting(session, "np_usd_to_irt_rate") or "60000"
    rate = float(rate_str)
    amount_irt = amount_usd * rate

    ipn_url = await _get_setting(session, "np_webhook_url") or ""
    if not ipn_url:
        await cb.answer(
            "❌ آدرس webhook تنظیم نشده.\nادمین باید np_webhook_url را از پنل ادمین ست کند.",
            show_alert=True,
        )
        return

    await cb.answer("⏳ در حال ساخت آدرس پرداخت...")

    order_id = f"np_{user.telegram_id}_{int(time.time())}"
    client = NOWPaymentsClient()
    try:
        payment = await client.create_payment(
            amount_usd=float(amount_usd),
            order_id=order_id,
            description=f"شارژ کیف پول {amount_usd}$ — کاربر {user.telegram_id}",
            ipn_callback_url=ipn_url,
        )
    except NOWPaymentsError as exc:
        await cb.message.edit_text(
            f"❌ خطا در ساخت درخواست پرداخت:\n<code>{exc}</code>",
            parse_mode="HTML",
            reply_markup=_BACK_KB,
        )
        return

    pay_address = payment.get("pay_address", "")
    pay_amount = float(payment.get("pay_amount") or 0)
    pay_currency = (payment.get("pay_currency") or settings.NP_OUTCOME_CURRENCY).upper()
    payment_id = str(payment.get("payment_id", ""))
    expiry_dt = datetime.now(timezone.utc) + timedelta(minutes=10)

    cp = CryptoPayment(
        user_id=user.id,
        chat_id=user.telegram_id,
        order_id=order_id,
        payment_id=payment_id,
        amount_usd=float(amount_usd),
        amount_irt=amount_irt,
        pay_address=pay_address,
        pay_amount=pay_amount,
        pay_currency=pay_currency.lower(),
        expires_at=expiry_dt,
        status="waiting",
        activated=False,
    )
    session.add(cp)
    await session.flush()

    caption = (
        f"<b>آدرس واریز کریپتو</b>\n\n"
        f"مبلغ: <b>{amount_usd}$</b>\n"
        f"معادل: <b>{amount_irt:,.0f} تومان</b>\n\n"
        f"شبکه: <b>TRON (TRC20)</b>\n\n"
        f"مبلغ دقیق:\n"
        f"<code>{pay_amount} {pay_currency}</code>\n\n"
        f"آدرس واریز:\n"
        f"<code>{pay_address}</code>\n\n"
        f"شماره فاکتور: <code>{payment_id}</code>\n\n"
        f"⏳ تا 10 دقیقه معتبره.\n\n"
        f"⚠️ <b>دقیقاً همین مبلغ را ارسال کنید.</b>\n"
        f"پس از تأیید شبکه، موجودی به‌صورت خودکار شارژ می‌شود."
    )

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="بازگشت به کیف پول", callback_data="wallet", **{"icon_custom_emoji_id": "5933748020960038714"})]
    ])

    try:
        qr_bytes = _make_qr_bytes(pay_address)
        photo = BufferedInputFile(qr_bytes, filename="qr.png")
        await cb.message.answer_photo(
            photo=photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=back_kb,
        )
        await cb.message.delete()
    except Exception:
        # fallback: show as text if QR generation fails
        await cb.message.edit_text(caption, parse_mode="HTML", reply_markup=back_kb)
