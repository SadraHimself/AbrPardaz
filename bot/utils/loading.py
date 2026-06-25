"""Loading indicator utilities."""
from __future__ import annotations

from aiogram.types import Message

_CUSTOM_EMOJI_ID = "5386367538735104399"
_LOADING_CHAR = "⌛"


def _loading_entities():
    try:
        from aiogram.types import MessageEntity
        return [MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id=_CUSTOM_EMOJI_ID)]
    except Exception:
        return []


async def answer_loading(message: Message) -> Message:
    """Send a new loading message and return it for later editing (reply-keyboard handlers)."""
    entities = _loading_entities()
    try:
        return await message.answer(_LOADING_CHAR, entities=entities if entities else None)
    except Exception:
        return await message.answer(_LOADING_CHAR)


async def edit_loading(msg: Message) -> None:
    """Edit an existing message to loading state (inline-keyboard handlers)."""
    entities = _loading_entities()
    try:
        await msg.edit_text(_LOADING_CHAR, entities=entities if entities else None)
    except Exception:
        try:
            await msg.edit_text(_LOADING_CHAR)
        except Exception:
            pass
