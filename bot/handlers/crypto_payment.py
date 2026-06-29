"""Crypto wallet top-up via NOWPayments Direct Payment flow."""
from __future__ import annotations

import io
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import BotSettings, CryptoPayment, User
from bot.services.nowpayments import NOWPaymentsClient, NOWPaymentsError

router = Router(name="crypto_payment")
logger = logging.getLogger(__name__)

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

# Display names for NOWPayments currency codes
_CURRENCY_NAMES: dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "ltc": "LTC",
    "trx": "TRX",
    "ton": "TON",
    "bnbbsc": "BNB (BSC)",
    "usdttrc20": "USDT (TRC20)",
    "usdterc20": "USDT (ETH)",
    "usdtton": "USDT (TON)",
    "usdcbsc": "USDC (BSC)",
    "usdcerc20": "USDC (ETH)",
    "usdtbsc": "USDT (BSC)",
    "usdtpolygon": "USDT (Polygon)",
    "usdcpolygon": "USDC (Polygon)",
    "usdtsolana": "USDT (Solana)",
    "usdcsolana": "USDC (Solana)",
    "usdcarbitrum": "USDC (Arbitrum)",
    "usdtarbitrum": "USDT (Arbitrum)",
    "usdtop": "USDT (Optimism)",
    "usdcop": "USDC (Optimism)",
}

# Sort priority for currency list (most common first)
_CURRENCY_PRIORITY = [
    "usdttrc20", "usdtton", "usdterc20", "usdtbsc",
    "trx", "ton", "btc", "eth", "ltc", "bnbbsc",
    "usdcerc20", "usdcbsc", "usdtpolygon", "usdcpolygon",
]

# icon_custom_emoji_id per currency (only one ID allowed per button)
_CURRENCY_ICONS: dict[str, str] = {
    "bnbbsc":    "5152587319148020582",
    "btc":       "5829938927703691622",
    "ltc":       "5829979991886011025",
    "ton":       "5832365227743646230",
    "trx":       "5832692572971077565",
    "usdtbsc":   "5458525793921546124",
    "usdtton":   "5188672371648634636",
    "usdttrc20": "5397915949879801627",
}

# Network display name per currency code (shown on address page)
_NETWORK_DISPLAY: dict[str, str] = {
    "btc": "Bitcoin",
    "eth": "Ethereum (ERC20)",
    "ltc": "Litecoin",
    "trx": "TRON (TRC20)",
    "ton": "TON",
    "bnbbsc": "BNB Smart Chain (BSC)",
    "usdttrc20": "TRON (TRC20)",
    "usdterc20": "Ethereum (ERC20)",
    "usdtton": "TON",
    "usdcbsc": "BNB Smart Chain (BSC)",
    "usdcerc20": "Ethereum (ERC20)",
    "usdtbsc": "BNB Smart Chain (BSC)",
    "usdtpolygon": "Polygon",
    "usdcpolygon": "Polygon",
    "usdtsolana": "Solana",
    "usdcsolana": "Solana",
    "usdtarbitrum": "Arbitrum",
    "usdcarbitrum": "Arbitrum",
    "usdtop": "Optimism",
    "usdcop": "Optimism",
}


async def _get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(BotSettings, key)
    return row.value if row else None


def _amount_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{a}$", callback_data=f"np_amount:{a}", **{"icon_custom_emoji_id": _AMOUNT_ICONS[a]})]
        for a in reversed(_USD_AMOUNTS)
    ]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="wallet", **{"icon_custom_emoji_id": "5933748020960038714"})])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_CURRENCY_PAIRS = [
    ("usdttrc20", "trx"),
    ("usdtton",   "ton"),
    ("usdtbsc",   "bnbbsc"),
    ("ltc",       "btc"),
]


def _btn(coin: str, amount_usd: int) -> InlineKeyboardButton:
    kwargs: dict = {}
    icon = _CURRENCY_ICONS.get(coin)
    if icon:
        kwargs["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(
        text=_CURRENCY_NAMES.get(coin, coin.upper()),
        callback_data=f"np_cur:{amount_usd}:{coin}",
        **kwargs,
    )


def _currency_kb(amount_usd: int, coins: list[str]) -> InlineKeyboardMarkup:
    coins_set = set(coins)
    used: set[str] = set()
    rows = []

    for left, right in _CURRENCY_PAIRS:
        row = []
        if left in coins_set:
            row.append(_btn(left, amount_usd))
            used.add(left)
        if right in coins_set:
            row.append(_btn(right, amount_usd))
            used.add(right)
        if row:
            rows.append(row)

    # هر ارز اضافه‌ای که تو جفت‌ها نبود، تک‌تک پایین اضافه می‌شه
    for coin in coins:
        if coin not in used:
            rows.append([_btn(coin, amount_usd)])

    rows.append([InlineKeyboardButton(
        text="بازگشت",
        callback_data="crypto_pay",
        **{"icon_custom_emoji_id": "5933748020960038714"},
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _make_qr_bytes(data: str) -> bytes:
    import qrcode
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

    lines = [
        '<tg-emoji emoji-id="5769403330761593044">👛</tg-emoji> <b>شارژ کیف پول با کریپتو</b>\n',
        "مبلغ دلاری را انتخاب کنید:",
    ]
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_amount_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("np_amount:"))
async def cb_np_amount(cb: CallbackQuery, session: AsyncSession):
    amount_usd = int(cb.data.split(":")[1])
    await cb.answer("⏳ در حال دریافت ارزهای موجود...")

    client = NOWPaymentsClient()
    try:
        coins = [c.lower() for c in await client.get_merchant_coins()]
    except Exception:
        coins = []

    if not coins:
        coins = list(_CURRENCY_PRIORITY)

    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5769403330761593044">👛</tg-emoji> <b>شارژ کیف پول با کریپتو</b>\n\n'
        f"مبلغ: <b>{amount_usd}$</b>\n\n"
        f"ارز پرداختی را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=_currency_kb(amount_usd, coins),
    )


@router.callback_query(F.data.startswith("np_cur:"))
async def cb_np_currency(cb: CallbackQuery, user: User, session: AsyncSession):
    parts = cb.data.split(":", 2)
    amount_usd = int(parts[1])
    currency = parts[2]

    rate_str = await _get_setting(session, "np_usd_to_irt_rate") or "60000"
    rate = float(rate_str)
    amount_irt = amount_usd * rate

    ipn_url = (await _get_setting(session, "np_webhook_url") or "").strip()
    logger.info("crypto_payment: ipn_url=%r for order by user %s", ipn_url, user.telegram_id)
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
            pay_currency=currency,
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
    pay_currency_raw = (payment.get("pay_currency") or currency).lower()
    pay_currency_display = (payment.get("pay_currency") or currency).upper()
    payment_id = str(payment.get("payment_id", ""))
    network = _NETWORK_DISPLAY.get(pay_currency_raw, pay_currency_display)
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
        pay_currency=pay_currency_raw,
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
        f"شبکه: <b>{network}</b>\n\n"
        f"مبلغ دقیق:\n"
        f"<code>{pay_amount} {pay_currency_display}</code>\n\n"
        f"آدرس واریز:\n"
        f"<code>{pay_address}</code>\n\n"
        f"شماره فاکتور: <code>{payment_id}</code>\n\n"
        f"این فاکتور تا 10 دقیقه دیگر معتبر میباشد و بعد از آن باطل میشود.\n\n"
        f'<tg-emoji emoji-id="6008233706039284019">⚠️</tg-emoji> <b>کاربر گرامی، دقت داشته باشید که دقیقا مبلغ ذکر شده یا بیشتر را نیز ارسال کنید</b>\n'
        f"پس از تأیید شبکه، موجودی به‌صورت خودکار شارژ می‌شود."
    )

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="بازگشت به منو", callback_data=f"np_cancel:{cp.id}", **{"icon_custom_emoji_id": "5933748020960038714"})]
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
        await cb.message.edit_text(caption, parse_mode="HTML", reply_markup=back_kb)


@router.callback_query(F.data.startswith("np_cancel:"))
async def cb_np_cancel(cb: CallbackQuery, user: User, session: AsyncSession):
    cp_id = int(cb.data.split(":")[1])
    cp = await session.get(CryptoPayment, cp_id)

    invoice_ref = None
    if cp and cp.user_id == user.id and not cp.activated and cp.status not in ("finished", "expired", "cancelled"):
        invoice_ref = cp.payment_id or cp.order_id
        cp.status = "cancelled"
        await session.flush()

    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass

    if invoice_ref:
        await cb.message.answer(
            f'<tg-emoji emoji-id="6026320431798030131">❌</tg-emoji> فاکتور <code>{invoice_ref}</code> نیز کنسل شد',
            parse_mode="HTML",
        )
