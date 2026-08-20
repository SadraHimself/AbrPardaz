"""Scaleway Instance API v1 client — https://api.scaleway.com

مرجع: `D:\\Abr Pardaz\\scalewayvpsresellerresearch.md` + OpenAPI رسمی
(`/en/developers/api/instance/v1/schema.yml`, نسخه‌ی خوانده‌شده: ۲۰ اوت ۲۰۲۶).

نکات کلیدی که پیاده‌سازی را شکل می‌دهند:
- احراز هویت: هدر `X-Auth-Token: <secret key>` (نه Bearer). همه‌ی مسیرها
  **per-zone** اند → `provider_server_id = "{zone}:{uuid}"` تا هر متد بدون
  اطلاعات اضافه zone را از خودِ ID دربیاورد (الگوی جیکور).
- ارز: فقط **یورو**. بیلینگ **فقط ساعتی** (pay-as-you-go، حداقل ۶۰ دقیقه).
  عددِ «ماهانه»ی سایت = ساعتی × ۷۳۰ و تخفیف ندارد.
- ⚠️ توقف هزینه **فقط با حذف کامل** است: خاموش‌کردن فقط شارژِ خودِ instance را
  قطع می‌کند؛ **دیسک (volume) و IPv4 رزروشده تا لحظه‌ی حذف شارژ می‌شوند**
  (research §۲). پس `delete_server` باید سرور + همه‌ی volumeها + IP را پاک کند.
- دیسک در قیمتِ تایپ نیست (volume جدا) و API هیچ endpoint قیمتی ندارد →
  نرخِ دیسک/IP تنظیمِ ادمین است (`scaleway_settings.py`).
- سرورِ تازه‌ساخته **خاموش** تحویل می‌شود → فرصتِ ست‌کردن user_data قبل از
  اولین بوت. رمز root فقط از مسیر **cloud-init** می‌نشیند (ایمیج‌های Scaleway
  با SSH-key بالا می‌آیند و ورودِ پسوردی بسته است) — دقیقاً مثل جیکور.
- IP: اگر `dynamic_ip_required` بماند، IP با هر خاموش/روشن عوض می‌شود. پس
  **flexible IP** (`routed_ipv4`) جدا رزرو و با `public_ips` وصل می‌شود و
  هنگام حذف صریحاً `DeleteIp` می‌خورد (وگرنه رزروِ یتیم ابدی شارژ می‌شود).
- ترافیک خروجی نامحدود و رایگان است و API مصرف تجمعی نمی‌دهد → `get_traffic=0`
  و `bandwidth=0` (سرعت پورت در `extra_data.bandwidth_mbit` نمایش داده می‌شود).
- suspend واقعی ندارد → poweroff/poweron.
- ویندوز عرضه نمی‌شود: رمزِ Administrator فقط به‌صورت رمزنگاری‌شده با کلید RSA
  برگردانده می‌شود (`admin_password_encryption_ssh_key_id`) و فلوی تحویلِ ربات
  آن را پشتیبانی نمی‌کند.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .base import BaseProvider, CreateServerParams, PlanInfo, ServerInfo

logger = logging.getLogger(__name__)

API_BASE = "https://api.scaleway.com"

# Availability Zoneهای Instance (enum رسمی OpenAPI v1) → نام نمایشی
ZONES: dict[str, str] = {
    "fr-par-1": "Paris-1",
    "fr-par-2": "Paris-2",
    "fr-par-3": "Paris-3",
    "nl-ams-1": "Amsterdam-1",
    "nl-ams-2": "Amsterdam-2",
    "nl-ams-3": "Amsterdam-3",
    "pl-waw-1": "Warsaw-1",
    "pl-waw-2": "Warsaw-2",
    "pl-waw-3": "Warsaw-3",
    "it-mil-1": "Milan-1",
}

# وضعیت Scaleway → ۴ حالت داخلی چهارچوب (active | off | suspended | building)
_STATUS_MAP = {
    "running": "active",
    "stopped": "off",
    "stopped in place": "off",
    "starting": "building",
    "stopping": "building",
    "locked": "suspended",
}
_RUNNING = {"running"}

# نوع دیسکِ بوت: همه‌ی خانواده‌های امروزیِ Scaleway از Block Storage پشتیبانی
# می‌کنند و ایمیجِ label-محور هم پیش‌فرض `instance_sbs` می‌سازد.
BOOT_VOLUME_TYPE = "sbs_volume"
MIN_DISK_GB = 10          # حداقل دیسک سیستم طبق داکس

# کد کوتاه خانواده‌ی تایپ برای نام نمایشی محصول (دکمه‌ی خرید باید کوتاه بماند —
# متن بلند روی موبایل بریده می‌شود). ترتیب مهم است: بلندترین پیشوند اول.
_FAMILY_CODES: tuple[tuple[str, str], ...] = (
    ("STANDARD3-X", "S3X"),
    ("STANDARD2-A", "S2A"),
    ("COMPUTE3-X", "C3X"),
    ("MEMORY3-X", "M3X"),
    ("BASIC3-X", "B3X"),
    ("BASIC2-A", "B2A"),
    ("POP2-HC", "PHC"),
    ("POP2-HM", "PHM"),
    ("POP2-HN", "PHN"),
    ("POP2", "PO2"),
    ("PRO2", "PR2"),
    ("PLAY2", "PL2"),
    ("STARDUST1", "SD1"),
    ("DEV1", "DV1"),
    ("GP1", "GP1"),
)

# خانواده‌هایی که عرضه نمی‌شوند (منبع واحد سیاست — ایمپورت/سود/سینک از همین
# استفاده می‌کنند):
#   *-WIN  → ویندوز؛ رمز فقط رمزنگاری‌شده برمی‌گردد (تحویل ممکن نیست)
#   RENDER/H100/L4/L40S/GPU → کارت گرافیک (خارج از دامنه‌ی فروش)
_EXCLUDED_TOKENS = ("-WIN", "RENDER", "H100", "L40S", "L4-", "GPU", "COPARM")


def is_excluded_type(commercial_type: str) -> bool:
    """آیا این commercial_type اصلاً عرضه نمی‌شود (ویندوز/GPU)."""
    t = (commercial_type or "").upper()
    if t.endswith("-L4") or t.endswith("-WIN"):
        return True
    return any(tok in t for tok in _EXCLUDED_TOKENS)


def family_of(commercial_type: str) -> str:
    """خانواده‌ی یک تایپ: `BASIC2-A4C-8G` → `BASIC2-A` · `POP2-HC-2C-4G` →
    `POP2-HC`. هر zone حدود صد تایپ دارد؛ پنل ایمپورت با همین گروه‌بندی
    یک مرحله‌ی «خانواده» می‌سازد تا کیبورد از سقف تلگرام رد نشود."""
    t = (commercial_type or "").upper()
    for prefix, _code in _FAMILY_CODES:
        if t.startswith(prefix):
            return prefix
    return t.split("-")[0] or "OTHER"


def family_code(commercial_type: str) -> str:
    """کد کوتاه خانواده: `BASIC2-A4C-8G` → `B2A`. ناشناخته → سه حرف اول."""
    t = (commercial_type or "").upper()
    for prefix, code in _FAMILY_CODES:
        if t.startswith(prefix):
            return code
    return (t.split("-")[0] or "SCW")[:3]


def short_name(commercial_type: str, cpu: int, ram_mb: int) -> str:
    """نام نمایشی محصول: `B2A-4C8G` — کوتاه، یکتا در هر zone و بدون برند."""
    ram_g = ram_mb // 1024 if ram_mb >= 1024 else 0
    ram_part = f"{ram_g}G" if ram_g else f"{ram_mb}M"
    return f"{family_code(commercial_type)}-{cpu}C{ram_part}"


def zone_label(zone: str) -> str:
    return ZONES.get(zone, zone)


def _gb_to_bytes(gb: int) -> int:
    """اندازه‌ی volume باید مضربی از ۵۱۲ بایت باشد؛ 10^9 هست (2^9 × 1953125)."""
    return int(gb) * 1_000_000_000


class ScalewayProvider(BaseProvider):
    def __init__(self, api_token: str, project_id: str = ""):
        self.token = (api_token or "").strip()
        # پروژه‌ی مقصد؛ خالی = پروژه‌ی پیش‌فرضِ خودِ API key
        self.project_id = (project_id or "").strip()
        # رمزی که ربات تولید و از مسیر cloud-init می‌نشاند — لایه‌ی سرویس
        # همین را تحویل می‌دهد (Scaleway هیچ رمزی برنمی‌گرداند)
        self.last_root_password: str | None = None
        # نام سیستم‌عاملِ آخرین ریبیلد. ⚠️ `server.image` بعد از تعویضِ دیسکِ بوت
        # همان ایمیجِ زمانِ ساخت می‌ماند، پس رکورد سرور باید از این خوانده شود
        # وگرنه کاربر بعد از نصب مجدد، اسمِ OSِ قبلی را می‌بیند.
        self.last_os_name: str | None = None

    # ── HTTP core ─────────────────────────────────────────────────────────────

    @staticmethod
    def _friendly_error(status: int, data: dict, raw: str) -> str:
        """پیام فارسیِ قابل‌نمایش از بدنه‌ی خطای Scaleway.

        بدنه‌ی خطا: {"type": "...", "message": "...", "resource": "...",
        "fields": {...}} یا {"details": [...]}"""
        etype = str((data or {}).get("type") or "")
        msg = str((data or {}).get("message") or raw or "")
        low = f"{etype} {msg}".lower()
        if "out_of_stock" in low or "no capacity" in low or "shortage" in low \
                or "resource_exhausted" in low:
            return "ظرفیت این پلن در این لوکیشن موقتاً تکمیل است — کمی بعد دوباره تلاش کنید"
        if "quota" in low:
            return ("__SCW_QUOTA__ سهمیه‌ی اکانت سرویس‌دهنده برای این نوع سرور پر "
                    "است — با پشتیبانی تماس بگیرید")
        if status == 401 or "denied_authentication" in low:
            return "Scaleway API: توکن نامعتبر یا منقضی است"
        if status == 403 or "permissions_denied" in low:
            return "Scaleway API: این توکن دسترسی لازم را ندارد (IAM permission)"
        if "invalid_arguments" in low:
            fields = (data or {}).get("fields") or {}
            det = "؛ ".join(f"{k}: {v}" for k, v in list(fields.items())[:3]) if fields else ""
            return f"Scaleway API {status}: پارامتر نامعتبر — {det or msg}"[:300]
        return f"Scaleway API {status} {etype}: {msg}"[:300]

    async def _request(self, method: str, path: str, json: Optional[dict] = None,
                       params: Optional[dict] = None, data: Optional[bytes] = None,
                       timeout: int = 30, raw_content_type: Optional[str] = None) -> dict:
        """درخواست با retry روی خطاهای گذرا (429/5xx/شبکه). خطای نهایی
        `RuntimeError` با پیام فارسیِ قابل‌نمایش (کلاس exception سفارشی نداریم)."""
        headers = {"X-Auth-Token": self.token}
        if raw_content_type:
            headers["Content-Type"] = raw_content_type
        last_err = "unknown"
        for attempt in range(4):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session:
                    async with session.request(
                        method, f"{API_BASE}{path}",
                        headers=headers, json=json, params=params, data=data,
                    ) as resp:
                        if resp.status == 204:
                            return {}
                        raw = await resp.text()
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            body = {}
                        if not isinstance(body, dict):
                            body = {"_list": body}
                        if resp.status < 400:
                            return body
                        last_err = self._friendly_error(resp.status, body, raw[:200])
                        if resp.status == 429:
                            # Scaleway سقف نرخ را عمومی نکرده — Retry-After اگر بود
                            try:
                                wait_s = float(resp.headers.get("Retry-After") or 0)
                            except (TypeError, ValueError):
                                wait_s = 0
                            await asyncio.sleep(min(max(wait_s, 3 * (attempt + 1)), 30))
                            continue
                        if resp.status in (500, 502, 503, 504):
                            await asyncio.sleep(3 * (attempt + 1))
                            continue
                        raise RuntimeError(last_err)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = str(e)
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Scaleway API retry limit — {last_err}"[:300])

    @staticmethod
    def _split_sid(server_id: str) -> tuple[str, str]:
        """`provider_server_id = "{zone}:{uuid}"` — خودکفا برای همه‌ی متدها."""
        zone, _, uuid = (server_id or "").partition(":")
        if not uuid or zone not in ZONES:
            raise RuntimeError(f"شناسه سرور Scaleway نامعتبر است: {server_id}")
        return zone, uuid

    def _z(self, zone: str, path: str) -> str:
        return f"/instance/v1/zones/{zone}{path}"

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_ips(srv: dict) -> tuple[Optional[str], Optional[str]]:
        """IPv4/IPv6 از `public_ips` (مدل routed IP)؛ fallback به `public_ip`
        قدیمی که هنوز برای سازگاری پر می‌شود."""
        ipv4 = ipv6 = None
        for entry in (srv.get("public_ips") or []):
            addr = (entry or {}).get("address")
            if not addr:
                continue
            if ":" in str(addr):
                ipv6 = ipv6 or str(addr)
            else:
                ipv4 = ipv4 or str(addr)
        if not ipv4:
            legacy = srv.get("public_ip") or {}
            if legacy.get("address"):
                ipv4 = str(legacy["address"])
        return ipv4, ipv6

    @staticmethod
    def _boot_volume(srv: dict) -> dict:
        """volume بوت (کلید "0" یا اولین volume) — برای ریبیلد و حذف."""
        vols = srv.get("volumes") or {}
        if isinstance(vols, dict):
            if "0" in vols and isinstance(vols["0"], dict):
                return vols["0"]
            for v in vols.values():
                if isinstance(v, dict):
                    return v
        return {}

    def _server_info(self, srv: dict, zone: str,
                     root_password: Optional[str] = None) -> ServerInfo:
        raw_status = str(srv.get("state") or "stopped").lower()
        ipv4, ipv6 = self._extract_ips(srv)
        boot = self._boot_volume(srv)
        try:
            disk_gb = int(int(boot.get("size") or 0) / 1_000_000_000)
        except (TypeError, ValueError):
            disk_gb = 0
        image = srv.get("image") or {}
        extra = {
            "machine_status": "1" if raw_status in _RUNNING else "0",
            "scaleway_status": raw_status,
            "scaleway_state_detail": srv.get("state_detail") or "",
            "username": "root",
        }
        if root_password:
            extra["root_password"] = root_password
        return ServerInfo(
            provider_server_id=f"{zone}:{srv.get('id')}",
            name=srv.get("name") or "",
            status=_STATUS_MAP.get(raw_status, "off"),
            ip_address=ipv4,
            ipv6_address=ipv6,
            ram=0,      # پاسخ سرور RAM/CPU نمی‌دهد؛ رکورد Server از پلن پر می‌شود
            cpu=0,
            disk=disk_gb,
            bandwidth=0,                      # ترافیک نامحدود
            os_name=image.get("name") or None,
            location=zone,
            datacenter=zone,
            traffic_used_gb=0.0,
            extra_data=extra,
        )

    # ── انتظار وضعیت ─────────────────────────────────────────────────────────

    async def _get_raw(self, zone: str, uuid: str) -> dict:
        body = await self._request("GET", self._z(zone, f"/servers/{uuid}"))
        return body.get("server") or {}

    async def _wait_state(self, zone: str, uuid: str, targets: set[str],
                          timeout_s: int = 600, need_ip: bool = False) -> dict:
        """Poll هر ۵ ثانیه تا رسیدن به وضعیت هدف. Scaleway task-id می‌دهد ولی
        منبع حقیقتِ قابل‌اتکا خودِ `server.state` است (الگوی تایم‌وب)."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        last: dict = {}
        polls = 0
        while asyncio.get_event_loop().time() < deadline:
            try:
                last = await self._get_raw(zone, uuid)
            except RuntimeError as e:
                if "404" in str(e) or "not_found" in str(e).lower():
                    raise
                last = {}
            state = str(last.get("state") or "").lower()
            polls += 1
            if polls % 6 == 1:
                logger.info("SCW_POLL %s/%s #%d state=%s ip=%s",
                            zone, uuid, polls, state or "?",
                            bool(self._extract_ips(last)[0]))
            if state in targets and (not need_ip or self._extract_ips(last)[0]):
                return last
            await asyncio.sleep(5)
        raise RuntimeError("Scaleway: مهلت انتظار عملیات تمام شد")

    # ── cloud-init ───────────────────────────────────────────────────────────

    @staticmethod
    def _cloud_config(password: str) -> bytes:
        """ست‌کردن رمز root + بازکردن SSH پسوردی.

        ایمیج‌های Scaleway فقط با SSH-key بالا می‌آیند؛ `ssh_pwauth` تنها
        `PasswordAuthentication` را باز می‌کند و `PermitRootLogin` را نه →
        هر دو صریحاً نوشته می‌شوند (همان الگوی اثبات‌شده‌ی جیکور)."""
        return (
            "#cloud-config\n"
            "disable_root: false\n"
            "ssh_pwauth: true\n"
            "chpasswd:\n"
            "  expire: false\n"
            "  list: |\n"
            f"    root:{password}\n"
            "runcmd:\n"
            "  - mkdir -p /etc/ssh/sshd_config.d\n"
            "  - printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n'"
            " > /etc/ssh/sshd_config.d/99-rootpass.conf\n"
            "  - sed -i 's/^#\\?PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config\n"
            "  - sed -i 's/^#\\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config\n"
            "  - systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true\n"
        ).encode()

    async def _set_cloud_init(self, zone: str, uuid: str, password: str) -> None:
        """user_data باید **قبل از اولین بوت** ست شود — سرورِ تازه‌ساخته‌ی
        Scaleway خاموش تحویل می‌شود، پس این پنجره همیشه در دسترس است."""
        await self._request(
            "PATCH", self._z(zone, f"/servers/{uuid}/user_data/cloud-init"),
            data=self._cloud_config(password),
            raw_content_type="text/plain",
        )

    # ── ساخت ─────────────────────────────────────────────────────────────────

    async def _create_ip(self, zone: str) -> dict:
        """رزرو یک flexible IP (routed_ipv4).

        ⚠️ بدون این، `dynamic_ip_required` پیش‌فرض یک IP پویا می‌دهد که با هر
        خاموش/روشن عوض می‌شود — برای مشتری قابل قبول نیست."""
        body: dict = {"type": "routed_ipv4"}
        if self.project_id:
            body["project"] = self.project_id
        data = await self._request("POST", self._z(zone, "/ips"), json=body)
        ip = data.get("ip") or {}
        if not ip.get("id"):
            raise RuntimeError("Scaleway آدرس IP رزروشده را برنگرداند")
        return ip

    async def _delete_ip(self, zone: str, ip_id: str) -> None:
        try:
            await self._request("DELETE", self._z(zone, f"/ips/{ip_id}"))
        except RuntimeError as e:
            if "404" not in str(e) and "not_found" not in str(e).lower():
                logger.warning("scaleway: delete ip %s failed: %s", ip_id, e)

    async def _delete_volume(self, zone: str, vol_id: str) -> None:
        """حذف volume — اول Instance API، بعد Block API.

        volumeهای `sbs_volume` مالِ سرویس Block Storage اند و بسته به نوع، فقط
        از یکی از دو API پاک می‌شوند؛ هر volumeِ جامانده تا ابد شارژ می‌شود."""
        for path in (self._z(zone, f"/volumes/{vol_id}"),
                     f"/block/v1/zones/{zone}/volumes/{vol_id}"):
            try:
                await self._request("DELETE", path)
                return
            except RuntimeError as e:
                low = str(e).lower()
                if "404" in low or "not_found" in low:
                    return
                logger.info("scaleway: volume delete via %s failed: %s", path, e)
        logger.warning("scaleway: volume %s could not be deleted (still billed!)", vol_id)

    async def create_server(self, params: CreateServerParams) -> ServerInfo:
        zone = (params.location or "").strip()
        if zone not in ZONES:
            raise RuntimeError("لوکیشن Scaleway نامعتبر است — پلن را دوباره ایمپورت کنید")
        if not params.plan_id:
            raise RuntimeError("نوع سرور (commercial_type) مشخص نیست")
        if not params.os_id:
            raise RuntimeError("سیستم‌عامل انتخاب نشده است")

        disk_gb = max(int(params.extra.get("disk") or 0), MIN_DISK_GB)

        # رمز root: ورودی لایه‌ی خرید؛ fallback تولید داخلی
        password = params.extra.get("root_password")
        if not password:
            import secrets as _sec
            import string as _str
            _alpha = _str.ascii_letters + _str.digits + "!@#$%^&*"
            password = "".join(_sec.choice(_alpha) for _ in range(16))

        ip = await self._create_ip(zone)
        ip_id, ip_addr = ip["id"], ip.get("address")

        body: dict = {
            "name": (params.name or "srv")[:63],
            "commercial_type": params.plan_id,
            # label مارکت‌پلیس (مثل `ubuntu_noble`) → آخرین ایمیجِ همان توزیع
            "image": params.os_id,
            "volumes": {"0": {"volume_type": BOOT_VOLUME_TYPE,
                              "size": _gb_to_bytes(disk_gb),
                              "boot": True}},
            "public_ips": [ip_id],
            "dynamic_ip_required": False,
            "tags": ["abrpardaz"] + [
                f"{k}:{v}" for k, v in (params.extra.get("labels") or {}).items()],
        }
        if self.project_id:
            body["project"] = self.project_id

        uuid: Optional[str] = None
        try:
            try:
                data = await self._request("POST", self._z(zone, "/servers"),
                                           json=body, timeout=60)
            except RuntimeError as e:
                # تداخل نام → یک retry با پسوند تصادفی (قاعده ۵.۲#۳)
                if "conflict" in str(e).lower() or "already" in str(e).lower():
                    import secrets as _sec
                    body["name"] = f"{(params.name or 'srv')[:50]}-{_sec.token_hex(2)}"
                    data = await self._request("POST", self._z(zone, "/servers"),
                                               json=body, timeout=60)
                else:
                    raise
            srv = data.get("server") or {}
            uuid = srv.get("id")
            if not uuid:
                raise RuntimeError("Scaleway شناسه سرور ساخته‌شده را برنگرداند")

            # سرور خاموش ساخته می‌شود → cloud-init، بعد روشن
            await self._set_cloud_init(zone, uuid, password)
            await self._request("POST", self._z(zone, f"/servers/{uuid}/action"),
                                json={"action": "poweron"}, timeout=60)
            fresh = await self._wait_state(zone, uuid, {"running"},
                                           timeout_s=600, need_ip=True)
        except Exception:
            # رِسکیو قبل از حذف (قاعده ۵.۲#۳): اگر سرور واقعاً آماده است تحویل
            # بده، نه حذف — وگرنه مشتریِ پول‌داده سرورِ ساخته‌شده را از دست می‌دهد
            if uuid:
                try:
                    chk = await self._get_raw(zone, uuid)
                    if str(chk.get("state") or "").lower() == "running" \
                            and self._extract_ips(chk)[0]:
                        self.last_root_password = password
                        info = self._server_info(chk, zone, root_password=password)
                        info.extra_data.setdefault("ip_id", ip_id)
                        return info
                except Exception:
                    pass
            # ساخت تراکنشی: هر چه ساخته شده پاک شود تا هزینه‌ی یتیم نماند
            try:
                if uuid:
                    await self._teardown(zone, uuid)
            except Exception:
                logger.exception("scaleway: cleanup after failed create")
            try:
                await self._delete_ip(zone, ip_id)
            except Exception:
                pass
            raise

        self.last_root_password = password
        info = self._server_info(fresh, zone, root_password=password)
        info.extra_data["ip_id"] = ip_id
        if not info.ip_address and ip_addr:
            info.ip_address = ip_addr
        return info

    # ── حذف ──────────────────────────────────────────────────────────────────

    async def _teardown(self, zone: str, uuid: str) -> bool:
        """حذف کاملِ سرور + volumeها + IPهای رزروشده.

        ⚠️ هر سه لازم‌اند: `DELETE /servers/{id}` فقط خودِ instance را می‌برد و
        دیسک و IP تا لحظه‌ی حذفِ خودشان شارژ می‌شوند (research §۲/§۴)."""
        try:
            srv = await self._get_raw(zone, uuid)
        except RuntimeError as e:
            low = str(e).lower()
            if "404" in low or "not_found" in low:
                return True   # قبلاً حذف شده
            raise

        vol_ids = [v.get("id") for v in (srv.get("volumes") or {}).values()
                   if isinstance(v, dict) and v.get("id")]
        ip_ids = [x.get("id") for x in (srv.get("public_ips") or []) if x.get("id")]
        legacy_ip = (srv.get("public_ip") or {}).get("id")
        if legacy_ip and legacy_ip not in ip_ids:
            ip_ids.append(legacy_ip)

        # حذف فقط روی سرور خاموش مجاز است
        if str(srv.get("state") or "").lower() != "stopped":
            try:
                await self._request("POST", self._z(zone, f"/servers/{uuid}/action"),
                                    json={"action": "poweroff"}, timeout=60)
            except RuntimeError as e:
                logger.info("scaleway: poweroff before delete: %s", e)
            try:
                await self._wait_state(zone, uuid, {"stopped"}, timeout_s=240)
            except RuntimeError as e:
                logger.warning("scaleway: server %s not stopped in time (%s) — "
                               "trying delete anyway", uuid, e)

        try:
            await self._request("DELETE", self._z(zone, f"/servers/{uuid}"), timeout=60)
        except RuntimeError as e:
            low = str(e).lower()
            if "404" not in low and "not_found" not in low:
                raise

        for vid in vol_ids:
            await self._delete_volume(zone, vid)
        for iid in ip_ids:
            await self._delete_ip(zone, iid)
        return True

    async def delete_server(self, server_id: str) -> bool:
        zone, uuid = self._split_sid(server_id)
        return await self._teardown(zone, uuid)

    # ── عملیات پایه ──────────────────────────────────────────────────────────

    async def get_server(self, server_id: str) -> ServerInfo:
        zone, uuid = self._split_sid(server_id)
        return self._server_info(await self._get_raw(zone, uuid), zone)

    async def _action(self, server_id: str, action: str, wait: set[str] | None = None,
                      timeout_s: int = 300) -> bool:
        zone, uuid = self._split_sid(server_id)
        await self._request("POST", self._z(zone, f"/servers/{uuid}/action"),
                            json={"action": action}, timeout=60)
        if wait:
            try:
                await self._wait_state(zone, uuid, wait, timeout_s=timeout_s)
            except RuntimeError as e:
                logger.warning("scaleway action %s wait: %s", action, e)
        return True

    async def start_server(self, server_id: str) -> bool:
        return await self._action(server_id, "poweron", {"running"})

    async def stop_server(self, server_id: str) -> bool:
        return await self._action(server_id, "poweroff", {"stopped"})

    async def restart_server(self, server_id: str) -> bool:
        return await self._action(server_id, "reboot", {"running"})

    async def suspend_server(self, server_id: str) -> bool:
        # suspend واقعی ندارد → خاموش. توجه: شارژِ instance قطع می‌شود ولی دیسک
        # و IP تا حذف شارژ دارند (هزینه‌ی ناچیزِ دوره‌ی تعلیق، پذیرفته‌شده).
        return await self.stop_server(server_id)

    async def unsuspend_server(self, server_id: str) -> bool:
        return await self.start_server(server_id)

    async def get_traffic(self, server_id: str) -> float:
        # ترافیک خروجی نامحدود و رایگان است و API مصرفِ تجمعی نمی‌دهد
        return 0.0

    # ── ریبیلد (تعویض دیسک بوت) ──────────────────────────────────────────────

    async def _resolve_image(self, zone: str, label_or_id: str,
                             arch: Optional[str] = None) -> dict:
        """label مارکت‌پلیس (یا UUID ایمیج) → آبجکت ایمیجِ Instance API.

        برای ریبیلد به `root_volume` (اسنپ‌شات پایه) نیاز داریم، و آن فقط از
        Instance API درمی‌آید؛ نگاشتِ label→id هم از Marketplace v2."""
        import re
        image_id = label_or_id
        # UUID = شناسه‌ی مستقیم ایمیج؛ هر چیز دیگری label مارکت‌پلیس است
        if not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                            label_or_id or ""):
            data = await self._request(
                "GET", "/marketplace/v2/local-images",
                params={"zone": zone, "image_label": label_or_id,
                        "type": "instance_sbs", "page_size": "100"})
            locals_ = [li for li in (data.get("local_images") or [])
                       if isinstance(li, dict)]
            # ⚠️ یک label در هر zone می‌تواند چند نسخه‌ی معماری داشته باشد
            # (x86_64 و arm64) — فیلتر سمت خودمان، چون رشته‌ی arch مارکت‌پلیس
            # («arm64») با arch سرور («arm») یکی نیست
            if arch:
                want = self._norm_arch(arch)
                same = [li for li in locals_ if self._norm_arch(li.get("arch")) == want]
                locals_ = same or locals_
            if not locals_:
                raise RuntimeError(
                    f"سیستم‌عامل «{label_or_id}» در این لوکیشن موجود نیست")
            image_id = locals_[0].get("id") or ""
        body = await self._request("GET", self._z(zone, f"/images/{image_id}"))
        img = body.get("image") or {}
        if not img.get("id"):
            raise RuntimeError("اطلاعات ایمیج سیستم‌عامل در دسترس نیست")
        return img

    async def rebuild_server(self, server_id: str, os_id: str,
                             rootpass: str = "") -> bool:
        """نصب مجدد OS.

        Scaleway endpointِ «reinstall» ندارد؛ کاری که کنسول می‌کند تعویضِ دیسکِ
        بوت است: خاموش → `PATCH /servers/{id}` با volumeِ تازه‌ای که از اسنپ‌شات
        ایمیج ساخته می‌شود → cloud-init → روشن → حذف دیسک قدیمی.
        هر شکستِ وسط راه، دیسک قبلی را برمی‌گرداند (سرور بدون بوت نمی‌ماند)."""
        zone, uuid = self._split_sid(server_id)
        password = rootpass or None
        if not password:
            import secrets as _sec
            import string as _str
            _alpha = _str.ascii_letters + _str.digits + "!@#$%^&*"
            password = "".join(_sec.choice(_alpha) for _ in range(16))

        srv = await self._get_raw(zone, uuid)
        arch = str(srv.get("arch") or "").lower() or None
        old = self._boot_volume(srv)
        old_id = old.get("id")
        old_type = old.get("volume_type") or BOOT_VOLUME_TYPE
        try:
            size = int(old.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            size = _gb_to_bytes(MIN_DISK_GB)

        img = await self._resolve_image(zone, os_id, arch=arch)
        snapshot_id = (img.get("root_volume") or {}).get("id")
        if not snapshot_id:
            raise RuntimeError("اسنپ‌شات پایه‌ی این سیستم‌عامل در دسترس نیست")

        # خاموش‌کردن لازم است: تعویض volume روی سرور روشن مجاز نیست
        if str(srv.get("state") or "").lower() != "stopped":
            await self._request("POST", self._z(zone, f"/servers/{uuid}/action"),
                                json={"action": "poweroff"}, timeout=60)
            await self._wait_state(zone, uuid, {"stopped"}, timeout_s=300)

        new_vol = {
            "name": f"{(srv.get('name') or 'srv')[:40]}-root",
            "volume_type": BOOT_VOLUME_TYPE,
            "size": size,
            "base_snapshot": snapshot_id,
            "boot": True,
        }
        try:
            await self._request("PATCH", self._z(zone, f"/servers/{uuid}"),
                                json={"volumes": {"0": new_vol}}, timeout=60)
        except RuntimeError:
            # برگرداندن دیسک قبلی تا سرور بی‌بوت نماند، بعد خطا بالا برود
            if old_id:
                try:
                    await self._request(
                        "PATCH", self._z(zone, f"/servers/{uuid}"),
                        json={"volumes": {"0": {"id": old_id,
                                                "volume_type": old_type,
                                                "boot": True}}}, timeout=60)
                    await self._request(
                        "POST", self._z(zone, f"/servers/{uuid}/action"),
                        json={"action": "poweron"}, timeout=60)
                except Exception:
                    logger.exception("scaleway: rollback of boot volume failed")
            raise

        await self._set_cloud_init(zone, uuid, password)
        await self._request("POST", self._z(zone, f"/servers/{uuid}/action"),
                            json={"action": "poweron"}, timeout=60)
        await self._wait_state(zone, uuid, {"running"}, timeout_s=600)

        # دیسک قدیمی الان جدا شده — تا حذف نشود شارژ می‌شود
        if old_id:
            fresh_ids = {v.get("id") for v in
                         ((await self._get_raw(zone, uuid)).get("volumes") or {}).values()
                         if isinstance(v, dict)}
            if old_id not in fresh_ids:
                await self._delete_volume(zone, old_id)

        self.last_root_password = password
        self.last_os_name = img.get("name") or None
        return True

    # ── کاتالوگ ───────────────────────────────────────────────────────────────

    @staticmethod
    def _type_specs(t: dict) -> tuple[int, int, int]:
        """(cpu، رم به MB، سرعت پورت به Mbit)."""
        cpu = int(t.get("ncpus") or 0)
        try:
            ram_mb = int(int(t.get("ram") or 0) / (1024 * 1024))
        except (TypeError, ValueError):
            ram_mb = 0
        net = t.get("network") or {}
        try:
            mbit = int(int(net.get("sum_internet_bandwidth") or 0) / 1_000_000)
        except (TypeError, ValueError):
            mbit = 0
        return cpu, ram_mb, mbit

    async def _raw_types(self, zone: str) -> dict:
        """map از commercial_type → آبجکت خام ServerType (با صفحه‌بندی)."""
        out: dict = {}
        page = 1
        while page <= 10:
            data = await self._request(
                "GET", self._z(zone, "/products/servers"),
                params={"per_page": "100", "page": str(page)})
            chunk = data.get("servers") or {}
            if not isinstance(chunk, dict) or not chunk:
                break
            out.update(chunk)
            if len(chunk) < 100:
                break
            page += 1
        return out

    async def availability(self, zone: str) -> dict:
        """map از commercial_type → `available|scarce|shortage`.

        این تنها سیگنالِ موجودیِ رسمیِ Scaleway است — سینک کاتالوگ با همین
        پلن‌های `shortage` را از فروش برمی‌دارد (بدون یادگیری از شکستِ ساخت)."""
        out: dict = {}
        page = 1
        while page <= 10:
            data = await self._request(
                "GET", self._z(zone, "/products/servers/availability"),
                params={"per_page": "100", "page": str(page)})
            chunk = data.get("servers") or {}
            if not isinstance(chunk, dict) or not chunk:
                break
            for name, val in chunk.items():
                out[name] = str((val or {}).get("availability") or "available")
            if len(chunk) < 100:
                break
            page += 1
        return out

    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        """تایپ‌های یک zone با «قیمت خرید» ساعتیِ یورو.

        ⚠️ قیمت اینجا فقط خودِ instance است؛ دیسک و IPv4 جدا شارژ می‌شوند و
        لایه‌ی `scaleway_settings` آن‌ها را به قیمت خرید اضافه می‌کند.
        `monthly_price` پاسخ deprecated و ۳۰روزه است — استفاده نمی‌شود."""
        if not location:
            raise RuntimeError("برای Scaleway، لیست پلن‌ها per-zone است — لوکیشن بدهید")
        zone = location
        if zone not in ZONES:
            raise RuntimeError(f"zone نامعتبر: {zone}")
        types = await self._raw_types(zone)
        out: list[PlanInfo] = []
        for name, t in types.items():
            if not isinstance(t, dict):
                continue
            if is_excluded_type(name):
                continue
            if t.get("end_of_service"):
                continue
            if int(t.get("gpu") or 0) > 0 or (t.get("gpu_info") or None):
                continue
            if t.get("baremetal"):
                continue
            caps = t.get("capabilities") or {}
            if caps.get("block_storage") is False:
                continue        # دیسک بوتِ ما همیشه Block است
            hourly = float(t.get("hourly_price") or 0)
            if hourly <= 0:
                continue
            cpu, ram_mb, mbit = self._type_specs(t)
            if not cpu or not ram_mb:
                continue
            out.append(PlanInfo(
                provider_plan_id=name,
                name=name,
                ram=ram_mb,
                cpu=cpu,
                disk=0,        # دیسک را لایه‌ی ایمپورت تعیین می‌کند (volume جدا)
                bandwidth=0,   # ترافیک نامحدود
                price_hourly=round(hourly, 6),
                # ماهانه‌ی محافظه‌کارانه: ماهِ ۳۱روزه = ۷۴۴ ساعت (research §۲ —
                # اگر ۷۳۰ بگیریم، در ماه‌های بلند ضرر می‌کنیم)
                price_monthly=round(hourly * 744, 4),
                location=zone,
                currency="eur",
            ))
        out.sort(key=lambda p: (p.ram, p.cpu, p.price_hourly or 0))
        return out

    async def raw_type_fields(self, zone: str) -> dict:
        """جزئیات خامِ هر تایپ برای پنل ایمپورت: معماری، سرعت پورت، حداقل/حداکثر
        دیسکِ محلی و پشتیبانی از Block."""
        out: dict = {}
        for name, t in (await self._raw_types(zone)).items():
            if not isinstance(t, dict):
                continue
            cpu, ram_mb, mbit = self._type_specs(t)
            caps = t.get("capabilities") or {}
            vc = t.get("volumes_constraint") or {}
            out[name] = {
                "arch": str(t.get("arch") or "x86_64"),
                "bandwidth_mbit": mbit,
                "cpu": cpu,
                "ram": ram_mb,
                "block_storage": caps.get("block_storage") is not False,
                "local_min_gb": int(int(vc.get("min_size") or 0) / 1_000_000_000),
                "local_max_gb": int(int(vc.get("max_size") or 0) / 1_000_000_000),
                "end_of_service": bool(t.get("end_of_service")),
            }
        return out

    # ── سیستم‌عامل ───────────────────────────────────────────────────────────

    # خانواده‌هایی که همه‌ی نسخه‌هایشان عرضه می‌شوند؛ بقیه فقط جدیدترین نسخه
    # (لیست OS باید خلوت و کاربردی بماند — الگوی جیکور)
    _OS_FULL_FAMILIES = {"ubuntu", "debian"}
    _OS_PRIORITY = {"ubuntu": 0, "debian": 1, "almalinux": 2, "rockylinux": 3,
                    "centos": 4, "fedora": 5}
    # ایمیج‌هایی که سیستم‌عاملِ خام نیستند (اپلاینس/اپ) یا پشتیبانی نمی‌شوند
    _OS_SKIP_TOKENS = ("windows", "docker", "wordpress", "gitlab", "nextcloud",
                       "plesk", "cpanel", "jitsi", "odoo", "openvpn", "pfsense")

    @staticmethod
    def _norm_arch(arch: Optional[str]) -> str:
        a = (arch or "").lower()
        return "arm" if a.startswith("arm") else "x86_64"

    async def list_os_templates(self, location: Optional[str] = None,
                                commercial_type: Optional[str] = None,
                                arch: Optional[str] = None) -> list[dict]:
        """ایمیج‌های قابل نصب روی یک zone (و در صورت نیاز، سازگار با یک تایپ).

        `id` که برمی‌گردد **label مارکت‌پلیس** است (مثل `ubuntu_noble`) — همان
        چیزی که `CreateServer` قبول می‌کند و همیشه به آخرین نسخه‌ی همان توزیع
        نگاشت می‌شود، پس با گذشتِ زمان کهنه نمی‌شود."""
        if not location:
            raise RuntimeError("برای Scaleway، لیست OS per-zone است — لوکیشن بدهید")
        zone = location
        want_arch = self._norm_arch(arch)

        params = {"zone": zone, "type": "instance_sbs", "page_size": "100"}
        labels: dict[str, dict] = {}
        page = 1
        while page <= 10:
            params["page"] = str(page)
            data = await self._request("GET", "/marketplace/v2/local-images",
                                       params=params)
            chunk = data.get("local_images") or []
            for li in chunk:
                if not isinstance(li, dict):
                    continue
                label = li.get("label") or ""
                if not label:
                    continue
                if self._norm_arch(li.get("arch")) != want_arch:
                    continue
                if commercial_type:
                    compat = li.get("compatible_commercial_types") or []
                    if compat and commercial_type not in compat:
                        continue
                labels.setdefault(label, li)
            if len(chunk) < 100:
                break
            page += 1

        # نام خواناى هر label از کاتالوگ مارکت‌پلیس (`ubuntu_noble` → «Ubuntu 24.04»)
        pretty: dict[str, str] = {}
        try:
            page = 1
            while page <= 5:
                data = await self._request(
                    "GET", "/marketplace/v2/images",
                    params={"include_eol": "false", "page_size": "100",
                            "page": str(page)})
                chunk = data.get("images") or []
                for img in chunk:
                    if isinstance(img, dict) and img.get("label"):
                        pretty[img["label"]] = img.get("name") or img["label"]
                if len(chunk) < 100:
                    break
                page += 1
        except RuntimeError as e:
            logger.info("scaleway: marketplace image names unavailable: %s", e)

        result: list[dict] = []
        for label in labels:
            low = label.lower()
            if any(tok in low for tok in self._OS_SKIP_TOKENS):
                continue
            fam = low.split("_")[0]
            name = pretty.get(label) or label.replace("_", " ").title()
            result.append({
                "id": label,
                "name": name,
                "architecture": want_arch,
                "min_disk": MIN_DISK_GB,
                "_flavor": fam,
                "_ver": _label_version(label, name),
            })

        # خلوت‌سازی: خانواده‌های کامل همه‌ی نسخه‌ها؛ بقیه فقط جدیدترین
        latest: dict = {}
        curated: list[dict] = []
        for o in result:
            if o["_flavor"] in self._OS_FULL_FAMILIES:
                curated.append(o)
            else:
                cur = latest.get(o["_flavor"])
                if cur is None or o["_ver"] > cur["_ver"]:
                    latest[o["_flavor"]] = o
        curated.extend(latest.values())
        curated.sort(key=lambda o: (self._OS_PRIORITY.get(o["_flavor"], 6),
                                    o["_flavor"], -o["_ver"]))
        return curated

    # ── لوکیشن‌ها / health / verify ──────────────────────────────────────────

    async def list_locations(self) -> list[dict]:
        """[{slug, display_name, count}] — zoneهایی که تایپِ قابل‌فروش دارند.

        Scaleway endpointِ «لیست zone»ها ندارد؛ enum ثابت است و تعداد از
        `products/servers` هر zone خوانده می‌شود (zoneهای بی‌پاسخ رد می‌شوند)."""
        out: list[dict] = []
        for slug, label in ZONES.items():
            try:
                plans = await self.list_plans(location=slug)
            except Exception as e:
                # یک zone خراب/کند نباید کل صفحه‌ی ایمپورت را بخواباند
                logger.info("scaleway: zone %s unavailable: %s", slug, e)
                continue
            out.append({"slug": slug, "display_name": label, "count": len(plans)})
        return out

    async def ping(self) -> bool:
        """تست اتصال/توکن سبک — برای health check دوره‌ای."""
        await self._request("GET", self._z("fr-par-1", "/servers"),
                            params={"per_page": "1"})
        return True

    async def verify(self) -> dict:
        """تست زنده هنگام افزودن اکانت: توکن + (در صورت وجود) project_id.
        خروجی: {zones, types, servers} — خطا یعنی credentials نامعتبر."""
        params = {"per_page": "1"}
        if self.project_id:
            params["project"] = self.project_id
        body = await self._request("GET", self._z("fr-par-1", "/servers"),
                                   params=params)
        types = await self.list_plans(location="fr-par-1")
        if not types:
            raise RuntimeError("هیچ نوع سرورِ قابل‌فروشی در fr-par-1 دیده نشد")
        return {
            "zones": len(ZONES),
            "types": len(types),
            "servers": len(body.get("servers") or []),
            "project": self.project_id or "پیش‌فرضِ توکن",
        }


def _label_version(label: str, name: str) -> float:
    """عددِ نسخه برای مرتب‌سازی: از نامِ خوانا (`Ubuntu 24.04 …` → 24.04)؛
    نبود عدد → ۰ (label‌های اسمی مثل `ubuntu_noble` عدد ندارند)."""
    import re
    m = re.search(r"(\d+(?:\.\d+)?)", name or "")
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)", label or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0
