"""High-level server management service (provider-agnostic)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BillingType, ProviderAccount, Server, ServerPlan, ServerStatus,
    SuspendReason, User,
)
from bot.providers import CreateServerParams, get_provider


class ServerService:

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _get_account(self, account_id: int) -> ProviderAccount:
        result = await self.session.execute(
            select(ProviderAccount).where(
                ProviderAccount.id == account_id,
                ProviderAccount.is_active == True,
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            raise RuntimeError(f"Provider account {account_id} not found or inactive")
        return account

    async def create_server(
        self,
        user: User,
        plan: ServerPlan,
        os_id: str,
        billing_type: BillingType,
        hostname: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> Server:
        account = await self._get_account(plan.provider_account_id)
        provider = get_provider(account)

        custom_name = hostname or f"tc-{user.telegram_id}-{int(datetime.now().timestamp())}"
        params = CreateServerParams(
            name=custom_name,
            plan_id=plan.provider_plan_id or "",
            os_id=os_id,
            location=plan.location or "",
            hostname=custom_name,
            extra={
                "ram": plan.ram,
                "disk": plan.disk,
                "cpu": plan.cpu,
                "bandwidth": plan.bandwidth,
                **(extra or {}),
            },
        )

        info = await provider.create_server(params)

        server = Server(
            user_id=user.id,
            provider_type=plan.provider_type,
            provider_account_id=account.id,
            provider_server_id=info.provider_server_id,
            name=custom_name,
            hostname=custom_name,
            ip_address=info.ip_address,
            ipv6_address=info.ipv6_address,
            ram=plan.ram,
            cpu=plan.cpu,
            disk=plan.disk,
            bandwidth=plan.bandwidth,
            os_name=info.os_name,
            location=plan.location,
            datacenter=plan.datacenter,
            status=ServerStatus.BUILDING,
            billing_type=billing_type,
            price_hourly=plan.price_hourly,
            price_monthly=plan.price_monthly,
            traffic_limit_gb=float(plan.bandwidth),
            last_billed_at=datetime.now(timezone.utc),
            expires_at=(datetime.now(timezone.utc) + timedelta(days=30))
            if billing_type == BillingType.MONTHLY else None,
            extra_data=info.extra_data,
        )
        self.session.add(server)
        await self.session.flush()
        return server

    async def sync_server_status(self, server: Server) -> Server:
        account = await self._get_account(server.provider_account_id)
        provider = get_provider(account)
        info = await provider.get_server(server.provider_server_id)
        server.ip_address = info.ip_address or server.ip_address
        server.ipv6_address = info.ipv6_address or server.ipv6_address
        if info.status == "active" and server.status == ServerStatus.BUILDING:
            server.status = ServerStatus.ACTIVE
        await self.session.flush()
        return server

    async def perform_action(self, server: Server, action: str, **kwargs) -> bool:
        account = await self._get_account(server.provider_account_id)
        provider = get_provider(account)
        sid = server.provider_server_id

        if action == "start":
            return await provider.start_server(sid)
        if action == "stop":
            return await provider.stop_server(sid)
        if action == "restart":
            server.status = ServerStatus.REBOOTING
            await self.session.flush()
            return await provider.restart_server(sid)
        if action == "rebuild":
            server.status = ServerStatus.REBUILDING
            await self.session.flush()
            return await provider.rebuild_server(sid, kwargs["os_id"])
        if action == "suspend":
            ok = await provider.suspend_server(sid)
            if ok:
                server.status = ServerStatus.SUSPENDED
                server.suspend_reason = kwargs.get("reason", SuspendReason.ADMIN)
                server.suspended_at = datetime.now(timezone.utc)
                await self.session.flush()
            return ok
        if action == "unsuspend":
            ok = await provider.unsuspend_server(sid)
            if ok:
                server.status = ServerStatus.ACTIVE
                server.suspend_reason = None
                server.suspended_at = None
                await self.session.flush()
            return ok
        if action == "delete":
            ok = await provider.delete_server(sid)
            if ok:
                server.status = ServerStatus.DELETED
                await self.session.flush()
            return ok
        if action == "change_ip":
            new_ip = await provider.change_ip(sid)
            if new_ip:
                server.ip_address = new_ip
                await self.session.flush()
            return bool(new_ip)
        if action == "edit":
            ok = await provider.edit_server(
                sid,
                ram=kwargs.get("ram"),
                cpu=kwargs.get("cpu"),
                disk=kwargs.get("disk"),
            )
            if ok:
                if kwargs.get("ram"):
                    server.ram = kwargs["ram"]
                if kwargs.get("cpu"):
                    server.cpu = kwargs["cpu"]
                if kwargs.get("disk"):
                    server.disk = kwargs["disk"]
                await self.session.flush()
            return ok
        if action == "add_traffic":
            return await provider.add_traffic(sid, kwargs["gb"])

        raise ValueError(f"Unknown action: {action}")

    async def get_user_servers(self, user_id: int) -> list[Server]:
        result = await self.session.execute(
            select(Server).where(
                Server.user_id == user_id,
                Server.status != ServerStatus.DELETED,
            ).order_by(Server.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_available_plans(self, provider_type=None, location=None) -> list[ServerPlan]:
        q = select(ServerPlan).where(ServerPlan.is_active == True)
        if provider_type:
            q = q.where(ServerPlan.provider_type == provider_type)
        if location:
            q = q.where(ServerPlan.location == location)
        result = await self.session.execute(q)
        return list(result.scalars().all())
