"""Shared logic for activating a finished NOWPayments crypto payment."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import CryptoPayment, Transaction, TransactionType, User
from bot.services.log_service import LogService

logger = logging.getLogger(__name__)


async def activate_crypto_payment(cp: CryptoPayment, session: AsyncSession, bot) -> None:
    """Credit the user's wallet and send a Telegram confirmation.

    Safe to call multiple times — checks cp.activated before acting.
    Caller must commit the session after this returns (or use the session's
    auto-commit context manager).
    """
    if cp.activated:
        return

    user = await session.get(User, cp.user_id)
    if not user:
        logger.error("activate_crypto_payment: user %s not found for order %s", cp.user_id, cp.order_id)
        return

    user.balance += cp.amount_irt
    session.add(Transaction(
        user_id=user.id,
        amount=cp.amount_irt,
        type=TransactionType.CREDIT,
        description=f"شارژ کریپتو — {cp.amount_usd:.0f}$ — {cp.order_id}",
        reference_id=cp.order_id,
    ))
    cp.activated = True
    cp.status = "finished"

    await LogService(bot, session).log_crypto_charge(user, cp.amount_usd, cp.amount_irt, cp.order_id)

    try:
        await bot.send_message(
            cp.chat_id,
            f'<tg-emoji emoji-id="5021905410089550576">✅</tg-emoji> <b>پرداخت کریپتو تأیید شد!</b>\n\n'
            f"مبلغ: <b>{cp.amount_usd:.0f}$</b>\n"
            f"<b>{cp.amount_irt:,.0f} تومان</b> به کیف پول شما اضافه شد.",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Failed to notify user %s after crypto credit: %s", cp.chat_id, exc)
