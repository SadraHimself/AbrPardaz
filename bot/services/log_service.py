"""Telegram forum-group logging service."""
from __future__ import annotations

from typing import Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import BotSettings, Server, User


_TOPIC_KEYS = {
    "finance":  "log_topic_finance",
    "new_user": "log_topic_new_user",
    "purchase": "log_topic_purchase",
    "server":   "log_topic_server",
    "backup":   "log_topic_backup",
}


class LogService:
    def __init__(self, bot: Bot, session: AsyncSession) -> None:
        self.bot = bot
        self.session = session

    async def _setting(self, key: str) -> Optional[str]:
        row = await self.session.get(BotSettings, key)
        return row.value if row else None

    async def _send(self, topic: str, text: str) -> None:
        gid = await self._setting("log_group_id")
        tid = await self._setting(_TOPIC_KEYS[topic])
        if not gid or not tid:
            return
        try:
            await self.bot.send_message(
                int(gid), text,
                parse_mode="HTML",
                message_thread_id=int(tid),
            )
        except Exception:
            pass

    @staticmethod
    def _user_line(user: User) -> str:
        uname = f"@{user.username}" if user.username else "—"
        name = user.first_name or "کاربر"
        return f"👤 {name} ({uname}) | <code>{user.telegram_id}</code>"

    async def log_new_user(self, user: User) -> None:
        await self._send(
            "new_user",
            f"🆕 <b>کاربر جدید</b>\n\n"
            f"{self._user_line(user)}",
        )

    async def log_wallet_charge(self, user: User, amount: float, new_balance: float) -> None:
        await self._send(
            "finance",
            f"💰 <b>شارژ کیف پول</b>\n\n"
            f"{self._user_line(user)}\n"
            f"💵 مبلغ: <b>{amount:,.0f} تومان</b>\n"
            f"💼 موجودی جدید: {new_balance:,.0f} تومان",
        )

    async def log_purchase(self, user: User, server: Server, plan_name: str,
                           billing_type: str, amount: float) -> None:
        billing_label = "ساعتی" if billing_type == "hourly" else "ماهانه"
        await self._send(
            "purchase",
            f"🛒 <b>خرید سرور</b>\n\n"
            f"{self._user_line(user)}\n"
            f"📦 پلن: {plan_name}\n"
            f"🖥 سرور: {server.name}\n"
            f"🌐 آیپی: <code>{server.ip_address or '—'}</code>\n"
            f"💳 نوع: {billing_label}\n"
            f"💵 مبلغ: {amount:,.0f} تومان",
        )

    async def log_ip_change(self, user: User, server: Server,
                            old_ip: str, new_ip: str) -> None:
        await self._send(
            "purchase",
            f"🌐 <b>تغییر IP</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🖥 سرور: {server.name}\n"
            f"⬅️ IP قدیم: <code>{old_ip or '—'}</code>\n"
            f"➡️ IP جدید: <code>{new_ip}</code>",
        )

    async def log_server_action(self, user: User, server: Server, action: str) -> None:
        labels = {
            "rebuild":         "🔁 ریبیلد",
            "restart":         "🔄 ریبوت",
            "start":           "▶️ روشن کردن",
            "stop":            "⏹ خاموش کردن",
            "delete":          "🗑 حذف",
            "change_password": "🔑 تغییر رمز",
            "unsuspend":       "✅ رفع ساسپند",
        }
        await self._send(
            "server",
            f"🖥 <b>عملیات سرور</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🖥 سرور: {server.name} (<code>{server.ip_address or '—'}</code>)\n"
            f"⚡ عملیات: {labels.get(action, action)}",
        )
