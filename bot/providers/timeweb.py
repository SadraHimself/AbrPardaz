"""Timeweb Cloud API client — https://api.timeweb.cloud

نکات کلیدی (مرجع: TIMEWEB.md):
- احراز: هدر Authorization: Bearer <JWT> (ساخت توکن: پنل ← «API и Terraform»)
- سیستم task/action-id ندارد: create پاسخ 201 با آبجکت کامل server می‌دهد و
  اکشن‌ها 204 خالی — پیشرفت با poll روی server.status دنبال می‌شود.
- رمز root را خود تایم‌وب تولید می‌کند و در فیلد `root_pass` آبجکت سرور
  برمی‌گرداند (تا اتمام نصب null است) — الگوی هتزنر (last_root_password).
  ویندوز: همان فیلد = رمز Administrator.
- reset-password و ریبیلد (PATCH با os_id) و resize (PATCH با preset_id) دارد.
- suspend ندارد → shutdown/start (شارژ با خاموشی قطع نمی‌شود؛ فقط حذف).
- حذف ممکن است به قرنطینه برود؛ اگر «تأیید حذف» روی اکانت روشن باشد حذف API
  کار نمی‌کند (423/فلوی hash+code) — باید در پنل خاموش باشد.
- ارز: روبل (RUB). قیمت تعرفه ماهانه است؛ ساعتی = ÷۷۲۰.
- ترافیک حجمی ندارد (bandwidth = سرعت کانال Mbit) → get_traffic=0.
- rate limit: ۲۰ درخواست/ثانیه به‌ازای هر endpoint؛ خطای 423 = قفل موقت منبع.
- واحدهای ram/disk در API «مگابایت» هستند.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .base import BaseProvider, CreateServerParams, PlanInfo, ServerInfo

logger = logging.getLogger(__name__)

API_BASE = "https://api.timeweb.cloud"

# وضعیت‌های تایم‌وب → وضعیت داخلی ربات (active | off | suspended | building)
_STATUS_MAP = {
    "on": "active", "rebooting": "active", "hard_rebooting": "active",
    "off": "off", "removed": "off", "removing": "off",
    "blocked": "suspended", "no_paid": "suspended", "permanent_blocked": "suspended",
    "installing": "building", "software_install": "building",
    "reinstalling": "building", "turning_on": "building",
    "turning_off": "building", "hard_turning_off": "building",
    "cloning": "building", "transfer": "building", "configuring": "building",
}
_RUNNING = {"on", "rebooting", "hard_rebooting"}

# لیبل انگلیسی لوکیشن‌ها برای مرحله‌ی لوکیشن خرید (region_name در extra پلن)
LOC_LABELS = {
    "ru-1": "Russia (SPb)", "ru-2": "Russia 2", "ru-3": "Russia 3",
    "pl-1": "Poland", "kz-1": "Kazakhstan", "nl-1": "Netherlands",
}

# خانواده‌هایی که همه‌ی نسخه‌هایشان ارائه می‌شود؛ بقیه فقط آخرین نسخه
_OS_FULL_FAMILIES = {"ubuntu", "windows"}
_OS_PRIORITY = {"ubuntu": 0, "debian": 1, "windows": 8}
# پنل‌ها/OSهای لایسنس‌دار که ارائه نمی‌شوند (قیمت‌شان در API نیست)
_OS_EXCLUDED = {"bitrix", "brainycp"}


class TimewebProvider(BaseProvider):
    def __init__(self, api_token: str):
        self.token = (api_token or "").strip()
        # رمز تولیدیِ تایم‌وب در آخرین create/rebuild/reset — رمز دلخواه نمی‌پذیرد
        self.last_root_password: str | None = None

    # ── HTTP core ─────────────────────────────────────────────────────────────

    @staticmethod
    def _friendly_error(status: int, data: dict) -> str:
        code = (data or {}).get("error_code") or str(status)
        msg = (data or {}).get("message") or ""
        if isinstance(msg, (list, tuple)):
            msg = "؛ ".join(str(m) for m in msg)
        return f"Timeweb API {status} {code}: {msg}"[:300]

    async def _request(self, method: str, path: str, json: Optional[dict] = None,
                       params: Optional[dict] = None, timeout: int = 30) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        last_err = "unknown"
        for attempt in range(5):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session:
                    async with session.request(
                        method, f"{API_BASE}{path}",
                        headers=headers, json=json, params=params,
                    ) as resp:
                        if resp.status == 204:
                            return {}
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            data = {}
                        if resp.status < 400:
                            return data if isinstance(data, dict) else {}
                        last_err = self._friendly_error(resp.status, data)
                        # 423 = منبع وسط عملیات قفل است؛ 429 = rate limit —
                        # هر دو گذرا هستند (مثل 5xx) → retry با backoff
                        if resp.status in (423, 429, 500, 502, 503, 504):
                            try:
                                wait_s = float(resp.headers.get("Retry-After") or 0)
                            except (TypeError, ValueError):
                                wait_s = 0
                            await asyncio.sleep(min(max(wait_s, 2 * (attempt + 1)), 30))
                            continue
                        raise RuntimeError(last_err)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = str(e)
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Timeweb API retry limit — {last_err}"[:300])

    async def _get_server_raw(self, server_id: str) -> dict:
        data = await self._request("GET", f"/api/v1/servers/{int(server_id)}")
        return data.get("server") or {}

    async def _wait_status(self, server_id: str, targets: set[str],
                           timeout_s: int = 300, need_root_pass: bool = False) -> dict:
        """Poll وضعیت سرور تا رسیدن به یکی از حالت‌های هدف (بدون task-id).
        هر ۵ ثانیه — rate limit تایم‌وب ۲۰/ثانیه است، خیالمان راحت."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        last: dict = {}
        while asyncio.get_event_loop().time() < deadline:
            try:
                last = await self._get_server_raw(server_id)
            except RuntimeError:
                last = {}
            st = (last.get("status") or "").lower()
            if st in targets and (not need_root_pass or last.get("root_pass")):
                return last
            await asyncio.sleep(5)
        raise RuntimeError("Timeweb: مهلت انتظار عملیات تمام شد")

    # ── Mapping helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_ips(srv: dict) -> tuple[Optional[str], Optional[str]]:
        """IP اصلی از شبکه‌های public (اول is_main، بعد هر IPv4)."""
        ipv4 = ipv6 = None
        for net in srv.get("networks") or []:
            if (net or {}).get("type") != "public":
                continue
            for e in net.get("ips") or []:
                addr = (e or {}).get("ip")
                if not addr:
                    continue
                if (e.get("type") or "") == "ipv6" or ":" in addr:
                    if e.get("is_main") or not ipv6:
                        ipv6 = addr
                else:
                    if e.get("is_main") or not ipv4:
                        ipv4 = addr
        return ipv4, ipv6

    def _server_info(self, srv: dict) -> ServerInfo:
        raw_status = (srv.get("status") or "off").lower()
        status = _STATUS_MAP.get(raw_status, "off")
        ipv4, ipv6 = self._extract_ips(srv)
        os_obj = srv.get("os") or {}
        os_name = " ".join(str(x) for x in (os_obj.get("name"), os_obj.get("version")) if x)
        is_windows = (os_obj.get("name") or "").lower() == "windows"
        disk_mb = 0
        for d in srv.get("disks") or []:
            if d.get("is_system"):
                disk_mb = int(d.get("size") or 0)
                break
        extra = {
            "machine_status": "1" if raw_status in _RUNNING else "0",
            "timeweb_status": raw_status,
            "username": "Administrator" if is_windows else "root",
        }
        if srv.get("root_pass"):
            extra["root_password"] = srv["root_pass"]
        if srv.get("vnc_pass"):
            extra["vnc_pass"] = srv["vnc_pass"]   # برای فاز کنسول
        return ServerInfo(
            provider_server_id=str(srv.get("id")),
            name=srv.get("name") or "",
            status=status,
            ip_address=ipv4,
            ipv6_address=ipv6,
            ram=int(srv.get("ram") or 0),
            cpu=int(srv.get("cpu") or 0),
            disk=disk_mb // 1024,
            bandwidth=0,
            os_name=os_name or None,
            location=srv.get("location"),
            traffic_used_gb=0.0,
            extra_data=extra,
        )

    # ── BaseProvider implementation ───────────────────────────────────────────

    async def create_server(self, params: CreateServerParams) -> ServerInfo:
        if not params.plan_id:
            raise RuntimeError("تعرفه (preset) مشخص نیست")
        if not params.os_id:
            raise RuntimeError("سیستم‌عامل انتخاب نشده است")
        body: dict = {
            "name": params.name,
            "preset_id": int(params.plan_id),
            "os_id": int(params.os_id),
        }
        # پهنای باند تعرفه (از extra پلن) — مطابق رفتار پنل تایم‌وب پاس داده می‌شود
        _mbit = params.extra.get("bandwidth_mbit")
        try:
            if _mbit:
                body["bandwidth"] = max(100, min(1000, int(_mbit)))
        except (TypeError, ValueError):
            pass
        labels = params.extra.get("labels") or {}
        if labels.get("tg_user_id"):
            body["comment"] = f"abrpardaz tg:{labels['tg_user_id']}"
        data = await self._request("POST", "/api/v1/servers", json=body, timeout=60)
        srv = data.get("server") or {}
        server_id = srv.get("id")
        if not server_id:
            raise RuntimeError("تایم‌وب شناسه سرور ساخته‌شده را برنگرداند")

        # IPv4 عمومی از همان ابتدای ساخت درخواست می‌شود — سرور API-ساخته گاهی
        # بدون IPv4 بالا می‌آید (E2E) و IPv6 هم فقط ru-1 دارد؛ زودتر بزنیم که
        # تا پایان نصب فرصت attach داشته باشد
        try:
            _early_v4, _ = await self._ips_endpoint(str(server_id))
            if not _early_v4:
                await self._request(
                    "POST", f"/api/v1/servers/{int(server_id)}/ips",
                    json={"type": "ipv4"})
        except Exception as e:
            logger.warning("timeweb early add-ip for %s: %s", server_id, e)

        # نصب چند دقیقه طول می‌کشد؛ تحویل به IP و رمز (root_pass) نیاز دارد.
        # ساخت تراکنشی: شکست/مهلت → سرور نیمه‌ساخته حذف شود تا بیل نخورد.
        try:
            fresh = await self._wait_status(str(server_id), {"on"},
                                            timeout_s=900, need_root_pass=True)
        except Exception:
            try:
                await self.delete_server(str(server_id))
            except Exception:
                pass
            raise
        self.last_root_password = fresh.get("root_pass")
        info = self._server_info(fresh)
        # E2E 2026-07-24: پاسخ سرور گاهی بدون IP است (networks.ips دیر پر می‌شود
        # یا سرور API-ساخته IPv4 عمومی نگرفته) — تحویل بدون IP بی‌معناست
        if not info.ip_address:
            info = await self._ensure_public_ip(str(server_id), info)
        return info

    async def _bound_floating_ip_ids(self, server_id: int) -> list:
        """شناسه‌ی IPهای عمومیِ متصل به این سرور — Public IP سرویس جداست و
        بعد از حذف سرور نباید یتیم بماند (۱۸۰₽/ماه بی‌سرور!)."""
        ids: list = []
        try:
            data = await self._request("GET", "/api/v1/floating-ips")
            for ip in (data.get("ips") or data.get("floating_ips") or []):
                if not isinstance(ip, dict):
                    continue
                if (ip.get("resource_type") == "server"
                        and str(ip.get("resource_id") or "") == str(server_id)
                        and ip.get("id")):
                    ids.append(ip["id"])
        except Exception as e:
            logger.warning("timeweb floating-ip list for %s: %s", server_id, e)
        return ids

    async def delete_server(self, server_id: str) -> bool:
        sid = int(server_id)
        # قبل از حذف، IPهای متصل را نگه می‌داریم تا بعدش پاک شوند
        fip_ids = await self._bound_floating_ip_ids(sid)
        try:
            data = await self._request("DELETE", f"/api/v1/servers/{sid}", timeout=60)
        except RuntimeError as e:
            if "not_found" in str(e).lower() or " 404 " in f" {e} ":
                await self._cleanup_floating_ips(fip_ids)
                return True  # قبلاً حذف شده
            raise
        # اگر «تأیید حذف» روی اکانت روشن باشد، پاسخ hash می‌دهد و حذف واقعاً
        # انجام نمی‌شود (کد به تلگرام/SMS می‌رود) — با GET تأیید می‌کنیم.
        info = (data or {}).get("server_delete") or {}
        deleted = False
        try:
            srv = await self._get_server_raw(str(sid))
            st = (srv.get("status") or "").lower()
            deleted = (not srv) or st in ("removing", "removed")
        except RuntimeError:
            deleted = True  # 404 = حذف شد
        if deleted:
            await self._cleanup_floating_ips(fip_ids)
            return True
        if info.get("hash"):
            raise RuntimeError(
                "حذف انجام نشد — «تأیید حذف سرویس‌ها» در پنل تایم‌وب فعال است؛ "
                "آن را خاموش کنید"
            )
        return False

    async def _cleanup_floating_ips(self, fip_ids: list) -> None:
        for fid in fip_ids or []:
            try:
                await self._request("DELETE", f"/api/v1/floating-ips/{fid}")
            except Exception as e:
                logger.warning("timeweb orphan floating-ip %s cleanup: %s", fid, e)

    async def _ips_endpoint(self, server_id: str) -> tuple[Optional[str], Optional[str]]:
        """IPها از endpoint اختصاصی /ips — منبع قابل‌اتکاتر از networks آبجکت سرور
        (که ips آن nullable است و گاهی دیر پر می‌شود)."""
        data = await self._request("GET", f"/api/v1/servers/{int(server_id)}/ips")
        ipv4 = ipv6 = None
        for e in data.get("server_ips") or []:
            addr = (e or {}).get("ip")
            if not addr:
                continue
            if (e.get("type") or "") == "ipv6" or ":" in addr:
                if e.get("is_main") or not ipv6:
                    ipv6 = addr
            else:
                if e.get("is_main") or not ipv4:
                    ipv4 = addr
        return ipv4, ipv6

    async def _ensure_public_ip(self, server_id: str, info: ServerInfo) -> ServerInfo:
        """اگر سرور بعد از ساخت IPv4 عمومی نداشت: اول endpoint اختصاصی IPها،
        و اگر واقعاً IP نبود، یک IPv4 اضافه کن و تا ظاهرشدنش poll کن."""
        try:
            ipv4, ipv6 = await self._ips_endpoint(server_id)
            if not ipv4:
                try:
                    await self._request(
                        "POST", f"/api/v1/servers/{int(server_id)}/ips",
                        json={"type": "ipv4"})
                except RuntimeError as e:
                    logger.warning("timeweb add-ip failed for %s: %s", server_id, e)
                deadline = asyncio.get_event_loop().time() + 180
                while asyncio.get_event_loop().time() < deadline and not ipv4:
                    await asyncio.sleep(5)
                    ipv4, ipv6 = await self._ips_endpoint(server_id)
            if ipv4:
                info.ip_address = ipv4
            if ipv6 and not info.ipv6_address:
                info.ipv6_address = ipv6
        except Exception as e:
            logger.warning("timeweb ensure_public_ip(%s): %s", server_id, e)
        return info

    async def get_server(self, server_id: str) -> ServerInfo:
        info = self._server_info(await self._get_server_raw(server_id))
        if not info.ip_address:
            # networks آبجکت سرور IP نداد → از endpoint اختصاصی /ips بخوان
            # (سرورهای موجودِ بدون IP در رکورد هم با همین سینک درست می‌شوند)
            try:
                ipv4, ipv6 = await self._ips_endpoint(server_id)
                if ipv4:
                    info.ip_address = ipv4
                if ipv6 and not info.ipv6_address:
                    info.ipv6_address = ipv6
            except Exception:
                pass
        return info

    async def start_server(self, server_id: str) -> bool:
        await self._request("POST", f"/api/v1/servers/{int(server_id)}/start")
        await self._wait_status(server_id, {"on"}, timeout_s=180)
        return True

    async def stop_server(self, server_id: str) -> bool:
        await self._request("POST", f"/api/v1/servers/{int(server_id)}/shutdown")
        await self._wait_status(server_id, {"off"}, timeout_s=180)
        return True

    async def restart_server(self, server_id: str) -> bool:
        await self._request("POST", f"/api/v1/servers/{int(server_id)}/reboot")
        await self._wait_status(server_id, {"on"}, timeout_s=240)
        return True

    async def rebuild_server(self, server_id: str, os_id: str, rootpass: str = "") -> bool:
        """نصب مجدد OS — PATCH با os_id (وضعیت reinstalling). رمز دلخواه
        پذیرفته نمی‌شود؛ رمز جدیدِ تولیدی تایم‌وب بعد از نصب از root_pass
        خوانده و در last_root_password گذاشته می‌شود."""
        old = (await self._get_server_raw(server_id)).get("root_pass")
        await self._request("PATCH", f"/api/v1/servers/{int(server_id)}",
                            json={"os_id": int(os_id)}, timeout=60)
        fresh = await self._wait_status(server_id, {"on"},
                                        timeout_s=900, need_root_pass=True)
        new_pass = fresh.get("root_pass")
        # اگر رمز همان قبلی ماند (اسپک ساکت است)، همان را تحویل می‌دهیم — معتبر است
        self.last_root_password = new_pass or old
        return True

    async def suspend_server(self, server_id: str) -> bool:
        # تایم‌وب suspend ندارد → خاموش‌کردن (توجه: بیل ادامه دارد؛ قطع فقط با حذف)
        return await self.stop_server(server_id)

    async def unsuspend_server(self, server_id: str) -> bool:
        return await self.start_server(server_id)

    async def get_traffic(self, server_id: str) -> float:
        # ترافیک حجمی گزارش/محدود نمی‌شود (bandwidth = سرعت کانال) → همیشه صفر
        return 0.0

    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        """تعرفه‌ها (presets) با «قیمت خرید» به روبل — قیمت ماهانه؛ ساعتی = ÷۷۲۰."""
        data = await self._request("GET", "/api/v1/presets/servers")
        plans: list[PlanInfo] = []
        for p in data.get("server_presets") or []:
            loc = p.get("location") or ""
            if location and loc != location:
                continue
            ram_mb = int(p.get("ram") or 0)
            disk_gb = int(p.get("disk") or 0) // 1024
            monthly = float(p.get("price") or 0)
            plans.append(PlanInfo(
                provider_plan_id=str(p.get("id")),
                name=f"tw-{p.get('id')} — {p.get('cpu')}c/"
                     f"{ram_mb // 1024 if ram_mb >= 1024 else ram_mb}"
                     f"{'G' if ram_mb >= 1024 else 'M'}/{disk_gb}G "
                     f"{(p.get('disk_type') or '').upper()}",
                ram=ram_mb,
                cpu=int(p.get("cpu") or 0),
                disk=disk_gb,
                bandwidth=0,   # سرعت کانال (Mbit) سقف حجم نیست → نامحدود
                price_hourly=round(monthly / 720.0, 6),
                price_monthly=monthly,
                location=loc,
                currency="rub",
            ))
        return plans

    async def preset_details(self, preset_id: str) -> Optional[dict]:
        """جزئیات خام یک تعرفه (برای ℹ️ پنل ادمین)."""
        data = await self._request("GET", "/api/v1/presets/servers")
        for p in data.get("server_presets") or []:
            if str(p.get("id")) == str(preset_id):
                return p
        return None

    async def list_os_templates(self, location: Optional[str] = None) -> list[dict]:
        """OSها (سراسری — per-region نیست). خلوت‌سازی مثل جیکور: ubuntu و
        windows همه‌ی نسخه‌ها؛ بقیه فقط جدیدترین. bitrix/brainycp (لایسنس‌دار
        بدون قیمت در API) حذف. min_disk به GB برای گِیت دیسک پلن در فلوی خرید
        (واحد requirements در اسپک مبهم است — MB فرض شده)."""
        data = await self._request("GET", "/api/v1/os/servers")
        result = []
        for os_ in data.get("servers_os") or []:
            name = (os_.get("name") or "").lower()
            if name in _OS_EXCLUDED:
                continue
            version = str(os_.get("version") or "").strip()
            try:
                ver = float(version.split("-")[0].split()[0] or 0)
            except (ValueError, IndexError):
                ver = 0.0
            req = os_.get("requirements") or {}
            disk_min_mb = int(req.get("disk_min") or 0)
            result.append({
                "id": str(os_.get("id")),
                "name": f"{os_.get('name')}-{version}" if version else str(os_.get("name")),
                "min_disk": (disk_min_mb + 1023) // 1024 if disk_min_mb else 0,
                "price_per_hour": 0,
                "_flavor": name,
                "_ver": ver,
            })
        latest: dict = {}
        curated: list[dict] = []
        for o in result:
            if o["_flavor"] in _OS_FULL_FAMILIES:
                curated.append(o)
            else:
                cur = latest.get(o["_flavor"])
                if cur is None or o["_ver"] > cur["_ver"]:
                    latest[o["_flavor"]] = o
        curated.extend(latest.values())
        curated.sort(key=lambda o: (_OS_PRIORITY.get(o["_flavor"], 5),
                                    o["_flavor"], -o["_ver"]))
        return curated

    # ── Optional overrides / Extras ───────────────────────────────────────────

    async def change_root_password(self, server_id: str, new_password: str) -> bool:
        """تایم‌وب رمز دلخواه نمی‌پذیرد — reset می‌کنیم؛ رمز جدیدِ تولیدی از
        root_pass خوانده و در last_root_password گذاشته می‌شود."""
        old = (await self._get_server_raw(server_id)).get("root_pass")
        await self._request("POST", f"/api/v1/servers/{int(server_id)}/reset-password")
        deadline = asyncio.get_event_loop().time() + 180
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(5)
            srv = await self._get_server_raw(server_id)
            np_ = srv.get("root_pass")
            if np_ and np_ != old:
                self.last_root_password = np_
                return True
        raise RuntimeError("تایم‌وب رمز جدید را برنگرداند — کمی بعد دوباره تلاش کنید")

    async def list_locations(self) -> list[dict]:
        """لوکیشن‌هایی که تعرفه دارند (از خود presets — v2/locations همه‌ی
        محصولات را می‌دهد نه فقط سرور ابری)."""
        data = await self._request("GET", "/api/v1/presets/servers")
        locs = sorted({p.get("location") for p in data.get("server_presets") or []
                       if p.get("location")})
        return [{"id": l, "slug": l, "display_name": LOC_LABELS.get(l, l)}
                for l in locs]

    async def count_servers(self) -> int:
        data = await self._request("GET", "/api/v1/servers",
                                   params={"limit": "1", "offset": "0"})
        return int(((data.get("meta") or {}).get("total")) or 0)

    async def ping(self) -> bool:
        await self._request("GET", "/api/v1/account/status")
        return True

    async def verify(self) -> dict:
        """تست زنده هنگام افزودن اکانت: توکن + وضعیت اکانت + موجودی + تعرفه‌ها."""
        st = (await self._request("GET", "/api/v1/account/status")).get("status") or {}
        if st.get("is_blocked") or st.get("is_permanent_blocked"):
            raise RuntimeError("اکانت تایم‌وب مسدود است")
        fin = (await self._request("GET", "/api/v1/account/finances")).get("finances") or {}
        presets = await self.list_plans()
        return {
            "balance": float(fin.get("balance") or 0),
            "currency": fin.get("currency") or "RUB",
            "presets": len(presets),
            "locations": len({p.location for p in presets}),
        }
