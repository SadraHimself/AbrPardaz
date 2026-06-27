"""Fallback polling for NOWPayments — runs every 5 minutes via Celery beat.

Checks CryptoPayment records that are pending (not yet activated)
and have a known payment_id, then queries NOWPayments for their status.
This is a safety net in case the IPN webhook was missed.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from bot.tasks.celery_app import app

logger = logging.getLogger(__name__)

_PENDING_STATUSES = {"waiting", "confirming", "confirmed", "sending"}


async def _do_poll() -> None:
    from bot.database.session import engine, AsyncSessionFactory
    from bot.database.models import CryptoPayment
    from bot.services.nowpayments import NOWPaymentsClient, NOWPaymentsError
    from bot.services.crypto_billing import activate_crypto_payment
    from bot.config import settings
    from aiogram import Bot

    try:
        await engine.dispose(close=False)
    except Exception:
        pass

    if not settings.NP_API_KEY:
        return

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(CryptoPayment).where(
                CryptoPayment.activated.is_(False),
                CryptoPayment.payment_id.isnot(None),
                CryptoPayment.status.in_(list(_PENDING_STATUSES)),
            )
        )
        pending = list(result.scalars().all())

    if not pending:
        return

    logger.info("crypto_polling: checking %d pending payment(s)", len(pending))
    client = NOWPaymentsClient()
    bot = Bot(token=settings.BOT_TOKEN)

    try:
        for cp in pending:
            try:
                data = await client.get_payment_status(cp.payment_id)
                new_status = data.get("payment_status", "")
            except NOWPaymentsError as exc:
                logger.warning("crypto_polling: failed to fetch %s: %s", cp.payment_id, exc)
                continue

            async with AsyncSessionFactory() as session:
                cp_fresh = await session.get(CryptoPayment, cp.id)
                if not cp_fresh or cp_fresh.activated:
                    continue
                cp_fresh.status = new_status
                if new_status == "finished":
                    await activate_crypto_payment(cp_fresh, session, bot)
                await session.commit()
    finally:
        await bot.session.close()


@app.task(name="bot.tasks.crypto_polling.poll_crypto_payments")
def poll_crypto_payments() -> None:
    asyncio.run(_do_poll())
