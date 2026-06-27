"""Crypto wallet top-up via NOWPayments invoice flow."""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import BotSettings, CryptoPayment, User
from bot.services.nowpayments import NOWPaymentsClient, NOWPaymentsError
from bot.utils.loading import edit_loading

router = Router(name="crypto_payment")

_USD_AMOUNTS = [5, 10, 20, 50, 100]
_BACK_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🔙 بازگشت", callback_data="wallet")
]])


async def _get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(BotSettings, key)
    return row.value if row else None


def _amount_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"💵 {a}$", callback_data=f"np_amount:{a}")]
        for a in _USD_AMOUNTS
    ]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="wallet")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "crypto_pay")
async def cb_crypto_pay(cb: CallbackQuery, session: AsyncSession):
    if not settings.NP_API_KEY:
        await cb.answer("درگاه کریپتو فعال نیست.", show_alert=True)
        return

    rate_str = await _get_setting(session, "np_usd_to_irt_rate") or "60000"
    rate = float(rate_str)

    lines = ["💎 <b>شارژ کیف پول با کریپتو</b>\n",
             "مبلغ دلاری را انتخاب کنید:\n"]
    for a in _USD_AMOUNTS:
        lines.append(f"• {a}$  ≈  {a * rate:,.0f} تومان")

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
            "❌ آدرس webhook تنظیم نشده.\nادمین باید np_webhook_url را ست کند.",
            show_alert=True,
        )
        return

    await edit_loading(cb.message)
    await cb.answer()

    order_id = f"np_{user.telegram_id}_{int(time.time())}"
    bot_username = (await cb.bot.get_me()).username

    client = NOWPaymentsClient()
    try:
        invoice = await client.create_invoice(
            amount_usd=float(amount_usd),
            order_id=order_id,
            description=f"شارژ کیف پول {amount_usd}$ — کاربر {user.telegram_id}",
            ipn_callback_url=ipn_url,
            success_url=f"https://t.me/{bot_username}",
        )
    except NOWPaymentsError as exc:
        await cb.message.edit_text(
            f"❌ خطا در ساخت فاکتور:\n<code>{exc}</code>",
            parse_mode="HTML",
            reply_markup=_BACK_KB,
        )
        return

    cp = CryptoPayment(
        user_id=user.id,
        chat_id=user.telegram_id,
        order_id=order_id,
        invoice_id=str(invoice.get("id", "")),
        amount_usd=float(amount_usd),
        amount_irt=amount_irt,
        status="waiting",
        activated=False,
    )
    session.add(cp)
    await session.flush()

    invoice_url = invoice.get("invoice_url", "")
    await cb.message.edit_text(
        f"💎 <b>فاکتور کریپتو ساخته شد</b>\n\n"
        f"💵 مبلغ: <b>{amount_usd}$</b>\n"
        f"💰 معادل: <b>{amount_irt:,.0f} تومان</b>\n\n"
        f"⏳ لینک پرداخت ۶۰ دقیقه اعتبار دارد.\n"
        f"پس از پرداخت، موجودی کیف پول به‌صورت خودکار شارژ می‌شود.\n\n"
        f"🔑 شناسه: <code>{order_id}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 پرداخت با کریپتو", url=invoice_url)],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="wallet")],
        ]),
    )
