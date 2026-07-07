"""aiohttp server for NOWPayments IPN callbacks.

Runs alongside the aiogram bot in the same asyncio event loop.
Endpoint: POST /np-webhook
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from aiohttp import web
from sqlalchemy import select

from bot.config import settings
from bot.database.models import CryptoPayment, PaymentOrder, Transaction, TransactionType, User
from bot.database.session import AsyncSessionFactory
from bot.services.crypto_billing import activate_crypto_payment
from bot.services.log_service import LogService
from bot.services.zarinpal import ZarinpalClient

logger = logging.getLogger(__name__)


_RESULT_PAGE = """<!doctype html>
<html lang="fa" dir="rtl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',Tahoma,Arial,sans-serif;min-height:100vh;display:flex;
    align-items:center;justify-content:center;padding:20px;
    background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);color:#e2e8f0}}
  .card{{background:#1e293b;border:1px solid #334155;border-radius:22px;padding:44px 32px;
    max-width:420px;width:100%;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,.45);
    animation:pop .35s ease}}
  @keyframes pop{{from{{transform:scale(.92);opacity:0}}to{{transform:scale(1);opacity:1}}}}
  .icon{{width:84px;height:84px;border-radius:50%;background:{accent};display:flex;
    align-items:center;justify-content:center;margin:0 auto 22px;font-size:44px;
    color:#fff;box-shadow:0 10px 28px {accent}55}}
  h1{{font-size:22px;margin-bottom:12px;font-weight:800}}
  p{{font-size:15px;color:#94a3b8;line-height:2}}
  .amount{{margin-top:18px;font-size:20px;font-weight:800;color:{accent}}}
  .btn{{display:inline-block;margin-top:26px;padding:13px 30px;background:{accent};
    color:#fff;border-radius:13px;text-decoration:none;font-size:15px;font-weight:700}}
</style></head>
<body><div class="card">
  <div class="icon">{glyph}</div>
  <h1>{title}</h1>
  <p>{msg}</p>
  {extra}
  {button}
</div></body></html>"""

_THEMES = {
    "success": ("#22c55e", "✓"),
    "fail":    ("#ef4444", "✕"),
    "pending": ("#f59e0b", "⏳"),
}


def _page(status: str, title: str, msg: str, bot_username: str = "", extra: str = "") -> web.Response:
    accent, glyph = _THEMES.get(status, ("#64748b", "•"))
    button = ""
    if bot_username:
        button = f'<a class="btn" href="https://t.me/{bot_username}">بازگشت به ربات</a>'
    html = _RESULT_PAGE.format(accent=accent, glyph=glyph, title=title, msg=msg,
                              extra=extra, button=button)
    return web.Response(text=html, content_type="text/html")


async def _bot_username(request: web.Request) -> str:
    """Cached bot username for the 'return to bot' button."""
    u = request.app.get("bot_username")
    if u is None:
        try:
            me = await request.app["bot"].get_me()
            u = me.username or ""
        except Exception:
            u = ""
        request.app["bot_username"] = u
    return u


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


async def _handle_zarinpal_callback(request: web.Request) -> web.Response:
    authority = request.query.get("Authority", "")
    status = request.query.get("Status", "")
    logger.info("Zarinpal callback: authority=%s status=%s", authority[:12] + "…" if authority else "-", status)

    uname = await _bot_username(request)

    if not authority:
        return _page("fail", "خطا", "اطلاعات بازگشت از درگاه ناقص است.", uname)

    bot = request.app["bot"]
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(PaymentOrder).where(
                PaymentOrder.authority == authority,
                PaymentOrder.gateway == "zarinpal",
            )
        )
        order = result.scalar_one_or_none()

        if not order:
            return _page("fail", "یافت نشد", "سفارش پرداخت یافت نشد.", uname)

        if order.status == "paid":
            return _page("success", "پرداخت موفق", "این پرداخت قبلاً تأیید شده است.", uname)

        if status != "OK":
            order.status = "failed"
            await session.commit()
            # Notify the user in the bot (most common cause with card lock on: the
            # payer's card doesn't match their national code, so Zarinpal blocked it).
            u = await session.get(User, order.user_id)
            if u:
                try:
                    await bot.send_message(
                        u.telegram_id,
                        '<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
                        "کاربر گرامی، پرداخت ریالی شما به‌خاطر عدم تطابق کارت بانکی با صاحب کد ملی رد شد.\n\n"
                        '‎<tg-emoji emoji-id="5258093637450866522">🤖</tg-emoji> @abrmakerbot',
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return _page("fail", "پرداخت ناموفق", "پرداخت انجام نشد یا کارت مجاز نبود.", uname)

        try:
            code, ref_id = await ZarinpalClient().verify(int(order.amount), authority)
        except Exception as exc:
            logger.warning("Zarinpal verify error authority=%s: %s", authority, exc)
            return _page("pending", "در حال بررسی", "پرداخت شما در حال بررسی است. اگر مبلغ کسر شده، به‌زودی شارژ می‌شود.", uname)

        if code in (100, 101):
            user = await session.get(User, order.user_id)
            if user and order.status != "paid":
                order.status = "paid"
                order.ref_id = ref_id
                order.paid_at = datetime.now(timezone.utc)
                user.balance += order.amount
                session.add(Transaction(
                    user_id=user.id,
                    amount=order.amount,
                    type=TransactionType.CREDIT,
                    description=f"شارژ زرین‌پال — {ref_id or authority}",
                    reference_id=ref_id or authority,
                ))
                await LogService(bot, session).log_wallet_charge(user, order.amount, user.balance)
                await session.commit()
                try:
                    await bot.send_message(
                        user.telegram_id,
                        '<tg-emoji emoji-id="5021905410089550576">✅</tg-emoji> <b>پرداخت موفق!</b>\n\n'
                        f"<b>{order.amount:,.0f} تومان</b> به کیف پول شما اضافه شد.\n"
                        f"شماره پیگیری: <code>{ref_id or '—'}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            amount_html = f'<div class="amount">{order.amount:,.0f} تومان</div>'
            return _page("success", "پرداخت موفق", "این مبلغ به کیف پول شما اضافه شد. به ربات بازگردید.",
                         uname, extra=amount_html)

        order.status = "failed"
        await session.commit()
        return _page("fail", "پرداخت ناموفق", f"تأیید پرداخت ناموفق بود (کد {code}).", uname)


def create_webhook_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/np-webhook", _handle_ipn)
    app.router.add_get("/zarinpal/callback", _handle_zarinpal_callback)
    return app
