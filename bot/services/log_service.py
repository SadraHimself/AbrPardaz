"""Telegram forum-group logging service."""
from __future__ import annotations

import html as _html
from typing import Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import BotSettings, ProviderAccount, Server, ServerPlan, User


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

    async def _origin_line(self, server: Server) -> str:
        """ارائه‌دهنده + گروه محصولِ سرور — در لاگ‌های خرید/عملیات ذکر می‌شود."""
        parts = []
        try:
            if server.provider_account_id:
                acc = await self.session.get(ProviderAccount, server.provider_account_id)
                if acc:
                    parts.append(acc.name)
        except Exception:
            pass
        try:
            plan_id = (server.extra_data or {}).get("plan_id")
            if plan_id:
                plan = await self.session.get(ServerPlan, plan_id)
                if plan and plan.category:
                    parts.append(f"گروه: {plan.category}")
        except Exception:
            pass
        return ("\n🏢 " + " | ".join(parts)) if parts else ""

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
            f"💵 مبلغ: {amount:,.0f} تومان"
            f"{await self._origin_line(server)}",
        )

    async def log_renewal(self, user: User, server: Server, plan_name: str,
                          amount: float) -> None:
        """تمدید دستی ماهانه — هم‌قالبِ لاگ خرید."""
        await self._send(
            "purchase",
            f"🔄 <b>تمدید سرور</b>\n\n"
            f"{self._user_line(user)}\n"
            f"📦 پلن: {plan_name}\n"
            f"🖥 سرور: {server.name}\n"
            f"🌐 آیپی: <code>{server.ip_address or '—'}</code>\n"
            f"💳 نوع: ماهانه\n"
            f"💵 مبلغ: {amount:,.0f} تومان"
            f"{await self._origin_line(server)}",
        )

    async def log_plan_upgrade(self, user: User, server: Server, old_plan: str,
                               new_plan: str, amount: float) -> None:
        """ارتقاء پلن سرویس — تاپیک «لاگ سرور»."""
        _amt = (f"\n💵 مابه‌التفاوت: {amount:,.0f} تومان" if amount > 0
                else "\n💵 بدون کسر فوری (ساعتی — نرخ جدید از ساعت بعد)")
        await self._send(
            "server",
            f"⬆️ <b>ارتقاء پلن</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🖥 سرور: {server.name}\n"
            f"🌐 آیپی: <code>{server.ip_address or '—'}</code>\n"
            f"📦 پلن: {old_plan} ← <b>{new_plan}</b>\n"
            f"⚙️ منابع: {server.cpu} هسته | {server.ram} MB | {server.disk} GB"
            f"{_amt}"
            f"{await self._origin_line(server)}",
        )

    async def log_traffic_purchase(self, user: User, server: Server,
                                   gb: float, amount: float) -> None:
        """خرید ترافیک اضافه — هم‌قالبِ لاگ خرید."""
        await self._send(
            "purchase",
            f"📶 <b>خرید ترافیک</b>\n\n"
            f"{self._user_line(user)}\n"
            f"📦 بسته: {gb:.0f} گیگابایت\n"
            f"🖥 سرور: {server.name}\n"
            f"🌐 آیپی: <code>{server.ip_address or '—'}</code>\n"
            f"💵 مبلغ: {amount:,.0f} تومان"
            f"{await self._origin_line(server)}",
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
            f"{fee_line}"
            f"{await self._origin_line(server)}",
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
            f"{fee_line}"
            f"{await self._origin_line(server)}",
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
            f"⚡ عملیات: {labels.get(action, action)}"
            f"{await self._origin_line(server)}",
        )

    async def log_snapshot_created(self, user: User, source_name: str,
                                   size_gb: float, hourly_toman: float) -> None:
        await self._send(
            "server",
            f"📸 <b>ساخت اسنپ‌شات</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🖥 منبع: {source_name}\n"
            f"💾 حجم: {size_gb:g} GB\n"
            f"💵 هزینه ساعتی: {hourly_toman:,.0f} تومان",
        )

    async def log_snapshot_deleted(self, user: User, source_name: str) -> None:
        await self._send(
            "server",
            f"🗑 <b>حذف اسنپ‌شات</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🖥 منبع: {source_name}",
        )

    async def log_snapshot_restored(self, user: User, source_name: str,
                                    target_server: Server) -> None:
        await self._send(
            "server",
            f"♻️ <b>استفاده از اسنپ‌شات</b>\n\n"
            f"{self._user_line(user)}\n"
            f"کاربر از اسنپ‌شات «{source_name}» برای سرور "
            f"{target_server.name} (<code>{target_server.ip_address or '—'}</code>) استفاده کرد."
            f"{await self._origin_line(target_server)}",
        )

    async def log_plan_unavailable(self, plan_name: str, location: str) -> None:
        await self._send(
            "server",
            f"⛔ <b>پلن در سرویس‌دهنده ناموجود شد</b>\n\n"
            f"پلن: <b>{plan_name}</b> — {location}\n"
            "محصول به‌صورت خودکار از فروش خارج شد (غیرفعال).",
        )

    async def log_plan_available(self, plan_name: str, location: str) -> None:
        await self._send(
            "server",
            f"✅ <b>پلن دوباره موجود شد</b>\n\n"
            f"پلن: <b>{plan_name}</b> — {location}\n"
            "وضعیت قبلی محصول برگردانده شد.",
        )

    async def log_low_balance(self, balance_rub: float, threshold_rub: float) -> None:
        await self._send(
            "server",
            f"⚠️ <b>موجودی اکانت تایم‌وب کم است</b>\n\n"
            f"موجودی فعلی: <b>{balance_rub:,.2f} ₽</b> (زیر {threshold_rub:g} ₽)\n\n"
            "تا شارژ نشود، سرورهای جدید در وضعیت «Not paid» می‌مانند و بعد از "
            "مهلت، سفارش لغو و وجه کاربر برگردانده می‌شود.\n"
            "لطفاً هرچه زودتر حساب تایم‌وب را شارژ کنید.",
        )

    async def log_timeweb_unpaid(self, hostname: str, preset_id: str = "",
                                 specs: str = "") -> None:
        tariff = ""
        if preset_id or specs:
            tariff = (f"تعرفه: <code>{preset_id or '?'}</code>"
                      + (f" · {specs}" if specs else "") + "\n")
        await self._send(
            "server",
            f"⛔ <b>سرور تایم‌وب «پرداخت‌نشده» (no_paid) ماند</b>\n\n"
            f"سرور <code>{hostname}</code> لغو شد و وجه کاربر کامل برگشت.\n"
            f"{tariff}\n"
            "علتِ اصلی (اثبات‌شده): موجودیِ اکانت تایم‌وب کفافِ «رزروِ ~یک‌ماهِ "
            "همه‌ی سرورهای فعلی + سرورِ جدید» را نمی‌دهد — پیش‌چکِ ربات جلوی "
            "بیشترش را می‌گیرد ولی اگر موجودی وسطِ کار تغییر کند رد می‌شود.\n"
            "راه‌حل: حساب تایم‌وب را شارژ کن (به‌ازای هر سرورِ هم‌زمان ~یک‌ماه هزینه).\n"
            "برای جزئیات، خطِ <code>TW_CREATE_DIAG</code> را در لاگِ سرور ببین.",
        )

    async def log_timeweb_funds(self, hostname: str) -> None:
        await self._send(
            "server",
            f"⛔ <b>ساخت سرور تایم‌وب رد شد — موجودی برای سرورِ جدید کافی نیست</b>\n\n"
            f"سرور <code>{hostname}</code> ساخته نشد و وجه کاربر کامل برگشت.\n\n"
            "تایم‌وب برای <b>هر سرور</b> تقریباً هزینه‌ی یک‌ماهش را از بالانس رزرو "
            "می‌کند. موجودیِ فعلی فقط کفافِ سرورهای فعلی را می‌دهد و برای سرورِ جدید "
            "جا نیست (وگرنه no_paid می‌شد).\n"
            "برای فروشِ سرورِ بیشتر، حساب تایم‌وب را شارژ کن — تقریباً به‌ازای هر "
            "سرورِ هم‌زمان، یک‌ماه هزینه.",
        )

    async def log_timeweb_ip_limit(self, hostname: str) -> None:
        await self._send(
            "server",
            f"⛔ <b>سهمیه‌ی روزانه‌ی IP تایم‌وب تمام شد</b>\n\n"
            f"سرور <code>{hostname}</code> ساخته شد ولی تایم‌وب IPv4 نداد "
            "(<code>403 daily_limit_exceeded</code>) — سرور حذف و وجه کاربر کامل "
            "برگشت.\n\n"
            "این محدودیتِ ضدسوءاستفاده‌ی خودِ تایم‌وب است (ساخت/حذف‌های زیادِ "
            "امروز). معمولاً روز بعد ریست می‌شود؛ برای برداشتنِ سقف به پشتیبانی "
            "تیکت بده.\n"
            "تا ۱ ساعت، دسته‌ی تایم‌وب برای کاربران «ظرفیت تکمیل» نشان داده می‌شود.",
        )

    async def log_timeweb_orphan_deleted(self, name: str, comment: str) -> None:
        await self._send(
            "server",
            f"🧹 <b>سرور یتیم تایم‌وب حذف شد</b>\n\n"
            f"سرور <code>{name}</code> ({comment}) در پنل تایم‌وب بود ولی در ربات "
            "رکوردی نداشت (بازمانده‌ی کرش/قطعی وسط ساخت یا حذفِ ناموفق) — برای "
            "قطعِ هزینه حذف شد.\n"
            "⚠️ اگر این سرور مالِ خریدی بود که تحویل نشده، پولِ کاربر را از "
            "تاریخچه‌ی تراکنش‌ها (خرید سرور با همین نام) بررسی و دستی برگردان.",
        )

    async def log_rootvds_funds(self, hostname: str) -> None:
        await self._send(
            "server",
            f"⛔ <b>ساخت سرور RootVDS رد شد — موجودی اکانت کافی نیست</b>\n\n"
            f"سرور <code>{hostname}</code> ساخته نشد و وجه کاربر کامل برگشت.\n"
            "RootVDS از بالانس اکانت دقیقه‌ای کسر می‌کند («No money»). "
            "برای ادامه‌ی فروش، حساب rootvds.ru را شارژ کن.",
        )

    # نامِ نمایشیِ هر سرویس‌دهنده برای پیام‌های قطعی/وصل
    _PROVIDER_LABEL = {
        "virtualizor": "ویرچولایزور", "hetzner": "هتزنر", "gcore": "جیکور",
        "timeweb": "تایم‌وب", "rootvds": "روت",
    }

    @classmethod
    def _provider_label(cls, provider_type=None) -> str:
        val = getattr(provider_type, "value", provider_type)
        return cls._PROVIDER_LABEL.get(str(val or "").lower(), "سرویس‌دهنده")

    async def log_provider_down(self, name: str, reason: str = "",
                                provider_type=None) -> None:
        await self._send(
            "server",
            f"🔴 <b>قطعی {self._provider_label(provider_type)}</b>\n\n"
            f"🖥 سرور: <b>{name}</b>\n"
            f"وضعیت: ارتباط برقرار نشد (سرور خاموش است یا اتصال قطع است)\n"
            f"دلیل احتمالی: <code>{reason or 'نامشخص'}</code>",
        )

    async def log_provider_up(self, name: str, provider_type=None) -> None:
        await self._send(
            "server",
            f"🟢 <b>{self._provider_label(provider_type)} دوباره وصل شد</b>\n\n"
            f"🖥 سرور: <b>{name}</b>",
        )

    # ── برنامه ریسلر ─────────────────────────────────────────────────────────

    async def log_reseller_sync(self, user: User, account_name: str, summary: dict) -> None:
        """خلاصه‌ی تغییرات یک دور سینک ریسلر (فقط وقتی چیزی تغییر کرده)."""
        def _sec(title: str, names: list) -> str:
            if not names:
                return ""
            # نام VM از پنل می‌آید — بدون escape، یک < در hostname کل پیام (از
            # جمله هشدار «بدون پلن» که سوارش است) را بی‌صدا ساقط می‌کند
            shown = "، ".join(f"<code>{_html.escape(str(n))}</code>" for n in names[:15])
            more = f" و {len(names) - 15} مورد دیگر" if len(names) > 15 else ""
            return f"\n{title}: {shown}{more}"
        text = (
            f"🏪 <b>سینک ریسلر</b>\n\n"
            f"{self._user_line(user)}\n"
            f"🏢 {account_name}"
            + _sec("➕ ثبت شد", summary.get("registered") or [])
            + _sec("🗑 حذف شد (از پنل رفته)", summary.get("deleted") or [])
            + _sec("🔗 پلن مچ شد", summary.get("rematched") or [])
        )
        unmatched = summary.get("unmatched") or []
        if unmatched:
            text += _sec("⚠️ VM بدون پلنِ مچ‌شده — دستی تعیین تکلیف شود", unmatched)
        await self._send("server", text)

    async def log_reseller_warn(self, user: User, reason: str) -> None:
        await self._send(
            "server",
            f"⚠️ <b>هشدار سینک ریسلر</b>\n\n"
            f"{self._user_line(user)}\n"
            f"{reason}",
        )

    async def log_reseller_debt(self, user: User, hours: int, amount_toman: float,
                                since_iso: Optional[str], backfill_remaining: float) -> None:
        since = "—"
        if since_iso:
            try:
                from datetime import datetime as _dt
                since = _dt.fromisoformat(str(since_iso)).strftime("%Y/%m/%d %H:%M")
            except (TypeError, ValueError):
                pass
        extra_line = (f"\n💳 بدهی «محاسبه گذشته» وصول‌نشده: {backfill_remaining:,.0f} تومان"
                      if backfill_remaining >= 1 else "")
        await self._send(
            "moderation",
            f"🔴 <b>بدهی ریسلر</b>\n\n"
            f"{self._user_line(user)}\n"
            f"⏱ بدهکار از: {since}\n"
            f"💵 انباشته: <b>{hours} ساعت ≈ {amount_toman:,.0f} تومان</b>\n"
            f"💼 موجودی فعلی: {user.balance:,.0f} تومان{extra_line}",
        )

    async def log_reseller_backfill(self, user: User, lines: list[str], total_paid: float,
                                    total_debt: float, admin_tg_id: int) -> None:
        """اجرای دستی «محاسبه از تاریخ ساخت» توسط ادمین."""
        body = "\n".join(lines[:20])
        if len(lines) > 20:
            body += f"\n… و {len(lines) - 20} مورد دیگر"
        debt_line = (f"\n📌 ثبت‌شده به‌عنوان بدهی (وصول خودکار): {total_debt:,.0f} تومان"
                     if total_debt >= 1 else "")
        await self._send(
            "finance",
            f"🧮 <b>محاسبه از تاریخ ساخت (ریسلر)</b>\n\n"
            f"👮 ادمین: <code>{admin_tg_id}</code>\n"
            f"👤 کاربر:\n{self._user_line(user)}\n\n"
            f"{body}\n\n"
            f"💵 کسرشده: <b>{total_paid:,.0f} تومان</b>{debt_line}\n"
            f"💼 موجودی جدید: {user.balance:,.0f} تومان",
        )

    async def log_reseller_backfill_collect(self, user: User, collected: float,
                                            remaining: float) -> None:
        """وصول خودکار بدهی «محاسبه گذشته» بعد از شارژ کیف پول."""
        rem_line = (f"\n📌 باقی‌مانده: {remaining:,.0f} تومان" if remaining >= 1
                    else "\n✅ بدهی کامل تسویه شد.")
        await self._send(
            "finance",
            f"💳 <b>وصول بدهی محاسبه گذشته (ریسلر)</b>\n\n"
            f"{self._user_line(user)}\n"
            f"💵 وصول‌شده: <b>{collected:,.0f} تومان</b>{rem_line}",
        )

    async def log_reseller_config(self, user: User, action: str, admin_tg_id: int) -> None:
        """تغییرات کانفیگ ریسلر توسط ادمین (فعال/غیرفعال/ایمیل/تخفیف)."""
        await self._send(
            "moderation",
            f"🏪 <b>تنظیمات ریسلر</b>\n\n"
            f"👮 ادمین: <code>{admin_tg_id}</code>\n"
            f"👤 کاربر:\n{self._user_line(user)}\n"
            f"🔧 {action}",
        )
