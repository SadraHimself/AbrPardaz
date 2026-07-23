"""Gcore Edge Cloud API client — https://api.gcore.com

نکات کلیدی (مرجع: GCORE.md):
- احراز هویت: هدر Authorization: APIKey <token>  (نه Bearer!)
- همه عملیات نوشتنی async هستند: پاسخ {"tasks": [id]} → poll هر ۵ ثانیه تا FINISHED/ERROR
- مسیرها project_id و region_id عددی می‌خواهند → provider_server_id = "{region_id}:{uuid}"
  تا هر متد BaseProvider بدون اطلاعات اضافه، region را از خودِ ID دربیاورد.
- flavor فقط vCPU/RAM دارد؛ دیسک = volume جدا. volume با delete_on_termination=true
  ساخته می‌شود + هنگام حذف صریحاً در query «volumes=» می‌آید (دو-قفله؛ volume یتیم
  «از ساخت تا حذف» شارژ می‌شود!)
- rebuild و change-password برای VM در API وجود ندارند (فقط bare metal rebuild دارد).
- ترافیک VM رایگان/نامحدود است و usage تجمعی گزارش نمی‌شود → get_traffic = 0.
- suspend داخلی ربات = stop/start: طبق داکس شارژ VM با stop کامل قطع می‌شود؛
  وضعیت شارژِ suspend واقعی جیکور در داکس روشن نیست (تصمیم پروژه 2026-07-21).
- رمز root را جیکور تولید/برنمی‌گرداند — ربات رمز می‌سازد و هنگام ساخت می‌فرستد.
- ForbiddenError = «ظرفیت پر»، نه خطای دسترسی. نسخه‌ها قاطی‌اند: create/action روی
  v2، بقیه v1 (اشتباه → HTTP 405).
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Optional

import aiohttp

from .base import BaseProvider, CreateServerParams, PlanInfo, ServerInfo

logger = logging.getLogger(__name__)

API_BASE = "https://api.gcore.com"

# وضعیت‌های جیکور → وضعیت داخلی ربات (active | off | suspended | building)
_STATUS_MAP = {
    "ACTIVE": "active", "REBOOT": "active", "HARD_REBOOT": "active",
    "MIGRATING": "active", "PASSWORD": "active", "RESCUE": "active",
    "SHUTOFF": "off", "ERROR": "off", "DELETED": "off", "SOFT_DELETED": "off",
    "SHELVED": "off", "SHELVED_OFFLOADED": "off", "UNKNOWN": "off",
    "PAUSED": "suspended", "SUSPENDED": "suspended",
    "BUILD": "building", "REBUILD": "building", "RESIZE": "building",
    "VERIFY_RESIZE": "building", "REVERT_RESIZE": "building",
}
# وضعیت‌هایی که ماشین واقعاً روشن است (برای دات 🟢/🔴 کیبورد)
_RUNNING = {"ACTIVE", "REBOOT", "HARD_REBOOT", "MIGRATING", "RESCUE", "PASSWORD"}


class GcoreProvider(BaseProvider):
    def __init__(self, api_token: str, project_id: int | str = 0):
        self.token = (api_token or "").strip()
        try:
            self.project_id = int(project_id or 0)
        except (TypeError, ValueError):
            self.project_id = 0
        # رمزی که خود ربات تولید و موقع ساخت به جیکور می‌دهد — لایه‌ی سرویس
        # همین را به کاربر تحویل می‌دهد (جیکور هیچ رمزی برنمی‌گرداند)
        self.last_root_password: str | None = None

    # ── HTTP core ─────────────────────────────────────────────────────────────

    @staticmethod
    def _friendly_error(status: int, data: dict) -> str:
        exc = (data or {}).get("exception_class") or ""
        msg = (data or {}).get("message") or ""
        low = msg.lower()
        # ForbiddenError با این متن = ظرفیت فیزیکی flavor پر است (نه دسترسی)
        if exc == "ForbiddenError" and "reached the limit" in low:
            return "ظرفیت این پلن در این لوکیشن موقتاً تکمیل است — کمی بعد دوباره تلاش کنید"
        if exc == "QuotaLimitExceed":
            return "سقف منابع اکانت سرویس‌دهنده پر شده است — با پشتیبانی تماس بگیرید"
        return f"Gcore API {status} {exc}: {msg}"[:300]

    async def _request(self, method: str, path: str, json: Optional[dict] = None,
                       params: Optional[dict] = None, timeout: int = 30) -> dict:
        headers = {"Authorization": f"APIKey {self.token}"}
        last_err = "unknown"
        for attempt in range(4):
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
                            data = await resp.json()
                        except Exception:
                            data = {}
                        if resp.status < 400:
                            return data if isinstance(data, dict) else {"results": data}
                        last_err = self._friendly_error(resp.status, data)
                        # گذرا: 429 (با رعایت Retry-After) و 5xx → retry با backoff
                        if resp.status == 429:
                            try:
                                wait_s = float(resp.headers.get("Retry-After") or 0)
                            except (TypeError, ValueError):
                                wait_s = 0
                            await asyncio.sleep(min(max(wait_s, 2 * (attempt + 1)), 30))
                            continue
                        if resp.status in (500, 502, 503, 504):
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        raise RuntimeError(last_err)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = str(e)
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Gcore API retry limit — {last_err}"[:300])

    async def _wait_task(self, task_id: str, timeout_s: int = 300) -> dict:
        """Polling یک task تا FINISHED/ERROR — هر ۵ ثانیه (طبق GCORE.md)."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            data = await self._request("GET", f"/cloud/v1/tasks/{task_id}")
            state = data.get("state")
            if state == "FINISHED":
                return data
            if state == "ERROR":
                err = data.get("error") or "unknown task error"
                raise RuntimeError(f"Gcore task failed — {str(err)[:200]}")
            await asyncio.sleep(5)
        raise RuntimeError("Gcore task timeout")

    async def _wait_first_task(self, data: dict, timeout_s: int = 300) -> dict:
        tasks = data.get("tasks") or []
        if not tasks:
            return {}
        return await self._wait_task(tasks[0], timeout_s=timeout_s)

    @staticmethod
    def _split_sid(server_id: str) -> tuple[int, str]:
        """provider_server_id = "{region_id}:{instance_uuid}" — خودکفا برای همه متدها."""
        rid, _, uuid = (server_id or "").partition(":")
        if not uuid:
            raise RuntimeError(f"شناسه سرور جیکور نامعتبر است: {server_id}")
        return int(rid), uuid

    # ── Mapping helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_ips(inst: dict) -> tuple[Optional[str], Optional[str]]:
        """کلیدهای addresses بسته به region فرق می‌کنند (اغلب pub_net) —
        روی همه‌ی شبکه‌ها iterate می‌کنیم، نه فقط pub_net (GCORE.md دام ۳)."""
        ipv4 = ipv6 = None
        for entries in (inst.get("addresses") or {}).values():
            for e in entries or []:
                addr = (e or {}).get("addr")
                if not addr:
                    continue
                if ":" in addr:
                    ipv6 = ipv6 or addr
                else:
                    ipv4 = ipv4 or addr
        return ipv4, ipv6

    @staticmethod
    def _windows_password(seed: Optional[str] = None) -> str:
        """رمز مطابق سیاست ویندوز جیکور: ۸–۱۶ کاراکتر با حداقل یک حرف کوچک،
        یک حرف بزرگ، یک رقم و یک کاراکتر خاص. ورودی سازگار همان برمی‌گردد."""
        import secrets as _sec
        import string as _str
        specials = "!#$%&'()*+,-./:;<=>?@[]^_{|}~"

        def _ok(p: str) -> bool:
            return bool(p) and 8 <= len(p) <= 16 \
                and any(c.islower() for c in p) and any(c.isupper() for c in p) \
                and any(c.isdigit() for c in p) and any(c in specials for c in p)

        if seed and _ok(seed):
            return seed
        while True:
            p = "".join(_sec.choice(_str.ascii_letters + _str.digits + "!#$%*+-=?@")
                        for _ in range(14))
            if _ok(p):
                return p

    def _server_info(self, inst: dict, region_id: int,
                     root_password: Optional[str] = None,
                     os_name: Optional[str] = None) -> ServerInfo:
        raw_status = (inst.get("status") or "UNKNOWN").upper()
        status = _STATUS_MAP.get(raw_status, "off")
        ipv4, ipv6 = self._extract_ips(inst)
        flavor = inst.get("flavor") or {}
        extra = {
            "machine_status": "1" if raw_status in _RUNNING else "0",
            "gcore_status": raw_status,
            "gcore_task_state": inst.get("task_state"),
        }
        if root_password:
            extra["root_password"] = root_password
        return ServerInfo(
            provider_server_id=f"{region_id}:{inst.get('id')}",
            name=inst.get("name") or "",
            status=status,
            ip_address=ipv4,
            ipv6_address=ipv6,
            ram=int(flavor.get("ram") or 0),
            cpu=int(flavor.get("vcpus") or 0),
            disk=0,   # دیسک در پاسخ instance نیست (volume جدا) — رکورد Server از پلن پر می‌شود
            bandwidth=0,
            os_name=os_name,
            location=str(inst.get("region") or region_id),
            traffic_used_gb=0.0,
            extra_data=extra,
        )

    # ── BaseProvider implementation ───────────────────────────────────────────

    async def _find_instance_by_name(self, region_id: int, name: str) -> Optional[dict]:
        """جستجوی instance با نام دقیق (فیلتر name تطبیق جزئی است → خودمان دقیق می‌کنیم)."""
        try:
            data = await self._request(
                "GET", f"/cloud/v1/instances/{self.project_id}/{region_id}",
                params={"name": name, "limit": "50"},
            )
        except Exception:
            return None
        for inst in data.get("results") or []:
            if inst.get("name") == name:
                return inst
        return None

    async def create_server(self, params: CreateServerParams) -> ServerInfo:
        region_id = int(params.extra.get("region_id") or 0)
        if not region_id:
            raise RuntimeError("region_id جیکور در مشخصات پلن نیست — پلن را دوباره ایمپورت کنید")
        if not params.os_id:
            raise RuntimeError("سیستم‌عامل انتخاب نشده است")

        disk_gb = int(params.extra.get("disk") or 0) or 5
        volume_type = str(params.extra.get("volume_type") or "standard")

        # رمز root: ورودی ربات (جیکور رمزی تولید نمی‌کند). fallback: تولید داخلی.
        password = params.extra.get("root_password")
        if not password:
            import secrets as _sec, string as _str
            _alpha = _str.ascii_letters + _str.digits + "!@#$%^&*"
            password = "".join(_sec.choice(_alpha) for _ in range(16))

        # تشخیص خانواده‌ی OS از روی خود image — مسیر تعیین رمز به آن وابسته است
        # (ویندوز user_data ندارد) + os_name برای رکورد سرور
        image_name: Optional[str] = None
        image_distro = ""
        is_windows = False
        try:
            imgs = await self._request(
                "GET", f"/cloud/v1/images/{self.project_id}/{region_id}",
                params={"visibility": "public"},
            )
            img = next((i for i in imgs.get("results") or []
                        if i.get("id") == params.os_id), None)
            if img:
                image_name = img.get("name")
                image_distro = (img.get("os_distro") or "").lower()
                _d = f"{image_distro} {img.get('name') or ''}".lower()
                is_windows = "windows" in _d
        except Exception:
            pass  # ناموفق = فرض لینوکس (اکثریت مطلق)

        # الگوی نام جیکور حداقل ۳ کاراکتر می‌خواهد — نام‌های خیلی کوتاه پسوند می‌گیرند
        _name = params.name if len(params.name or "") >= 3 else f"{params.name or 'srv'}-vm"
        body: dict = {
            "name": _name,
            "flavor": params.plan_id,
            "interfaces": [{"type": "external", "ip_family": "ipv4"}],
            "volumes": [{
                "source": "image",
                "image_id": params.os_id,
                "size": disk_gb,
                "boot_index": 0,
                "type_name": volume_type,
                # حذف خودکار volume با حذف سرور — وگرنه volume یتیم شارژ ابدی دارد
                "delete_on_termination": True,
                "name": f"boot-{_name}",
            }],
            "tags": {"managed_by": "abrpardaz",
                     **{k: str(v) for k, v in (params.extra.get("labels") or {}).items()}},
        }
        # مسیر تعیین رمز — نتیجه‌ی E2E واقعی 2026-07-22: فیلد password رمز root
        # را ست نمی‌کند (رمزِ «کاربر پیش‌فرض image» + SSH پسوردی/ورود root بسته)
        # → تحویل «root + رمز» فقط از مسیر cloud-init جواب می‌دهد؛ پس پیش‌فرض شد.
        # توجه: با ارسال فیلد password، جیکور user_data را نادیده می‌گیرد — فقط یکی!
        if is_windows:
            # ویندوز: user_data کار نمی‌کند؛ فیلد password رمز کاربر Admin را ست
            # می‌کند و باید با سیاست رمز جیکور (۸-۱۶ + هر ۴ دسته) سازگار باشد
            password = self._windows_password(password)
            body["password"] = password
        elif params.extra.get("gcore_password_field"):
            # سوییچ عیب‌یابی (extra_config اکانت): رفتار قدیمی فیلد password
            body["password"] = password
        else:
            # لینوکس (پیش‌فرض): cloud-init — ست صریح رمز root + بازکردن SSH پسوردی
            # (ssh_pwauth کاربر را باز می‌کند ولی PermitRootLogin هم لازم است)
            cloud_cfg = (
                "#cloud-config\n"
                "disable_root: false\n"
                "ssh_pwauth: true\n"
                "chpasswd:\n"
                "  expire: false\n"
                "  list: |\n"
                f"    root:{password}\n"
                "runcmd:\n"
                "  - mkdir -p /etc/ssh/sshd_config.d\n"
                "  - printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/99-rootpass.conf\n"
                "  - sed -i 's/^#\\?PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config\n"
                "  - sed -i 's/^#\\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config\n"
                "  - systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true\n"
            )
            body["user_data"] = base64.b64encode(cloud_cfg.encode()).decode()
        self.last_root_password = password

        # یوزرنیمی که رمز واقعاً رویش می‌نشیند — برای پیام تحویل
        if is_windows:
            delivered_user = "Admin"
        elif params.extra.get("gcore_password_field"):
            delivered_user = image_distro or "root"   # کاربر پیش‌فرض image
        else:
            delivered_user = "root"                    # مسیر cloud-init

        async def _do_create(create_body: dict) -> dict:
            return await self._request(
                "POST", f"/cloud/v2/instances/{self.project_id}/{region_id}",
                json=create_body, timeout=60,
            )

        try:
            data = await _do_create(body)
        except RuntimeError as e:
            # 409 ConflictError = منبع هم‌نام موجود → یک retry با پسوند تصادفی
            if "conflict" in str(e).lower():
                import secrets as _sec
                body["name"] = f"{params.name}-{_sec.token_hex(2)}"
                body["volumes"][0]["name"] = f"boot-{body['name']}"
                data = await _do_create(body)
            else:
                raise

        instance_id: Optional[str] = None
        try:
            task = await self._wait_first_task(data, timeout_s=300)
            created = (task.get("created_resources") or {}).get("instances") or []
            instance_id = created[0] if created else None
            if not instance_id:
                inst = await self._find_instance_by_name(region_id, body["name"])
                instance_id = (inst or {}).get("id")
            if not instance_id:
                raise RuntimeError("جیکور شناسه سرور ساخته‌شده را برنگرداند")
        except Exception:
            # ساخت تراکنشی: شکست وسط راه → سرور نیمه‌ساخته پاک شود تا بیل نخورد
            try:
                cleanup_id = instance_id
                if not cleanup_id:
                    inst = await self._find_instance_by_name(region_id, body["name"])
                    cleanup_id = (inst or {}).get("id")
                if cleanup_id:
                    await self.delete_server(f"{region_id}:{cleanup_id}")
            except Exception:
                pass
            raise

        # IP فقط بعد از ACTIVE در addresses ظاهر می‌شود → چند بار get تا IP قطعی
        inst: dict = {}
        for _ in range(12):
            try:
                inst = (await self._request(
                    "GET",
                    f"/cloud/v1/instances/{self.project_id}/{region_id}/{instance_id}",
                )) or {}
            except Exception:
                inst = {}
            ipv4, _ = self._extract_ips(inst)
            if ipv4 and (inst.get("status") or "").upper() not in ("BUILD",):
                break
            await asyncio.sleep(5)
        if not inst:
            inst = {"id": instance_id, "status": "BUILD", "name": body["name"]}
        info = self._server_info(inst, region_id, root_password=password,
                                 os_name=image_name)
        info.extra_data["username"] = delivered_user
        return info

    async def delete_server(self, server_id: str) -> bool:
        region_id, uuid = self._split_sid(server_id)
        # قطع کامل هزینه = حذف instance + volumeها؛ volume بوت delete_on_termination
        # دارد ولی برای اطمینان، IDهای attach‌شده صریحاً هم در query می‌آیند.
        vol_ids = ""
        try:
            inst = await self._request(
                "GET", f"/cloud/v1/instances/{self.project_id}/{region_id}/{uuid}")
            vol_ids = ",".join(
                v.get("id") for v in (inst.get("volumes") or []) if v.get("id"))
        except RuntimeError as e:
            if "notfound" in str(e).lower().replace(" ", ""):
                return True  # قبلاً حذف شده
        params = {"delete_floatings": "true"}
        if vol_ids:
            params["volumes"] = vol_ids
        try:
            data = await self._request(
                "DELETE", f"/cloud/v1/instances/{self.project_id}/{region_id}/{uuid}",
                params=params, timeout=60,
            )
        except RuntimeError as e:
            if "notfound" in str(e).lower().replace(" ", ""):
                return True
            raise
        try:
            await self._wait_first_task(data, timeout_s=180)
        except Exception as e:
            # حذف در پس‌زمینه ادامه دارد؛ تأیید نهایی با get
            logger.warning("gcore delete wait: %s", e)
            try:
                await self._request(
                    "GET", f"/cloud/v1/instances/{self.project_id}/{region_id}/{uuid}")
                return False
            except RuntimeError:
                return True
        return True

    async def get_server(self, server_id: str) -> ServerInfo:
        region_id, uuid = self._split_sid(server_id)
        inst = await self._request(
            "GET", f"/cloud/v1/instances/{self.project_id}/{region_id}/{uuid}")
        return self._server_info(inst, region_id)

    async def _action(self, server_id: str, action: str, timeout_s: int = 180) -> bool:
        region_id, uuid = self._split_sid(server_id)
        data = await self._request(
            "POST", f"/cloud/v2/instances/{self.project_id}/{region_id}/{uuid}/action",
            json={"action": action},
        )
        await self._wait_first_task(data, timeout_s=timeout_s)
        return True

    async def start_server(self, server_id: str) -> bool:
        return await self._action(server_id, "start")

    async def stop_server(self, server_id: str) -> bool:
        return await self._action(server_id, "stop")

    async def restart_server(self, server_id: str) -> bool:
        return await self._action(server_id, "reboot")

    async def rebuild_server(self, server_id: str, os_id: str, rootpass: str = "") -> bool:
        # جیکور برای VM endpoint ریبیلد ندارد (فقط bare metal) — GCORE.md بخش ز.
        # تصمیم پروژه: در نسخه اول پشتیبانی نمی‌شود؛ دکمه در UI گارد شده است.
        raise NotImplementedError("این سرویس‌دهنده نصب مجدد سیستم‌عامل را پشتیبانی نمی‌کند")

    async def suspend_server(self, server_id: str) -> bool:
        # تصمیم پروژه: stop به‌جای suspend واقعی — طبق داکس، شارژ خود VM با stop
        # کامل قطع می‌شود (volume همچنان شارژ دارد ولی ناچیز است)؛ وضعیت شارژ
        # suspend واقعی جیکور در داکس مشخص نیست.
        return await self._action(server_id, "stop")

    async def unsuspend_server(self, server_id: str) -> bool:
        return await self._action(server_id, "start")

    async def get_traffic(self, server_id: str) -> float:
        # جیکور ترافیک تجمعی گزارش نمی‌دهد (metrics فقط نرخ لحظه‌ای Bps است) و
        # ترافیک VM رایگان/نامحدود است → همیشه صفر؛ سینک/هشدار ۸۰٪ عملاً غیرفعال.
        return 0.0

    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        """پلن‌ها (flavors) با «قیمت خرید» به ارز اکانت (currency_code هر flavor).

        قرارداد داخلی جیکور: location = str(region_id) عددی.
        توجه: قیمت flavor فقط vCPU/RAM است — هزینه‌ی volume بوت را لایه‌ی
        gcore_settings هنگام ایمپورت/سینک اضافه می‌کند (در API نیست).
        """
        if not location:
            raise RuntimeError("برای جیکور، لیست پلن‌ها per-region است — region بدهید")
        region_id = int(location)
        data = await self._request(
            "GET", f"/cloud/v1/flavors/{self.project_id}/{region_id}",
            params={"include_prices": "true", "exclude_windows": "true"},
        )
        plans: list[PlanInfo] = []
        for f in data.get("results") or []:
            if f.get("disabled"):
                continue
            if (f.get("os_type") or "linux") != "linux":
                continue  # نسخه اول: فقط لینوکس
            if (f.get("architecture") or "x86_64") != "x86_64":
                continue
            if (f.get("price_status") or "show") != "show":
                continue  # hide/error → قیمت قابل اتکا نیست
            ram_mb = int(f.get("ram") or 0)
            cpu = int(f.get("vcpus") or 0)
            plans.append(PlanInfo(
                provider_plan_id=f.get("flavor_id") or f.get("flavor_name") or "",
                name=f"{f.get('flavor_id')} — {cpu}c/{ram_mb // 1024 if ram_mb >= 1024 else ram_mb}"
                     f"{'G' if ram_mb >= 1024 else 'M'}",
                ram=ram_mb,
                cpu=cpu,
                disk=0,        # دیسک پلن را لایه‌ی ایمپورت تعیین می‌کند (volume جدا)
                bandwidth=0,   # ترافیک جیکور نامحدود است
                price_hourly=float(f.get("price_per_hour") or 0),
                price_monthly=float(f.get("price_per_month") or 0),
                location=str(region_id),
                currency=(f.get("currency_code") or "USD").lower(),
            ))
        return plans

    # خانواده‌هایی که همه‌ی نسخه‌هایشان ارائه می‌شود؛ بقیه فقط آخرین نسخه
    # (تصمیم 2026-07-22 — لیست OS خلوت و کاربردی بماند)
    _OS_FULL_FAMILIES = {"ubuntu", "windows"}
    # ترتیب نمایش: اوبونتو اول، دبیان دوم، بقیه وسط، ویندوز آخر
    _OS_PRIORITY = {"ubuntu": 0, "debian": 1, "windows": 8}

    async def list_os_templates(self, location: Optional[str] = None) -> list[dict]:
        """ایمیج‌های عمومی x86_64 یک region — id همان image_id (uuid) است که
        create قبول می‌کند. location = str(region_id) (ایمیج‌ها per-region اند).

        سیاست خلوت‌سازی: ubuntu و windows همه‌ی نسخه‌ها؛ سایر توزیع‌ها فقط
        جدیدترین نسخه. ⚠️ ویندوز min_disk بزرگ دارد و لایسنسش سمت جیکور جدا
        شارژ می‌شود — فیلتر min_disk نسبت به دیسک پلن در لایه‌ی فلوی خرید است."""
        if not location:
            raise RuntimeError("برای جیکور، لیست OS per-region است — region بدهید")
        region_id = int(location)
        data = await self._request(
            "GET", f"/cloud/v1/images/{self.project_id}/{region_id}",
            params={"visibility": "public", "include_prices": "true"},
        )
        result = []
        for img in data.get("results") or []:
            distro = (img.get("os_distro") or "").lower()
            name = img.get("name") or ""
            if (img.get("architecture") or "x86_64") != "x86_64":
                continue
            if (img.get("status") or "active") != "active":
                continue
            try:
                ver = float(str(img.get("os_version") or "0").split("-")[0])
            except ValueError:
                ver = 0.0
            fam = distro or (name.split("-")[0].lower() if name else "other")
            result.append({
                "id": img.get("id"),
                "name": name,
                "min_disk": int(img.get("min_disk") or 0),
                # قیمت خود image = لایسنس (ویندوز)؛ لینوکس 0 — مبنای سورشارژ per-OS
                "price_per_hour": float(img.get("price_per_hour") or 0),
                "architecture": img.get("architecture", "x86_64"),
                "_flavor": fam,
                "_ver": ver,
            })
        # خلوت‌سازی: خانواده‌های کامل → همه نسخه‌ها؛ بقیه → فقط جدیدترین
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
        # بلوکی: اولویت خانواده، بعد الفبا، داخل خانواده نسخه جدیدتر اول
        curated.sort(key=lambda o: (self._OS_PRIORITY.get(o["_flavor"], 5),
                                    o["_flavor"], -o["_ver"]))
        return curated

    # ── Extras (خارج از BaseProvider) ────────────────────────────────────────

    async def list_regions(self) -> list[dict]:
        """regionهایی که Cloud VM (KVM اختصاصی) دارند — برای ایمپورت و مرحله لوکیشن.

        تمرکز فروش روی Cloud VM است نه Basic VM (تصمیم پروژه 2026-07-21) →
        regionهایی که فقط Basic VM دارند (has_kvm=false) لیست نمی‌شوند."""
        data = await self._request(
            "GET", "/cloud/v1/regions", params={"limit": "200"})
        out = []
        for r in data.get("results") or []:
            if not r.get("has_kvm"):
                continue
            out.append({
                "id": int(r.get("id")),
                "display_name": r.get("display_name") or "",
                "slug": r.get("slug") or str(r.get("id")),
                "country": r.get("country") or "",
                "zone": r.get("zone") or "",
            })
        out.sort(key=lambda x: x["display_name"])
        return out

    async def preview_volume_price(self, region_id: int, size_gb: int,
                                   type_name: str = "standard") -> dict:
        """قیمت زنده‌ی یک volume از endpoint رسمی pricing preview جیکور.
        (این endpoint در GCORE.md نیامده بود — از داکس آنلاین: 2026-07-24.)
        خروجی: {price_per_hour, price_per_month, currency}."""
        data = await self._request(
            "POST", f"/cloud/v1/pricing/{self.project_id}/{region_id}/volumes",
            json={"source": "new-volume", "size": int(size_gb),
                  "type_name": type_name},
        )
        return {
            "price_per_hour": float(data.get("price_per_hour") or 0),
            "price_per_month": float(data.get("price_per_month") or 0),
            "currency": (data.get("currency_code") or "USD").lower(),
        }

    async def client_info(self) -> dict:
        """اطلاعات اکانت (id برای quota + email برای نمایش) — تست توکن."""
        return await self._request("GET", "/iam/clients/me")

    async def ping(self) -> bool:
        """تست اتصال/توکن سبک — برای health check دوره‌ای."""
        await self._request("GET", "/cloud/v1/regions", params={"limit": "1"})
        return True

    async def verify(self) -> dict:
        """تست زنده‌ی کامل هنگام افزودن اکانت: توکن + project_id.
        خروجی: {email, client_id, regions} — خطا یعنی credentials نامعتبر."""
        me = await self.client_info()
        regions = await self.list_regions()
        if not regions:
            raise RuntimeError("هیچ region دارای VM برای این اکانت دیده نشد")
        # اعتبار project_id فقط با یک فراخوانی project-دار معلوم می‌شود
        await self._request(
            "GET", f"/cloud/v1/flavors/{self.project_id}/{regions[0]['id']}",
            params={"limit": "1"},
        )
        return {
            "email": me.get("email") or "",
            "client_id": me.get("id"),
            "regions": len(regions),
        }
