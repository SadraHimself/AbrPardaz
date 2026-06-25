"""Celery task: DB backup → zip → send to Telegram backup topic."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import datetime

from bot.tasks.celery_app import app


def _run(coro):
    return asyncio.run(coro)


@app.task(name="bot.tasks.backup.run_database_backup")
def run_database_backup():
    async def _do():
        from bot.database.session import AsyncSessionFactory
        from bot.database.models import (
            BotSettings, DiscountCode, ProviderAccount, Server, ServerPlan,
            ServerStatus, Transaction, User,
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

            users = (await session.execute(select(User))).scalars().all()
            servers = (await session.execute(select(Server))).scalars().all()
            plans = (await session.execute(select(ServerPlan))).scalars().all()
            txns = (await session.execute(
                select(Transaction).order_by(Transaction.created_at.desc()).limit(5000)
            )).scalars().all()
            providers = (await session.execute(select(ProviderAccount))).scalars().all()
            discounts = (await session.execute(select(DiscountCode))).scalars().all()

            data = {
                "generated_at": datetime.utcnow().isoformat(),
                "users": [
                    {
                        "id": u.id,
                        "telegram_id": u.telegram_id,
                        "username": u.username,
                        "first_name": u.first_name,
                        "last_name": u.last_name,
                        "phone_number": u.phone_number,
                        "email": u.email,
                        "balance": u.balance,
                        "is_phone_verified": u.is_phone_verified,
                        "is_kyc_verified": u.is_kyc_verified,
                        "is_admin": u.is_admin,
                        "is_banned": u.status.value == "banned",
                        "status": u.status.value,
                        "extra_data": u.extra_data,
                        "created_at": u.created_at.isoformat() if u.created_at else None,
                    }
                    for u in users
                ],
                "servers": [
                    {
                        "id": s.id,
                        "user_id": s.user_id,
                        "name": s.name,
                        "hostname": s.hostname,
                        "ip_address": s.ip_address,
                        "ipv6_address": s.ipv6_address,
                        "status": s.status.value,
                        "billing_type": s.billing_type.value,
                        "ram": s.ram,
                        "cpu": s.cpu,
                        "disk": s.disk,
                        "bandwidth": s.bandwidth,
                        "os_name": s.os_name,
                        "location": s.location,
                        "price_hourly": s.price_hourly,
                        "price_monthly": s.price_monthly,
                        "traffic_used_gb": s.traffic_used_gb,
                        "traffic_limit_gb": s.traffic_limit_gb,
                        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                        "created_at": s.created_at.isoformat() if s.created_at else None,
                        "provider_account_id": s.provider_account_id,
                        "provider_server_id": s.provider_server_id,
                    }
                    for s in servers
                ],
                "plans": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "display_name": p.display_name,
                        "category": p.category,
                        "ram": p.ram,
                        "cpu": p.cpu,
                        "disk": p.disk,
                        "bandwidth": p.bandwidth,
                        "price_hourly": p.price_hourly,
                        "price_monthly": p.price_monthly,
                        "is_active": p.is_active,
                    }
                    for p in plans
                ],
                "transactions": [
                    {
                        "id": t.id,
                        "user_id": t.user_id,
                        "server_id": t.server_id,
                        "type": t.type.value,
                        "amount": t.amount,
                        "description": t.description,
                        "reference_id": t.reference_id,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                    }
                    for t in txns
                ],
                "providers": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "provider_type": p.provider_type.value,
                        "api_endpoint": p.api_endpoint,
                        "is_active": p.is_active,
                    }
                    for p in providers
                ],
                "discount_codes": [
                    {
                        "id": d.id,
                        "code": d.code,
                        "percent": d.percent,
                        "is_active": d.is_active,
                        "max_uses": d.max_uses,
                        "used_count": d.used_count,
                        "expires_at": d.expires_at.isoformat() if d.expires_at else None,
                    }
                    for d in discounts
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

            active_count = sum(1 for s in servers if s.status.value not in ("deleted",))
            bot = Bot(token=cfg.BOT_TOKEN)
            try:
                await bot.send_document(
                    group_id,
                    BufferedInputFile(zip_buf.getvalue(), filename=f"backup_{now_str}.zip"),
                    caption=(
                        f"💾 <b>بکاپ دیتابیس</b>\n\n"
                        f"📅 {now_str}\n"
                        f"👥 کاربران: {len(data['users'])}\n"
                        f"🖥 سرورها (کل): {len(data['servers'])} | فعال: {active_count}\n"
                        f"💳 تراکنش‌ها: {len(data['transactions'])}\n"
                        f"📦 محصولات: {len(data['plans'])}"
                    ),
                    parse_mode="HTML",
                    message_thread_id=topic_id,
                )
            finally:
                await bot.session.close()

    _run(_do())
