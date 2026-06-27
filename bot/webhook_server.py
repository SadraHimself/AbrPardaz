"""aiohttp server for NOWPayments IPN callbacks.

Runs alongside the aiogram bot in the same asyncio event loop.
Endpoint: POST /np-webhook
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from aiohttp import web
from sqlalchemy import select

from bot.config import settings
from bot.database.models import CryptoPayment
from bot.database.session import AsyncSessionFactory
from bot.services.crypto_billing import activate_crypto_payment

logger = logging.getLogger(__name__)


def _verify_hmac(raw_body: bytes, received_sig: str) -> bool:
    if not settings.NP_IPN_SECRET or not received_sig:
        return False
    # Signature is computed on sorted-keys JSON (per NOWPayments docs)
    body_dict = json.loads(raw_body)
    sorted_json = json.dumps(body_dict, sort_keys=True, separators=(",", ":"))
    expected = hmac.new(
        settings.NP_IPN_SECRET.encode(),
        sorted_json.encode(),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, received_sig)


async def _handle_ipn(request: web.Request) -> web.Response:
    raw_body = await request.read()
    sig = request.headers.get("x-nowpayments-sig", "")

    if not _verify_hmac(raw_body, sig):
        logger.warning("IPN HMAC mismatch — possible forgery from %s", request.remote)
        return web.Response(status=403, text="invalid signature")

    try:
        data = json.loads(raw_body)
    except Exception:
        return web.Response(status=400, text="bad json")

    payment_status = data.get("payment_status", "")
    order_id = data.get("order_id", "")
    payment_id = str(data.get("payment_id", ""))

    logger.info("IPN: order=%s status=%s payment_id=%s", order_id, payment_status, payment_id)

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(CryptoPayment).where(CryptoPayment.order_id == order_id)
        )
        cp = result.scalar_one_or_none()

        if not cp:
            logger.warning("IPN: CryptoPayment not found for order_id=%s", order_id)
            return web.Response(status=200, text="ok")

        # Always update payment_id when we first learn it
        if payment_id and not cp.payment_id:
            cp.payment_id = payment_id

        if cp.activated:
            logger.info("IPN: already activated order=%s — skipping", order_id)
            await session.commit()
            return web.Response(status=200, text="ok")

        # Update status for non-final states
        cp.status = payment_status

        if payment_status == "finished":
            bot = request.app["bot"]
            await activate_crypto_payment(cp, session, bot)

        await session.commit()

    return web.Response(status=200, text="ok")


def create_webhook_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/np-webhook", _handle_ipn)
    return app
