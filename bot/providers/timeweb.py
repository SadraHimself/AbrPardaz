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

# لیبل شهرِ لوکیشن‌ها برای مرحله‌ی لوکیشن خرید (region_name در extra پلن).
# شهر واقعی از description خودِ preset («Cloud MSK 50») هم استخراج می‌شود؛ این
# نگاشت fallback و برای زمانی است که description شهر را نداشته باشد.
# ⚠️ کد لوکیشن تایم‌وب (ru-1/us-4/...) قابل‌اتکا به شهر نیست (هر اکانت متفاوت) —
# fallback آخر است؛ منبع اصلیِ شهر، description خودِ تعرفه است.
LOC_LABELS = {
    "kz-1": "Almaty (Kazakhstan)", "nl-1": "Amsterdam", "pl-1": "Poland",
    "de-1": "Germany", "us-4": "USA",
}
# کد شهر داخل description تعرفه («Cloud MSK 50» / «Cloud NSK 50») → (کد, نام کامل).
# کدها همان‌هایی‌اند که در zoneهای سایت دیده می‌شوند (SPB-3، MSK-1، ...).
# تطبیق توکن‌محور است (نه substring) تا «de» در «dedicated» شهر نشود.
_CITY_CODE = {
    "spb": "Saint Petersburg", "msk": "Moscow", "nsk": "Novosibirsk",
    "ekb": "Yekaterinburg", "kzn": "Kazan", "ala": "Almaty",
    "ams": "Amsterdam", "fra": "Frankfurt", "gdn": "Gdansk", "waw": "Warsaw",
    "nyc": "New York", "buf": "New York",
}
import re as _re

# override بر اساس کد لوکیشنِ همین اکانت (تأییدشده توسط ادمین 2026-07-25) —
# برای لوکیشن‌هایی که description شهر ندارند (مثل ru-1). code → (کد کوتاه, شهر).
# اولویت: توکنِ description → این نگاشت → کد خام. اگر اکانت عوض شد این آپدیت شود.
_LOC_CITY = {
    "ru-1": ("SPB", "Saint Petersburg"),
    "ru-2": ("NSK", "Novosibirsk"),
    "ru-3": ("MSK", "Moscow"),
    "ru-4": ("EKB", "Yekaterinburg"),
    "ru-5": ("KZN", "Kazan"),
    "kz-1": ("ALA", "Almaty"),
    "nl-1": ("AMS", "Amsterdam"),
    "de-1": ("FRA", "Frankfurt"),
    "us-4": ("BUF", "New York"),
}


def _preset_city_token(preset: dict) -> Optional[str]:
    desc = f"{preset.get('description') or ''} {preset.get('description_short') or ''}"
    tokens = set(_re.findall(r"[a-z]+", desc.lower()))
    for code in _CITY_CODE:
        if code in tokens:
            return code
    return None


def _resolve_city(preset: dict, location: str = "") -> tuple[str, str]:
    """(کد کوتاه, نام کامل) شهر — اولویت: توکنِ description، بعد نگاشت لوکیشنِ
    اکانت (_LOC_CITY)، بعد کد خام. منبع واحد برای همه‌ی برچسب‌ها."""
    tok = _preset_city_token(preset)
    if tok:
        return tok.upper(), _CITY_CODE[tok]
    if location in _LOC_CITY:
        return _LOC_CITY[location]
    letters = "".join(ch for ch in (location or "") if ch.isalpha())
    return (letters.upper() or "TW"), LOC_LABELS.get(location, location or "?")


def city_from_preset(preset: dict, location: str = "") -> str:
    """نام کامل شهر یک تعرفه."""
    return _resolve_city(preset, location)[1]


def city_code_from_preset(preset: dict, location: str = "") -> str:
    """کد کوتاه شهر (SPB/MSK/NSK/...) برای نامِ پلن."""
    return _resolve_city(preset, location)[0]


def tw_build_labels(raw_presets: list) -> dict:
    """map از preset_id به {city, city_code, cpu_type, cpu_label, display_name}.

    نام کوتاه و مرتب per (شهر, نوع): Standard=۱٬۲٬۳ · High CPU=۱۱٬۲۲٬۳۳ ·
    Dedicated=۱۱۱٬۲۲۲ (به ترتیب رم سپس دیسک). مثال: MSK-1، MSK-22، MSK-111.
    منبع واحدِ نام برای ادمین، خرید و سینک."""
    meta: dict = {}
    groups: dict = {}
    for p in raw_presets or []:
        pid = str(p.get("id"))
        loc = p.get("location") or ""
        ccode, cfull = _resolve_city(p, loc)
        tk, tl = cpu_type_from_preset(p)
        meta[pid] = {
            "city": cfull, "city_code": ccode,
            "cpu_type": tk, "cpu_label": tl,
            "ram": int(p.get("ram") or 0), "disk": int(p.get("disk") or 0),
        }
        groups.setdefault((ccode, tk), []).append(pid)
    _pref = {"standard": "", "premium": "P", "highcpu": "H", "dedicated": "D"}
    for (ccode, tk), pids in groups.items():
        pids.sort(key=lambda x: (meta[x]["ram"], meta[x]["disk"]))
        for i, pid in enumerate(pids, start=1):
            if i < 10:
                if tk == "highcpu":
                    code = str(i) * 2          # 11، 22، 33
                elif tk == "dedicated":
                    code = str(i) * 3          # 111، 222
                elif tk == "premium":
                    code = f"P{i}"
                else:
                    code = str(i)              # Standard: 1، 2، 3
            else:
                code = f"{_pref.get(tk, '')}{i}"   # >۹ عضو: پیشوند حرفی امن
            meta[pid]["display_name"] = f"{ccode}-{code}"
    return meta


def cpu_type_from_preset(preset: dict) -> tuple[str, str]:
    """نوع سرور (کلید, لیبل) از description/tags تعرفه — مثل تفکیک جیکور.
    Premium NVMe / High CPU / Dedicated CPU / Standard."""
    blob = " ".join([
        str(preset.get("description") or ""),
        str(preset.get("description_short") or ""),
        " ".join(str(t) for t in (preset.get("tags") or [])),
    ]).lower()
    if "dedicated" in blob:
        return "dedicated", "Dedicated CPU"
    if "high" in blob and "cpu" in blob:
        return "highcpu", "High CPU"
    if "premium" in blob:
        return "premium", "Premium NVMe"
    return "standard", "Standard"

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
                           timeout_s: int = 300, need_root_pass: bool = False,
                           fail_on: set[str] | None = None,
                           no_paid_grace_s: int = 240) -> dict:
        """Poll وضعیت سرور تا رسیدن به یکی از حالت‌های هدف (بدون task-id).
        هر ۵ ثانیه — rate limit تایم‌وب ۲۰/ثانیه است، خیالمان راحت.

        fail_on: وضعیت‌های واقعاً پایان‌یافته (blocked/permanent_blocked) → شکست فوری.
        no_paid: دو حالت دارد — (الف) سرورِ ساخته‌شده که رمز دارد ولی لحظه‌ای
        no_paid است (تسویه‌ی بیلینگ) → تحویلش می‌دهیم (خودش روشن می‌شود). (ب)
        no_paidِ *بدونِ رمز* که پایدار می‌ماند → خودش حل نمی‌شود؛ این یعنی حالتِ
        پرداختِ اکانتِ تایم‌وب فاکتوری/دستی است (نه کمبودِ بالانس)، پس بعد از
        no_paid_grace_s سریع شکست می‌دهیم تا پولِ مشتری زود برگردد، نه اینکه تا
        سقفِ timeout_s معطل بماند."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        no_paid_since: float | None = None
        last: dict = {}
        _polls = 0
        while asyncio.get_event_loop().time() < deadline:
            _poll_err = None
            try:
                last = await self._get_server_raw(server_id)
            except RuntimeError as _e:
                last = {}
                _poll_err = str(_e)[:100]
            st = (last.get("status") or "").lower()
            rp = last.get("root_pass")
            # لاگِ تشخیصیِ لحظه‌به‌لحظه — دقیقاً می‌بینیم سرور در چه وضعیتی گیر
            # کرده و کِی رمز می‌گیرد (برای فهمیدنِ ریشه‌ی no_paid، بدون حدس).
            _polls += 1
            logger.warning(
                "TW_POLL server=%s #%d status=%s rootpass=%s err=%s",
                server_id, _polls, st or "?", bool(rp), _poll_err or "-")
            if st in targets and (not need_root_pass or rp):
                return last
            # (الف) سرورِ ساخته‌شده که فقط منتظرِ تسویه‌ی بیلینگ است (رمز دارد ولی
            # لحظه‌ای no_paid) → آماده‌ی تحویل.
            if need_root_pass and rp and st == "no_paid":
                return last
            if fail_on and st in fail_on:
                logger.warning("timeweb server %s entered %s — aborting wait",
                               server_id, st)
                raise RuntimeError(
                    "ظرفیت سرویس‌دهنده موقتاً در دسترس نیست — لطفاً بعداً تلاش کنید")
            # (ب) no_paidِ پایدارِ بدونِ رمز = حالتِ پرداختِ اکانت (فاکتوری) →
            # خودش حل نمی‌شود؛ زود شکست بده که برگشتِ وجه ۲۰ دقیقه طول نکشد.
            now = asyncio.get_event_loop().time()
            if st == "no_paid":
                if no_paid_since is None:
                    no_paid_since = now
                elif now - no_paid_since >= no_paid_grace_s:
                    logger.warning(
                        "timeweb server %s stuck no_paid > %ss (balance is fine → "
                        "account is in invoice/manual payment mode)",
                        server_id, no_paid_grace_s)
                    raise RuntimeError(
                        "سرور در وضعیت «پرداخت‌نشده» (no_paid) ماند — حالتِ پرداختِ "
                        "اکانتِ تایم‌وب باید روی «از بالانس» تنظیم شود")
            else:
                no_paid_since = None
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

        # پیش‌چک موجودی اکانت تایم‌وب: اگر موجودی از هزینه‌ی ماهانه سرور کمتر
        # باشد، سرور با وضعیت «Not paid» ساخته می‌شود و هرگز روشن نمی‌شود (E2E)
        # → قبل از ساخت جلویش را بگیریم که پول مشتری الکی گیر نکند
        need_rub = float(params.extra.get("cost_monthly") or 0)
        if need_rub:
            try:
                fin = (await self._request(
                    "GET", "/api/v1/account/finances")).get("finances") or {}
                bal = float(fin.get("balance") or 0)
            except Exception:
                bal = None
            if bal is not None and bal < need_rub:
                logger.warning("timeweb balance %.2f RUB < needed %.2f RUB — refusing create",
                               bal, need_rub)
                raise RuntimeError(
                    "ظرفیت سرویس‌دهنده موقتاً در دسترس نیست — لطفاً بعداً تلاش کنید")
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
        # برنامه مارکت‌پلیس (اختیاری) — سازگاری OS در فلوی خرید با os_ids خود
        # برنامه فیلتر شده؛ نصبش وضعیت software_install دارد و زمان‌برتر است
        software_id = params.extra.get("software_id")
        if software_id:
            body["software_id"] = int(software_id)
        labels = params.extra.get("labels") or {}
        if labels.get("tg_user_id"):
            body["comment"] = f"abrpardaz tg:{labels['tg_user_id']}"
        data = await self._request("POST", "/api/v1/servers", json=body, timeout=60)
        srv = data.get("server") or {}
        server_id = srv.get("id")
        if not server_id:
            raise RuntimeError("تایم‌وب شناسه سرور ساخته‌شده را برنگرداند")
        # لاگِ تشخیصی: وضعیتِ *لحظه‌ی ساخت*. اگر همین‌جا no_paid باشد، یعنی خودِ
        # سفارش پرداخت‌نشده ثبت شده (نه مشکلِ IP/صبر) — به‌احتمالِ زیاد تعرفه‌ی
        # legacy دیگر خودکار پرداخت نمی‌شود. preset را هم ثبت می‌کنیم تا معلوم شود.
        self.last_create_status = (srv.get("status") or "").lower()
        logger.warning(
            "TW_CREATE_DIAG preset=%s os=%s sw=%s → id=%s create_status=%s",
            body.get("preset_id"), body.get("os_id"), body.get("software_id"),
            server_id, self.last_create_status or "?")

        # ⚠️ IPv4 عمومی را اینجا (بلافاصله بعد از ساخت) اضافه *نمی‌کنیم*. افزودنِ
        # یک سرویسِ پولیِ جدا (IPv4) وسطِ تسویه‌ی سفارش، سرور را به no_paid می‌زند
        # (ریسِ متناوب — گاهی سالم، گاهی no_paid؛ همان باگی که سرورها را بی‌IP و
        # پرداخت‌نشده می‌کرد). درست مثل ساختِ دستی، IPv4 را *بعد از* بالاآمدنِ سرور
        # (on + root_pass) با _ensure_public_ip اضافه می‌کنیم — پایین‌تر.

        # نصب چند دقیقه طول می‌کشد؛ تحویل به IP و رمز (root_pass) نیاز دارد.
        # ساخت تراکنشی: شکست/مهلت → سرور نیمه‌ساخته حذف شود تا بیل نخورد.
        try:
            # سقف انتظار کوتاه‌تر شد تا برگشتِ وجهِ سرورِ ناموفق ۲۰ دقیقه طول
            # نکشد: ۴۸۰s عادی، ۹۰۰s با برنامه‌ی مارکت‌پلیس (نصب طولانی‌تر). سرورِ
            # سالم معمولاً در ۲ تا ۵ دقیقه on می‌شود؛ اگر تا این سقف on نشد لغوِ
            # سایلنت + برگشت کامل وجه. no_paidِ پایدار هم زودتر (۲۴۰s) شکست می‌دهد.
            fresh = await self._wait_status(
                str(server_id), {"on"},
                timeout_s=(900 if software_id else 480), need_root_pass=True,
                fail_on={"blocked", "permanent_blocked"})
        except Exception:
            # چک نهاییِ نجات: ممکن است سرور واقعاً ساخته شده باشد ولی poll وسط راه
            # خطا/تایم‌اوت خورده باشد یا وضعیت لحظه‌ای no_paid مانده باشد (E2E
            # 2026-07-25: سرور در پنل کامل بود ولی ربات لغو کرد). معیارِ «سرورِ
            # واقعی» = داشتنِ root_pass (نصبِ OS تمام شده)؛ چنین سروری را هرگز
            # حذف نمی‌کنیم — no_paid فقط تسویه‌ی بیلینگ است و چون موجودی چک شده
            # خودش روشن می‌شود. تحویلش بده نه حذف.
            try:
                chk = await self._get_server_raw(str(server_id))
                if chk.get("root_pass"):
                    self.last_root_password = chk.get("root_pass")
                    info = self._server_info(chk)
                    if not info.ip_address:
                        info = await self._ensure_public_ip(str(server_id), info)
                    info.extra_data["username"] = "Administrator" \
                        if (chk.get("os") or {}).get("name") == "windows" else "root"
                    return info
            except Exception:
                pass
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
        خوانده و در last_root_password گذاشته می‌شود.

        ⚠️ دام تایمینگ (E2E 2026-07-24): بلافاصله بعد از PATCH، سرور چند ثانیه
        هنوز status=on با رمز قدیمی گزارش می‌دهد — اگر همان لحظه «on» را موفقیت
        بگیریم، رمز کهنه تحویل کاربر می‌شود. سه‌فازی صبر می‌کنیم."""
        sid = int(server_id)
        old = (await self._get_server_raw(server_id)).get("root_pass")
        resp = await self._request("PATCH", f"/api/v1/servers/{sid}",
                                   json={"os_id": int(os_id)}, timeout=60)
        patched_pass = ((resp or {}).get("server") or {}).get("root_pass")

        # فاز ۱: صبر تا واقعاً وارد فاز نصب شود (خروج از on) یا رمز عوض شود — تا ۳ دقیقه
        entry_deadline = asyncio.get_event_loop().time() + 180
        while asyncio.get_event_loop().time() < entry_deadline:
            await asyncio.sleep(4)
            try:
                srv = await self._get_server_raw(server_id)
            except RuntimeError:
                continue
            st = (srv.get("status") or "").lower()
            if st != "on":
                break   # وارد reinstalling/turning_off/... شد
            np_ = srv.get("root_pass")
            if np_ and np_ != old:
                break   # رمز عوض شده = کار انجام شده

        # فاز ۲: صبر تا پایان نصب و برگشتن به on
        fresh = await self._wait_status(server_id, {"on"},
                                        timeout_s=900, need_root_pass=True)
        new_pass = fresh.get("root_pass")

        # فاز ۳: اگر رمز هنوز همان قدیمی است، مهلت کوتاه برای نشستن رمز تازه
        grace = asyncio.get_event_loop().time() + 90
        while new_pass == old and asyncio.get_event_loop().time() < grace:
            await asyncio.sleep(5)
            try:
                srv = await self._get_server_raw(server_id)
            except RuntimeError:
                continue
            if srv.get("root_pass"):
                new_pass = srv["root_pass"]

        self.last_root_password = new_pass or patched_pass or old
        return True

    async def suspend_server(self, server_id: str) -> bool:
        # تایم‌وب suspend ندارد → خاموش‌کردن (توجه: بیل ادامه دارد؛ قطع فقط با حذف)
        return await self.stop_server(server_id)

    async def unsuspend_server(self, server_id: str) -> bool:
        return await self.start_server(server_id)

    async def get_traffic(self, server_id: str) -> float:
        # ترافیک حجمی گزارش/محدود نمی‌شود (bandwidth = سرعت کانال) → همیشه صفر
        return 0.0

    async def _account_discount(self) -> float:
        """درصد تخفیف اکانت (finances.discount_percent). قیمتِ preset در API
        «بدون تخفیف» است ولی بیلینگ واقعی با تخفیف حساب می‌شود — پس قیمت خرید
        ما باید تخفیف را اعمال کند تا با فاکتور واقعی تایم‌وب یکی شود."""
        try:
            fin = (await self._request(
                "GET", "/api/v1/account/finances")).get("finances") or {}
            d = float(fin.get("discount_percent") or 0)
            return d if 0 <= d < 100 else 0.0
        except Exception:
            return 0.0

    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        """تعرفه‌ها (presets) با «قیمت خرید» به روبل — قیمت ماهانه؛ ساعتی = ÷۷۲۰.
        قیمت با تخفیفِ اکانت (اگر باشد) تعدیل می‌شود تا با فاکتور واقعی بخورد."""
        data = await self._request("GET", "/api/v1/presets/servers")
        discount = await self._account_discount()
        disc_mult = 1.0 - discount / 100.0
        plans: list[PlanInfo] = []
        for p in data.get("server_presets") or []:
            loc = p.get("location") or ""
            if location and loc != location:
                continue
            ram_mb = int(p.get("ram") or 0)
            disk_gb = int(p.get("disk") or 0) // 1024
            monthly = round(float(p.get("price") or 0) * disc_mult, 2)
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

    async def list_software(self) -> list[dict]:
        """برنامه‌های مارکت‌پلیس (ПО) — با لیست OSهای مجاز هر برنامه (os_ids)
        و حداقل منابع (requirements). مرتب بر اساس محبوبیت (installations)."""
        data = await self._request("GET", "/api/v1/software/servers")
        out = []
        for s in data.get("servers_software") or []:
            if not s.get("id") or not s.get("name"):
                continue
            out.append({
                "id": str(s.get("id")),
                "name": s.get("name") or "",
                "os_ids": [str(i) for i in (s.get("os_ids") or [])],
                "installations": int(s.get("installations") or 0),
                "requirements": s.get("requirements") or {},
            })
        out.sort(key=lambda x: (-x["installations"], x["name"].lower()))
        return out

    async def list_os_templates(self, location: Optional[str] = None,
                                only_ids: Optional[set] = None) -> list[dict]:
        """OSها (سراسری — per-region نیست). خلوت‌سازی مثل جیکور: ubuntu و
        windows همه‌ی نسخه‌ها؛ بقیه فقط جدیدترین. bitrix/brainycp (لایسنس‌دار
        بدون قیمت در API) حذف. min_disk به GB برای گِیت دیسک پلن در فلوی خرید
        (واحد requirements در اسپک مبهم است — MB فرض شده).

        only_ids: فقط همین IDها برگردند (OSهای مجازِ یک برنامه‌ی مارکت‌پلیس) —
        در این حالت خلوت‌سازی اعمال نمی‌شود تا نسخه‌ی سازگارِ برنامه حذف نشود."""
        data = await self._request("GET", "/api/v1/os/servers")
        result = []
        for os_ in data.get("servers_os") or []:
            name = (os_.get("name") or "").lower()
            if only_ids is not None:
                if str(os_.get("id")) not in only_ids:
                    continue
            elif name in _OS_EXCLUDED:
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
        if only_ids is not None:
            # لیست OSهای مجاز برنامه — بدون خلوت‌سازی، فقط مرتب‌سازی
            result.sort(key=lambda o: (_OS_PRIORITY.get(o["_flavor"], 5),
                                       o["_flavor"], -o["_ver"]))
            return result
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
        """لوکیشن‌هایی که تعرفه دارند — لیبل شهر از description خودِ تعرفه‌ها
        (منبع واحد با فلوی خرید تا ادمین و کاربر تناقض نداشته باشند).
        اگر description شهر نداشت، LOC_LABELS، بعد کد خام."""
        data = await self._request("GET", "/api/v1/presets/servers")
        by_loc: dict[str, list] = {}
        for p in data.get("server_presets") or []:
            loc = p.get("location")
            if loc:
                by_loc.setdefault(loc, []).append(p)
        out = []
        for loc in sorted(by_loc):
            # شهرِ نمایندهِ لوکیشن — کد لوکیشن هم پاس داده می‌شود تا نگاشت
            # _LOC_CITY برای لوکیشن‌های بدون شهرِ description (ru-1→SPB) فعال شود
            from collections import Counter
            cities = Counter(city_from_preset(p, loc) for p in by_loc[loc])
            cities.pop("?", None)
            city = cities.most_common(1)[0][0] if cities else LOC_LABELS.get(loc, loc)
            out.append({"id": loc, "slug": loc, "display_name": city,
                        "count": len(by_loc[loc])})
        return out

    async def count_servers(self) -> int:
        data = await self._request("GET", "/api/v1/servers",
                                   params={"limit": "1", "offset": "0"})
        return int(((data.get("meta") or {}).get("total")) or 0)

    async def ping(self) -> bool:
        await self._request("GET", "/api/v1/account/status")
        return True

    async def get_balance(self) -> Optional[float]:
        """موجودی فعلیِ اکانت تایم‌وب به روبل — برای هشدارِ موجودی کم.
        در خطا None برمی‌گرداند تا هشدار الکی زده نشود."""
        try:
            fin = (await self._request(
                "GET", "/api/v1/account/finances")).get("finances") or {}
            return float(fin.get("balance") or 0)
        except Exception:
            return None

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
