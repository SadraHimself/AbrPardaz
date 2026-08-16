"""Celery tasks: برنامه ریسلر (فقط ویرچولایزور).

- sync_reseller_servers (هر ۱۰ دقیقه): کشف/ثبت VMهای هر ریسلر از پنل، حذف
  two-strike رکوردهای غایب، وصول خودکار بدهی «محاسبه گذشته»، پاک‌سازی وضعیت بدهی.
- notify_reseller_billing_summary (هر ۳ ساعت): یک پیام جمعِ کسرهای بازه به ریسلر
  (بیلینگ ساعتی می‌ماند؛ فقط اطلاع‌رسانی batch است).
- handle_reseller_debt (on-demand از run_hourly_billing): هشدار بدهی —
  کاربر هر ۳ ساعت، تاپیک moderation ادمین هر ۱ ساعت. هیچ اقدامی علیه سرورها نمی‌شود.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bot.tasks.celery_app import app

logger = logging.getLogger(__name__)


def _run(coro):
    return asyncio.run(coro)


def _due(ts_str, hours: float) -> bool:
    """آیا از آخرین رخداد بیش از `hours` ساعت گذشته (یا اصلاً رخ نداده)؟"""
    if not ts_str:
        return True
    try:
        dt = datetime.fromisoformat(str(ts_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - dt) >= timedelta(hours=hours)


@app.task(name="bot.tasks.reseller.sync_reseller_servers")
def sync_reseller_servers():
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import ProviderAccount, User
        from bot.providers import get_provider
        from bot.services import reseller as rs
        from bot.services.log_service import LogService
        from sqlalchemy import select
        from aiogram import Bot
        from bot.config import settings

        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        async with AsyncSessionFactory() as session:
            all_users = (await session.execute(
                select(User).where(User.extra_data.isnot(None))
            )).scalars().all()
            users = [u for u in all_users if rs.is_active_reseller(u)]
            if not users:
                return

            bot = Bot(token=settings.BOT_TOKEN)
            log = LogService(bot, session)
            try:
                by_account: dict[int, list] = {}
                for u in users:
                    try:
                        aid = int(rs.get_reseller_cfg(u).get("account_id") or 0)
                    except (TypeError, ValueError):
                        aid = 0
                    if aid:
                        by_account.setdefault(aid, []).append(u)

                for aid, aid_users in by_account.items():
                    account = await session.get(ProviderAccount, aid)
                    if not account or not account.is_active:
                        continue
                    provider = get_provider(account)
                    try:
                        # لیست ناقص/خالی خودش RuntimeError می‌دهد («لیست ناقص =
                        # خطا، نه سکوت») → این اکانت این دور کامل رها می‌شود و
                        # هیچ ثبتی/حذفی انجام نمی‌شود
                        panel_rows = await provider.list_all_vps()
                    except Exception as e:
                        logger.warning("reseller sync: panel list failed (account %s): %s", aid, e)
                        continue

                    for u in aid_users:
                        try:
                            cfg0 = rs.get_reseller_cfg(u)
                            email0 = str(cfg0.get("email") or "")
                            uid = str(cfg0.get("uid") or "")
                            if not uid:
                                # resolve بدون قفل — فراخوانی کُند پنل
                                found = await provider.find_user_by_email(email0)
                                if not found:
                                    await log.log_reseller_warn(
                                        u, "کاربر پنل با ایمیل ریسلر پیدا نشد — سینک انجام نشد.")
                                    continue
                                uid = str(found)

                            # قفل ردیف کاربر داخل سرویس گرفته می‌شود (کشِ uid هم
                            # زیر همان قفل نوشته می‌شود). None = کانفیگ هم‌زمان
                            # تغییر کرده/غیرفعال شده — این دور هیچ کاری نکن.
                            summary = await rs.sync_user_reseller_servers(
                                session, u, account, panel_rows, uid,
                                expected_email=email0)
                            if summary is None:
                                await session.rollback()
                                continue

                            # وصول خودکار بدهی «محاسبه گذشته» تا سقف موجودی
                            collected = await rs.collect_backfill_debt(session, u)
                            collected_total = sum(p for _, p in collected)
                            remaining = (sum(float(i.get("remaining") or 0)
                                             for i in rs.get_backfill_debt(u))
                                         if collected else 0.0)

                            # اگر بدهی ساعتی کاملاً جبران شده، وضعیت هشدار پاک
                            # شود — cfg بعد از قفلِ سینک تازه است
                            cfg = rs.get_reseller_cfg(u)
                            if cfg.get("debt_since") or cfg.get("debt_warned_at") \
                                    or cfg.get("debt_admin_warned_at"):
                                hours, _amount = await rs.compute_reseller_owed(session, u)
                                if hours == 0:
                                    for k in ("debt_since", "debt_warned_at",
                                              "debt_admin_warned_at"):
                                        cfg.pop(k, None)
                                    rs.set_reseller_cfg(u, cfg)

                            # قفل ردیف کاربر با commit آزاد می‌شود؛ همه‌ی
                            # ارسال‌های تلگرامی «بعد از» commit — پیام موفقیتِ
                            # قبل از commit با شکستِ commit دروغ می‌شود و قفلِ
                            # نگه‌داشته حین I/O شبکه، بیلینگ دقیقه‌ای را می‌بلوکد.
                            await session.commit()

                            if any(summary.values()):
                                await log.log_reseller_sync(u, account.name, summary)
                            if collected:
                                await log.log_reseller_backfill_collect(
                                    u, collected_total, remaining)
                                try:
                                    await bot.send_message(
                                        u.telegram_id,
                                        f"💳 مبلغ {collected_total:,.0f} تومان بابت بدهی «محاسبه گذشته» "
                                        f"سرورهای ریسلری از کیف پول شما کسر شد."
                                        + (f"\nباقی‌مانده بدهی: {remaining:,.0f} تومان"
                                           if remaining >= 1 else "\nبدهی به‌طور کامل تسویه شد."),
                                        parse_mode="HTML",
                                    )
                                except Exception:
                                    pass
                        except Exception as e:
                            logger.warning("reseller sync: user %s failed: %s", u.id, e)
                            try:
                                await session.rollback()
                            except Exception:
                                pass
            finally:
                await bot.session.close()

    _run(_do())


@app.task(name="bot.tasks.reseller.notify_reseller_billing_summary")
def notify_reseller_billing_summary():
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import Transaction, TransactionType, User
        from bot.services import reseller as rs
        from sqlalchemy import select
        from aiogram import Bot
        from bot.config import settings

        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        window_start = datetime.now(timezone.utc) - timedelta(hours=3)
        async with AsyncSessionFactory() as session:
            all_users = (await session.execute(
                select(User).where(User.extra_data.isnot(None))
            )).scalars().all()
            users = [u for u in all_users if rs.is_reseller_user(u)]
            if not users:
                return

            bot = Bot(token=settings.BOT_TOKEN)
            try:
                for u in users:
                    try:
                        # تراکنش‌های سرورهای ریسلر (شامل حذف‌شده‌ها — کسرهای قبل از حذف)
                        server_ids = [s.id for s in await rs.get_reseller_servers(
                            session, u.id, include_deleted=True)]
                        if not server_ids:
                            continue
                        txs = (await session.execute(
                            select(Transaction).where(
                                Transaction.user_id == u.id,
                                Transaction.type == TransactionType.DEBIT,
                                Transaction.server_id.in_(server_ids),
                                Transaction.created_at >= window_start,
                            )
                        )).scalars().all()
                        txs = [t for t in txs if (t.description or "").startswith("ساعتی — ")]
                        total = sum(float(t.amount or 0) for t in txs)
                        if total <= 0:
                            continue
                        n_servers = len({t.server_id for t in txs})
                        await bot.send_message(
                            u.telegram_id,
                            f'<tg-emoji emoji-id="5852614259082530343">⏱</tg-emoji> '
                            f"<b>بیلینگ سرورهای ریسلری</b>\n\n"
                            f"در ۳ ساعت گذشته مبلغ <b>{total:,.0f} تومان</b> بابت "
                            f"{n_servers} سرور ({len(txs)} ساعت) از حساب شما کسر شد.\n"
                            f'<tg-emoji emoji-id="5778318458802409852">💰</tg-emoji> '
                            f"موجودی فعلی: {u.balance:,.0f} تومان",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            finally:
                await bot.session.close()

    _run(_do())


@app.task(name="bot.tasks.reseller.handle_reseller_debt")
def handle_reseller_debt(user_id: int):
    async def _do():
        from bot.database.session import engine, AsyncSessionFactory
        from bot.database.models import User
        from bot.services import reseller as rs
        from bot.services.log_service import LogService
        from sqlalchemy import select
        from aiogram import Bot
        from bot.config import settings

        try:
            await engine.dispose(close=False)
        except Exception:
            pass

        async with AsyncSessionFactory() as session:
            user = (await session.execute(
                select(User).where(User.id == user_id).with_for_update()
            )).scalar_one_or_none()
            if not user:
                return

            hours, amount = await rs.compute_reseller_owed(session, user)
            cfg = rs.get_reseller_cfg(user)
            if hours == 0:
                # جبران شده — وضعیت بدهی پاک شود
                if any(cfg.get(k) for k in ("debt_since", "debt_warned_at",
                                            "debt_admin_warned_at")):
                    for k in ("debt_since", "debt_warned_at", "debt_admin_warned_at"):
                        cfg.pop(k, None)
                    rs.set_reseller_cfg(user, cfg)
                    await session.commit()
                return

            now = datetime.now(timezone.utc)
            if not cfg.get("debt_since"):
                cfg["debt_since"] = now.isoformat()
            warn_user = _due(cfg.get("debt_warned_at"), 3)
            warn_admin = _due(cfg.get("debt_admin_warned_at"), 1)
            if warn_user:
                cfg["debt_warned_at"] = now.isoformat()
            if warn_admin:
                cfg["debt_admin_warned_at"] = now.isoformat()
            rs.set_reseller_cfg(user, cfg)
            await session.commit()
            if not warn_user and not warn_admin:
                return

            backfill_remaining = sum(float(i.get("remaining") or 0)
                                     for i in rs.get_backfill_debt(user))
            bot = Bot(token=settings.BOT_TOKEN)
            try:
                if warn_user:
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            '<tg-emoji emoji-id="4956611513369494230">⚠️</tg-emoji> '
                            "<b>موجودی کافی نیست</b>\n\n"
                            "هزینه ساعتی سرورهای ریسلری شما قابل کسر نیست و در حال "
                            f"انباشت بدهی است ({hours} ساعت — حدود {amount:,.0f} تومان).\n"
                            "لطفاً کیف پول را شارژ کنید؛ ساعت‌های عقب‌افتاده پس از شارژ "
                            "به‌صورت خودکار کسر می‌شوند.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                if warn_admin:
                    log = LogService(bot, session)
                    await log.log_reseller_debt(
                        user, hours, amount, cfg.get("debt_since"), backfill_remaining)
            finally:
                await bot.session.close()

    _run(_do())
