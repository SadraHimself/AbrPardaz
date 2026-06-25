"""Celery tasks: server-related async operations."""
from __future__ import annotations

import asyncio

from bot.tasks.celery_app import app


def _run(coro):
    return asyncio.run(coro)


@app.task(name="bot.tasks.server.sync_all_traffic")
def sync_all_traffic():
    """هر ۱۵ دقیقه ترافیک همه سرورهای فعال را آپدیت می‌کند."""
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import Server, ServerStatus, SuspendReason
        from bot.services.billing import BillingService
        from bot.providers import get_provider
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            billing = BillingService(session)
            result = await session.execute(
                select(Server).where(Server.status == ServerStatus.ACTIVE)
            )
            servers = list(result.scalars().all())

            for server in servers:
                if not server.provider_account_id or not server.provider_server_id:
                    continue
                try:
                    account = await session.get(
                        __import__("bot.database.models", fromlist=["ProviderAccount"]).ProviderAccount,
                        server.provider_account_id,
                    )
                    if not account:
                        continue
                    provider = get_provider(account)
                    used_gb = await provider.get_traffic(server.provider_server_id)
                    ok = await billing.update_traffic(server, used_gb)

                    if not ok:
                        # ترافیک تمام شد
                        await billing.suspend_server_db(server, SuspendReason.TRAFFIC_EXCEEDED)
                        await provider.suspend_server(server.provider_server_id)
                        notify_user_traffic_exceeded.delay(server.user_id, server.id)

                    elif server.traffic_limit_gb and (used_gb / server.traffic_limit_gb) >= 0.8:
                        # هشدار ۸۰٪ مصرف
                        pct = int(used_gb / server.traffic_limit_gb * 100)
                        notify_traffic_warning.delay(server.user_id, server.id, pct)

                except Exception:
                    pass  # ادامه با سرور بعدی

            await session.commit()

    _run(_do())


@app.task(name="bot.tasks.server.notify_user_suspend")
def notify_user_suspend(user_id: int, server_id: int):
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import User, Server
        from bot.services.notification import NotificationService
        from aiogram import Bot
        from bot.config import settings

        async with AsyncSessionFactory() as session:
            user = await session.get(User, user_id)
            server = await session.get(Server, server_id)
            if user and server:
                bot = Bot(token=settings.BOT_TOKEN)
                notif = NotificationService(bot)
                await notif.server_suspended(user.telegram_id, server)
                await bot.session.close()

    _run(_do())


@app.task(name="bot.tasks.server.notify_low_balance")
def notify_low_balance(telegram_id: int, balance: float):
    async def _do():
        from aiogram import Bot
        from bot.config import settings
        from bot.services.notification import NotificationService

        bot = Bot(token=settings.BOT_TOKEN)
        notif = NotificationService(bot)
        await notif.low_balance_warning(telegram_id, balance)
        await bot.session.close()

    _run(_do())


@app.task(name="bot.tasks.server.notify_user_traffic_exceeded")
def notify_user_traffic_exceeded(user_id: int, server_id: int):
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import User, Server
        from bot.services.notification import NotificationService
        from aiogram import Bot
        from bot.config import settings

        async with AsyncSessionFactory() as session:
            user = await session.get(User, user_id)
            server = await session.get(Server, server_id)
            if user and server:
                bot = Bot(token=settings.BOT_TOKEN)
                notif = NotificationService(bot)
                await notif.traffic_exceeded(user.telegram_id, server)
                await bot.session.close()

    _run(_do())


@app.task(name="bot.tasks.server.notify_hourly_billing")
def notify_hourly_billing(user_id: int, server_id: int, amount: float, new_balance: float):
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import User, Server
        from aiogram import Bot
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from bot.config import settings

        async with AsyncSessionFactory() as session:
            user = await session.get(User, user_id)
            server = await session.get(Server, server_id)
            if not user or not server:
                return
            extra = server.extra_data or {}
            if not extra.get("hourly_notify", True):
                return
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🔕 خاموش کردن اطلاع‌رسانی",
                        callback_data=f"srv_mute_hourly:{server_id}",
                    )
                ]])
                await bot.send_message(
                    user.telegram_id,
                    f"⏱ <b>بیلینگ ساعتی</b>\n\n"
                    f"🖥 سرور: {server.name}\n"
                    f"💸 کسر شد: {amount:,.0f} تومان\n"
                    f"💰 موجودی جدید: {new_balance:,.0f} تومان",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            finally:
                await bot.session.close()

    _run(_do())


@app.task(name="bot.tasks.server.sync_building_servers")
def sync_building_servers():
    """هر ۵ دقیقه سرورهای در حال ساخت/ریبیلد را چک و وضعیت را آپدیت می‌کند."""
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import Server, ServerStatus
        from bot.services.server import ServerService
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            svc = ServerService(session)
            result = await session.execute(
                select(Server).where(
                    Server.status.in_([ServerStatus.BUILDING, ServerStatus.REBUILDING])
                )
            )
            servers = list(result.scalars().all())

            for server in servers:
                if not server.provider_account_id or not server.provider_server_id:
                    continue
                try:
                    await svc.sync_server_status(server)
                except Exception:
                    pass

            await session.commit()

    _run(_do())


@app.task(name="bot.tasks.server.notify_traffic_warning")
def notify_traffic_warning(user_id: int, server_id: int, percent: int):
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import User, Server
        from bot.services.notification import NotificationService
        from aiogram import Bot
        from bot.config import settings

        async with AsyncSessionFactory() as session:
            user = await session.get(User, user_id)
            server = await session.get(Server, server_id)
            if user and server:
                bot = Bot(token=settings.BOT_TOKEN)
                notif = NotificationService(bot)
                await notif.traffic_warning(user.telegram_id, server, percent)
                await bot.session.close()

    _run(_do())
