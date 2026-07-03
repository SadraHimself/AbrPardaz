"""8-hourly USD & EUR rate updater.

Fetches both rates in a SINGLE Navasan API request (/latest/) to conserve the
free 120 req/month quota (3 req/day → ~90/month), then stores them in
BotSettings (np_usd_to_irt_rate / np_eur_to_irt_rate) and logs to the
exchange-rate forum topic.

USD is used for wallet top-ups (crypto → Toman); EUR is reserved for future
monthly billing of foreign servers.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import aiohttp
from celery.signals import worker_ready

from bot.config import settings
from bot.tasks.celery_app import app

logger = logging.getLogger(__name__)

_NAVASAN_URL = "http://api.navasan.tech/latest/"

# Navasan returns rates in Toman — sane bounds to reject bad/garbage data
_MIN_TOMAN = 50_000
_MAX_TOMAN = 2_000_000

# item keys read from the single /latest/ response
_USD_ITEM = "usd_sell"   # دلار تهران (فروش) — used for wallet top-ups
_EUR_ITEM = "eur"        # یورو — reserved for foreign servers


def _extract(data: dict, item: str) -> int | None:
    """Pull a Toman price out of a Navasan item node, with range validation."""
    node = data.get(item) if isinstance(data, dict) else None
    if not isinstance(node, dict):
        return None
    raw = str(node.get("value", "")).replace(",", "").strip()
    try:
        val = int(float(raw))
    except (ValueError, TypeError):
        return None
    if _MIN_TOMAN <= val <= _MAX_TOMAN:
        return val
    logger.warning("exchange_rate: %s value %s out of range", item, val)
    return None


async def _fetch_rates() -> tuple[int, int] | None:
    """Return (usd_toman, eur_toman) from a SINGLE Navasan request."""
    api_key = settings.NAVASAN_API_KEY
    if not api_key:
        logger.warning("exchange_rate: NAVASAN_API_KEY is not set")
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _NAVASAN_URL,
                params={"api_key": api_key},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    logger.warning("exchange_rate: Navasan HTTP %s: %s", resp.status, body)
                    return None
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.error("exchange_rate: fetch error: %s", exc)
        return None

    usd = _extract(data, _USD_ITEM)
    eur = _extract(data, _EUR_ITEM)
    if usd is None or eur is None:
        logger.warning("exchange_rate: missing rate (usd=%s eur=%s)", usd, eur)
        return None
    logger.info("exchange_rate: Navasan→ usd=%s eur=%s Toman", usd, eur)
    return usd, eur


def _diff_text(old: float | None, new: int) -> str:
    if old is None:
        return ""
    d = new - int(old)
    if d == 0:
        return ""
    sign = "+" if d > 0 else ""
    return f" ({sign}{d:,.0f})"


async def _do_update() -> None:
    from bot.database.session import AsyncSessionFactory, engine
    from bot.database.models import BotSettings
    from aiogram import Bot

    try:
        await engine.dispose(close=False)
    except Exception:
        pass

    rates = await _fetch_rates()
    if rates is None:
        return
    usd, eur = rates

    old_usd: float | None = None
    old_eur: float | None = None

    async with AsyncSessionFactory() as session:
        usd_row = await session.get(BotSettings, "np_usd_to_irt_rate")
        if usd_row:
            old_usd = float(usd_row.value) if usd_row.value else None
            usd_row.value = str(usd)
        else:
            session.add(BotSettings(key="np_usd_to_irt_rate", value=str(usd)))

        eur_row = await session.get(BotSettings, "np_eur_to_irt_rate")
        if eur_row:
            old_eur = float(eur_row.value) if eur_row.value else None
            eur_row.value = str(eur)
        else:
            session.add(BotSettings(key="np_eur_to_irt_rate", value=str(eur)))

        now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
        ts_row = await session.get(BotSettings, "exrate_updated_at")
        if ts_row:
            ts_row.value = now_str
        else:
            session.add(BotSettings(key="exrate_updated_at", value=now_str))

        gid_row = await session.get(BotSettings, "log_group_id")
        tid_row = await session.get(BotSettings, "log_topic_exchange_rate")
        await session.commit()

    logger.info("exchange_rate: saved usd=%s eur=%s", usd, eur)

    if not gid_row or not gid_row.value or not tid_row or not tid_row.value:
        return

    now = datetime.now().strftime("%H:%M")
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        await bot.send_message(
            int(gid_row.value),
            f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> <b>نرخ ارز آپدیت شد</b>\n\n'
            f"دلار: <b>{usd:,.0f} تومان</b>{_diff_text(old_usd, usd)}\n"
            f"یورو: <b>{eur:,.0f} تومان</b>{_diff_text(old_eur, eur)}\n"
            f"ساعت: {now}",
            parse_mode="HTML",
            message_thread_id=int(tid_row.value),
        )
    except Exception as exc:
        logger.warning("exchange_rate: log send failed: %s", exc)
    finally:
        await bot.session.close()


@app.task(name="bot.tasks.exchange_rate.update_exchange_rate")
def update_exchange_rate() -> None:
    asyncio.run(_do_update())


@worker_ready.connect
def _run_on_startup(sender=None, **kwargs):
    """Refresh rates the moment the worker comes up (i.e. on deploy/restart),
    so the 8-hour cycle starts from the exact time the bot is updated."""
    try:
        update_exchange_rate.delay()
    except Exception as exc:  # pragma: no cover
        logger.warning("exchange_rate: startup trigger failed: %s", exc)
