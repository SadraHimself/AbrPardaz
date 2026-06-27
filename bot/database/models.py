from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, JSON, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


# ── Enums ────────────────────────────────────────────────────────────────────

class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    BANNED = "banned"
    SUSPENDED = "suspended"


class ServerStatus(str, enum.Enum):
    PENDING = "pending"
    BUILDING = "building"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"
    REBUILDING = "rebuilding"
    REBOOTING = "rebooting"


class BillingType(str, enum.Enum):
    HOURLY = "hourly"
    MONTHLY = "monthly"


class ProviderType(str, enum.Enum):
    VIRTUALIZOR = "virtualizor"


class TransactionType(str, enum.Enum):
    CREDIT = "credit"
    DEBIT = "debit"
    REFUND = "refund"


class SuspendReason(str, enum.Enum):
    LOW_BALANCE = "low_balance"
    TRAFFIC_EXCEEDED = "traffic_exceeded"
    EXPIRED = "expired"
    ADMIN = "admin"


class SubProductType(str, enum.Enum):
    TRAFFIC = "traffic"
    EXTRA_IP = "extra_ip"


# ── Models ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))

    # Auth
    phone_number: Mapped[Optional[str]] = mapped_column(String(20))
    national_id: Mapped[Optional[str]] = mapped_column(String(10))
    is_phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_kyc_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # Wallet
    balance: Mapped[float] = mapped_column(Float, default=0.0)

    # Status
    status: Mapped[UserStatus] = mapped_column(Enum(UserStatus), default=UserStatus.ACTIVE)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Terms acceptance
    terms_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Contact info (used to create Virtualizor user accounts)
    email: Mapped[Optional[str]] = mapped_column(String(255))

    # Per-provider state: {"virt_uids": {"<provider_account_id>": <uid>}}
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    servers: Mapped[list[Server]] = relationship("Server", back_populates="user")
    transactions: Mapped[list[Transaction]] = relationship("Transaction", back_populates="user")


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)

    # Provider
    provider_type: Mapped[ProviderType] = mapped_column(Enum(ProviderType), nullable=False)
    provider_account_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("provider_accounts.id"))
    provider_server_id: Mapped[Optional[str]] = mapped_column(String(255))

    # Identity
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hostname: Mapped[Optional[str]] = mapped_column(String(255))

    # Network
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    ipv6_address: Mapped[Optional[str]] = mapped_column(String(100))

    # Hardware
    ram: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu: Mapped[int] = mapped_column(Integer, nullable=False)
    disk: Mapped[int] = mapped_column(Integer, nullable=False)
    bandwidth: Mapped[int] = mapped_column(Integer, nullable=False)
    os_name: Mapped[Optional[str]] = mapped_column(String(255))

    # Location
    location: Mapped[Optional[str]] = mapped_column(String(100))
    datacenter: Mapped[Optional[str]] = mapped_column(String(100))

    # Status
    status: Mapped[ServerStatus] = mapped_column(Enum(ServerStatus), default=ServerStatus.PENDING)
    suspend_reason: Mapped[Optional[SuspendReason]] = mapped_column(Enum(SuspendReason))

    # Billing
    billing_type: Mapped[BillingType] = mapped_column(Enum(BillingType), nullable=False)
    price_hourly: Mapped[Optional[float]] = mapped_column(Float)
    price_monthly: Mapped[Optional[float]] = mapped_column(Float)

    # Traffic
    traffic_used_gb: Mapped[float] = mapped_column(Float, default=0.0)
    traffic_limit_gb: Mapped[Optional[float]] = mapped_column(Float)

    # Timestamps
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_billed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    suspended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    user: Mapped[User] = relationship("User", back_populates="servers")
    provider_account: Mapped[Optional[ProviderAccount]] = relationship("ProviderAccount", back_populates="servers")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    server_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("servers.id"))
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    reference_id: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship("User", back_populates="transactions")


class ProviderAccount(Base):
    __tablename__ = "provider_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_type: Mapped[ProviderType] = mapped_column(Enum(ProviderType), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(String(1000))
    api_secret: Mapped[Optional[str]] = mapped_column(String(1000))
    api_endpoint: Mapped[Optional[str]] = mapped_column(String(500))
    extra_config: Mapped[Optional[dict]] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Strict KYC: only fully-verified users can buy from this provider
    strict_kyc: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    servers: Mapped[list[Server]] = relationship("Server", back_populates="provider_account")


class ServerPlan(Base):
    __tablename__ = "server_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_type: Mapped[ProviderType] = mapped_column(Enum(ProviderType), nullable=False)
    provider_account_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("provider_accounts.id"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)

    # Specs
    ram: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu: Mapped[int] = mapped_column(Integer, nullable=False)
    disk: Mapped[int] = mapped_column(Integer, nullable=False)
    bandwidth: Mapped[int] = mapped_column(Integer, nullable=False)

    # Pricing (Tomans)
    price_hourly: Mapped[Optional[float]] = mapped_column(Float)
    price_monthly: Mapped[Optional[float]] = mapped_column(Float)

    # Location
    location: Mapped[Optional[str]] = mapped_column(String(100))
    datacenter: Mapped[Optional[str]] = mapped_column(String(100))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    provider_plan_id: Mapped[Optional[str]] = mapped_column(String(255))
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON)

    sub_products: Mapped[list[SubProduct]] = relationship("SubProduct", back_populates="plan", cascade="all, delete-orphan")


class SubProduct(Base):
    """ریز-محصولات قابل خرید برای هر پلن (ترافیک اضافه، IP اضافه و ...)"""
    __tablename__ = "sub_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("server_plans.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[SubProductType] = mapped_column(Enum(SubProductType), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)  # GB برای ترافیک، تعداد برای IP
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    plan: Mapped[ServerPlan] = relationship("ServerPlan", back_populates="sub_products")


class OTP(Base):
    __tablename__ = "otps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiscountCode(Base):
    __tablename__ = "discount_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    discount_percent: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[Optional[int]] = mapped_column(Integer)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    # اختصاصی برای یک کاربر (اختیاری)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    gateway: Mapped[str] = mapped_column(String(50), nullable=False)
    authority: Mapped[Optional[str]] = mapped_column(String(255))
    ref_id: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class BotSettings(Base):
    """تنظیمات ربات — key/value store قابل ویرایش از پنل ادمین"""
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now())


class CryptoPayment(Base):
    """ردیابی پرداخت‌های کریپتو از طریق NOWPayments."""
    __tablename__ = "crypto_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    invoice_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    amount_irt: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="waiting")
    activated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), onupdate=func.now())


class DailyStat(Base):
    """آمار روزانه — بعد از ۳۰ روز پاک می‌شود"""
    __tablename__ = "daily_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, unique=True, index=True)
    new_users: Mapped[int] = mapped_column(Integer, default=0)
    new_servers: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    active_users: Mapped[int] = mapped_column(Integer, default=0)
    total_wallet: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
