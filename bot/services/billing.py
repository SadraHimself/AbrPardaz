"""Billing service: charge, credit, suspend logic."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_, and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BillingType, Server, ServerStatus, SuspendReason,
    Transaction, TransactionType, User,
)
from bot.services.currency import obj_currency, to_toman

logger = logging.getLogger(__name__)


class BillingService:

    def __init__(self, session: AsyncSession):
        self.session = session

    # ── Balance helpers ───────────────────────────────────────────────────────

    async def get_balance(self, user_id: int) -> float:
        result = await self.session.execute(select(User.balance).where(User.id == user_id))
        return result.scalar_one_or_none() or 0.0

    async def credit(self, user_id: int, amount: float, description: str = "",
                     reference_id: Optional[str] = None) -> Transaction:
        result = await self.session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = result.scalar_one_or_none()
        if user:
            user.balance += amount
            extra = dict(user.extra_data or {})
            if "balance_empty_at" in extra or "balance_warn_level" in extra:
                extra.pop("balance_empty_at", None)
                extra.pop("balance_warn_level", None)
                user.extra_data = extra
        else:
            await self.session.execute(
                update(User).where(User.id == user_id).values(balance=User.balance + amount)
            )
        tx = Transaction(
            user_id=user_id, amount=amount,
            type=TransactionType.CREDIT,
            description=description, reference_id=reference_id,
        )
        self.session.add(tx)
        await self.session.flush()
        return tx

    async def debit(self, user_id: int, amount: float, server_id: Optional[int] = None,
                    description: str = "") -> bool:
        """Debit balance. Returns False if insufficient funds."""
        result = await self.session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = result.scalar_one_or_none()
        if not user or user.balance < amount:
            return False

        user.balance -= amount
        tx = Transaction(
            user_id=user_id, server_id=server_id,
            amount=amount, type=TransactionType.DEBIT,
            description=description,
        )
        self.session.add(tx)
        await self.session.flush()
        return True

    # ── Hourly billing ────────────────────────────────────────────────────────

    async def charge_hourly(self, server: Server) -> bool:
        """
        Charge one hour of usage (always debited in Toman; currency-priced
        servers are converted with the live rate). Returns False if balance
        insufficient (caller should suspend the server).
        """
        amount = server.price_hourly or 0.0
        if amount <= 0:
            return True

        currency = obj_currency(server)
        if currency == "irt":
            amount_toman = amount
        else:
            amount_toman = await to_toman(self.session, amount, currency)
            if amount_toman <= 0:
                # نرخ ارز در دسترس نیست — این ساعت را رد نکن؛ بدون advance کردن
                # last_billed_at برگرد تا اجرای بعدی (بعد از آپدیت نرخ) جبران شود.
                logger.warning("charge_hourly: no %s rate — postponing billing for server %s",
                               currency, server.id)
                return True

        success = await self.debit(
            server.user_id, amount_toman,
            server_id=server.id,
            description=f"ساعتی — {server.name}",
        )
        if success:
            now = datetime.now(timezone.utc)
            # Anchor last_billed_at to creation time to prevent cumulative drift.
            # e.g. created at 02:19 → bills at 03:19, 04:19, 05:19 exactly.
            created = server.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            elapsed_hours = int((now - created).total_seconds() // 3600)
            server.last_billed_at = created + timedelta(hours=max(elapsed_hours, 1))
        return success

    # ── Monthly billing ───────────────────────────────────────────────────────

    async def charge_monthly(self, server: Server) -> bool:
        amount = server.price_monthly or 0.0
        if amount <= 0:
            return True

        currency = obj_currency(server)
        if currency == "irt":
            amount_toman = amount
        else:
            amount_toman = await to_toman(self.session, amount, currency)
            if amount_toman <= 0:
                logger.warning("charge_monthly: no %s rate — postponing billing for server %s",
                               currency, server.id)
                return True

        # هزینه IPهای اضافه (تومانی، از تنظیمات پروایدر) در هر تمدید ماهانه هم اعمال می‌شود
        extra_ips = (server.extra_data or {}).get("extra_ips") or []
        if extra_ips and server.provider_account_id:
            from bot.database.models import ProviderAccount
            acc = await self.session.get(ProviderAccount, server.provider_account_id)
            ip_fee = float(((acc.extra_config or {}) if acc else {}).get("extra_ip_fee", 0) or 0)
            if ip_fee > 0:
                amount_toman += ip_fee * len(extra_ips)

        success = await self.debit(
            server.user_id, amount_toman,
            server_id=server.id,
            description=f"ماهیانه — {server.name}",
        )
        if success:
            server.last_billed_at = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return success

    # ── Suspension ────────────────────────────────────────────────────────────

    async def suspend_server_db(self, server: Server, reason: SuspendReason) -> None:
        server.status = ServerStatus.SUSPENDED
        server.suspend_reason = reason
        server.suspended_at = datetime.now(timezone.utc)
        await self.session.flush()

    async def unsuspend_server_db(self, server: Server) -> None:
        server.status = ServerStatus.ACTIVE
        server.suspend_reason = None
        server.suspended_at = None
        await self.session.flush()

    # ── Traffic billing ───────────────────────────────────────────────────────

    async def update_traffic(self, server: Server, used_gb: float) -> bool:
        """
        Update traffic counter. Returns True if within limit, False if exceeded.
        """
        server.traffic_used_gb = used_gb
        await self.session.flush()
        if server.traffic_limit_gb is not None and used_gb >= server.traffic_limit_gb:
            return False
        return True

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_active_servers_for_billing(self) -> list[Server]:
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        result = await self.session.execute(
            select(Server).where(
                Server.status == ServerStatus.ACTIVE,
                Server.billing_type == BillingType.HOURLY,
                or_(
                    and_(Server.last_billed_at.is_(None), Server.created_at <= one_hour_ago),
                    Server.last_billed_at <= one_hour_ago,
                ),
            )
        )
        return list(result.scalars().all())

    async def get_users_with_suspended_servers(self) -> list[int]:
        result = await self.session.execute(
            select(Server.user_id).where(
                Server.status == ServerStatus.SUSPENDED,
                Server.suspend_reason == SuspendReason.LOW_BALANCE,
            ).distinct()
        )
        return list(result.scalars().all())
