"""Rate-limit middleware — progressive banning for spamming users.

Strategy: silent drop. No reply is sent to a rate-limited user — responding
at all still amplifies the attack. The client simply gets no answer.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from bot.config import settings

_WINDOW = 10        # seconds
_MAX_MSGS = 10      # messages in window before triggering
_BAN_DURATIONS = (60, 300, 86400)  # 1 min → 5 min → 24 h

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

        tg_id = None

        if event.message and event.message.chat.type == "private":
            tg_id = event.message.from_user.id
        elif event.callback_query:
            tg_id = event.callback_query.from_user.id

        if not tg_id:
            return await handler(event, data)

        r = _get_redis()
        ban_key = f"rl:{tg_id}:ban_until"
        offense_key = f"rl:{tg_id}:offense"
        count_key = f"rl:{tg_id}:count"

        # ── 1. Active ban → silent drop ────────────────────────────────────
        ban_val = await r.get(ban_key)
        if ban_val and float(ban_val) > time.time():
            return  # ignore entirely — no reply

        # ── 2. Count messages in window ───────────────────────────────────
        count = await r.incr(count_key)
        if count == 1:
            await r.expire(count_key, _WINDOW)

        if count < _MAX_MSGS:
            return await handler(event, data)

        # ── 3. Limit exceeded → progressive ban, silent drop ─────────────
        offense = int(await r.incr(offense_key))
        await r.expire(offense_key, 90_000)  # ~25 h, survives 24h ban
        await r.delete(count_key)

        idx = min(offense - 1, len(_BAN_DURATIONS) - 1)
        ban_secs = _BAN_DURATIONS[idx]
        await r.setex(ban_key, ban_secs, str(time.time() + ban_secs))
        # Silently drop — no message sent back
