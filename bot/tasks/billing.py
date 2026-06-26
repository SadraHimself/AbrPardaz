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
    هر ساعت اجرا می‌شود. برای هر سرور ساعتی فعال:
      1. یک ساعت از کیف پول کسر می‌شود.
      2. اگر موجودی ناکافی → سرور ساسپند می‌شود.
      3. اگر سرور قبلاً ساسپند بود و موجودی داشت → رفع ساسپند.
    """
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import Server, ServerStatus, SuspendReason, User
        from bot.services.billing import BillingService
        from bot.providers import get_provider
        from sqlalchemy import select
        # Reset pool — each asyncio.run() creates a new event loop; old pool connections are invalid
        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        async with AsyncSessionFactory() as session:
            billing = BillingService(session)
            servers = await billing.get_active_servers_for_billing()

            for server in servers:
                success = await billing.charge_hourly(server)
                if success:
                    user_obj = await session.get(User, server.user_id)
                    from bot.tasks.server import notify_hourly_billing
                    notify_hourly_billing.delay(
                        server.user_id, server.id,
                        float(server.price_hourly or 0),
                        float(user_obj.balance if user_obj else 0),
                    )
                else:
                    # کسر موجودی ناموفق → ساسپند
                    await billing.suspend_server_db(server, SuspendReason.LOW_BALANCE)
                    try:
                        account_result = await session.get(
                            __import__("bot.database.models", fromlist=["ProviderAccount"]).ProviderAccount,
                            server.provider_account_id,
                        )
                        if account_result:
                            provider = get_provider(account_result)
                            await provider.suspend_server(server.provider_server_id)
                    except Exception:
                        pass

                    from bot.tasks.server import notify_user_suspend
                    notify_user_suspend.delay(server.user_id, server.id)

            await session.commit()

    try:
        _run(_do())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


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
            await session.execute(delete(Transaction).where(Transaction.created_at < cutoff))

    _run(_do())
