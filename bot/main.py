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

    # Start IPN webhook server alongside the bot (only when configured)
    if settings.NP_API_KEY and settings.NP_IPN_SECRET:
        from aiohttp import web
        from bot.webhook_server import create_webhook_app
        webhook_app = create_webhook_app(bot)
        runner = web.AppRunner(webhook_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", settings.NP_WEBHOOK_PORT)
        await site.start()
        logger.info("IPN webhook server listening on port %d", settings.NP_WEBHOOK_PORT)

    logger.info("Starting polling...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
