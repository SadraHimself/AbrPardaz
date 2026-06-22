"""Abstract base class that every cloud provider must implement."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ServerInfo:
    provider_server_id: str
    name: str
    status: str                        # "active" | "off" | "suspended" | "building"
    ip_address: Optional[str] = None
    ipv6_address: Optional[str] = None
    ram: int = 0                       # MB
    cpu: int = 0
    disk: int = 0                      # GB
    bandwidth: int = 0                 # GB/month
    os_name: Optional[str] = None
    location: Optional[str] = None
    datacenter: Optional[str] = None
    traffic_used_gb: float = 0.0
    extra_data: dict = field(default_factory=dict)


@dataclass
class PlanInfo:
    provider_plan_id: str
    name: str
    ram: int        # MB
    cpu: int
    disk: int       # GB
    bandwidth: int  # GB
    price_hourly: Optional[float] = None
    price_monthly: Optional[float] = None
    location: Optional[str] = None


@dataclass
class CreateServerParams:
    name: str
    plan_id: str         # provider plan id/slug
    os_id: str           # provider OS template id/slug
    location: str
    hostname: Optional[str] = None
    ssh_key_ids: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class BaseProvider(ABC):
    """هر پروایدر باید این متدها را پیاده‌سازی کند."""

    @abstractmethod
    async def create_server(self, params: CreateServerParams) -> ServerInfo:
        """ساخت سرور جدید"""

    @abstractmethod
    async def delete_server(self, server_id: str) -> bool:
        """حذف کامل سرور"""

    @abstractmethod
    async def get_server(self, server_id: str) -> ServerInfo:
        """اطلاعات سرور"""

    @abstractmethod
    async def start_server(self, server_id: str) -> bool:
        """روشن کردن سرور"""

    @abstractmethod
    async def stop_server(self, server_id: str) -> bool:
        """خاموش کردن سرور"""

    @abstractmethod
    async def restart_server(self, server_id: str) -> bool:
        """ریبوت سرور"""

    @abstractmethod
    async def rebuild_server(self, server_id: str, os_id: str) -> bool:
        """نصب مجدد OS"""

    @abstractmethod
    async def suspend_server(self, server_id: str) -> bool:
        """ساسپند (بلاک شبکه، سرور خاموش نمیشه)"""

    @abstractmethod
    async def unsuspend_server(self, server_id: str) -> bool:
        """رفع ساسپند"""

    @abstractmethod
    async def get_traffic(self, server_id: str) -> float:
        """مصرف ترافیک برحسب GB"""

    @abstractmethod
    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        """لیست پلن‌های موجود"""

    @abstractmethod
    async def list_os_templates(self) -> list[dict]:
        """لیست OS های قابل نصب"""

    # ── Optional overrides ────────────────────────────────────────────────────

    async def change_ip(self, server_id: str) -> Optional[str]:
        """تغییر IP - برخی پروایدرها پشتیبانی می‌کنند"""
        raise NotImplementedError("This provider does not support IP change")

    async def edit_server(self, server_id: str, ram: Optional[int] = None,
                          cpu: Optional[int] = None, disk: Optional[int] = None) -> bool:
        """تغییر سخت‌افزار سرور"""
        raise NotImplementedError("This provider does not support live editing")

    async def add_traffic(self, server_id: str, gb: int) -> bool:
        """افزودن ترافیک اضافه"""
        raise NotImplementedError("This provider does not support traffic add-on")
