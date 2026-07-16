"""High-level server management service (provider-agnostic)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BillingType, ProviderAccount, ProviderType, Server, ServerPlan, ServerStatus,
    SuspendReason, User,
)
from bot.providers import CreateServerParams, get_provider

logger = logging.getLogger(__name__)


class ServerService:

    def __init__(self, session: AsyncSession):
        self.session = session
        # رمز واقعیِ آخرین rebuild/change_password — برخی سرویس‌دهنده‌ها (هتزنر)
        # رمز دلخواه نمی‌پذیرند و رمز تولیدی خودشان را برمی‌گردانند
        self.last_root_password: str | None = None

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
        # هتزنر: کاتالوگ مشترک است — اکانتِ ساخت با «توزیع متوازن» انتخاب می‌شود
        # (کمترین سرورِ زنده، زیر لیمیت VM). پلن‌ها provider_account_id مرجع دارند.
        if plan.provider_type == ProviderType.HETZNER:
            from bot.services.hetzner_settings import pick_account
            account = await pick_account(self.session)
            if not account:
                raise RuntimeError(
                    "ظرفیت ساخت سرور در حال حاضر تکمیل است — لطفاً بعداً تلاش کنید"
                )
        else:
            account = await self._get_account(plan.provider_account_id)
        provider = get_provider(account)

        import secrets as _sec, string as _str
        _rand = "".join(_sec.choice(_str.ascii_lowercase + _str.digits) for _ in range(6))
        custom_name = hostname or f"srv-{_rand}"
        provider_extra = account.extra_config or {}
        plan_extra = plan.extra_data or {}

        merged_extra: dict = {
            "ram": plan.ram,
            "disk": plan.disk,
            "cpu": plan.cpu,
            "bandwidth": plan.bandwidth,
            # لیبل ردیابی برای سرویس‌دهنده‌های label-دار (هتزنر) — ویرچولایزور نادیده می‌گیرد
            "labels": {"tg_user_id": str(user.telegram_id)},
            **provider_extra,
            **plan_extra,
            **(extra or {}),
        }

        # Inject per-user Virtualizor uid (stored from a previous purchase)
        # or pass email so addvs creates the user inline (uid=0 + user_email approach).
        user_extra = user.extra_data or {}
        stored_uid = (user_extra.get("virt_uids") or {}).get(str(account.id))
        if stored_uid:
            merged_extra["virtualizor_uid"] = stored_uid
        elif user.email:
            merged_extra["user_email"] = user.email
            merged_extra["user_pass"] = user.email

        params = CreateServerParams(
            name=custom_name,
            plan_id=plan.provider_plan_id or "",
            os_id=os_id,
            location=plan.location or "",
            hostname=custom_name,
            extra=merged_extra,
        )

        info = await provider.create_server(params)

        # Persist the Virtualizor uid (created inline via uid=0 + user_email) so the
        # user's next purchase reuses the same account instead of spawning a new one.
        new_uid = (info.extra_data or {}).get("uid")
        if new_uid and not stored_uid:
            ud = dict(user.extra_data or {})
            virt_uids = dict(ud.get("virt_uids") or {})
            virt_uids[str(account.id)] = str(new_uid)
            ud["virt_uids"] = virt_uids
            user.extra_data = ud  # reassign so SQLAlchemy flags the JSON column dirty

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
            # ارز پلن منتقل می‌شود + plan_id تا بیلینگ همیشه قیمتِ روزِ پلن را بخواند
            # (تغییر قیمت پلن → فوراً روی همین سرور هم اعمال می‌شود)
            extra_data={**(info.extra_data or {}),
                        "currency": (plan.extra_data or {}).get("currency", "irt"),
                        "plan_id": plan.id},
        )
        self.session.add(server)
        await self.session.flush()
        return server

    async def sync_server_status(self, server: Server) -> Server:
        account = await self._get_account(server.provider_account_id)
        provider = get_provider(account)
        info = await provider.get_server(server.provider_server_id)
        if info.ip_address:
            server.ip_address = info.ip_address
        if info.ipv6_address:
            server.ipv6_address = info.ipv6_address

        # Merge live machine state from Virtualizor so the detail page label and the
        # keyboard dot reflect the real state (running / off / locked).
        _extra = dict(server.extra_data or {})
        for key in ("machine_status", "locked", "serid", "node", "vpsid"):
            val = (info.extra_data or {}).get(key)
            if val is not None:
                _extra[key] = val
        server.extra_data = _extra  # reassign so SQLAlchemy flags the JSON column dirty

        # Map live Virtualizor status onto DB status:
        #   suspended             → SUSPENDED
        #   locked + offline      → PENDING  (freshly created / rebuilding)
        #   running / powered-off → ACTIVE   (on/off is shown via machine_status)
        virt_status = info.status  # "active" | "suspended" | "building" | "off"
        if server.status != ServerStatus.DELETED:
            if virt_status == "suspended":
                server.status = ServerStatus.SUSPENDED
            elif server.status == ServerStatus.SUSPENDED:
                pass  # keep an admin/billing suspension until explicitly unsuspended
            elif virt_status == "building":
                server.status = ServerStatus.PENDING
            else:  # "active" or "off"
                server.status = ServerStatus.ACTIVE

        await self.session.flush()
        return server

    async def _delete_server(self, server: Server, force: bool = True) -> bool:
        """Delete a VM, tolerant of a missing/deleted provider account or a down
        node — the DB record is always cleaned up so the user is never stuck with a
        server whose provider no longer exists."""
        sid = server.provider_server_id

        # Resolve the provider; if the account is gone/inactive, there is nothing to
        # call on the node — just clean up the DB record.
        account = None
        try:
            account = await self._get_account(server.provider_account_id)
        except Exception:
            account = None

        if account is not None and sid:
            provider = get_provider(account)
            ok = False
            try:
                ok = await provider.delete_server(sid)
            except RuntimeError as _e:
                _em = str(_e).lower()
                if any(w in _em for w in ("not found", "does not exist", "no vps", "invalid vpsid", "no such")):
                    ok = True  # VPS already gone from provider
                elif force:
                    logger.warning("delete server %s: provider error, force-cleaning DB: %s", server.id, _e)
                    ok = True  # node unreachable / other error — force clean
                else:
                    raise
            if not ok:
                try:
                    await provider.get_server(sid)
                    if force:
                        logger.warning("delete server %s: still exists but force-cleaning DB", server.id)
                        ok = True
                except RuntimeError:
                    ok = True  # VPS not found by provider either → force clean
            if not ok:
                return False

        server.status = ServerStatus.DELETED
        await self.session.flush()
        return True

    async def perform_action(self, server: Server, action: str, **kwargs) -> bool:
        # Delete must work even if the provider account was removed / the node is
        # down, so resolve it separately (don't require an active account first).
        if action == "delete":
            return await self._delete_server(server, force=kwargs.get("force", True))

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
            ok = await provider.rebuild_server(sid, kwargs["os_id"], rootpass=kwargs.get("new_password", ""))
            if ok:
                real = getattr(provider, "last_root_password", None) or kwargs.get("new_password", "")
                self.last_root_password = real or None
                if real:
                    _extra = dict(server.extra_data or {})
                    _extra["root_password"] = real
                    server.extra_data = _extra
                    await self.session.flush()
            return ok
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
        if action == "change_password":
            new_pass = kwargs["password"]
            ok = await provider.change_root_password(sid, new_pass)
            if ok:
                real = getattr(provider, "last_root_password", None) or new_pass
                self.last_root_password = real
                _extra = dict(server.extra_data or {})
                _extra["root_password"] = real
                server.extra_data = _extra
                await self.session.flush()
            return ok

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
