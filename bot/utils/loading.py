"""Loading indicator and premium emoji utilities."""
from __future__ import annotations

from aiogram.types import Message

_LOADING_TEXT = '<tg-emoji emoji-id="5386367538735104399">⌛️</tg-emoji>'
_LOADING_FALLBACK = "⌛️"


def pe(emoji_id: str, fallback: str) -> str:
    """Wrap an emoji in a Telegram premium custom emoji tag (HTML parse mode only)."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# قرارداد اموجی خطا/هشدار — اول پیام‌های HTML (با RLM تا سمت راست بیفتد).
# فقط برای متن پیام؛ toastهای cb.answer و دکمه‌ها tg-emoji پشتیبانی نمی‌کنند.
ERR = "‏" + pe("4956612582816351459", "❌")
WARN = "‏" + pe("4956611513369494230", "⚠️")


# Premium emoji map — use in message text with parse_mode="HTML"
# Button text (ReplyKeyboard) does not support HTML; buttons keep plain Unicode.
P = {
    "server":  pe("5262701463049609410", "💻"),
    "buy":     pe("5265226165085282693", "🛒"),
    "wallet":  pe("6102735781258861018", "💰"),
    "profile": pe("5974048815789903111", "👤"),
    "support": pe("5368476981312631953", "📞"),
    "admin":   pe("5895483165182529286", "🛡"),
}


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
