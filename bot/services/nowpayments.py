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
    """Return True if signature is valid, or if NP_IPN_SECRET is not configured (open mode)."""
    if not settings.NP_IPN_SECRET:
        return True  # secret not configured — accept all (warned at startup)
    if not received_sig:
        logger.warning("IPN received with no x-nowpayments-sig header")
        return False
    body_dict = json.loads(raw_body)
    # NOWPayments signs sorted-keys compact JSON (no spaces)
    sorted_compact = json.dumps(body_dict, sort_keys=True, separators=(",", ":"))
    expected_compact = hmac.new(
        settings.NP_IPN_SECRET.encode(),
        sorted_compact.encode(),
        hashlib.sha512,
    ).hexdigest()
    if hmac.compare_digest(expected_compact, received_sig.lower()):
        return True
    # Fallback: try default separators (spaces) in case their side serializes differently
    sorted_spaced = json.dumps(body_dict, sort_keys=True)
    expected_spaced = hmac.new(
        settings.NP_IPN_SECRET.encode(),
        sorted_spaced.encode(),
        hashlib.sha512,
    ).hexdigest()
    if hmac.compare_digest(expected_spaced, received_sig.lower()):
        return True
    logger.warning(
        "IPN HMAC mismatch — order=%s sig_received=%s sig_compact=%s",
        body_dict.get("order_id", "?"), received_sig[:16] + "…", expected_compact[:16] + "…",
    )
    return False


async def _handle_ipn(request: web.Request) -> web.Response:
    raw_body = await request.read()
    sig = request.headers.get("x-nowpayments-sig", "")
    logger.info("IPN POST received from %s body_len=%d has_sig=%s", request.remote, len(raw_body), bool(sig))

    if not _verify_hmac(raw_body, sig):
        logger.warning("IPN rejected from %s", request.remote)
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
