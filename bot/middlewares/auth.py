"""Upsert User from Telegram update; inject into handler data."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import User, UserStatus


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession = data.get("session")
        if not session:
            return await handler(event, data)

        tg_user = None
        if isinstance(event, Update):
            if event.message:
                # Only register users from private chats — group/forum members must not be added
                if event.message.chat.type == "private":
                    tg_user = event.message.from_user
            elif event.callback_query:
                # Ignore callbacks from non-private chats (e.g. group inline buttons)
                cb_msg = event.callback_query.message
                if cb_msg is None or cb_msg.chat.type == "private":
                    tg_user = event.callback_query.from_user

        # Never register bots as users
        if tg_user and tg_user.is_bot:
            tg_user = None

        if tg_user:
            result = await session.execute(
                select(User).where(User.telegram_id == tg_user.id)
            )
            user = result.scalar_one_or_none()
            is_new = False
            if not user:
                is_new = True
                user = User(
                    telegram_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                )
                session.add(user)
                await session.flush()
            else:
                # @username و نام قابل تغییرند؛ کلید پایدار telegram_id است.
                # با هر آپدیت، پروفایل را تازه نگه دار (فقط در صورت تغییر → بدون رایت اضافه)
                if (
                    user.username != tg_user.username
                    or user.first_name != tg_user.first_name
                    or user.last_name != tg_user.last_name
                ):
                    user.username = tg_user.username
                    user.first_name = tg_user.first_name
                    user.last_name = tg_user.last_name

            # Auto-unban users whose temporary ban has expired
            if user.status == UserStatus.BANNED:
                extra = user.extra_data or {}
                ban_until_raw = extra.get("ban_until")
                if ban_until_raw:
                    try:
                        ban_until_dt = datetime.fromisoformat(ban_until_raw)
                        if datetime.now(timezone.utc) >= ban_until_dt:
                            user.status = UserStatus.ACTIVE
                            new_extra = dict(extra)
                            new_extra.pop("ban_until", None)
                            new_extra.pop("ban_reason", None)
                            user.extra_data = new_extra
                            await session.flush()
                    except (ValueError, TypeError):
                        pass

            # Block banned users (admins are always allowed through)
            is_admin = user.is_admin or (user.telegram_id in settings.admin_ids)
            if user.status == UserStatus.BANNED and not is_admin:
                extra = user.extra_data or {}
                reason = extra.get("ban_reason", "")
                ban_msg = "🚫 حساب شما بن شده است."
                if reason:
                    ban_msg += f"\nعلت: {reason}"
                if isinstance(event, Update):
                    if event.message:
                        try:
                            await event.message.answer(ban_msg)
                        except Exception:
                            pass
                    elif event.callback_query:
                        try:
                            await event.callback_query.answer("🚫 حساب شما بن شده است.", show_alert=True)
                        except Exception:
                            pass
                data["user"] = user
                return  # don't call handler

            data["user"] = user
            data["is_new_user"] = is_new

            # ── گیت ورود: تا وقتی قوانین پذیرفته نشده، فقط فلوی ورود مجاز است ──
            # (بدون این، کاربر جدید می‌توانست با تایپ «تهیه سرور» گیت را دور بزند)
            if not user.terms_accepted_at and not is_admin:
                allowed = False
                if isinstance(event, Update):
                    if event.callback_query:
                        cbd = event.callback_query.data or ""
                        allowed = cbd in ("check_join", "accept_terms", "decline_terms")
                    elif event.message:
                        allowed = (event.message.text or "").startswith("/start")
                if not allowed:
                    from bot.handlers.start import send_entry_gate
                    bot = data.get("bot")
                    try:
                        if isinstance(event, Update) and event.callback_query:
                            await event.callback_query.answer(
                                "ابتدا مراحل ورود را کامل کنید.", show_alert=True,
                            )
                        if bot:
                            await send_entry_gate(bot, user.telegram_id, session, user)
                    except Exception:
                        pass
                    return  # هندلر اصلی اجرا نمی‌شود

        return await handler(event, data)
