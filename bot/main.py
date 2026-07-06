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
        elif upd.message:
            await upd.message.answer("⚠️ خطایی رخ داد. دوباره تلاش کنید.")
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
