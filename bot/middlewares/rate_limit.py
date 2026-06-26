"""Rate-limit middleware — progressive banning for spamming users."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from bot.config import settings

_WINDOW = 10        # seconds
_MAX_MSGS = 10      # max messages in window before ban
_BAN_DURATIONS = (60, 300, 86400)  # 1 min → 5 min → 24 h (seconds)

_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


class RateLimitMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        tg_id = chat_id = None
        bot = None
        is_callback = False

        if event.message and event.message.chat.type == "private":
            tg_id = event.message.from_user.id
            chat_id = event.message.chat.id
            bot = event.message.bot
        elif event.callback_query:
            tg_id = event.callback_query.from_user.id
            bot = event.callback_query.bot
            if event.callback_query.message:
                chat_id = event.callback_query.message.chat.id
            is_callback = True

        if not tg_id:
            return await handler(event, data)

        r = _get_redis()
        ban_key = f"rl:{tg_id}:ban_until"
        offense_key = f"rl:{tg_id}:offense"
        count_key = f"rl:{tg_id}:count"

        # ── 1. Check active ban ────────────────────────────────────────────
        ban_val = await r.get(ban_key)
        if ban_val:
            remaining = float(ban_val) - time.time()
            if remaining > 0:
                notice = _remaining_text(remaining)
                if is_callback:
                    try:
                        await event.callback_query.answer(notice, show_alert=True)
                    except Exception:
                        pass
                elif bot and chat_id:
                    try:
                        await bot.send_message(chat_id, notice)
                    except Exception:
                        pass
                return  # drop the update

        # ── 2. Count messages in sliding window ───────────────────────────
        count = await r.incr(count_key)
        if count == 1:
            await r.expire(count_key, _WINDOW)

        if count < _MAX_MSGS:
            return await handler(event, data)

        # ── 3. Limit exceeded — apply progressive ban ─────────────────────
        offense = int(await r.incr(offense_key))
        await r.expire(offense_key, 90_000)  # ~25 h — survives 24h ban
        await r.delete(count_key)

        idx = min(offense - 1, len(_BAN_DURATIONS) - 1)
        ban_secs = _BAN_DURATIONS[idx]
        await r.setex(ban_key, ban_secs, str(time.time() + ban_secs))

        notice = _ban_text(ban_secs)
        if bot and chat_id:
            try:
                await bot.send_message(chat_id, notice)
            except Exception:
                pass
        return  # drop this update too


def _remaining_text(remaining: float) -> str:
    if remaining < 120:
        return f"⏳ محدود شده‌اید. {int(remaining)} ثانیه صبر کنید."
    if remaining < 3600:
        return f"⏳ محدود شده‌اید. {int(remaining / 60)} دقیقه صبر کنید."
    return f"⏳ محدود شده‌اید. {int(remaining / 3600)} ساعت صبر کنید."


def _ban_text(ban_secs: int) -> str:
    if ban_secs < 3600:
        return f"🚫 پیام‌های بیش از حد. {ban_secs // 60} دقیقه محدود شدید."
    return f"🚫 پیام‌های بیش از حد. {ban_secs // 3600} ساعت محدود شدید."
