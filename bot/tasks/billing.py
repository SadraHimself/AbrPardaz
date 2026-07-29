"""Celery tasks: billing, suspension, alerts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from bot.tasks.celery_app import app


def _run(coro):
    """Run async coroutine from sync Celery task."""
    return asyncio.run(coro)


@app.task(name="bot.tasks.billing.run_hourly_billing", bind=True, max_retries=3)
def run_hourly_billing(self):
    """
    هر دقیقه اجرا می‌شود. برای هر سرور ساعتی فعال:
      1. یک ساعت از کیف پول کسر می‌شود.
      2. اگر موجودی ناکافی → grace period 3 ساعته شروع می‌شود (suspend فوری نیست).
    """
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import Server, ServerStatus, User
        from bot.services.billing import BillingService
        from sqlalchemy import select
        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        async with AsyncSessionFactory() as session:
            billing = BillingService(session)
            servers = await billing.get_active_servers_for_billing()

            users_empty_balance: set[int] = set()
            users_charged_ok: set[int] = set()

            from bot.services.currency import obj_currency, to_toman

            for server in servers:
                success = await billing.charge_hourly(server)
                if success:
                    # ⚠️ True همیشه یعنی «کسر شد» نیست: نبودِ نرخ ارز، قیمت صفر،
                    # یا سرویسی که همین لحظه به ماهانه تبدیل شده هم True می‌دهند
                    # (بدون کسر). فقط وقتی واقعاً کسر شده نوتیف بفرست.
                    charged = float(getattr(billing, "last_hourly_charged_toman", 0) or 0)
                    if charged <= 0:
                        continue
                    users_charged_ok.add(server.user_id)
                    user_obj = await session.get(User, server.user_id)
                    from bot.tasks.server import notify_hourly_billing
                    notify_hourly_billing.delay(
                        server.user_id, server.id,
                        charged,
                        float(user_obj.balance if user_obj else 0),
                    )
                else:
                    users_empty_balance.add(server.user_id)

            # کاربری که همه‌ی کسرهایش دوباره موفق شد → grace کهنه پاک شود، وگرنه
            # timestamp قدیمی در شکستِ بعدی باعث حذف فوریِ بدون اخطار می‌شود
            recovered = users_charged_ok - users_empty_balance
            if recovered:
                res = await session.execute(select(User).where(User.id.in_(recovered)))
                for u in res.scalars():
                    ex = dict(u.extra_data or {})
                    if "balance_empty_at" in ex or "balance_warn_level" in ex:
                        ex.pop("balance_empty_at", None)
                        ex.pop("balance_warn_level", None)
                        u.extra_data = ex

            await session.commit()

        for uid in users_empty_balance:
            handle_balance_empty.delay(uid)

    try:
        _run(_do())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


@app.task(name="bot.tasks.billing.run_snapshot_billing", bind=True, max_retries=3)
def run_snapshot_billing(self):
    """هر ساعت: هزینه‌ی نگهداری هر اسنپ‌شات فعال را ساعتی کسر می‌کند.
    موجودی ناکافی → اسنپ‌شات از هتزنر و DB حذف می‌شود (اسنپ‌شات قابل تعلیق نیست)."""
    async def _do():
        from datetime import datetime, timedelta, timezone
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import ProviderAccount, Snapshot, User
        from bot.services.billing import BillingService
        from bot.services.snapshot import hourly_toman
        from bot.providers.hetzner import HetznerProvider
        from bot.services.log_service import LogService
        from aiogram import Bot
        from bot.config import settings
        from sqlalchemy import select
        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        async with AsyncSessionFactory() as session:
            billing = BillingService(session)
            snaps = list((await session.execute(
                select(Snapshot).where(
                    Snapshot.is_active == True,
                    (Snapshot.last_billed_at.is_(None)) | (Snapshot.last_billed_at <= one_hour_ago),
                )
            )).scalars().all())
            if not snaps:
                return

            bot = Bot(token=settings.BOT_TOKEN)
            log = LogService(bot, session)
            try:
                for snap in snaps:
                    account = await session.get(ProviderAccount, snap.provider_account_id)
                    if not account:
                        continue
                    amount = await hourly_toman(session, snap)
                    if amount <= 0:
                        continue  # نرخ ارز در دسترس نیست — بدون advance، دور بعد جبران
                    ok = await billing.debit(
                        snap.user_id, amount,
                        description=f"اسنپ‌شات — {snap.source_server_name or '—'}")
                    if ok:
                        # لنگر +۱ ساعت از قبلی (روی گریدِ ساخت، بدون drift) —
                        # ساعت‌های عقب‌افتاده در اجراهای بعدی کسر می‌شوند نه بخشیده
                        created = snap.created_at
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        prev = snap.last_billed_at or created
                        if prev.tzinfo is None:
                            prev = prev.replace(tzinfo=timezone.utc)
                        snap.last_billed_at = max(prev, created) + timedelta(hours=1)
                    else:
                        # موجودی ناکافی → حذف اسنپ‌شات
                        try:
                            await HetznerProvider(account.api_key or "").delete_image(snap.hetzner_image_id)
                        except Exception:
                            pass
                        snap.is_active = False
                        user = await session.get(User, snap.user_id)
                        if user:
                            try:
                                await bot.send_message(
                                    user.telegram_id,
                                    '‏<tg-emoji emoji-id="4956611513369494230">⚠️</tg-emoji> '
                                    f"اسنپ‌شات «{snap.source_server_name or '—'}» به‌دلیل کمبود موجودی حذف شد.",
                                    parse_mode="HTML")
                            except Exception:
                                pass
                        await log.log_snapshot_deleted(user or User(telegram_id=0),
                                                       snap.source_server_name or "—")
                await session.commit()
            finally:
                await bot.session.close()

    try:
        _run(_do())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


@app.task(name="bot.tasks.billing.handle_balance_empty")
def handle_balance_empty(user_id: int):
    """
    وقتی کسر ساعتی ناموفق است صدا زده می‌شود.
    - بار اول: پیام اخطار اولیه + ثبت زمان در extra_data
    - بعد از ۱ ساعت: پیام 1/3
    - بعد از ۲ ساعت: پیام 2/3
    - بعد از ۳ ساعت: حذف کامل تمام سرورها + پیام نهایی
    اگر کاربر در این مدت موجودی شارژ کند، grace period پاک می‌شود.
    """
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import User, Server, ServerStatus, ProviderAccount
        from bot.providers import get_provider
        from sqlalchemy import select
        from aiogram import Bot
        from bot.config import settings

        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(User).where(User.id == user_id).with_for_update()
            )
            user = result.scalar_one_or_none()
            if not user:
                return

            # ── شرط ریکاوری ──
            # ⚠️ باگ قبلی: «balance > 0» — موجودیِ مثبتِ ولی ناکافی (مثلاً ۳هزار
            # تومان) grace را هر دقیقه پاک می‌کرد و چون کسر ساعتی هم شکست می‌خورد،
            # سرورها برای همیشه رایگان و روشن می‌ماندند. ریکاوری واقعی یعنی موجودی،
            # هزینه‌ی یک ساعتِ «همه‌ی» سرورهای ساعتیِ فعال کاربر را پوشش دهد.
            from bot.database.models import BillingType
            from bot.services.currency import server_live_price, to_toman

            def _clear_grace() -> bool:
                ex = dict(user.extra_data or {})
                if "balance_empty_at" in ex or "balance_warn_level" in ex:
                    ex.pop("balance_empty_at", None)
                    ex.pop("balance_warn_level", None)
                    user.extra_data = ex
                    return True
                return False

            srv_rows = await session.execute(
                select(Server).where(
                    Server.user_id == user.id,
                    Server.status == ServerStatus.ACTIVE,
                    Server.billing_type == BillingType.HOURLY,
                )
            )
            hourly_servers = list(srv_rows.scalars().all())
            if not hourly_servers:
                # سرور ساعتی فعالی نمانده — grace بی‌موضوع است
                if _clear_grace():
                    await session.commit()
                return

            total_hourly_toman = 0.0
            for s in hourly_servers:
                try:
                    amount, cur = await server_live_price(session, s, hourly=True)
                    amount = float(amount or 0)
                    if amount <= 0:
                        continue
                    total_hourly_toman += (
                        amount if cur == "irt" else await to_toman(session, amount, cur)
                    )
                except Exception:
                    pass

            if total_hourly_toman > 0 and user.balance >= total_hourly_toman:
                # موجودی دوباره کافی شده (شارژ کرده) — grace پاک شود
                if _clear_grace():
                    await session.commit()
                return

            now = datetime.now(timezone.utc)
            extra = dict(user.extra_data or {})
            balance_empty_at_str = extra.get("balance_empty_at")
            warn_level = int(extra.get("balance_warn_level", -1))

            bot = Bot(token=settings.BOT_TOKEN)
            try:
                if balance_empty_at_str is None:
                    # بار اول — ثبت زمان و ارسال پیام اخطار
                    extra["balance_empty_at"] = now.isoformat()
                    extra["balance_warn_level"] = 0
                    user.extra_data = extra
                    await session.commit()
                    await bot.send_message(
                        user.telegram_id,
                        '<tg-emoji emoji-id="4956611513369494230">⚠️</tg-emoji> کاربر گرامی موجودی شما به اتمام رسیده برای جلوگیری از حذف شدن سرویس ها موجودی خود را شارژ کنید',
                        parse_mode="HTML",
                    )
                    return

                balance_empty_at = datetime.fromisoformat(balance_empty_at_str)
                if balance_empty_at.tzinfo is None:
                    balance_empty_at = balance_empty_at.replace(tzinfo=timezone.utc)
                elapsed_hours = (now - balance_empty_at).total_seconds() / 3600

                # تایم‌استمپِ کهنه (مثلاً کاربر مدتی هیچ سرویس ساعتی نداشته و
                # مهلتِ قدیمی پاک نشده): چرخه از نو شروع شود، نه اینکه سرویسِ
                # تازه‌خریده‌شده بدون هیچ اخطاری فوراً حذف شود
                if elapsed_hours >= 12:
                    extra["balance_empty_at"] = now.isoformat()
                    extra["balance_warn_level"] = 0
                    user.extra_data = extra
                    await session.commit()
                    await bot.send_message(
                        user.telegram_id,
                        '<tg-emoji emoji-id="4956611513369494230">⚠️</tg-emoji> کاربر گرامی موجودی شما به اتمام رسیده برای جلوگیری از حذف شدن سرویس ها موجودی خود را شارژ کنید',
                        parse_mode="HTML",
                    )
                    return

                if elapsed_hours >= 3 and warn_level < 3:
                    # حذف سرورهای ساعتی. ⚠️ سرویس ماهانه اینجا حذف نمی‌شود:
                    # هزینه‌اش از قبل پرداخت شده و چرخه‌ی خودش را دارد
                    # (یادآوری ۳روزه → ساسپند ۲۴ساعته → حذف). قبلاً همه‌ی
                    # سرورها حذف می‌شدند و ماهِ پیش‌پرداخت‌شده می‌سوخت.
                    srv_result = await session.execute(
                        select(Server).where(
                            Server.user_id == user.id,
                            Server.status != ServerStatus.DELETED,
                            Server.billing_type == BillingType.HOURLY,
                        )
                    )
                    servers = list(srv_result.scalars().all())
                    for server in servers:
                        try:
                            if server.provider_account_id and server.provider_server_id:
                                account = await session.get(ProviderAccount, server.provider_account_id)
                                if account:
                                    provider = get_provider(account)
                                    await provider.delete_server(server.provider_server_id)
                        except Exception:
                            pass
                        server.status = ServerStatus.DELETED

                    extra["balance_warn_level"] = 3
                    extra.pop("balance_empty_at", None)
                    user.extra_data = extra
                    await session.commit()
                    await bot.send_message(
                        user.telegram_id,
                        '<tg-emoji emoji-id="5258093637450866522">🤖</tg-emoji> کاربر عزیز سرویس های ساعتی شما به دلیل عدم شارژ کیف پول حذف شدند',
                        parse_mode="HTML",
                    )

                elif elapsed_hours >= 2 and warn_level < 2:
                    extra["balance_warn_level"] = 2
                    user.extra_data = extra
                    await session.commit()
                    await bot.send_message(
                        user.telegram_id,
                        '<tg-emoji emoji-id="6008233706039284019">⚠️</tg-emoji> 2/3 کاربر گرامی جهت جلوگیری از حذف شدن سرویس های خود اقدام به شارژ کیف پول کنید',
                        parse_mode="HTML",
                    )

                elif elapsed_hours >= 1 and warn_level < 1:
                    extra["balance_warn_level"] = 1
                    user.extra_data = extra
                    await session.commit()
                    await bot.send_message(
                        user.telegram_id,
                        '<tg-emoji emoji-id="6008233706039284019">⚠️</tg-emoji> 1/3 کاربر گرامی جهت جلوگیری از حذف شدن سرویس های خود اقدام به شارژ کیف پول کنید',
                        parse_mode="HTML",
                    )

            except Exception:
                pass
            finally:
                await bot.session.close()

    _run(_do())


# ── پیام‌های چرخه‌ی تمدید ماهانه ─────────────────────────────────────────────
_SIGN = '\n\n‎<tg-emoji emoji-id="5258093637450866522">🤖</tg-emoji> @abrmakerbot'


def _renew_reminder_text(name: str, ip: str, days: int) -> str:
    return (
        '‏<tg-emoji emoji-id="5258503720928288433">🔔</tg-emoji> کاربر گرامی\n\n'
        f"موعد تمدید ماهیانه سرویس {name} با ایپی {ip} تا {days} روز دیگر می باشد، "
        "برای تمدید سرویس کیف پول خود را به مقدار قیمت سرویس شارژ کنید." + _SIGN
    )


def _renew_suspend_text(name: str, ip: str) -> str:
    return (
        '‏<tg-emoji emoji-id="6008233706039284019">⚠️</tg-emoji> کاربر گرامی\n\n'
        f"سرویس {name} با آدرس آیپی {ip} به علت عدم تمدید تا 24 ساعت دیگر ساسپند شده است. "
        "برای تمدید سرویس کیف پول خود را به مقدار مورد نیاز شارژ کنید" + _SIGN
    )


def _renewed_text(amount: float, name: str, ip: str) -> str:
    return (
        f"کاربر گرامی مبلغ {amount:,.0f} تومان برای تمدید سرویس {name} با آدرس آیپی {ip} "
        "از کیف پول شما کاسته شد و سرویس تمدید شد." + _SIGN
    )


async def _renew_and_unsuspend(session, billing, server, bot) -> bool:
    """تلاش تمدید یک سرور ساسپندِ منقضی؛ موفق → آن‌ساسپند + ۳۰ روز + رسید. """
    from bot.database.models import ProviderAccount, User
    from bot.providers import get_provider

    success = await billing.charge_monthly(server)
    if not success:  # False یا None
        return False
    await billing.unsuspend_server_db(server)
    try:
        if server.provider_account_id and server.provider_server_id:
            account = await session.get(ProviderAccount, server.provider_account_id)
            if account:
                await get_provider(account).unsuspend_server(server.provider_server_id)
    except Exception:
        pass
    server.expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    extra = dict(server.extra_data or {})
    extra.pop("renew_notice_sent", None)
    server.extra_data = extra
    user = await session.get(User, server.user_id)
    if user:
        try:
            await bot.send_message(
                user.telegram_id,
                _renewed_text(billing.last_monthly_charge_toman, server.name,
                              server.ip_address or "—"),
                parse_mode="HTML")
        except Exception:
            pass
    return True


@app.task(name="bot.tasks.billing.run_monthly_expiry_check", bind=True, max_retries=3)
def run_monthly_expiry_check(self):
    """
    هر شب — چرخه‌ی کامل تمدید ماهانه:
      ۱) یادآوری ۳ روز آخر (روزی یک‌بار: ۳، ۲، ۱ روز مانده)
      ۲) سررسید: موجودی کافی → تمدید خودکار + رسید | ناکافی → ساسپند ۲۴ساعته + اخطار
      ۳) ساسپندِ بیش از ۲۴ ساعت: یک تلاش آخر برای تمدید، وگرنه حذف کامل
    """
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import (Server, ServerStatus, BillingType,
                                         SuspendReason, ProviderAccount, User)
        from bot.services.billing import BillingService
        from bot.providers import get_provider
        from sqlalchemy import select
        from aiogram import Bot
        from bot.config import settings
        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        now = datetime.now(timezone.utc)

        async with AsyncSessionFactory() as session:
            billing = BillingService(session)
            bot = Bot(token=settings.BOT_TOKEN)

            async def _tell(user_id: int, text: str) -> None:
                u = await session.get(User, user_id)
                if not u:
                    return
                try:
                    await bot.send_message(u.telegram_id, text, parse_mode="HTML")
                except Exception:
                    pass

            try:
                # ── ۱) یادآوری ۳ روز آخر (هر روز یک‌بار — dedup با renew_notice_sent)
                res = await session.execute(
                    select(Server).where(
                        Server.billing_type == BillingType.MONTHLY,
                        Server.status == ServerStatus.ACTIVE,
                        Server.expires_at > now,
                        Server.expires_at <= now + timedelta(days=3),
                    )
                )
                for server in res.scalars().all():
                    exp = server.expires_at
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    days_left = max(1, -(-int((exp - now).total_seconds()) // 86400))
                    extra = dict(server.extra_data or {})
                    if extra.get("renew_notice_sent") == days_left:
                        continue
                    extra["renew_notice_sent"] = days_left
                    server.extra_data = extra
                    await _tell(server.user_id, _renew_reminder_text(
                        server.name, server.ip_address or "—", days_left))

                # ── ۲) سررسید: تمدید خودکار یا ساسپند ۲۴ساعته
                result = await session.execute(
                    select(Server).where(
                        Server.billing_type == BillingType.MONTHLY,
                        Server.status == ServerStatus.ACTIVE,
                        Server.expires_at <= now,
                    )
                )
                for server in result.scalars().all():
                    success = await billing.charge_monthly(server)
                    if success is None:
                        # قیمت/نرخ ارز در دسترس نیست — نه تمدید، نه تعلیق؛ اجرای
                        # بعدی (بعد از آپدیت نرخ) جبران می‌کند.
                        continue
                    if success:
                        server.expires_at = now + timedelta(days=30)
                        extra = dict(server.extra_data or {})
                        extra.pop("renew_notice_sent", None)
                        server.extra_data = extra
                        await _tell(server.user_id, _renewed_text(
                            billing.last_monthly_charge_toman, server.name,
                            server.ip_address or "—"))
                    else:
                        await billing.suspend_server_db(server, SuspendReason.EXPIRED)
                        try:
                            account = await session.get(ProviderAccount, server.provider_account_id)
                            if account:
                                await get_provider(account).suspend_server(server.provider_server_id)
                        except Exception:
                            pass
                        await _tell(server.user_id, _renew_suspend_text(
                            server.name, server.ip_address or "—"))

                # ── ۳) ساسپندِ منقضیِ بیش از ۲۴ ساعت: تلاش آخر، وگرنه حذف
                res = await session.execute(
                    select(Server).where(
                        Server.billing_type == BillingType.MONTHLY,
                        Server.status == ServerStatus.SUSPENDED,
                        Server.suspend_reason == SuspendReason.EXPIRED,
                        Server.suspended_at <= now - timedelta(hours=24),
                    )
                )
                for server in res.scalars().all():
                    if await _renew_and_unsuspend(session, billing, server, bot):
                        continue
                    try:
                        if server.provider_account_id and server.provider_server_id:
                            account = await session.get(ProviderAccount, server.provider_account_id)
                            if account:
                                await get_provider(account).delete_server(server.provider_server_id)
                    except Exception:
                        pass
                    server.status = ServerStatus.DELETED
                    await _tell(server.user_id,
                                f"کاربر گرامی سرویس {server.name} به علت عدم تمدید حذف شد." + _SIGN)

                await session.commit()
            finally:
                await bot.session.close()

    try:
        _run(_do())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120)


@app.task(name="bot.tasks.billing.retry_expired_renewals", bind=True, max_retries=1)
def retry_expired_renewals(self):
    """هر ۱۰ دقیقه: سرورهای ماهانه‌ی ساسپند‌شده به دلیل انقضا — اگر کاربر کیف پول
    را شارژ کرده باشد، مبلغ خودکار کسر و سرویس تمدید/آن‌ساسپند می‌شود (بدون
    انتظار تا اجرای شبانه)."""
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import Server, ServerStatus, BillingType, SuspendReason
        from bot.services.billing import BillingService
        from sqlalchemy import select
        from aiogram import Bot
        from bot.config import settings
        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        async with AsyncSessionFactory() as session:
            billing = BillingService(session)
            res = await session.execute(
                select(Server).where(
                    Server.billing_type == BillingType.MONTHLY,
                    Server.status == ServerStatus.SUSPENDED,
                    Server.suspend_reason == SuspendReason.EXPIRED,
                )
            )
            servers = list(res.scalars().all())
            if not servers:
                return
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                for server in servers:
                    await _renew_and_unsuspend(session, billing, server, bot)
                await session.commit()
            finally:
                await bot.session.close()

    try:
        _run(_do())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120)


@app.task(name="bot.tasks.billing.send_low_balance_alerts")
def send_low_balance_alerts():
    """هر روز به کاربرانی که موجودی کمی دارند هشدار می‌دهد."""
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import User, Server, ServerStatus
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(User).where(
                    User.balance < 5000,  # کمتر از ۵۰۰۰ تومان
                    User.status == "active",
                )
            )
            users = list(result.scalars().all())
            for user in users:
                # چک کن سرور فعال داشته باشد
                srv_result = await session.execute(
                    select(Server).where(
                        Server.user_id == user.id,
                        Server.status == ServerStatus.ACTIVE,
                    ).limit(1)
                )
                if srv_result.scalar_one_or_none():
                    from bot.tasks.server import notify_low_balance
                    notify_low_balance.delay(user.telegram_id, user.balance)

    _run(_do())


@app.task(name="bot.tasks.billing.cleanup_old_transactions")
def cleanup_old_transactions():
    """هر 72 ساعت تراکنش‌های قدیمی‌تر از 72 ساعت را پاک می‌کند."""
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import Transaction
        from sqlalchemy import delete

        cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                delete(Transaction).where(Transaction.created_at < cutoff)
            )
            await session.commit()  # بدون commit هیچ رکوردی حذف نمی‌شد (باگ قبلی)
            import logging
            logging.getLogger(__name__).info(
                "cleanup_old_transactions: deleted %s old transactions", result.rowcount
            )

    _run(_do())
