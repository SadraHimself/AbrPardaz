"""Push notifications to users via the bot."""
from __future__ import annotations

from aiogram import Bot

from bot.database.models import Server, SuspendReason
from bot.utils.loading import WARN


class NotificationService:

    def __init__(self, bot: Bot):
        self.bot = bot

    async def _send(self, telegram_id: int, text: str) -> None:
        try:
            await self.bot.send_message(telegram_id, text, parse_mode="HTML")
        except Exception:
            pass  # User may have blocked the bot

    async def server_created(self, telegram_id: int, server: Server) -> None:
        await self._send(
            telegram_id,
            f"✅ سرور شما با موفقیت ساخته شد!\n\n"
            f"🖥 نام: {server.name}\n"
            f"🌐 IP: {server.ip_address or 'در حال تخصیص...'}\n"
            f"💾 RAM: {server.ram} MB | CPU: {server.cpu} Core | Disk: {server.disk} GB",
        )

    async def server_suspended(self, telegram_id: int, server: Server) -> None:
        reasons = {
            SuspendReason.LOW_BALANCE: "موجودی ناکافی",
            SuspendReason.TRAFFIC_EXCEEDED: "ترافیک تمام شد",
            SuspendReason.EXPIRED: "اشتراک منقضی شد",
            SuspendReason.ADMIN: "توسط ادمین",
        }
        reason_text = reasons.get(server.suspend_reason, "نامشخص")
        await self._send(
            telegram_id,
            f"{WARN} سرور شما ساسپند شد!\n\n"
            f"🖥 نام: {server.name}\n"
            f"📋 دلیل: {reason_text}\n\n"
            f"برای رفع ساسپند، کیف پول را شارژ کنید یا با پشتیبانی تماس بگیرید.",
        )

    async def server_unsuspended(self, telegram_id: int, server: Server) -> None:
        await self._send(
            telegram_id,
            f"✅ سرور شما فعال شد!\n\n🖥 نام: {server.name}\n🌐 IP: {server.ip_address}",
        )

    async def traffic_warning(self, telegram_id: int, server: Server, percent: int) -> None:
        await self._send(
            telegram_id,
            f"{WARN} هشدار ترافیک!\n\n"
            f"🖥 سرور: {server.name}\n"
            f"📊 {percent}٪ از ترافیک ماهیانه شما مصرف شده.\n"
            f"برای خرید ترافیک اضافه وارد ربات شوید.",
        )

    async def traffic_exceeded(self, telegram_id: int, server: Server) -> None:
        await self._send(
            telegram_id,
            f"🚫 ترافیک تمام شد!\n\n"
            f"🖥 سرور: {server.name}\n"
            f"سرور شما ساسپند خواهد شد. برای خرید ترافیک اضافه وارد ربات شوید.",
        )

    async def low_balance_warning(self, telegram_id: int, balance: float) -> None:
        await self._send(
            telegram_id,
            f"{WARN} موجودی کم!\n\n"
            f"💰 موجودی فعلی: {balance:,.0f} تومان\n"
            f"در صورت عدم شارژ، سرور‌های شما ساسپند می‌شوند.",
        )

    async def payment_confirmed(self, telegram_id: int, amount: float) -> None:
        await self._send(
            telegram_id,
            f"✅ پرداخت تأیید شد!\n\n💰 مبلغ {amount:,.0f} تومان به کیف پول شما اضافه شد.",
        )

    async def server_ip_changed(self, telegram_id: int, server: Server, old_ip: str) -> None:
        await self._send(
            telegram_id,
            f"🔄 IP سرور شما تغییر کرد!\n\n"
            f"🖥 نام: {server.name}\n"
            f"⬅️ IP قدیم: {old_ip}\n"
            f"➡️ IP جدید: {server.ip_address}",
        )
