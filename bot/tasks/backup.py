"""Celery task: pg_dump → zip → send to Telegram backup topic."""
from __future__ import annotations

import asyncio
import io
import os
import subprocess
import zipfile
from datetime import datetime
from urllib.parse import unquote, urlparse

from bot.tasks.celery_app import app


def _run(coro):
    return asyncio.run(coro)


def _pg_dump(database_url: str) -> bytes:
    """Run pg_dump and return the SQL dump as bytes."""
    url = database_url.replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    parsed = urlparse(url)

    host = parsed.hostname or "localhost"
    port = str(parsed.port or 5432)
    user = parsed.username or "postgres"
    password = unquote(parsed.password) if parsed.password else ""
    dbname = parsed.path.lstrip("/")

    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password

    result = subprocess.run(
        [
            "pg_dump",
            "-h", host,
            "-p", port,
            "-U", user,
            "-d", dbname,
            "--no-password",
            "--clean",         # DROP before CREATE
            "--if-exists",     # safe DROP
            "--encoding=UTF8",
        ],
        capture_output=True,
        env=env,
        timeout=300,
    )

    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"pg_dump failed (exit {result.returncode}): {err[:500]}")

    return result.stdout


@app.task(name="bot.tasks.backup.run_database_backup")
def run_database_backup():
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import BotSettings, Server, ServerStatus, Transaction, User
        from bot.config import settings as cfg
        from aiogram import Bot
        from aiogram.types import BufferedInputFile
        from sqlalchemy import func, select

        async with AsyncSessionFactory() as session:
            group_row = await session.get(BotSettings, "log_group_id")
            topic_row = await session.get(BotSettings, "log_topic_backup")
            if not group_row or not topic_row:
                return

            group_id = int(group_row.value)
            topic_id = int(topic_row.value)

            # Quick stats for caption
            user_count = (await session.execute(select(func.count(User.id)))).scalar() or 0
            server_count = (await session.execute(
                select(func.count(Server.id)).where(Server.status != ServerStatus.DELETED)
            )).scalar() or 0
            tx_count = (await session.execute(select(func.count(Transaction.id)))).scalar() or 0

        # Run pg_dump outside the session (pure subprocess)
        now_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        sql_bytes = _pg_dump(cfg.DATABASE_URL)

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"backup_{now_str}.sql", sql_bytes)
        zip_buf.seek(0)

        size_kb = len(zip_buf.getvalue()) // 1024

        bot = Bot(token=cfg.BOT_TOKEN)
        try:
            await bot.send_document(
                group_id,
                BufferedInputFile(zip_buf.getvalue(), filename=f"backup_{now_str}.zip"),
                caption=(
                    f"💾 <b>بکاپ کامل دیتابیس</b>\n\n"
                    f"📅 {now_str} UTC\n"
                    f"👥 کاربران: {user_count}\n"
                    f"🖥 سرورهای فعال: {server_count}\n"
                    f"💳 تراکنش‌ها: {tx_count}\n"
                    f"📦 حجم فشرده: {size_kb} KB\n\n"
                    f"<i>بازیابی: همین فایل ZIP را در چت خصوصی برای ربات بفرستید</i>"
                ),
                parse_mode="HTML",
                message_thread_id=topic_id,
            )
        finally:
            await bot.session.close()

    _run(_do())
