"""Telegram forum-group logging service."""
from __future__ import annotations

from typing import Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import BotSettings, Server, User


_TOPIC_KEYS = {
    "finance":       "log_topic_finance",
    "new_user":      "log_topic_new_user",
    "purchase":      "log_topic_purchase",
    "server":        "log_topic_server",
    "backup":        "log_topic_backup",
    "moderation":    "log_topic_moderation",
    "exchange_rate": "log_topic_exchange_rate",
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

    async def log_crypto_charge(self, user: User, amount_usd: float, amount_irt: float, order_id: str) -> None:
        await self._send(
            "finance",
            f"💎 <b>شارژ کریپتو</b>\n\n"
            f"{self._user_line(user)}\n"
            f"💵 مبلغ: <b>{amount_usd:.0f}$</b> ≈ <b>{amount_irt:,.0f} تومان</b>\n"
            f"💳 روش: درگاه NOWPayments\n"
            f"🔑 شناسه: <code>{order_id}</code>\n"
            f"💼 موجودی جدید: {user.balance:,.0f} تومان",
        )

    async def log_admin_wallet_change(self, target: User, amount: float, is_credit: bool,
                                      admin_tg_id: int, admin_name: str = "ادمین") -> None:
        icon = "💚" if is_credit else "🔴"
        action = "افزایش موجودی" if is_credit else "کاهش موجودی"
        sign = "+" if is_credit else "-"
        await self._send(
            "finance",
            f"{icon} <b>{action} توسط ادمین</b>\n\n"
            f"👮 ادمین: {admin_name} | <code>{admin_tg_id}</code>\n\n"
            f"👤 کاربر:\n{self._user_line(target)}\n"
            f"💵 مبلغ: <b>{sign}{amount:,.0f} تومان</b>\n"
            f"💼 موجودی جدید: {target.balance:,.0f} تومان",
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
                            old_ip: str, new_ip: str, fee: float = 0) -> None:
        fee_line = f"\n💵 هزینه: <b>{fee:,.0f} تومان</b>" if fee > 0 else "\n💵 هزینه: رایگان"
        await self._send(
            "purchase",
            f"🌐 <b>تغییر IP</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🖥 سرور: {server.name}\n"
            f"⬅️ IP قدیم: <code>{old_ip or '—'}</code>\n"
            f"➡️ IP جدید: <code>{new_ip}</code>"
            f"{fee_line}",
        )

    async def log_extra_ip(self, user: User, server: Server,
                           new_ip: str, fee: float = 0) -> None:
        fee_line = f"\n💵 هزینه: <b>{fee:,.0f} تومان</b>" if fee > 0 else "\n💵 هزینه: رایگان"
        await self._send(
            "purchase",
            f"➕ <b>خرید IP اضافه</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🖥 سرور: {server.name}\n"
            f"🌐 IP اصلی: <code>{server.ip_address or '—'}</code>\n"
            f"🆕 IP اضافه: <code>{new_ip}</code>"
            f"{fee_line}",
        )

    async def log_ban_user(self, target: User, reason: str, days: int, admin_id: int) -> None:
        duration = f"{days} روز" if days > 0 else "دائمی"
        await self._send(
            "moderation",
            f"🚫 <b>بن کاربر</b>\n\n"
            f"{self._user_line(target)}\n"
            f"📝 علت: {reason}\n"
            f"⏱ مدت: {duration}\n"
            f"👮 توسط ادمین: <code>{admin_id}</code>",
        )

    async def log_unban_user(self, target: User, admin_id: int) -> None:
        await self._send(
            "moderation",
            f"✅ <b>آنبن کاربر</b>\n\n"
            f"{self._user_line(target)}\n"
            f"👮 توسط ادمین: <code>{admin_id}</code>",
        )

    async def log_server_action(self, user: User, server: Server, action: str) -> None:
        labels = {
            "rebuild":         "🔁 ریبیلد",
            "restart":         "🔄 ریبوت",
            "start":           "▶️ روشن کردن",
            "stop":            "⏹ خاموش کردن",
            "delete":          "🗑 حذف",
            "change_password": "🔑 تغییر رمز",
            "add_ip":          "🌐 افزودن IP",
            "unsuspend":       "✅ رفع ساسپند",
        }
        await self._send(
            "server",
            f"🖥 <b>عملیات سرور</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🖥 سرور: {server.name} (<code>{server.ip_address or '—'}</code>)\n"
            f"⚡ عملیات: {labels.get(action, action)}",
        )

    async def log_provider_down(self, name: str, reason: str = "") -> None:
        await self._send(
            "server",
            f"🔴 <b>قطعی سرور ویرچولایزور</b>\n\n"
            f"🖥 سرور: <b>{name}</b>\n"
            f"وضعیت: ارتباط برقرار نشد (سرور خاموش است یا اتصال قطع است)\n"
            f"دلیل احتمالی: <code>{reason or 'نامشخص'}</code>",
        )

    async def log_provider_up(self, name: str) -> None:
        await self._send(
            "server",
            f"🟢 <b>سرور ویرچولایزور دوباره وصل شد</b>\n\n"
            f"🖥 سرور: <b>{name}</b>",
        )
