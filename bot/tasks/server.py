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
        from bot.database.session import engine, AsyncSessionFactory
        try:
            await engine.dispose(close=False)
        except Exception:
            pass
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
                        text="خاموش کردن اطلاع‌رسانی",
                        callback_data=f"srv_mute_hourly:{server_id}",
                        **{"icon_custom_emoji_id": "5990205245806875298"},
                    )
                ]])
                await bot.send_message(
                    user.telegram_id,
                    f'<tg-emoji emoji-id="5852614259082530343">⏱</tg-emoji> <b>بیلینگ ساعتی</b>\n\n'
                    f"سرور: {server.name}\n"
                    f"کسر شد: {amount:,.0f} تومان\n"
                    f'<tg-emoji emoji-id="5778318458802409852">💰</tg-emoji> موجودی جدید: {new_balance:,.0f} تومان',
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


@app.task(name="bot.tasks.server.check_providers_health")
def check_providers_health():
    """هر ۳۰ دقیقه اتصال هر پروایدر ویرچولایزور را تست می‌کند؛ در صورت تغییر وضعیت
    (قطع/وصل) در تاپیک «لاگ سرور» اطلاع می‌دهد. (فقط هنگام تغییر، تا اسپم نشود.)"""
    async def _do():
        from bot.database.session import AsyncSessionFactory, engine
        try:
            await engine.dispose(close=False)
        except Exception:
            pass
        from bot.database.models import ProviderAccount
        from bot.providers import get_provider
        from bot.services.log_service import LogService
        from aiogram import Bot
        from bot.config import settings
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(ProviderAccount).where(ProviderAccount.is_active == True)
            )
            accounts = list(result.scalars().all())
            if not accounts:
                return

            bot = Bot(token=settings.BOT_TOKEN)
            log = LogService(bot, session)
            try:
                for account in accounts:
                    prev_ok = (account.extra_config or {}).get("health_ok", True)
                    ok, reason = True, ""
                    try:
                        prov = get_provider(account)
                        if hasattr(prov, "ping"):
                            await prov.ping()   # سبک (هتزنر) — کل کاتالوگ لازم نیست
                        else:
                            await prov.list_plans()
                    except Exception as e:
                        ok, reason = False, str(e)[:200]

                    if ok != prev_ok:
                        cfg = dict(account.extra_config or {})
                        cfg["health_ok"] = ok
                        account.extra_config = cfg
                        if ok:
                            await log.log_provider_up(account.name)
                        else:
                            await log.log_provider_down(account.name, reason)
                await session.commit()
            finally:
                await bot.session.close()

    _run(_do())


@app.task(name="bot.tasks.server.sync_hetzner_catalog", bind=True, max_retries=1)
def sync_hetzner_catalog(self):
    """هر ۳۰ دقیقه موجودی کاتالوگ هتزنر sync می‌شود:
    - پلن ایمپورت‌شده‌ای که در لوکیشنش ناموجود شود → خودکار غیرفعال + لاگ
    - اگر دوباره موجود شود → وضعیت قبلی برمی‌گردد + لاگ
    - قیمت خرید (cost_hourly/cost_monthly) هم با نرخ روز هتزنر آپدیت می‌شود
    """
    async def _do():
        from bot.database.session import AsyncSessionFactory, engine
        try:
            await engine.dispose(close=False)
        except Exception:
            pass
        from aiogram import Bot
        from bot.config import settings
        from bot.database.models import ProviderAccount, ProviderType, ServerPlan
        from bot.providers.hetzner import HetznerProvider
        from bot.services.log_service import LogService
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            accounts = list((await session.execute(
                select(ProviderAccount).where(
                    ProviderAccount.provider_type == ProviderType.HETZNER,
                    ProviderAccount.is_active == True,
                )
            )).scalars().all())
            if not accounts:
                return

            bot = Bot(token=settings.BOT_TOKEN)
            log = LogService(bot, session)
            try:
                for account in accounts:
                    plans = list((await session.execute(
                        select(ServerPlan).where(
                            ServerPlan.provider_account_id == account.id
                        )
                    )).scalars().all())
                    if not plans:
                        continue
                    prov = HetznerProvider(api_token=account.api_key or "")
                    for loc in {p.location for p in plans if p.location}:
                        try:
                            offered = {o.provider_plan_id: o
                                       for o in await prov.list_plans(location=loc)}
                        except Exception:
                            continue  # خطای گذرا — دور بعدی جبران می‌شود
                        for plan in [p for p in plans if p.location == loc]:
                            extra = dict(plan.extra_data or {})
                            info = offered.get(plan.provider_plan_id)
                            if info is None:
                                if not extra.get("unavailable"):
                                    extra["unavailable"] = True
                                    extra["was_active"] = plan.is_active
                                    plan.is_active = False
                                    plan.extra_data = extra
                                    await log.log_plan_unavailable(
                                        plan.display_name or plan.name, loc)
                                continue
                            changed = False
                            if extra.get("cost_hourly") != info.price_hourly:
                                extra["cost_hourly"] = info.price_hourly
                                changed = True
                            if extra.get("cost_monthly") != info.price_monthly:
                                extra["cost_monthly"] = info.price_monthly
                                changed = True
                            if extra.get("unavailable"):
                                extra.pop("unavailable", None)
                                plan.is_active = bool(extra.pop("was_active", False))
                                changed = True
                                await log.log_plan_available(
                                    plan.display_name or plan.name, loc)
                            if changed:
                                plan.extra_data = extra
                                # قیمت فروش دنبال قیمت خرید (سود درصدی اکانت)
                                cfg = account.extra_config or {}
                                mh = cfg.get("margin_hourly")
                                mm = cfg.get("margin_monthly")
                                if mh is not None and info.price_hourly:
                                    plan.price_hourly = round(
                                        float(info.price_hourly) * (1 + float(mh) / 100), 4)
                                if mm is not None and info.price_monthly:
                                    plan.price_monthly = round(
                                        float(info.price_monthly) * (1 + float(mm) / 100), 2)
                await session.commit()
            finally:
                await bot.session.close()

    try:
        _run(_do())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120)


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
