"""Hourly USD/IRR rate updater — scrapes tgju.org and updates np_usd_to_irt_rate."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import aiohttp

from bot.tasks.celery_app import app

logger = logging.getLogger(__name__)

_TGJU_URL = "https://www.tgju.org/profile/price_dollar_rl"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Multiple regex patterns to handle page layout changes
_PRICE_PATTERNS = [
    r'"p"\s*:\s*"([\d,]+)"',           # JSON embedded: "p":"588,000"
    r'data-price=["\']?([\d,]+)["\']?', # data-price="588000"
    r'class="info-price[^"]*"[^>]*>([\d,]+)<',  # <span class="info-price">588,000</span>
    r'class="price[^"]*"[^>]*>([\d,]+)<',        # <span class="price">588,000</span>
]


async def _fetch_rate_toman() -> float | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _TGJU_URL,
                headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning("exchange_rate: tgju.org returned HTTP %s", resp.status)
                    return None
                html = await resp.text()

        for pattern in _PRICE_PATTERNS:
            m = re.search(pattern, html)
            if m:
                price_str = m.group(1).replace(",", "")
                price_rial = float(price_str)
                # tgju.org shows price in Rial — divide by 10 for Toman
                if price_rial > 10_000:  # sanity check (must be > 1000 Toman)
                    return price_rial / 10
        logger.warning("exchange_rate: no price pattern matched on tgju.org")
        return None
    except Exception as exc:
        logger.error("exchange_rate: fetch error: %s", exc)
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
