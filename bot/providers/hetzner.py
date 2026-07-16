"""Hetzner Cloud API client — https://api.hetzner.cloud/v1

نکات کلیدی (مرجع: HETZNER.md):
- احراز هویت: هدر Authorization: Bearer <token>  (توکن per-Project)
- توقف هزینه فقط با DELETE — خاموش‌کردن سرور بیل را قطع نمی‌کند
- هر تغییر یک Action برمی‌گرداند که باید تا success/error دنبال شود
- rate limit پیش‌فرض ۳۶۰۰ درخواست/ساعت/پروژه → polling با فاصله + retry با backoff
- قیمت‌ها per-location و شامل net/gross هستند؛ برای فروش gross مبناست (EUR)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .base import BaseProvider, CreateServerParams, PlanInfo, ServerInfo

logger = logging.getLogger(__name__)

API_BASE = "https://api.hetzner.cloud/v1"

# وضعیت‌های هتزنر → وضعیت داخلی ربات
_STATUS_MAP = {
    "running": "active",
    "off": "off",
    "initializing": "building",
    "starting": "building",
    "stopping": "building",
    "deleting": "building",
    "migrating": "building",
    "rebuilding": "building",
    "unknown": "off",
}

_GB = 1024 ** 3


class HetznerProvider(BaseProvider):
    def __init__(self, api_token: str):
        self.token = (api_token or "").strip()
        # رمز تولیدیِ هتزنر در آخرین rebuild/reset — چون رمز دلخواه نمی‌پذیرد
        self.last_root_password: str | None = None

    # ── HTTP core ─────────────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, json: Optional[dict] = None,
                       params: Optional[dict] = None, timeout: int = 30) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
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
                            return data
                        err = (data or {}).get("error", {}) or {}
                        code = err.get("code", str(resp.status))
                        last_err = f"{code}: {err.get('message', '')}"
                        # خطاهای گذرا → retry با backoff (طبق داکس: 409 conflict/423/429/5xx)
                        if resp.status in (429, 502, 503, 504) or \
                           (resp.status == 423 and code == "locked") or \
                           (resp.status == 409 and code == "conflict"):
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        raise RuntimeError(f"Hetzner API {last_err}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = str(e)
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Hetzner API retry limit — {last_err}")

    async def _paginate(self, path: str, key: str, params: Optional[dict] = None) -> list[dict]:
        """همه صفحات یک اندپوینت لیستی را جمع می‌کند."""
        items: list[dict] = []
        page = 1
        while True:
            q = dict(params or {})
            q.update({"page": page, "per_page": 50})
            data = await self._request("GET", path, params=q)
            batch = data.get(key) or []
            items.extend(batch)
            pagination = (data.get("meta") or {}).get("pagination") or {}
            if not pagination.get("next_page"):
                break
            page = pagination["next_page"]
        return items

    async def _wait_action(self, action: Optional[dict], timeout_s: int = 180) -> None:
        """Polling یک Action تا success/error — با فاصله تا rate limit مصرف نشود."""
        if not action or not action.get("id"):
            return
        if action.get("status") == "success":
            return
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            data = await self._request("GET", f"/actions/{action['id']}")
            act = data.get("action") or {}
            if act.get("status") == "success":
                return
            if act.get("status") == "error":
                err = act.get("error") or {}
                raise RuntimeError(
                    f"Hetzner action {act.get('command')} failed — "
                    f"{err.get('code')}: {err.get('message')}"
                )
        raise RuntimeError("Hetzner action timeout")

    # ── Mapping helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _server_info(srv: dict, root_password: Optional[str] = None) -> ServerInfo:
        st = srv.get("server_type") or {}
        ipv4 = ((srv.get("public_net") or {}).get("ipv4") or {})
        ipv6 = ((srv.get("public_net") or {}).get("ipv6") or {})
        status = _STATUS_MAP.get(srv.get("status", "unknown"), "off")
        extra = {
            "machine_status": "1" if srv.get("status") == "running" else "0",
            "hetzner_status": srv.get("status"),
            "labels": srv.get("labels") or {},
        }
        if root_password:
            extra["root_password"] = root_password
        return ServerInfo(
            provider_server_id=str(srv.get("id")),
            name=srv.get("name") or "",
            status=status,
            ip_address=ipv4.get("ip"),
            ipv6_address=ipv6.get("ip"),
            ram=int(float(st.get("memory", 0)) * 1024),
            cpu=int(st.get("cores", 0)),
            disk=int(st.get("disk", 0)),
            bandwidth=int((srv.get("included_traffic") or 0) / _GB),
            os_name=((srv.get("image") or {}) or {}).get("name"),
            location=((srv.get("datacenter") or {}).get("location") or {}).get("name"),
            datacenter=(srv.get("datacenter") or {}).get("name"),
            traffic_used_gb=float(srv.get("outgoing_traffic") or 0) / _GB,
            extra_data=extra,
        )

    # ── BaseProvider implementation ───────────────────────────────────────────

    @staticmethod
    def _type_offered_at(t: dict, location: str) -> bool:
        """آیا این server_type در این لوکیشن قابل سفارش است؟

        منبع: فیلد locations[] خودِ پلن — هر entry ممکن است deprecation داشته باشد
        (یعنی در آن لوکیشن از رده خارج شده حتی اگر هنوز قیمت برگردد).
        اگر ساختار locations در دسترس نبود، محافظه‌کارانه True (مبنای قیمت)."""
        entries = t.get("locations") or []
        if not entries:
            return True
        for tl in entries:
            if isinstance(tl, str):
                if tl == location:
                    return True
                continue
            if not isinstance(tl, dict):
                continue
            loc_obj = tl.get("location")
            name = (loc_obj.get("name") if isinstance(loc_obj, dict)
                    else loc_obj if isinstance(loc_obj, str)
                    else tl.get("name"))
            if name != location:
                continue
            return not tl.get("deprecation")
        return False

    async def _available_type_ids_at(self, location: str) -> set:
        """IDهای server_type که واقعاً در این لوکیشن «موجود»اند (نه فقط قیمت‌دار).

        هتزنر برای برخی پلن‌ها قیمتِ لوکیشن را می‌دهد ولی ظرفیت ندارد/نسل قدیمی است
        → خطای unsupported location for server type موقع ساخت. منبع درست:
        GET /datacenters → server_types.available"""
        dcs = await self._paginate("/datacenters", "datacenters")
        ids: set = set()
        for dc in dcs:
            if ((dc.get("location") or {}).get("name")) == location:
                ids |= set((dc.get("server_types") or {}).get("available") or [])
        return ids

    async def create_server(self, params: CreateServerParams) -> ServerInfo:
        # پیش‌چک عرضه‌ی پلن در لوکیشن — جلوی خطای گنگ API را می‌گیرد
        if params.location:
            try:
                t = await self._request("GET", "/server_types",
                                        params={"name": params.plan_id})
                tlist = t.get("server_types") or []
            except Exception:
                tlist = []
            if tlist and not self._type_offered_at(tlist[0], params.location):
                raise RuntimeError(
                    f"پلن {params.plan_id} فعلاً در لوکیشن {params.location} موجود نیست"
                )

        body: dict = {
            "name": params.name,
            "server_type": params.plan_id,
            "image": params.os_id or "ubuntu-24.04",
            "start_after_create": True,
            "labels": {
                "managed_by": "abrpardaz",
                **{k: str(v) for k, v in (params.extra.get("labels") or {}).items()},
            },
        }
        if params.location:
            body["location"] = params.location
        try:
            data = await self._request("POST", "/servers", json=body, timeout=60)
        except RuntimeError as e:
            # نام سرور در پروژه باید یکتا باشد — کاربر دیگری قبلاً همین hostname را داده
            if "uniqueness" in str(e).lower():
                import secrets as _sec
                body["name"] = f"{params.name}-{_sec.token_hex(2)}"
                data = await self._request("POST", "/servers", json=body, timeout=60)
            else:
                raise
        srv = data.get("server") or {}
        root_password = data.get("root_password")
        try:
            await self._wait_action(data.get("action"), timeout_s=300)
        except Exception:
            # اگر ساخت شکست خورد، سرور نیمه‌ساخته را پاک کن تا بیل نخورد
            if srv.get("id"):
                try:
                    await self._request("DELETE", f"/servers/{srv['id']}")
                except Exception:
                    pass
            raise
        # وضعیت نهایی + IP قطعی
        fresh = await self._request("GET", f"/servers/{srv['id']}")
        return self._server_info(fresh.get("server") or srv, root_password=root_password)

    async def delete_server(self, server_id: str) -> bool:
        # تنها راه قطع کامل هزینه در هتزنر
        data = await self._request("DELETE", f"/servers/{server_id}")
        try:
            await self._wait_action(data.get("action"), timeout_s=120)
        except Exception:
            pass
        return True

    async def get_server(self, server_id: str) -> ServerInfo:
        data = await self._request("GET", f"/servers/{server_id}")
        return self._server_info(data.get("server") or {})

    async def _server_action(self, server_id: str, action: str,
                             json: Optional[dict] = None, timeout_s: int = 120) -> bool:
        data = await self._request("POST", f"/servers/{server_id}/actions/{action}", json=json)
        await self._wait_action(data.get("action"), timeout_s=timeout_s)
        return True

    async def start_server(self, server_id: str) -> bool:
        return await self._server_action(server_id, "poweron")

    async def stop_server(self, server_id: str) -> bool:
        return await self._server_action(server_id, "poweroff")

    async def restart_server(self, server_id: str) -> bool:
        return await self._server_action(server_id, "reboot")

    async def rebuild_server(self, server_id: str, os_id: str, rootpass: str = "") -> bool:
        """نصب مجدد. هتزنر رمز دلخواه نمی‌گیرد — رمز جدید تولیدشده توسط هتزنر در
        self.last_root_password ذخیره می‌شود تا هندلر به کاربر نشان دهد."""
        data = await self._request(
            "POST", f"/servers/{server_id}/actions/rebuild", json={"image": os_id}
        )
        self.last_root_password = data.get("root_password")  # None اگر ssh-key ست باشد
        await self._wait_action(data.get("action"), timeout_s=300)
        return True

    async def suspend_server(self, server_id: str) -> bool:
        # هتزنر ساسپند ندارد — سیاست: خاموش‌کردن (توجه: بیل ادامه دارد؛ قطع کامل فقط با حذف)
        return await self._server_action(server_id, "poweroff")

    async def unsuspend_server(self, server_id: str) -> bool:
        return await self._server_action(server_id, "poweron")

    async def get_traffic(self, server_id: str) -> float:
        data = await self._request("GET", f"/servers/{server_id}")
        return float(((data.get("server") or {}).get("outgoing_traffic")) or 0) / _GB

    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        """پلن‌ها (server_types) با قیمت خرید gross per-location.

        price_hourly/price_monthly در PlanInfo = «قیمت خرید» به یورو (gross).
        """
        types = await self._paginate("/server_types", "server_types")

        # سیگنال ۲: موجودی لحظه‌ای (ظرفیت) از datacenters.available —
        # پلنِ «عرضه‌شده» ممکن است ته کشیده باشد (کنسول: Not available…)
        stock_ids: set = set()
        if location:
            try:
                stock_ids = await self._available_type_ids_at(location)
            except Exception as e:
                logger.warning("hetzner: stock fetch failed for %s: %s", location, e)
            if not stock_ids:
                # خالی = تقریباً همیشه یعنی ساختار/خطا؛ fail-open تا لیست کور نشود
                logger.warning("hetzner: empty stock set for %s — stock filter skipped", location)

        plans: list[PlanInfo] = []
        for t in types:
            if t.get("deprecated"):
                continue
            # سیگنال ۱: عرضه‌ی per-location از فیلد locations خودِ پلن
            # (cpx11@fsn1 قیمت دارد ولی deprecation دارد → «unsupported location»)
            if location and not self._type_offered_at(t, location):
                continue
            # سیگنال ۲: باید در ظرفیتِ لحظه‌ای دیتاسنترهای لوکیشن هم باشد
            if location and stock_ids and t.get("id") not in stock_ids:
                continue
            for price in t.get("prices") or []:
                loc = price.get("location")
                if location and loc != location:
                    continue
                try:
                    hourly = float(((price.get("price_hourly") or {}).get("gross")) or 0)
                    monthly = float(((price.get("price_monthly") or {}).get("gross")) or 0)
                except (TypeError, ValueError):
                    hourly = monthly = 0
                plans.append(PlanInfo(
                    provider_plan_id=t.get("name") or str(t.get("id")),
                    name=f"{t.get('name')} — {t.get('cores')}c/{t.get('memory')}GB/{t.get('disk')}G "
                         f"[{t.get('cpu_type')}/{t.get('architecture')}]",
                    ram=int(float(t.get("memory", 0)) * 1024),
                    cpu=int(t.get("cores", 0)),
                    disk=int(t.get("disk", 0)),
                    bandwidth=int((price.get("included_traffic") or 0) / _GB),
                    price_hourly=hourly,
                    price_monthly=monthly,
                    location=loc,
                ))
        return plans

    async def list_os_templates(self) -> list[dict]:
        """ایمیج‌های رسمی (system) — id = name چون POST /servers نام را قبول می‌کند.

        مرتب‌سازی بلوکی: توزیع‌های هم‌خانواده کنار هم (Ubuntu ها با هم، Debian ها
        با هم، ...) و داخل هر خانواده نسخه‌ی جدیدتر اول."""
        images = await self._paginate(
            "/images", "images", params={"type": "system", "status": "available"}
        )
        result = []
        for img in images:
            if img.get("deprecated"):
                continue
            try:
                ver = float(str(img.get("os_version") or "0").split("-")[0])
            except ValueError:
                ver = 0.0
            result.append({
                "id": img.get("name") or str(img.get("id")),
                "name": f"{img.get('description') or img.get('name')}"
                        + (" (ARM)" if img.get("architecture") == "arm" else ""),
                "architecture": img.get("architecture", "x86"),
                "_flavor": (img.get("os_flavor") or "").lower(),
                "_ver": ver,
            })
        result.sort(key=lambda o: (o["_flavor"], -o["_ver"], o["architecture"]))
        return result

    # ── Extras (خارج از BaseProvider) ────────────────────────────────────────

    async def reset_password(self, server_id: str) -> str:
        """ریست رمز root — رمز جدید را خود هتزنر تولید می‌کند و برمی‌گرداند."""
        data = await self._request("POST", f"/servers/{server_id}/actions/reset_password")
        new_pass = data.get("root_password") or ""
        await self._wait_action(data.get("action"), timeout_s=120)
        if not new_pass:
            raise RuntimeError("هتزنر رمز جدید برنگرداند")
        self.last_root_password = new_pass
        return new_pass

    async def change_root_password(self, server_id: str, new_password: str) -> bool:
        """هتزنر رمز دلخواه نمی‌پذیرد — reset می‌کنیم و رمز تولیدی هتزنر در
        last_root_password قرار می‌گیرد تا لایه‌ی سرویس همان را به کاربر بدهد."""
        await self.reset_password(server_id)
        return True

    async def change_ip(self, server_id: str) -> Optional[str]:
        """تعویض IPv4 اصلی از مسیر Primary IP:
        ساخت IP جدید → خاموش (unassign فقط روی سرور خاموش) → جداکردن قدیمی →
        اتصال جدید → حذف قدیمی (توقف بیل آن) → روشن. در خطا، برگشت به IP قبلی."""
        import secrets as _sec

        data = await self._request("GET", f"/servers/{server_id}")
        srv = data.get("server") or {}
        ipv4 = ((srv.get("public_net") or {}).get("ipv4") or {})
        old_id, old_ip = ipv4.get("id"), ipv4.get("ip")
        if not old_id:
            raise RuntimeError("این سرور IPv4 اصلی ندارد")
        was_running = srv.get("status") == "running"

        # سرور فقط یک IPv4 اصلی می‌تواند داشته باشد → اول جدا، بعد ساختِ
        # مستقیماً-متصل (assignee_id) — ساخت آزاد با datacenter خطای invalid_input می‌داد
        if was_running:
            await self._server_action(server_id, "poweroff")
        d = await self._request("POST", f"/primary_ips/{old_id}/actions/unassign")
        await self._wait_action(d.get("action"))

        new_id = new_ip = None
        try:
            created = await self._request("POST", "/primary_ips", json={
                "type": "ipv4",
                "name": f"ip-{server_id}-{_sec.token_hex(3)}",
                "assignee_type": "server",
                "assignee_id": int(server_id),
                "auto_delete": True,   # با حذف سرور، این IP هم حذف شود (بیل نماند)
            })
            new_pi = created.get("primary_ip") or {}
            new_id, new_ip = new_pi.get("id"), new_pi.get("ip")
            await self._wait_action(created.get("action"))
            if not new_ip:
                raise RuntimeError("ساخت Primary IP جدید ناموفق بود")
            try:
                await self._request("DELETE", f"/primary_ips/{old_id}")
            except Exception as e:
                logger.warning("change_ip: old primary ip %s not deleted: %s", old_id, e)
            if was_running:
                await self._server_action(server_id, "poweron")
            return new_ip
        except Exception:
            # برگشت: IP جدید (اگر ساخته شد) حذف و IP قدیمی دوباره وصل شود
            if new_id:
                try:
                    d = await self._request("POST", f"/primary_ips/{new_id}/actions/unassign")
                    await self._wait_action(d.get("action"))
                except Exception:
                    pass
                try:
                    await self._request("DELETE", f"/primary_ips/{new_id}")
                except Exception:
                    pass
            try:
                d = await self._request(
                    "POST", f"/primary_ips/{old_id}/actions/assign",
                    json={"assignee_id": int(server_id), "assignee_type": "server"},
                )
                await self._wait_action(d.get("action"))
            except Exception:
                pass
            if was_running:
                try:
                    await self._server_action(server_id, "poweron")
                except Exception:
                    pass
            raise

    async def list_locations(self) -> list[dict]:
        locs = await self._paginate("/locations", "locations")
        return [{
            "name": l.get("name"),
            "city": l.get("city"),
            "country": l.get("country"),
            "network_zone": l.get("network_zone"),
        } for l in locs]

    async def ping(self) -> bool:
        """تست اتصال/توکن — برای health check و افزودن اکانت."""
        await self._request("GET", "/locations", params={"per_page": 1})
        return True

    # ── Snapshots (Images) ────────────────────────────────────────────────────

    async def create_snapshot(self, server_id: str, description: Optional[str] = None,
                              labels: Optional[dict] = None) -> dict:
        """ساخت اسنپ‌شات و صبر تا کامل‌شدن. طبق داکس: image_size تا وقتی
        status=creating است null می‌ماند → بعد از success دوباره خوانده می‌شود.
        خروجی: dict شامل id/image_size/disk_size/architecture/os_*."""
        body: dict = {"type": "snapshot"}
        if description:
            body["description"] = description
        if labels:
            body["labels"] = labels
        data = await self._request(
            "POST", f"/servers/{server_id}/actions/create_image", json=body, timeout=60)
        image_id = (data.get("image") or {}).get("id")
        # اسنپ‌شات‌ها ممکن است چند دقیقه طول بکشند
        await self._wait_action(data.get("action"), timeout_s=900)
        img = await self.get_image(image_id)
        return {
            "id": str(img.get("id") or image_id),
            "image_size": float(img.get("image_size") or 0),
            "disk_size": int(img.get("disk_size") or 0),
            "architecture": img.get("architecture", "x86"),
            "os_flavor": img.get("os_flavor"),
            "os_version": img.get("os_version"),
            "description": img.get("description"),
            "status": img.get("status"),
        }

    async def get_image(self, image_id: str) -> dict:
        data = await self._request("GET", f"/images/{image_id}")
        return data.get("image") or {}

    async def delete_image(self, image_id: str) -> bool:
        """DELETE /images/{id} → 204. فقط snapshot/backup قابل حذف‌اند."""
        await self._request("DELETE", f"/images/{image_id}")
        return True

    async def rebuild_from_image(self, server_id: str, image_id: str) -> Optional[str]:
        """ریستور: rebuild سرور با image اسنپ‌شات (دیسک مقصد پاک می‌شود!).
        رمز فقط وقتی برمی‌گردد که سرور SSH key نداشته باشد؛ وگرنه رمزِ داخل
        اسنپ‌شات فعال است. رمز برگشتی در last_root_password ذخیره می‌شود.

        ⚠️ فراخوان باید پیش از این، سازگاری معماری و disk_size را چک کند."""
        data = await self._request(
            "POST", f"/servers/{server_id}/actions/rebuild",
            json={"image": image_id}, timeout=60)
        self.last_root_password = data.get("root_password")
        await self._wait_action(data.get("action"), timeout_s=600)
        return data.get("root_password")

    async def server_type_info(self, server_id: str) -> dict:
        """disk (GB) و architecture سرور مقصد — برای بررسی سازگاری rebuild."""
        data = await self._request("GET", f"/servers/{server_id}")
        st = (data.get("server") or {}).get("server_type") or {}
        return {"disk": int(st.get("disk") or 0),
                "architecture": st.get("architecture", "x86")}

    async def snapshot_price_per_gb_month(self) -> float:
        """قیمت هر GB در ماه (gross EUR) — مقادیر string هستند."""
        data = await self._request("GET", "/pricing")
        ppg = (((data.get("pricing") or {}).get("image") or {})
               .get("price_per_gb_month") or {})
        try:
            return float(ppg.get("gross") or ppg.get("net") or 0)
        except (TypeError, ValueError):
            return 0.0

    async def count_servers(self) -> int:
        """تعداد کل سرورهای موجود روی اکانت (برای نمایش/کنترل لیمیت VM).

        هتزنر سقف لیمیت اکانت را از API نمی‌دهد (فقط خطای resource_limit_exceeded
        موقع عبور) — پس لیمیت را ادمین دستی ثبت می‌کند و این متد مصرفِ فعلی را
        زنده می‌شمارد."""
        data = await self._request("GET", "/servers", params={"per_page": 1})
        pagination = (data.get("meta") or {}).get("pagination") or {}
        return int(pagination.get("total_entries") or 0)
