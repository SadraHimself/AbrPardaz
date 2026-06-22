"""FastAPI routes for the Telegram Mini App."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Optional
from urllib.parse import parse_qs, unquote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import Server, ServerStatus, User
from bot.database.session import AsyncSessionFactory
from bot.services.server import ServerService

router = APIRouter(prefix="/api")


# ── Telegram WebApp Auth ──────────────────────────────────────────────────────

def verify_telegram_init_data(init_data: str) -> dict:
    """Verify Telegram WebApp initData signature."""
    parsed = parse_qs(init_data)
    received_hash = parsed.pop("hash", [None])[0]
    if not received_hash:
        raise HTTPException(401, "Missing hash")

    data_check_string = "\n".join(
        f"{k}={v[0]}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        raise HTTPException(401, "Invalid signature")

    user_data = json.loads(unquote(parsed.get("user", ["{}"])[0]))
    return user_data


async def get_db_user(init_data: str = Query(...)) -> tuple[User, AsyncSession]:
    tg_user = verify_telegram_init_data(init_data)
    telegram_id = tg_user.get("id")
    if not telegram_id:
        raise HTTPException(401, "User not found in initData")

    session = AsyncSessionFactory()
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not registered in bot")
    return user, session


# ── Schemas ───────────────────────────────────────────────────────────────────

class ServerActionRequest(BaseModel):
    action: str
    os_id: Optional[str] = None
    ram: Optional[int] = None
    cpu: Optional[int] = None
    disk: Optional[int] = None
    gb: Optional[int] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/me")
async def get_me(init_data: str = Query(...)):
    tg_user = verify_telegram_init_data(init_data)
    telegram_id = tg_user.get("id")

    async with AsyncSessionFactory() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(404, "User not found")
        return {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "first_name": user.first_name,
            "balance": user.balance,
            "is_phone_verified": user.is_phone_verified,
            "is_kyc_verified": user.is_kyc_verified,
        }


@router.get("/servers")
async def get_servers(init_data: str = Query(...)):
    user, session = await get_db_user(init_data)
    async with session:
        svc = ServerService(session)
        servers = await svc.get_user_servers(user.id)
        return [
            {
                "id": s.id,
                "name": s.name,
                "ip": s.ip_address,
                "status": s.status.value,
                "ram": s.ram,
                "cpu": s.cpu,
                "disk": s.disk,
                "location": s.location,
                "billing_type": s.billing_type.value,
                "traffic_used": s.traffic_used_gb,
                "traffic_limit": s.traffic_limit_gb,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            }
            for s in servers
        ]


@router.post("/servers/{server_id}/action")
async def server_action(server_id: int, body: ServerActionRequest, init_data: str = Query(...)):
    user, session = await get_db_user(init_data)
    async with session:
        server = await session.get(Server, server_id)
        if not server or server.user_id != user.id:
            raise HTTPException(404, "Server not found")

        allowed_actions = {"start", "stop", "restart", "rebuild", "change_ip"}
        if body.action not in allowed_actions:
            raise HTTPException(400, f"Action '{body.action}' not allowed via API")

        svc = ServerService(session)
        kwargs = {}
        if body.os_id:
            kwargs["os_id"] = body.os_id
        if body.ram:
            kwargs["ram"] = body.ram
        if body.cpu:
            kwargs["cpu"] = body.cpu
        if body.disk:
            kwargs["disk"] = body.disk
        if body.gb:
            kwargs["gb"] = body.gb

        try:
            ok = await svc.perform_action(server, body.action, **kwargs)
            await session.commit()
            return {"success": ok, "server_id": server_id, "action": body.action}
        except Exception as e:
            raise HTTPException(500, str(e))


@router.get("/servers/{server_id}")
async def get_server_detail(server_id: int, init_data: str = Query(...)):
    user, session = await get_db_user(init_data)
    async with session:
        server = await session.get(Server, server_id)
        if not server or server.user_id != user.id:
            raise HTTPException(404, "Server not found")

        return {
            "id": server.id,
            "name": server.name,
            "hostname": server.hostname,
            "ip": server.ip_address,
            "ipv6": server.ipv6_address,
            "status": server.status.value,
            "suspend_reason": server.suspend_reason.value if server.suspend_reason else None,
            "ram": server.ram,
            "cpu": server.cpu,
            "disk": server.disk,
            "bandwidth": server.bandwidth,
            "os": server.os_name,
            "location": server.location,
            "billing_type": server.billing_type.value,
            "price_hourly": server.price_hourly,
            "price_monthly": server.price_monthly,
            "traffic_used_gb": server.traffic_used_gb,
            "traffic_limit_gb": server.traffic_limit_gb,
            "created_at": server.created_at.isoformat(),
            "expires_at": server.expires_at.isoformat() if server.expires_at else None,
        }
