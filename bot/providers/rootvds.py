"""RootVDS provider — rootvds.ru (رفرنس: D:\\Abr Pardaz\\ROOTVDS.md).

خلاصه‌ی API (داکسِ رسمی + دستورالعمل پشتیبانی):
- همه‌ی درخواست‌ها POST با بدنه‌ی form-urlencoded و هدر Auth-Token؛ پاسخ JSON با
  status_code (string!). ارز: روبل. بیلینگ: کسر دقیقه‌ای از بالانس (minute_pay).
- create (/api/vds/install/) پاسخ فوری با آبجکت server می‌دهد؛ task-id ندارد →
  poll روی /api/vds/info/ تا status=="on" و ipv4 پر شود. IP با خودِ سرور می‌آید
  (سرویس جدا مثل تایم‌وب نیست).
- رمز root را ما می‌سازیم و در create می‌فرستیم (لینوکس)؛ الگوی مجاز: فقط حروف
  لاتین+عدد+خاص از «- ! _» با حداقل یک کوچک/بزرگ/عدد/خاص. ویندوز root_pass
  نمی‌گیرد → بدون رمز، بعداً از info.root_pass.
- ظرفیت/موجودی: خطاهای صریح در create (No money / not possible to create /
  location is not available / preset_id is not available) → شکست سریع + برگشت
  وجه؛ auto-hide نداریم (درسِ تایم‌وب).
- suspend ندارد → off/on. حذف = قطع هزینه (payment_mode=balance).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import string
from typing import Optional

import aiohttp

from .base import BaseProvider, CreateServerParams, PlanInfo, ServerInfo

logger = logging.getLogger(__name__)

API_BASE = "https://api.rootvds.ru"

# نگاشت وضعیت rootvds → ۴ حالت داخلی چهارچوب
_STATUS_MAP = {
    "on": "active",
    "off": "off",
    "done": "building",          # create تازه — هنوز روشن/آماده نشده
    "turning_on": "building",
    "turning_off": "building",
    "reboot": "building",
    "reinstalling": "building",
    "cloning": "building",
    "delete": "off",
}
_RUNNING = {"on"}

# خطاهای ظرفیت/موجودی provider (متن انگلیسی داکس) — پیام فارسی قابل‌نمایش
_CAPACITY_ERRORS = (
    "not possible to create",
    "location is not available",
    "is not available",
)


def _gen_root_pass(length: int = 14) -> str:
    """رمز سازگار با rootvds: فقط لاتین+عدد+خاص از «- ! _»، حداقل یکی از هر
    دسته (الزام API). رمز دلخواهِ فلوی عمومی charset دیگری دارد → خودمان می‌سازیم."""
    specials = "-!_"
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, specials]
    chars = [secrets.choice(p) for p in pools]
    alphabet = string.ascii_letters + string.digits + specials
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


class RootVDSProvider(BaseProvider):
    def __init__(self, api_token: str):
        self.token = (api_token or "").strip()
        # رمز واقعی آخرین create/rebuild — لایه‌ی سرویس همین را تحویل می‌دهد
        self.last_root_password: str | None = None

    # ── HTTP core ─────────────────────────────────────────────────────────────

    async def _request(self, path: str, data: Optional[dict] = None,
                       timeout: int = 30) -> dict:
        """POST form-urlencoded با Auth-Token. خطای گذرا (423/429/5xx) retry
        می‌شود؛ خطای نهایی RuntimeError با پیام توصیفی."""
        headers = {"Auth-Token": self.token}
        last_err = "unknown"
        for attempt in range(5):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session:
                    async with session.post(
                        f"{API_BASE}{path}", headers=headers, data=data or {},
                    ) as resp:
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            body = {}
                        if not isinstance(body, dict):
                            body = {"_list": body}
                        # status_code داخل بدنه string است؛ HTTP status هم هست
                        try:
                            code = int(body.get("status_code") or resp.status)
                        except (TypeError, ValueError):
                            code = resp.status
                        if code < 400:
                            return body
                        msg = str(body.get("message") or "")
                        last_err = f"RootVDS API {code}: {msg}"[:300]
                        # 423 (Server error حین عملیات) / 429 / 5xx / busy = گذرا
                        transient = code in (423, 429, 500, 502, 503, 504) \
                            or "please wait" in msg.lower() \
                            or "server is busy" in msg.lower()
                        if transient:
                            await asyncio.sleep(min(3 * (attempt + 1), 20))
                            continue
                        raise RuntimeError(last_err)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = str(e)
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"RootVDS API retry limit — {last_err}"[:300])

    async def _list(self, path: str) -> list:
        """لیست‌های مرجع (location/preset/os) — شکل پاسخ مستند نیست، پس هر
        کلیدِ آرایه‌ایِ بدنه را می‌پذیریم (پارس دفاعی)."""
        body = await self._request(path)
        for key in ("_list", "locations", "location", "presets", "preset",
                    "tariffs", "list", "os", "data", "items", "result",
                    "servers", "vds"):
            v = body.get(key)
            if isinstance(v, list):
                return v
            # کلیدِ درست ولی dictِ id→obj (نه آرایه)
            if isinstance(v, dict):
                vals = [x for x in v.values() if isinstance(x, dict)]
                if vals:
                    return vals
        # بدنه‌ی dict با مقادیر dict (id→obj)
        vals = [v for v in body.values() if isinstance(v, dict)]
        if vals and all("id" in v for v in vals):
            return vals
        for v in body.values():
            if isinstance(v, list):
                return v
        # شکل ناشناخته — کلیدها و نمونه‌ی بدنه را لاگ کن تا پارس دقیق شود
        logger.warning("RV_LIST_DIAG path=%s keys=%s sample=%s",
                       path, list(body.keys())[:10], str(body)[:400])
        return []

    async def _get_server_raw(self, server_id: str) -> dict:
        body = await self._request("/api/vds/info/", {"server_id": int(server_id)})
        return body.get("server") or {}

    async def _wait_status(self, server_id: str, targets: set[str],
                           timeout_s: int = 600, need_ip: bool = False,
                           need_root_pass: bool = False) -> dict:
        """Poll هر ۵ ثانیه تا رسیدن به وضعیت هدف (+ IP/رمز اگر لازم)."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        last: dict = {}
        polls = 0
        while asyncio.get_event_loop().time() < deadline:
            try:
                last = await self._get_server_raw(server_id)
            except RuntimeError:
                last = {}
            st = (last.get("status") or "").lower()
            polls += 1
            if polls % 6 == 1:   # هر ~۳۰ ثانیه یک خط لاگ کافی است
                logger.warning("RV_POLL server=%s #%d status=%s ip=%s pass=%s",
                               server_id, polls, st or "?",
                               bool(last.get("ipv4")), bool(last.get("root_pass")))
            if st in targets \
                    and (not need_ip or last.get("ipv4")) \
                    and (not need_root_pass or last.get("root_pass")):
                return last
            await asyncio.sleep(5)
        raise RuntimeError("RootVDS: مهلت انتظار عملیات تمام شد")

    # ── Mapping ───────────────────────────────────────────────────────────────

    @staticmethod
    def _server_info(srv: dict) -> ServerInfo:
        raw_status = (srv.get("status") or "off").lower()
        os_name = " ".join(str(x) for x in (srv.get("os_name"),
                                            srv.get("os_version")) if x)
        is_windows = "windows" in (srv.get("os_name") or "").lower()
        try:
            disk_gb = int(srv.get("disk_size") or 0)
        except (TypeError, ValueError):
            disk_gb = 0
        # disk_size در create برحسب GB نمونه‌سازی شده (40)؛ اگر MB بود (بزرگ) تبدیل
        if disk_gb > 4096:
            disk_gb //= 1024
        extra = {
            "machine_status": "1" if raw_status in _RUNNING else "0",
            "rootvds_status": raw_status,
            "username": "Administrator" if is_windows else "root",
        }
        if srv.get("root_pass"):
            extra["root_password"] = srv["root_pass"]
        return ServerInfo(
            provider_server_id=str(srv.get("id")),
            name=srv.get("name") or "",
            status=_STATUS_MAP.get(raw_status, "off"),
            ip_address=srv.get("ipv4") or None,
            ipv6_address=srv.get("ipv6") or None,
            ram=int(srv.get("ram") or 0),
            cpu=int(srv.get("cpu") or 0),
            disk=disk_gb,
            bandwidth=0,
            os_name=os_name or None,
            location=srv.get("location"),
            traffic_used_gb=0.0,
            extra_data=extra,
        )

    # ── BaseProvider ──────────────────────────────────────────────────────────

    async def create_server(self, params: CreateServerParams) -> ServerInfo:
        if not params.plan_id:
            raise RuntimeError("تعرفه (preset) مشخص نیست")
        if not params.os_id:
            raise RuntimeError("سیستم‌عامل انتخاب نشده است")

        # پیش‌چکِ سبک موجودی اکانت — «No money» را قبل از کسر پول مشتری بفهمیم
        bal = await self.get_balance()
        if bal is not None and bal <= 0:
            raise RuntimeError(
                "__RV_FUNDS__ موجودی اکانت RootVDS کافی نیست — لطفاً بعداً تلاش کنید")

        root_pass = _gen_root_pass()
        body = {
            "preset_id": int(params.plan_id),
            "os_id": int(params.os_id),
            # server_name حداکثر ۲۰ کاراکتر
            "server_name": (params.name or "srv")[:20],
            "root_pass": root_pass,
        }
        try:
            data = await self._request("/api/vds/install/", body, timeout=60)
        except RuntimeError as e:
            msg = str(e)
            low = msg.lower()
            # ویندوز رمز نمی‌گیرد → بدون root_pass دوباره؛ رمز بعداً از info
            if "does not support windows" in low:
                body.pop("root_pass", None)
                root_pass = ""
                data = await self._request("/api/vds/install/", body, timeout=60)
            elif "no money" in low or "balances are not activated" in low:
                logger.warning("rootvds create refused: %s", msg)
                raise RuntimeError(
                    "__RV_FUNDS__ موجودی اکانت RootVDS کافی نیست — "
                    "لطفاً بعداً تلاش کنید")
            elif any(t in low for t in _CAPACITY_ERRORS):
                logger.warning("rootvds capacity: %s", msg)
                raise RuntimeError(
                    "ظرفیت سرویس‌دهنده موقتاً در دسترس نیست — لطفاً بعداً تلاش کنید")
            else:
                raise
        srv = data.get("server") or {}
        server_id = srv.get("id")
        if not server_id:
            raise RuntimeError("RootVDS شناسه سرور ساخته‌شده را برنگرداند")
        logger.warning("RV_CREATE_DIAG preset=%s os=%s → id=%s status=%s",
                       body.get("preset_id"), body.get("os_id"), server_id,
                       (srv.get("status") or "?"))

        # ساخت تراکنشی: تا on + ipv4 (+ رمز) صبر؛ شکست/مهلت → حذف تا بیل نخورد
        try:
            fresh = await self._wait_status(
                str(server_id), {"on"}, timeout_s=600,
                need_ip=True, need_root_pass=not root_pass)
        except Exception:
            # رِسکیو: اگر واقعاً ساخته شده (IP دارد)، تحویل بده نه حذف
            try:
                chk = await self._get_server_raw(str(server_id))
                if chk.get("ipv4") and (chk.get("root_pass") or root_pass):
                    self.last_root_password = chk.get("root_pass") or root_pass
                    info = self._server_info(chk)
                    info.extra_data.setdefault("root_password",
                                               self.last_root_password)
                    return info
            except Exception:
                pass
            try:
                await self.delete_server(str(server_id))
            except Exception:
                pass
            raise
        self.last_root_password = fresh.get("root_pass") or root_pass
        info = self._server_info(fresh)
        info.extra_data.setdefault("root_password", self.last_root_password)
        return info

    async def delete_server(self, server_id: str) -> bool:
        sid = int(server_id)
        try:
            await self._request("/api/vds/delete/", {"server_id": sid}, timeout=60)
        except RuntimeError as e:
            low = str(e).lower()
            if "not found" in low or "being deleted" in low:
                return True   # قبلاً حذف شده / در حال حذف
            raise
        return True

    async def get_server(self, server_id: str) -> ServerInfo:
        return self._server_info(await self._get_server_raw(server_id))

    async def start_server(self, server_id: str) -> bool:
        await self._request("/api/vds/on/", {"server_id": int(server_id)})
        return True

    async def stop_server(self, server_id: str) -> bool:
        await self._request("/api/vds/off/", {"server_id": int(server_id)})
        return True

    async def restart_server(self, server_id: str) -> bool:
        await self._request("/api/vds/reboot/", {"server_id": int(server_id)})
        return True

    async def rebuild_server(self, server_id: str, os_id: str,
                             rootpass: str = "") -> bool:
        """نصب مجدد OS (reos). رمز قابل‌تعیین نیست — بعد از پایان از info خوانده
        می‌شود (last_root_password). حداقل دیسک 35GB (خطای صریح API)."""
        old = (await self._get_server_raw(server_id)).get("root_pass")
        await self._request("/api/vds/reos/",
                            {"server_id": int(server_id), "os_id": os_id},
                            timeout=60)
        fresh = await self._wait_status(str(server_id), {"on"},
                                        timeout_s=900, need_root_pass=True)
        new_pass = fresh.get("root_pass")
        # مهلت کوتاه برای نشستن رمز تازه اگر همان قدیمی برگشت (الگوی تایم‌وب)
        grace = asyncio.get_event_loop().time() + 60
        while new_pass == old and asyncio.get_event_loop().time() < grace:
            await asyncio.sleep(5)
            try:
                srv = await self._get_server_raw(server_id)
            except RuntimeError:
                continue
            if srv.get("root_pass"):
                new_pass = srv["root_pass"]
        self.last_root_password = new_pass or old
        return True

    async def suspend_server(self, server_id: str) -> bool:
        # suspend واقعی ندارد → خاموش (بیل ادامه دارد؛ فقط حذف قطع می‌کند)
        return await self.stop_server(server_id)

    async def unsuspend_server(self, server_id: str) -> bool:
        return await self.start_server(server_id)

    async def get_traffic(self, server_id: str) -> float:
        return 0.0   # API ترافیک مصرفی نمی‌دهد

    async def change_ip(self, server_id: str) -> Optional[str]:
        """تغییر IPv4 — سرور با IP جدید ریبوت می‌شود."""
        old = (await self._get_server_raw(server_id)).get("ipv4")
        await self._request("/api/vds/change_ipv4/",
                            {"server_id": int(server_id)}, timeout=60)
        deadline = asyncio.get_event_loop().time() + 300
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(5)
            try:
                srv = await self._get_server_raw(server_id)
            except RuntimeError:
                continue
            ip = srv.get("ipv4")
            if ip and ip != old and (srv.get("status") or "").lower() == "on":
                return ip
        return None

    # ── کاتالوگ ───────────────────────────────────────────────────────────────

    @staticmethod
    def _preset_fields(p: dict) -> dict:
        """فیلدهای یک preset با نام‌های متغیر (شکل لیست مستند نیست) — دفاعی."""
        def _num(*keys, default=0):
            for k in keys:
                v = p.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return default
        ram = int(_num("ram", "ram_mb", "memory"))
        if 0 < ram <= 512:            # اگر GB بود → MB
            ram *= 1024
        disk = int(_num("disk_size", "disk", "disk_gb"))
        if disk > 4096:               # اگر MB بود → GB
            disk //= 1024
        return {
            "id": str(p.get("id") or ""),
            "name": str(p.get("name") or p.get("title") or "").strip(),
            "cpu": int(_num("cpu", "cpu_count", "cores", default=1)),
            "ram": ram,
            "disk": disk,
            "price": _num("price", "price_month", "monthly_price"),
            "location": str(p.get("location") or p.get("location_id") or ""),
            "disk_type": str(p.get("disk_type") or ""),
            "cpu_frequency": p.get("cpu_frequency"),
        }

    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        presets = await self._list("/api/vds/preset/")
        out: list[PlanInfo] = []
        for raw in presets:
            if not isinstance(raw, dict):
                continue
            f = self._preset_fields(raw)
            if not f["id"] or f["price"] <= 0:
                continue
            if location and f["location"] != location:
                continue
            out.append(PlanInfo(
                provider_plan_id=f["id"],
                name=f["name"] or f"rv-{f['id']}",
                ram=f["ram"], cpu=f["cpu"], disk=f["disk"],
                bandwidth=0,
                price_hourly=round(f["price"] / 720.0, 6),
                price_monthly=f["price"],
                location=f["location"] or None,
                currency="rub",
            ))
        if presets and not out and not location:
            # لیست هست ولی فیلدها نمی‌خوانند (id/price با اسم دیگر) — نمونه لاگ شود
            logger.warning("RV_PRESET_DIAG count=%d sample=%s",
                           len(presets), str(presets[0])[:400])
        return out

    async def list_locations(self) -> list[dict]:
        """[{slug, display_name, count}] — نام از لیست لوکیشن‌ها، شمارش از تعرفه‌ها."""
        locs = await self._list("/api/vds/location/")
        names: dict[str, str] = {}
        for l in locs:
            if not isinstance(l, dict):
                continue
            slug = str(l.get("location") or l.get("code") or l.get("slug")
                       or l.get("id") or "").strip()
            name = str(l.get("name") or l.get("title") or l.get("city")
                       or slug).strip()
            if slug:
                names[slug] = name
        counts: dict[str, int] = {}
        for p in await self.list_plans():
            if p.location:
                counts[p.location] = counts.get(p.location, 0) + 1
        out = []
        for slug in sorted(set(names) | set(counts)):
            out.append({
                "slug": slug,
                "display_name": names.get(slug, slug),
                "count": counts.get(slug, 0),
            })
        return out

    async def list_os_templates(self) -> list[dict]:
        items = await self._list("/api/vds/os/")
        out = []
        for o in items:
            if not isinstance(o, dict) or o.get("id") is None:
                continue
            name = " ".join(str(x) for x in (
                o.get("name") or o.get("os_name") or o.get("title"),
                o.get("version") or o.get("os_version"),
            ) if x).strip()
            out.append({"id": str(o["id"]), "name": name or f"os-{o['id']}"})
        return out

    # ── Health / verify ───────────────────────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        try:
            acc = (await self._request("/api/account/")).get("account") or {}
            return float(acc.get("balance") or 0)
        except Exception:
            return None

    async def ping(self) -> bool:
        await self._request("/api/account/")
        return True

    async def verify(self) -> dict:
        """تست زنده هنگام افزودن اکانت: توکن + موجودی + تعرفه‌ها + لوکیشن‌ها."""
        acc = (await self._request("/api/account/")).get("account") or {}
        plans = await self.list_plans()
        return {
            "balance": float(acc.get("balance") or 0),
            "currency": acc.get("currency") or "RUB",
            "presets": len(plans),
            "locations": len({p.location for p in plans if p.location}),
        }
