"""Celery task: DB backup → zip → send to Telegram backup topic."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import datetime

from bot.tasks.celery_app import app


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@app.task(name="bot.tasks.backup.run_database_backup")
def run_database_backup():
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import (
            BotSettings, Server, ServerPlan, ServerStatus, Transaction, User,
        )
        from bot.config import settings as cfg
        from aiogram import Bot
        from aiogram.types import BufferedInputFile
        from sqlalchemy import select

        async with AsyncSessionFactory() as session:
            group_row = await session.get(BotSettings, "log_group_id")
            topic_row = await session.get(BotSettings, "log_topic_backup")
            if not group_row or not topic_row:
                return

            group_id = int(group_row.value)
            topic_id = int(topic_row.value)

            # Collect data
            users = (await session.execute(select(User))).scalars().all()
            servers = (await session.execute(
                select(Server).where(Server.status != ServerStatus.DELETED)
            )).scalars().all()
            plans = (await session.execute(select(ServerPlan))).scalars().all()
            txns = (await session.execute(
                select(Transaction).order_by(Transaction.created_at.desc()).limit(1000)
            )).scalars().all()

            data = {
                "generated_at": datetime.utcnow().isoformat(),
                "users": [
                    {
                        "id": u.id,
                        "telegram_id": u.telegram_id,
                        "username": u.username,
                        "first_name": u.first_name,
                        "balance": u.balance,
                        "is_phone_verified": u.is_phone_verified,
                        "email": u.email,
                        "status": u.status.value,
                    }
                    for u in users
                ],
                "servers": [
                    {
                        "id": s.id,
                        "user_id": s.user_id,
                        "name": s.name,
                        "ip_address": s.ip_address,
                        "status": s.status.value,
                        "billing_type": s.billing_type.value,
                        "price_hourly": s.price_hourly,
                        "price_monthly": s.price_monthly,
                        "provider_account_id": s.provider_account_id,
                    }
                    for s in servers
                ],
                "plans": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "display_name": p.display_name,
                        "ram": p.ram,
                        "cpu": p.cpu,
                        "disk": p.disk,
                        "price_hourly": p.price_hourly,
                        "price_monthly": p.price_monthly,
                        "is_active": p.is_active,
                    }
                    for p in plans
                ],
                "recent_transactions": [
                    {
                        "id": t.id,
                        "user_id": t.user_id,
                        "type": t.type.value,
                        "amount": t.amount,
                        "description": t.description,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                    }
                    for t in txns
                ],
            }

            now_str = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(
                    f"backup_{now_str}.json",
                    json.dumps(data, ensure_ascii=False, indent=2, default=str),
                )
            zip_buf.seek(0)

            bot = Bot(token=cfg.BOT_TOKEN)
            try:
                await bot.send_document(
                    group_id,
                    BufferedInputFile(zip_buf.getvalue(), filename=f"backup_{now_str}.zip"),
                    caption=(
                        f"💾 <b>بکاپ دیتابیس</b>\n\n"
                        f"📅 {now_str}\n"
                        f"👥 کاربران: {len(data['users'])}\n"
                        f"🖥 سرورهای فعال: {len(data['servers'])}\n"
                        f"📦 محصولات: {len(data['plans'])}"
                    ),
                    parse_mode="HTML",
                    message_thread_id=topic_id,
                )
            finally:
                await bot.session.close()

    _run(_do())
