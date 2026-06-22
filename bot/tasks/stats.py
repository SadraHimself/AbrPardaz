"""Daily statistics aggregation + 30-day cleanup."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from bot.database.models import DailyStat, Server, ServerStatus, Transaction, TransactionType, User, UserStatus
from bot.tasks.celery_app import app


def _run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


@app.task(name="bot.tasks.stats.aggregate_daily_stats", bind=True, max_retries=3)
def aggregate_daily_stats(self):
    try:
        _run_async(_do_aggregate())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=300)


@app.task(name="bot.tasks.stats.cleanup_old_stats")
def cleanup_old_stats():
    _run_async(_do_cleanup())


async def _do_aggregate():
    from bot.database.session import AsyncSessionFactory
    today = date.today()
    start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    async with AsyncSessionFactory() as session:
        existing = await session.execute(select(DailyStat).where(DailyStat.date == today))
        stat = existing.scalar_one_or_none()

        new_users = (await session.execute(
            select(func.count(User.id)).where(User.created_at >= start, User.created_at < end)
        )).scalar_one() or 0

        new_servers = (await session.execute(
            select(func.count(Server.id)).where(Server.created_at >= start, Server.created_at < end)
        )).scalar_one() or 0

        revenue = float((await session.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.created_at >= start,
                Transaction.created_at < end,
                Transaction.transaction_type == TransactionType.DEBIT,
            )
        )).scalar_one() or 0)

        active_users = (await session.execute(
            select(func.count(func.distinct(Server.user_id))).where(
                Server.status == ServerStatus.ACTIVE
            )
        )).scalar_one() or 0

        total_wallet = float((await session.execute(
            select(func.coalesce(func.sum(User.balance), 0)).where(
                User.status == UserStatus.ACTIVE
            )
        )).scalar_one() or 0)

        if stat:
            stat.new_users = new_users
            stat.new_servers = new_servers
            stat.revenue = revenue
            stat.active_users = active_users
            stat.total_wallet = total_wallet
        else:
            stat = DailyStat(
                date=today,
                new_users=new_users,
                new_servers=new_servers,
                revenue=revenue,
                active_users=active_users,
                total_wallet=total_wallet,
            )
            session.add(stat)

        await session.commit()


async def _do_cleanup():
    from bot.database.session import AsyncSessionFactory
    cutoff = date.today() - timedelta(days=30)
    async with AsyncSessionFactory() as session:
        await session.execute(delete(DailyStat).where(DailyStat.date < cutoff))
        await session.commit()
