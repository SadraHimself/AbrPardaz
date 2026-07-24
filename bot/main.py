"""Bot entry point."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from bot.config import settings
from bot.database.base import Base
from bot.database.session import engine
from bot.handlers import setup_routers
from bot.middlewares import AuthMiddleware, DbSessionMiddleware, RateLimitMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    logger.info("Bot started: @%s", me.username)

    # Create DB tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # مقادیر enum جدید — create_all انام موجود را آپدیت نمی‌کند (PG12+ داخل تراکنش OK)
    from sqlalchemy import text as _sql_text
    try:
        async with engine.begin() as conn:
            await conn.execute(_sql_text(
                "ALTER TYPE providertype ADD VALUE IF NOT EXISTS 'HETZNER'"
            ))
    except Exception as e:
        logger.warning("enum migration skipped: %s", e)
    try:
        async with engine.begin() as conn:
            await conn.execute(_sql_text(
                "ALTER TYPE providertype ADD VALUE IF NOT EXISTS 'GCORE'"
            ))
    except Exception as e:
        logger.warning("enum migration (GCORE) skipped: %s", e)
    try:
        async with engine.begin() as conn:
            await conn.execute(_sql_text(
                "ALTER TYPE providertype ADD VALUE IF NOT EXISTS 'TIMEWEB'"
            ))
    except Exception as e:
        logger.warning("enum migration (TIMEWEB) skipped: %s", e)

    # drift ستون: servers.provider_account_id در دیتابیس‌های قدیمی NOT NULL است
    # ولی مدل Optional است (لازم برای حذف اکانت provider). با lock_timeout تا
    # اگر جدول قفل بود، استارتاپ هنگ نکند — دفعه‌ی بعدِ ری‌استارت اعمال می‌شود.
    try:
        async with engine.begin() as conn:
            await conn.execute(_sql_text("SET LOCAL lock_timeout = '5s'"))
            await conn.execute(_sql_text(
                "ALTER TABLE servers ALTER COLUMN provider_account_id DROP NOT NULL"
            ))
    except Exception as e:
        logger.warning("servers.provider_account_id nullable migration skipped: %s", e)

    # پاک‌سازی یک‌بارِ display_name محصولات هتزنرِ قدیمی از پسوند لوکیشن («CPX22 — fsn1» → «CPX22»)
    try:
        from bot.database.models import ProviderType, ServerPlan
        from bot.database.session import AsyncSessionFactory
        from sqlalchemy import select
        async with AsyncSessionFactory() as s:
            rows = (await s.execute(
                select(ServerPlan).where(ServerPlan.provider_type == ProviderType.HETZNER)
            )).scalars().all()
            fixed = 0
            for p in rows:
                if p.display_name and "—" in p.display_name:
                    p.display_name = p.display_name.split("—")[0].strip()
                    fixed += 1
            if fixed:
                await s.commit()
                logger.info("cleaned %s hetzner display names", fixed)
    except Exception as e:
        logger.warning("hetzner display-name cleanup skipped: %s", e)

    logger.info("Database tables ready.")


async def on_shutdown(bot: Bot) -> None:
    logger.info("Shutting down...")
    await engine.dispose()


async def on_error(event) -> bool:
    """Global safety net: log every unhandled handler exception and ALWAYS answer
    the callback query so buttons never spin forever."""
    logger.exception("Unhandled error while processing update", exc_info=event.exception)
    try:
        upd = event.update
        if upd.callback_query:
            await upd.callback_query.answer("⚠️ خطایی رخ داد. دوباره تلاش کنید.", show_alert=True)
        elif upd.message and upd.message.chat.type == "private":
            # فقط چت خصوصی — خطای پردازش سرویس‌پیام‌های گروه (مثل ساخت تاپیک)
            # نباید داخل گروه/تاپیک‌ها اسپم شود
            await upd.message.answer(
                '‏<tg-emoji emoji-id="4956611513369494230">⚠️</tg-emoji> خطایی رخ داد. دوباره تلاش کنید.',
                parse_mode="HTML",
            )
    except Exception:
        pass
    return True  # mark as handled


async def main() -> None:
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    storage = RedisStorage.from_url(settings.REDIS_URL)
    dp = Dispatcher(storage=storage)

    # Middlewares (order: DB → Auth → RateLimit)
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(AuthMiddleware())
    dp.update.outer_middleware(RateLimitMiddleware())

    setup_routers(dp)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    dp.errors.register(on_error)

    # Start the webhook/callback server alongside the bot when any gateway needs it
    # (NOWPayments IPN and/or Zarinpal return callback).
    if settings.NP_API_KEY or settings.ZARINPAL_MERCHANT_ID:
        from aiohttp import web
        from bot.webhook_server import create_webhook_app
        if settings.NP_API_KEY and not settings.NP_IPN_SECRET:
            logger.warning(
                "NP_IPN_SECRET is not set — IPN webhook will accept requests WITHOUT signature verification. "
                "Set NP_IPN_SECRET in .env for production security."
            )
        webhook_app = create_webhook_app(bot)
        runner = web.AppRunner(webhook_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", settings.NP_WEBHOOK_PORT)
        await site.start()
        logger.info("Webhook/callback server listening on port %d", settings.NP_WEBHOOK_PORT)

    logger.info("Starting polling...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
