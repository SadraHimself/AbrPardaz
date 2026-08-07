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
                    # ویرچولایزور: سقف هم زنده خوانده می‌شود. ماژول WHMCS سرِ
                    # سالگرد ماهانه‌ی هر VM سقف را «سقف منهای مصرف دوره» می‌کند،
                    # پس کپیِ دیتابیس کهنه می‌شود و تعلیقِ اشتباه می‌دهد.
                    # منبع حقیقت = پنل (یک درخواست، هم سقف هم مصرف).
                    if hasattr(provider, "bandwidth_info"):
                        quota_gb, used_gb = await provider.bandwidth_info(
                            server.provider_server_id)
                        if quota_gb > 0:
                            if server.traffic_limit_gb != float(quota_gb):
                                server.traffic_limit_gb = float(quota_gb)
                        else:
                            # صفر = نامحدود (هرگز 0.0 ننویس؛ update_traffic
                            # بلافاصله TRAFFIC_EXCEEDED می‌کند)
                            server.traffic_limit_gb = None
                    else:
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
                    cfg = dict(account.extra_config or {})
                    prev_ok = cfg.get("health_ok", True)

                    # چند تلاش با فاصله — یک تایم‌اوت گذرا نباید «قطعی» اعلام شود
                    ok, reason = False, ""
                    for attempt in range(3):
                        try:
                            prov = get_provider(account)
                            if hasattr(prov, "ping"):
                                await prov.ping()   # سبک (هتزنر)
                            else:
                                await prov.list_plans()
                            ok, reason = True, ""
                            break
                        except Exception as e:
                            reason = str(e)[:200]
                            await asyncio.sleep(5)

                    # آستانه‌ی تأیید: فقط پس از ۲ چکِ ناموفقِ پشت‌سرهم «قطع» اعلام کن
                    # (یک اجرای ناموفق تنها = نوسان گذرا، اعلام نمی‌شود)
                    fail_streak = int(cfg.get("health_fail_streak", 0))
                    changed = False
                    if ok:
                        if fail_streak or not prev_ok:
                            changed = True
                        cfg["health_fail_streak"] = 0
                        if prev_ok is False:
                            cfg["health_ok"] = True
                            await log.log_provider_up(account.name,
                                                      account.provider_type)
                    else:
                        fail_streak += 1
                        cfg["health_fail_streak"] = fail_streak
                        if fail_streak >= 2 and prev_ok is not False:
                            cfg["health_ok"] = False
                            changed = True
                            await log.log_provider_down(account.name, reason,
                                                        account.provider_type)

                    account.extra_config = cfg
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
        from bot.services.hetzner_settings import get_margins
        from bot.services.log_service import LogService
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            # کاتالوگ مشترک: پلن‌ها مستقل از اکانت‌اند؛ برای خواندن قیمت زنده از
            # اکانتِ مرجعِ هر پلن استفاده می‌کنیم. سود سراسری است.
            mh, mm = await get_margins(session)
            accounts = list((await session.execute(
                select(ProviderAccount).where(
                    ProviderAccount.provider_type == ProviderType.HETZNER,
                    ProviderAccount.is_active == True,
                )
            )).scalars().all())
            if not accounts:
                return
            acc_by_id = {a.id: a for a in accounts}
            fallback_acc = accounts[0]

            bot = Bot(token=settings.BOT_TOKEN)
            log = LogService(bot, session)
            try:
                all_plans = list((await session.execute(
                    select(ServerPlan).where(
                        ServerPlan.provider_type == ProviderType.HETZNER
                    )
                )).scalars().all())
                if all_plans:
                    ref = acc_by_id.get(all_plans[0].provider_account_id) or fallback_acc
                    prov = HetznerProvider(api_token=ref.api_key or "")
                    plans = all_plans
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
                                # قیمت فروش دنبال قیمت خرید (سود سراسری هتزنر)
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


@app.task(name="bot.tasks.server.sync_gcore_catalog", bind=True, max_retries=1)
def sync_gcore_catalog(self):
    """هر ۳۰ دقیقه (دقیقه ۲۰ و ۵۰) کاتالوگ جیکور sync می‌شود:
    - flavor ناموجود/غیرفعال‌شده در region → پلن خودکار غیرفعال + لاگ (was_active)
    - دوباره موجود → برگشت وضعیت + لاگ
    - قیمت خرید = flavor تازه + دیسکِ پلن × نرخ دیسک؛ فروش = خرید × (۱+سود٪)
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
        from bot.providers.gcore import GcoreProvider
        from bot.services.gcore_settings import (
            disk_monthly_cost, full_costs, get_ip_rate, get_margins,
            get_volume_rate, ip_monthly_cost, is_excluded_flavor,
        )
        from bot.services.log_service import LogService
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            account = (await session.execute(
                select(ProviderAccount).where(
                    ProviderAccount.provider_type == ProviderType.GCORE,
                    ProviderAccount.is_active == True,
                ).order_by(ProviderAccount.id)
            )).scalars().first()
            if not account:
                return
            all_plans = list((await session.execute(
                select(ServerPlan).where(
                    ServerPlan.provider_type == ProviderType.GCORE
                )
            )).scalars().all())
            if not all_plans:
                return

            mh, _mm_unused = await get_margins(session)
            manual_rate = await get_volume_rate(session)   # 0 = قیمت زنده از API
            ip_manual = await get_ip_rate(session)         # 0 = قیمت زنده از API
            _dm_cache: dict = {}
            prov = GcoreProvider(
                api_token=account.api_key or "",
                project_id=(account.extra_config or {}).get("project_id") or 0,
            )

            bot = Bot(token=settings.BOT_TOKEN)
            log = LogService(bot, session)
            try:
                # پلن‌ها به تفکیک region (شناسه عددی در extra_data.region_id)
                region_ids = {int((p.extra_data or {}).get("region_id") or 0)
                              for p in all_plans}
                region_ids.discard(0)
                for rid in region_ids:
                    try:
                        offered = {o.provider_plan_id: o
                                   for o in await prov.list_plans(location=str(rid))}
                    except Exception:
                        continue  # خطای گذرا — دور بعدی جبران می‌شود
                    for plan in [p for p in all_plans
                                 if int((p.extra_data or {}).get("region_id") or 0) == rid]:
                        # خانواده‌های استثناشده (shared/memory) نباید در فروش بمانند
                        if is_excluded_flavor(plan.provider_plan_id or ""):
                            if plan.is_active:
                                plan.is_active = False
                            continue
                        extra = dict(plan.extra_data or {})
                        info = offered.get(plan.provider_plan_id)
                        loc_label = extra.get("region_name") or plan.location or str(rid)
                        if info is None:
                            if not extra.get("unavailable"):
                                extra["unavailable"] = True
                                extra["was_active"] = plan.is_active
                                plan.is_active = False
                                plan.extra_data = extra
                                await log.log_plan_unavailable(
                                    plan.display_name or plan.name, loc_label)
                            continue
                        # نوع دیسک/سرعت: پلن‌های قدیمیِ بدون volume_type = Standard؛
                        # لیبل/سرعت همیشه با VOLUME_TYPES نرمال می‌شود (self-heal —
                        # تغییرِ برندینگ روی پلن‌های ایمپورت‌شده‌ی قبلی هم بنشیند)
                        from bot.services.gcore_settings import VOLUME_TYPES as _VTS
                        _vt = extra.get("volume_type") or "standard"
                        _vmeta = _VTS.get(_vt) or _VTS["standard"]
                        changed = False
                        if extra.get("volume_type") != _vt or \
                           extra.get("volume_label") != _vmeta["label"] or \
                           extra.get("bandwidth_mbit") != _vmeta["mbit"]:
                            extra["volume_type"] = _vt
                            extra["volume_label"] = _vmeta["label"]
                            extra["bandwidth_mbit"] = _vmeta["mbit"]
                            changed = True
                        # قیمت خرید کامل = flavor تازه + دیسک (per نوع) + External IP
                        # (نرخ دستی یا قیمت زنده از API). زنده‌ی ناموفق → قیمت قبلی حفظ
                        dm = await disk_monthly_cost(
                            session, prov, rid, int(plan.disk or 0), _dm_cache,
                            type_name=_vt)
                        ipm = await ip_monthly_cost(session, prov, rid, _dm_cache)
                        if (dm <= 0 and (manual_rate <= 0 or _vt != "standard")) or \
                           (ipm <= 0 and ip_manual <= 0):
                            ch = extra.get("cost_hourly")
                            cm = extra.get("cost_monthly")
                        else:
                            ch, cm = full_costs(info.price_hourly or 0,
                                                info.price_monthly or 0, dm, ipm)
                        if extra.get("flavor_cost_hourly") != info.price_hourly or \
                           extra.get("flavor_cost_monthly") != info.price_monthly or \
                           extra.get("cost_hourly") != ch or \
                           extra.get("cost_monthly") != cm:
                            extra["flavor_cost_hourly"] = info.price_hourly
                            extra["flavor_cost_monthly"] = info.price_monthly
                            extra["cost_hourly"], extra["cost_monthly"] = ch, cm
                            if ipm > 0:
                                extra["ip_cost_monthly"] = ipm
                            if info.currency:
                                extra["currency"] = info.currency
                            changed = True
                        if extra.get("unavailable"):
                            extra.pop("unavailable", None)
                            plan.is_active = bool(extra.pop("was_active", False))
                            changed = True
                            await log.log_plan_available(
                                plan.display_name or plan.name, loc_label)
                        if changed:
                            plan.extra_data = extra
                            # قیمت فروش دنبال قیمت خرید (سود سراسری جیکور — فقط ساعتی)
                            if mh is not None and extra.get("cost_hourly"):
                                plan.price_hourly = round(
                                    float(extra["cost_hourly"]) * (1 + float(mh) / 100), 4)
                        if plan.price_monthly is not None:
                            plan.price_monthly = None  # فروش جیکور فقط ساعتی است
                await session.commit()
            finally:
                await bot.session.close()

    try:
        _run(_do())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120)


@app.task(name="bot.tasks.server.sync_timeweb_catalog", bind=True, max_retries=1)
def sync_timeweb_catalog(self):
    """هر ۳۰ دقیقه (دقیقه ۵ و ۳۵) کاتالوگ تایم‌وب sync می‌شود:
    - تعرفه‌ای که دیگر عرضه نشود → پلن خودکار غیرفعال + لاگ (was_active)
    - دوباره عرضه شود → برگشت وضعیت + لاگ
    - قیمت خرید تازه (₽ ماهانه + ساعتی=÷۷۲۰) → فروش با سود سراسری بازمحاسبه
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
        from bot.providers.timeweb import TimewebProvider
        from bot.services.timeweb_settings import full_costs as tw_full_costs
        from bot.services.timeweb_settings import get_margins, restore_cooldown_passed
        from bot.services.log_service import LogService
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            account = (await session.execute(
                select(ProviderAccount).where(
                    ProviderAccount.provider_type == ProviderType.TIMEWEB,
                    ProviderAccount.is_active == True,
                ).order_by(ProviderAccount.id)
            )).scalars().first()
            if not account:
                return
            all_plans = list((await session.execute(
                select(ServerPlan).where(
                    ServerPlan.provider_type == ProviderType.TIMEWEB
                )
            )).scalars().all())
            if not all_plans:
                return

            # موجودی: پلن‌هایی که کول‌داونِ ناموجودی‌شان تمام شده دوباره باز شوند
            # (auto-retry — چون API تایم‌وب فیلد موجودی ندارد و از شکست یاد می‌گیریم)
            try:
                await restore_cooldown_passed(session)
            except Exception:
                pass

            mh, mm = await get_margins(session)
            prov = TimewebProvider(api_token=account.api_key or "")
            try:
                offered = {p.provider_plan_id: p for p in await prov.list_plans()}
                # تعرفه‌های خام برای بازسازی نام/شهر/نوع پلن‌های قدیمی
                _raw = await prov._request("GET", "/api/v1/presets/servers")
                raw_map = {str(x.get("id")): x for x in _raw.get("server_presets") or []}
            except Exception:
                await session.commit()   # restore_cooldown_passed را از دست نده
                return  # خطای گذرا — دور بعدی جبران می‌کند

            from bot.providers.timeweb import tw_build_labels
            _labels = tw_build_labels(list(raw_map.values()))

            bot = Bot(token=settings.BOT_TOKEN)
            log = LogService(bot, session)
            try:
                for plan in all_plans:
                    extra = dict(plan.extra_data or {})
                    info = offered.get(plan.provider_plan_id)
                    # بازسازی/تصحیح نام کوتاه/شهر/نوع از تعرفه‌ی زنده (self-heal
                    # برچسب‌های نادرستِ گذشته — فقط اگر تغییر کرده باشد می‌نویسد)
                    _lbl = _labels.get(plan.provider_plan_id)
                    if _lbl:
                        _city = _lbl["city"]
                        _tk = _lbl["cpu_type"]
                        _dn = _lbl["display_name"]
                        if (extra.get("region_name") != _city
                                or extra.get("cpu_type") != _tk
                                or extra.get("city_code") != _lbl["city_code"]
                                or plan.display_name != _dn):
                            extra["region_name"] = _city
                            extra["city_code"] = _lbl["city_code"]
                            extra["cpu_type"] = _tk
                            extra["cpu_type_label"] = _lbl["cpu_label"]
                            plan.display_name = _dn
                            plan.extra_data = extra
                    loc_label = extra.get("region_name") or plan.location or "?"
                    if info is None or (plan.location and info.location != plan.location):
                        if not extra.get("unavailable"):
                            extra["unavailable"] = True
                            extra["was_active"] = plan.is_active
                            plan.is_active = False
                            plan.extra_data = extra
                            await log.log_plan_unavailable(
                                plan.display_name or plan.name, loc_label)
                        continue
                    # قیمت خرید کامل = تعرفه تازه + IPv4 عمومی (سرویس جدای فاکتور)
                    ch, cm = tw_full_costs(info.price_monthly or 0)
                    changed = False
                    if extra.get("preset_rub") != info.price_monthly or \
                       extra.get("cost_hourly") != ch or \
                       extra.get("cost_monthly") != cm:
                        extra["preset_rub"] = info.price_monthly
                        extra["cost_hourly"] = ch
                        extra["cost_monthly"] = cm
                        changed = True
                    if extra.get("unavailable"):
                        extra.pop("unavailable", None)
                        _was = bool(extra.pop("was_active", False))
                        # اگر هنوز ناموجودِ ظرفیت است (کول‌داون فعال)، مخفی بماند
                        plan.is_active = _was and not extra.get("out_of_stock")
                        changed = True
                        await log.log_plan_available(
                            plan.display_name or plan.name, loc_label)
                    # تایم‌وب فقط-ماهانه (2026-07-26): ساعتی همیشه پاک می‌ماند
                    if plan.price_hourly is not None:
                        plan.price_hourly = None
                    if changed:
                        plan.extra_data = extra
                        # قیمت فروش دنبال قیمت خرید (سود ماهانه‌ی سراسری تایم‌وب)
                        if mm is not None and extra.get("cost_monthly"):
                            plan.price_monthly = round(
                                float(extra["cost_monthly"]) * (1 + float(mm) / 100), 2)
                await session.commit()

                # هشدار موجودی کمِ اکانت تایم‌وب — یک‌بار وقتی زیر آستانه می‌رود
                # (edge-triggered)؛ با شارژِ دوباره بالای آستانه ری‌آرم می‌شود که
                # اسپم نشود و دفعه‌ی بعد هم هشدار بدهد.
                try:
                    from bot.database.models import BotSettings
                    from bot.services.timeweb_settings import LOW_BALANCE_RUB
                    bal = await prov.get_balance()
                    if bal is not None:
                        flag = await session.get(
                            BotSettings, "timeweb_low_balance_alerted")
                        alerted = bool(flag and flag.value == "1")
                        if bal < LOW_BALANCE_RUB and not alerted:
                            await log.log_low_balance(bal, LOW_BALANCE_RUB)
                            if flag:
                                flag.value = "1"
                            else:
                                session.add(BotSettings(
                                    key="timeweb_low_balance_alerted", value="1"))
                            await session.commit()
                        elif bal >= LOW_BALANCE_RUB and alerted:
                            flag.value = "0"
                            await session.commit()
                except Exception:
                    pass

                # جاروکشِ یتیم‌ها (تضمین «به هر دلیلی نرسید → کامل پاک شود»):
                # سرورِ تایم‌وبیِ ساختِ ربات (کامنت abrpardaz) که در DB نیست و
                # بیش از ۴۵ دقیقه عمر دارد = بازمانده‌ی کرش/برق وسط ساخت یا
                # حذفِ ناموفق → در provider حذف می‌شود که بی‌سروصدا بیل نخورد؛
                # ادمین هم خبردار می‌شود (پولِ مشتری از Transactions قابل پیگیری است).
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    from bot.database.models import Server as _Srv, ServerStatus as _SSt
                    known = {
                        str(x) for x in (await session.execute(
                            select(_Srv.provider_server_id).where(
                                _Srv.provider_type == ProviderType.TIMEWEB,
                                _Srv.status != _SSt.DELETED,
                            )
                        )).scalars().all() if x
                    }
                    raw_srv = await prov._request(
                        "GET", "/api/v1/servers",
                        params={"limit": "100", "offset": "0"})
                    now_utc = _dt.now(_tz.utc)
                    for s in (raw_srv.get("servers") or []):
                        if not isinstance(s, dict):
                            continue
                        sid = str(s.get("id") or "")
                        comment = str(s.get("comment") or "")
                        status = (s.get("status") or "").lower()
                        if not sid or sid in known:
                            continue
                        if not comment.startswith("abrpardaz tg:"):
                            continue   # سرور دستی ادمین در پنل — دست نمی‌زنیم
                        if status in ("removing", "removed"):
                            continue
                        # سنِ سرور — created_at ناخوانا = دست نزن (شاید وسط ساخت باشد)
                        try:
                            created = _dt.fromisoformat(
                                str(s.get("created_at") or "").replace(" ", "T")
                                .replace("Z", "+00:00"))
                            if created.tzinfo is None:
                                created = created.replace(tzinfo=_tz.utc)
                        except Exception:
                            continue
                        if (now_utc - created).total_seconds() < 45 * 60:
                            continue
                        try:
                            await TimewebProvider(
                                api_token=account.api_key or "").delete_server(sid)
                            await log.log_timeweb_orphan_deleted(
                                s.get("name") or sid, comment)
                        except Exception as _e:
                            import logging as _lg
                            _lg.getLogger(__name__).warning(
                                "timeweb orphan %s delete failed: %s", sid, _e)
                except Exception:
                    pass
            finally:
                await bot.session.close()

    try:
        _run(_do())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120)


@app.task(name="bot.tasks.server.sync_rootvds_catalog", bind=True, max_retries=1)
def sync_rootvds_catalog(self):
    """هر ۳۰ دقیقه (دقیقه ۱۵ و ۴۵) کاتالوگ RootVDS sync می‌شود:
    - تعرفه‌ای که دیگر عرضه نشود → پلن خودکار غیرفعال + لاگ (was_active)
    - دوباره عرضه شود → برگشت وضعیت + لاگ
    - قیمت خرید تازه (₽ ماهانه + ساعتی=÷۷۲۰) → فروش با سود سراسری بازمحاسبه
    (auto-hide ظرفیت نداریم — درس تایم‌وب؛ فقط بودن/نبودن تعرفه در کاتالوگ)"""
    async def _do():
        from bot.database.session import AsyncSessionFactory, engine
        try:
            await engine.dispose(close=False)
        except Exception:
            pass
        from aiogram import Bot
        from bot.config import settings
        from bot.database.models import ProviderAccount, ProviderType, ServerPlan
        from bot.providers.rootvds import RootVDSProvider
        from bot.services.rootvds_settings import get_margins
        from bot.services.log_service import LogService
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            account = (await session.execute(
                select(ProviderAccount).where(
                    ProviderAccount.provider_type == ProviderType.ROOTVDS,
                    ProviderAccount.is_active == True,
                ).order_by(ProviderAccount.id)
            )).scalars().first()
            if not account:
                return
            all_plans = list((await session.execute(
                select(ServerPlan).where(
                    ServerPlan.provider_type == ProviderType.ROOTVDS
                )
            )).scalars().all())
            if not all_plans:
                return

            mh, mm = await get_margins(session)
            prov = RootVDSProvider(api_token=account.api_key or "")
            try:
                offered = {p.provider_plan_id: p for p in await prov.list_plans()}
            except Exception:
                return  # خطای گذرا — دور بعدی جبران می‌کند

            bot = Bot(token=settings.BOT_TOKEN)
            log = LogService(bot, session)
            try:
                for plan in all_plans:
                    extra = dict(plan.extra_data or {})
                    info = offered.get(plan.provider_plan_id)
                    loc_label = extra.get("region_name") or plan.location or "?"
                    if info is None or (plan.location and info.location != plan.location):
                        if not extra.get("unavailable"):
                            extra["unavailable"] = True
                            extra["was_active"] = plan.is_active
                            plan.is_active = False
                            plan.extra_data = extra
                            await log.log_plan_unavailable(
                                plan.display_name or plan.name, loc_label)
                        continue
                    changed = False
                    if extra.get("cost_hourly") != info.price_hourly or \
                       extra.get("cost_monthly") != info.price_monthly:
                        extra["cost_hourly"] = info.price_hourly
                        extra["cost_monthly"] = info.price_monthly
                        changed = True
                    if extra.get("unavailable"):
                        extra.pop("unavailable", None)
                        plan.is_active = bool(extra.pop("was_active", False))
                        changed = True
                        await log.log_plan_available(
                            plan.display_name or plan.name, loc_label)
                    if changed:
                        plan.extra_data = extra
                        # قیمت فروش دنبال قیمت خرید (سود سراسری RootVDS)
                        if mh is not None and extra.get("cost_hourly"):
                            plan.price_hourly = round(
                                float(extra["cost_hourly"]) * (1 + float(mh) / 100), 4)
                        if mm is not None and extra.get("cost_monthly"):
                            plan.price_monthly = round(
                                float(extra["cost_monthly"]) * (1 + float(mm) / 100), 2)
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
