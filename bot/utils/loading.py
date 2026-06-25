"""Loading indicator utilities."""
from __future__ import annotations

from aiogram.types import Message

_LOADING_TEXT = '<tg-emoji emoji-id="5386367538735104399">⌛️</tg-emoji>'
_LOADING_FALLBACK = "⌛️"


async def answer_loading(message: Message) -> Message:
    """Send a new loading message and return it for later editing (reply-keyboard handlers)."""
    try:
        return await message.answer(_LOADING_TEXT, parse_mode="HTML")
    except Exception:
        return await message.answer(_LOADING_FALLBACK)


async def edit_loading(msg: Message) -> None:
    """Edit an existing message to loading state (inline-keyboard handlers)."""
    try:
        await msg.edit_text(_LOADING_TEXT, parse_mode="HTML")
    except Exception:
        try:
            await msg.edit_text(_LOADING_FALLBACK)
        except Exception:
            pass
