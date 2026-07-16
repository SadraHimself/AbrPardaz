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

            from bot.services.currency import obj_currency, to_toman

            for server in servers:
                success = await billing.charge_hourly(server)
                if success:
                    user_obj = await session.get(User, server.user_id)
                    # مبلغ نوتیف همیشه ریالی است (پلن ارزی با نرخ روز تبدیل می‌شود)
                    cur = obj_currency(server)
                    amount_toman = float(server.price_hourly or 0)
                    if cur != "irt":
                        amount_toman = await to_toman(session, amount_toman, cur)
                    from bot.tasks.server import notify_hourly_billing
                    notify_hourly_billing.delay(
                        server.user_id, server.id,
                        amount_toman,
                        float(user_obj.balance if user_obj else 0),
                    )
                else:
                    users_empty_balance.add(server.user_id)

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
                        # anchor به ساخت، مثل بیلینگ ساعتی سرور (بدون drift)
                        created = snap.created_at
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        elapsed = int((datetime.now(timezone.utc) - created).total_seconds() // 3600)
                        snap.last_billed_at = created + timedelta(hours=max(elapsed, 1))
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

            # کاربر موجودی شارژ کرده — grace period پاک کن
            if user.balance > 0:
                extra = dict(user.extra_data or {})
                if "balance_empty_at" in extra or "balance_warn_level" in extra:
                    extra.pop("balance_empty_at", None)
                    extra.pop("balance_warn_level", None)
                    user.extra_data = extra
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

                if elapsed_hours >= 3 and warn_level < 3:
                    # حذف کامل تمام سرورها
                    srv_result = await session.execute(
                        select(Server).where(
                            Server.user_id == user.id,
                            Server.status != ServerStatus.DELETED,
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
                        '<tg-emoji emoji-id="5258093637450866522">🤖</tg-emoji> کاربر عزیز سرویس های فعال شما به دلیل عدم شارژ کیف پول حذف شدند',
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


@app.task(name="bot.tasks.billing.run_monthly_expiry_check", bind=True, max_retries=3)
def run_monthly_expiry_check(self):
    """
    هر شب چک می‌کند آیا سرور ماهیانه منقضی شده.
    اگر شده → شارژ ماهیانه را کسر می‌کند یا ساسپند می‌شود.
    """
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import Server, ServerStatus, BillingType, SuspendReason
        from bot.services.billing import BillingService
        from bot.providers import get_provider
        from sqlalchemy import select
        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        now = datetime.now(timezone.utc)

        async with AsyncSessionFactory() as session:
            billing = BillingService(session)
            result = await session.execute(
                select(Server).where(
                    Server.billing_type == BillingType.MONTHLY,
                    Server.status == ServerStatus.ACTIVE,
                    Server.expires_at <= now,
                )
            )
            servers = list(result.scalars().all())

            for server in servers:
                success = await billing.charge_monthly(server)
                if success:
                    from datetime import timedelta
                    server.expires_at = now + timedelta(days=30)
                else:
                    await billing.suspend_server_db(server, SuspendReason.EXPIRED)
                    try:
                        account = await session.get(
                            __import__("bot.database.models", fromlist=["ProviderAccount"]).ProviderAccount,
                            server.provider_account_id,
                        )
                        if account:
                            provider = get_provider(account)
                            await provider.suspend_server(server.provider_server_id)
                    except Exception:
                        pass
                    from bot.tasks.server import notify_user_suspend
                    notify_user_suspend.delay(server.user_id, server.id)

            await session.commit()

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
