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
import time
from datetime import datetime, timedelta, timezone

import aiohttp
from celery.signals import worker_ready

from bot.config import settings

# Iran is permanently UTC+3:30 (DST abolished in 2022) — use a fixed offset so we
# don't depend on the system tz database for display strings.
_TEHRAN = timezone(timedelta(hours=3, minutes=30))

# Hard guard: never update more than once within this window, regardless of what
# triggered the task (schedule catch-up on restart, accidental double-fire, etc.).
_MIN_INTERVAL_SECONDS = 7 * 3600
from bot.tasks.celery_app import app

logger = logging.getLogger(__name__)

_NAVASAN_URL = "http://api.navasan.tech/latest/"

# Navasan returns rates in Toman — sane bounds to reject bad/garbage data
_MIN_TOMAN = 50_000
_MAX_TOMAN = 2_000_000

# item keys read from the single /latest/ response
_USD_ITEM = "usd_sell"   # دلار تهران (فروش) — used for wallet top-ups
_EUR_ITEM = "eur"        # یورو — reserved for foreign servers
_RUB_ITEM = "rub"        # روبل — بیلینگ تایم‌وب؛ اگر Navasan نداشت، نرخ دستی ادمین مبناست

# روبل خیلی ارزان‌تر از دلار/یورو است — بازه‌ی اعتبارسنجی جدا
_RUB_MIN_TOMAN = 100
_RUB_MAX_TOMAN = 50_000


def _extract(data: dict, item: str, lo: int = _MIN_TOMAN, hi: int = _MAX_TOMAN) -> int | None:
    """Pull a Toman price out of a Navasan item node, with range validation."""
    node = data.get(item) if isinstance(data, dict) else None
    if not isinstance(node, dict):
        return None
    raw = str(node.get("value", "")).replace(",", "").strip()
    try:
        val = int(float(raw))
    except (ValueError, TypeError):
        return None
    if lo <= val <= hi:
        return val
    logger.warning("exchange_rate: %s value %s out of range", item, val)
    return None


async def _fetch_rates() -> tuple[int, int, int | None] | None:
    """Return (usd_toman, eur_toman, rub_toman|None) from a SINGLE Navasan request."""
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
    # روبل اختیاری است: نبودش نباید آپدیت دلار/یورو را بشکند (fallback = نرخ دستی)
    rub = _extract(data, _RUB_ITEM, lo=_RUB_MIN_TOMAN, hi=_RUB_MAX_TOMAN)
    if usd is None or eur is None:
        logger.warning("exchange_rate: missing rate (usd=%s eur=%s)", usd, eur)
        return None
    logger.info("exchange_rate: Navasan→ usd=%s eur=%s rub=%s Toman", usd, eur, rub)
    return usd, eur, rub


def _diff_text(old: float | None, new: int) -> str:
    if old is None:
        return ""
    d = new - int(old)
    if d == 0:
        return ""
    sign = "+" if d > 0 else ""
    return f" ({sign}{d:,.0f})"


async def _do_update(only_if_empty: bool = False) -> None:
    from bot.database.session import AsyncSessionFactory, engine
    from bot.database.models import BotSettings
    from aiogram import Bot

    try:
        await engine.dispose(close=False)
    except Exception:
        pass

    async with AsyncSessionFactory() as session:
        rate_row = await session.get(BotSettings, "np_usd_to_irt_rate")
        ts_row = await session.get(BotSettings, "exrate_updated_ts")
    rate_set = bool(rate_row and rate_row.value)
    try:
        last_ts = float(ts_row.value) if (ts_row and ts_row.value) else None
    except (ValueError, TypeError):
        last_ts = None

    # Startup trigger: only fetch on a fresh install (rate never set), so restarts
    # don't re-fetch.
    if only_if_empty and rate_set:
        logger.info("exchange_rate: startup skip — rate already set (%s)", rate_row.value)
        return

    # Hard min-interval guard: skip any run too soon after the last one. Legit
    # scheduled runs are 8h apart (> 7h) so they always pass; early re-fires don't.
    if last_ts is not None:
        elapsed = time.time() - last_ts
        if elapsed < _MIN_INTERVAL_SECONDS:
            logger.info("exchange_rate: skip — only %.1fh since last update", elapsed / 3600)
            return

    rates = await _fetch_rates()
    if rates is None:
        return
    usd, eur, rub = rates

    old_usd: float | None = None
    old_eur: float | None = None
    old_rub: float | None = None

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

        # روبل (بیلینگ تایم‌وب): فقط وقتی Navasan داد آپدیت می‌شود —
        # نبودش، نرخ دستی/قبلی ادمین را دست‌نخورده می‌گذارد
        if rub is not None:
            rub_row = await session.get(BotSettings, "np_rub_to_irt_rate")
            if rub_row:
                old_rub = float(rub_row.value) if rub_row.value else None
                rub_row.value = str(rub)
            else:
                session.add(BotSettings(key="np_rub_to_irt_rate", value=str(rub)))

        now_dt = datetime.now(_TEHRAN)
        for key, val in (
            ("exrate_updated_at", now_dt.strftime("%Y/%m/%d %H:%M")),
            ("exrate_updated_ts", str(int(time.time()))),
        ):
            row = await session.get(BotSettings, key)
            if row:
                row.value = val
            else:
                session.add(BotSettings(key=key, value=val))

        gid_row = await session.get(BotSettings, "log_group_id")
        tid_row = await session.get(BotSettings, "log_topic_exchange_rate")
        await session.commit()

    logger.info("exchange_rate: saved usd=%s eur=%s rub=%s", usd, eur, rub)

    if not gid_row or not gid_row.value or not tid_row or not tid_row.value:
        return

    now = now_dt.strftime("%H:%M")
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        await bot.send_message(
            int(gid_row.value),
            f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> <b>نرخ ارز آپدیت شد</b>\n\n'
            f"دلار: <b>{usd:,.0f} تومان</b>{_diff_text(old_usd, usd)}\n"
            f"یورو: <b>{eur:,.0f} تومان</b>{_diff_text(old_eur, eur)}\n"
            + (f"روبل: <b>{rub:,.0f} تومان</b>{_diff_text(old_rub, rub)}\n" if rub is not None else "")
            + f"ساعت: {now}",
            parse_mode="HTML",
            message_thread_id=int(tid_row.value),
        )
    except Exception as exc:
        logger.warning("exchange_rate: log send failed: %s", exc)
    finally:
        await bot.session.close()


@app.task(name="bot.tasks.exchange_rate.update_exchange_rate")
def update_exchange_rate(only_if_empty: bool = False) -> None:
    asyncio.run(_do_update(only_if_empty=only_if_empty))


@worker_ready.connect
def _run_on_startup(sender=None, **kwargs):
    """On a FRESH install (no rate stored yet), fetch once so the panel isn't empty.
    On subsequent restarts the rate already exists → this is a no-op, and updates
    happen only on the fixed 8-hour schedule."""
    try:
        update_exchange_rate.delay(only_if_empty=True)
    except Exception as exc:  # pragma: no cover
        logger.warning("exchange_rate: startup trigger failed: %s", exc)
