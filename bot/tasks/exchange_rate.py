"""Hourly USD/IRR rate updater — scrapes tgju.org and updates np_usd_to_irt_rate."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import aiohttp

from bot.tasks.celery_app import app

logger = logging.getLogger(__name__)

_TGJU_API_URL = "https://api.tgju.org/v1/market/indicator/summary-table-data/price_dollar_rl"
_TGJU_HTML_URL = "https://www.tgju.org/profile/price_dollar_rl"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html",
}

# Sanity range for USD/Rial: 500,000 – 50,000,000
_MIN_RIAL = 500_000
_MAX_RIAL = 50_000_000

# HTML fallback patterns (used only if API fails)
_HTML_PATTERNS = [
    r'"p"\s*:\s*"([\d,]+)"',
    r'data-price=["\']?([\d,]+)["\']?',
    r'class="info-price[^"]*"[^>]*>([\d,]+)<',
]


async def _fetch_rate_toman() -> float | None:
    """Return USD price in Toman from tgju.org (tries JSON API first, HTML fallback)."""
    async with aiohttp.ClientSession() as session:
        # --- Try JSON API (more reliable) ---
        try:
            async with session.get(
                _TGJU_API_URL,
                headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    # Response: {"data": [{"p": "1,743,000", ...}, ...]}
                    rows = (data or {}).get("data") or []
                    if rows:
                        p_str = str(rows[0].get("p", "")).replace(",", "")
                        if p_str.isdigit():
                            rial = float(p_str)
                            if _MIN_RIAL <= rial <= _MAX_RIAL:
                                logger.info("exchange_rate: API→ %s Rial", rial)
                                return rial / 10
                            logger.warning("exchange_rate: API value %s out of range", rial)
        except Exception as exc:
            logger.warning("exchange_rate: API fetch error: %s", exc)

        # --- HTML fallback ---
        try:
            async with session.get(
                _TGJU_HTML_URL,
                headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning("exchange_rate: HTML fallback HTTP %s", resp.status)
                    return None
                html = await resp.text()
        except Exception as exc:
            logger.error("exchange_rate: HTML fetch error: %s", exc)
            return None

    for pattern in _HTML_PATTERNS:
        m = re.search(pattern, html)
        if m:
            price_str = m.group(1).replace(",", "")
            if not price_str.isdigit():
                continue
            rial = float(price_str)
            if _MIN_RIAL <= rial <= _MAX_RIAL:
                logger.info("exchange_rate: HTML→ %s Rial (pattern=%s)", rial, pattern[:20])
                return rial / 10
    logger.warning("exchange_rate: no valid price found in HTML")
    return None


async def _do_update() -> None:
    from bot.database.session import AsyncSessionFactory, engine
    from bot.database.models import BotSettings
    from bot.config import settings
    from aiogram import Bot

    try:
        await engine.dispose(close=False)
    except Exception:
        pass

    rate = await _fetch_rate_toman()
    if rate is None:
        return

    rate_rounded = round(rate)
    old_rate: float | None = None

    async with AsyncSessionFactory() as session:
        row = await session.get(BotSettings, "np_usd_to_irt_rate")
        if row:
            old_rate = float(row.value) if row.value else None
            row.value = str(rate_rounded)
        else:
            session.add(BotSettings(key="np_usd_to_irt_rate", value=str(rate_rounded)))

        gid_row = await session.get(BotSettings, "log_group_id")
        tid_row = await session.get(BotSettings, "log_topic_exchange_rate")
        await session.commit()

    logger.info("exchange_rate: %s → %s Toman/USD", old_rate, rate_rounded)

    if not gid_row or not gid_row.value or not tid_row or not tid_row.value:
        return

    change_text = ""
    if old_rate is not None:
        diff = rate_rounded - int(old_rate)
        if diff != 0:
            sign = "+" if diff > 0 else ""
            change_text = f"\nتغییر: <b>{sign}{diff:,.0f} تومان</b>"

    now = datetime.now().strftime("%H:%M")
    bot = Bot(token=settings.BOT_TOKEN)
    try:
        await bot.send_message(
            int(gid_row.value),
            f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> <b>نرخ دلار آپدیت شد</b>\n\n'
            f"نرخ جدید: <b>{rate_rounded:,.0f} تومان</b>{change_text}\n"
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
