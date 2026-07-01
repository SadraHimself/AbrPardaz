"""Hourly USD/IRT rate updater — scrapes alanchand.com and updates np_usd_to_irt_rate."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import aiohttp

from bot.tasks.celery_app import app

logger = logging.getLogger(__name__)

_ALANCHAND_URL = "https://alanchand.com/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}

# alanchand.com shows prices in Toman directly
_MIN_TOMAN = 50_000
_MAX_TOMAN = 1_000_000

_PERSIAN_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹،", "0123456789,")


def _parse_price(s: str) -> float | None:
    cleaned = s.translate(_PERSIAN_TRANS).replace(",", "").strip()
    return float(cleaned) if cleaned.isdigit() else None


async def _fetch_rate_toman() -> float | None:
    """Return USD sell price in Toman from alanchand.com."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _ALANCHAND_URL,
                headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    logger.warning("exchange_rate: alanchand HTTP %s", resp.status)
                    return None
                html = await resp.text()
    except Exception as exc:
        logger.error("exchange_rate: fetch error: %s", exc)
        return None

    # Find the HTML section containing دلار (dollar)
    idx = html.find("دلار آمریکا")
    if idx == -1:
        idx = html.find("دلار")
    if idx == -1:
        logger.warning("exchange_rate: 'دلار' not found in page")
        return None

    # Extract all Persian/ASCII price-like numbers from the next 400 chars
    snippet = html[idx: idx + 400]
    candidates = re.findall(r"[۰-۹\d]{2,3}(?:[,،][۰-۹\d]{3})+", snippet)
    prices = [p for raw in candidates if (p := _parse_price(raw)) and _MIN_TOMAN <= p <= _MAX_TOMAN]

    if not prices:
        logger.warning("exchange_rate: no valid price in snippet: %r", snippet[:120])
        return None

    # Use the highest price found (sell rate)
    sell = max(prices)
    logger.info("exchange_rate: alanchand→ sell=%s Toman (candidates=%s)", sell, prices)
    return sell


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
