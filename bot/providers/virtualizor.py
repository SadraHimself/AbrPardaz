"""Virtualizor KVM API client."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

from .base import BaseProvider, CreateServerParams, PlanInfo, ServerInfo


def _encode_form(params: dict) -> str:
    """Build application/x-www-form-urlencoded keeping [ ] unencoded so PHP
    parses space[0][size]=20 as $_POST['space'][0]['size']."""
    from urllib.parse import quote
    parts = []
    for k, v in params.items():
        ek = quote(str(k), safe="[]")
        ev = quote(str(v), safe="")
        parts.append(f"{ek}={ev}")
    return "&".join(parts)


class VirtualizorProvider(BaseProvider):

    def __init__(self, panel_url: str, api_key: str, api_pass: str,
                 virt_type: str = "kvm"):
        self.panel_url = panel_url.rstrip("/")
        self.api_key = api_key
        self.api_pass = api_pass
        # نوع مجازی‌سازی این پنل — قالب‌های OS بر همین اساس فیلتر می‌شوند
        self.virt_type = (virt_type or "kvm").strip().lower()

    async def _request(
        self,
        act: str,
        params: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> dict:
        import json as _json

        # adminapipass = plain password (confirmed live: MD5-hashed form → Access Denied).
        url_params: dict = {
            "adminapikey": self.api_key,
            "adminapipass": self.api_pass,
            "act": act,
            "api": "json",
        }
        if query:
            url_params.update({k: str(v) for k, v in query.items()})

        form_body = _encode_form(dict(params) if params else {})
        connector = aiohttp.TCPConnector(ssl=False)
        # connect=5 زیادی سخت‌گیر بود و پنل‌های کمی کند «Connection timeout» می‌دادند
        timeout = aiohttp.ClientTimeout(total=35, connect=12)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        candidates = [self.panel_url]
        if self.panel_url.startswith("http://"):
            candidates.append(self.panel_url.replace("http://", "https://", 1))
        elif self.panel_url.startswith("https://"):
            candidates.append(self.panel_url.replace("https://", "http://", 1))

        last_error = None
        async with aiohttp.ClientSession(connector=connector) as session:
            for base_url in candidates:
                url = f"{base_url}/index.php"
                try:
                    async with session.post(
                        url, params=url_params, data=form_body, headers=headers,
                        timeout=timeout, allow_redirects=False,
                    ) as resp:
                        if resp.status in (301, 302, 303):
                            location = resp.headers.get("Location", "")
                            if "login" in location:
                                raise RuntimeError(
                                    "API credentials رد شد — Configuration → Admin API → "
                                    "Enable API را فعال کنید و Key/Pass را مجدداً کپی کنید"
                                )
                            last_error = f"ریدایرکت {resp.status} به: {location}"
                            continue
                        raw = await resp.text()
                        if not raw or not raw.strip():
                            last_error = "پنل پاسخ خالی برگرداند"
                            continue
                        if raw.strip().startswith("<") or "window.location" in raw:
                            last_error = f"پنل HTML برگرداند — {base_url}"
                            continue
                        try:
                            data = _json.loads(raw)
                        except _json.JSONDecodeError:
                            last_error = f"پاسخ JSON نیست: {raw[:150]}"
                            continue
                        # Unknown act= values silently return the admin dashboard (HTTP 200, no error key).
                        # Treat this as a hard error so bad action names fail loudly.
                        if data.get("title") == "Admin Panel":
                            raise RuntimeError(
                                f"Virtualizor: action '{act}' not recognized — "
                                "returned admin dashboard instead of data"
                            )
                        error = data.get("error") or data.get("errors")
                        if not error and data.get("fatal_error_text"):
                            error = f"{data.get('fatal_error_heading', 'Fatal Error')}: {data['fatal_error_text']}"
                        if error:
                            raise RuntimeError(f"Virtualizor: {error}")
                        return data
                except aiohttp.ClientError as e:
                    last_error = str(e)
                    continue

        raise RuntimeError(last_error or "اتصال به Virtualizor ناموفق بود")

    # Power-state vocabularies — Virtualizor reports the running state as either a
    # number (1/0/2) OR a string ("online"/"offline"/...). The old code only matched
    # the literal "1", so a string like "online" was read as "0" → a running VM showed
    # as offline/red. These sets make the check value-format agnostic.
    _ON_WORDS = {"1", "online", "running", "on", "up", "active", "started", "poweron", "power on"}
    _OFF_WORDS = {"0", "2", "offline", "off", "down", "stopped", "shutoff", "shut off",
                  "halted", "poweroff", "power off", "suspended", "disabled"}

    @classmethod
    def _running_flag(cls, vs: dict) -> str:
        """Resolve the live power state to "1" (running) / "0" (off) / "" (unknown).

        Checks every key Virtualizor might use for power state, accepting both
        numeric and string forms."""
        for key in ("vps_status", "machine_status", "vm_status", "power_status", "status"):
            if key not in vs:
                continue
            raw = vs.get(key)
            if raw is None:
                continue
            s = str(raw).strip().lower()
            if not s:
                continue
            if s in cls._ON_WORDS:
                return "1"
            if s in cls._OFF_WORDS:
                return "0"
            try:
                return "1" if int(float(s)) == 1 else "0"
            except (ValueError, TypeError):
                continue
        return ""

    @classmethod
    def _vs_status(cls, vs: dict) -> str:
        if str(vs.get("suspended", "0")) == "1":
            return "suspended"
        running = cls._running_flag(vs)
        if running == "1":
            return "active"
        if str(vs.get("locked", "0")) == "1":
            return "building"
        return "off"

    def _parse_vs(self, vs: dict) -> ServerInfo:
        ips = vs.get("ips") or {}
        first_ip = next(iter(ips.values()), None) if isinstance(ips, dict) else None
        return ServerInfo(
            provider_server_id=str(vs.get("vpsid", "")),
            name=vs.get("hostname", ""),
            status=self._vs_status(vs),
            ip_address=first_ip,
            ram=int(vs.get("ram", 0) or 0),
            cpu=int(vs.get("cores", 0) or 0),
            disk=int(vs.get("space", 0) or 0),
            bandwidth=int(vs.get("bandwidth", 0) or 0),
            traffic_used_gb=float(vs.get("used_bandwidth", 0) or 0),
            os_name=vs.get("os_name"),
            extra_data={
                "vpsid": vs.get("vpsid"),
                "node": vs.get("server_name"),
                "serid": vs.get("serid"),
                "machine_status": (self._running_flag(vs) or "1"),
                "locked": str(vs.get("locked", "0")),
            },
        )

    # ── BaseProvider implementation ───────────────────────────────────────────

    async def create_server(self, params: CreateServerParams) -> ServerInfo:
        import asyncio as _asyncio

        node_id = params.extra.get("node_id") or params.extra.get("serid")
        if not node_id:
            node_id = await self._get_default_node()

        st_uuid = params.extra.get("st_uuid") or await self.get_primary_storage_uuid()
        disk_gb = params.extra.get("disk", 20)

        # Only fetch a free IP when there's no plan — plans have their own IP pool
        # and Virtualizor assigns from it automatically via num_ips.
        _plan_id_early = str(params.plan_id).strip()
        _has_plan_early = _plan_id_early.isdigit() and int(_plan_id_early) > 0
        ip_to_assign = None
        if node_id is not None and not _has_plan_early:
            try:
                free_ips = await self.list_ips(int(node_id))
                if free_ips:
                    ip_to_assign = free_ips[0]
            except Exception:
                pass

        # os_id from FSM; fall back to plan/provider extra_data if FSM value is non-numeric
        os_id_val = params.os_id
        if not str(os_id_val).strip().isdigit():
            os_id_val = params.extra.get("osid", os_id_val)
        # قالب‌های OS روی پنل حذف/جایگزین می‌شوند و osidِ پین‌شده‌ی پلن کهنه
        # می‌ماند → خطای مبهم موقع ساخت. اینجا با لیست زنده اعتبارسنجی می‌شود.
        try:
            _live = {str(o.get("id")) for o in await self.list_os_templates()}
            if _live and str(os_id_val).strip() not in _live:
                raise RuntimeError(
                    "سیستم‌عامل انتخاب‌شده روی سرور موجود نیست — لطفاً دوباره از "
                    "لیست انتخاب کنید یا با پشتیبانی تماس بگیرید.")
        except RuntimeError:
            raise
        except Exception:
            pass          # خطای شبکه در اعتبارسنجی نباید جلوی ساخت را بگیرد

        payload: dict = {
            # Submit trigger: the documented field is `addvps=1` ("If set the vps will
            # be created" — official Create VPS docs). Without it, act=addvs only loads
            # the "Add Virtual Server" form (returns ips/ostemplates/plans) instead of
            # provisioning. `addvs=1` kept too as a harmless cross-version fallback.
            "addvps": 1,
            "addvs": 1,
            "hostname": params.name,
            "rootpass": params.extra.get("root_password", "AbrPardaz@2024"),
            "osid": os_id_val,
            "bandwidth": params.extra.get("bandwidth", 1000),
            "ram": params.extra.get("ram", 1024),
            "cores": params.extra.get("cpu", 1),
            "virt": params.extra.get("virt_type", "kvm"),
            # Fields the official Create VPS example always sends. Omitting them makes
            # Virtualizor silently re-render the add form (no error, no vs_info) instead
            # of provisioning, especially for KVM.
            "swapram": params.extra.get("swapram", 0),
            "cpu": params.extra.get("cpu_units", 1000),
            "cpu_percent": params.extra.get("cpu_percent", 100),
            "network_speed": params.extra.get("network_speed", 0),
            "num_ips6": 0,
            "num_ips6_subnet": 0,
            "vnc": 0,  # VNC disabled — the bot no longer exposes a VNC console
        }

        if node_id is not None:
            # serid is what the official Create VPS example sets (serid=0 = master node,
            # which is valid). node_select is also accepted; send both for compatibility.
            payload["serid"] = int(node_id)
            payload["node_select"] = int(node_id)

        uid_val = params.extra.get("virtualizor_uid")
        user_email = params.extra.get("user_email")
        if uid_val:
            payload["uid"] = uid_val
        elif user_email:
            payload["uid"] = 0
            payload["user_email"] = user_email
            payload["user_pass"] = params.extra.get("user_pass", user_email)

        plan_id_str = str(params.plan_id).strip()
        has_plan = plan_id_str.isdigit() and int(plan_id_str) > 0
        if has_plan:
            payload["plid"] = int(plan_id_str)
            # Do NOT set num_ips here — plans carry their own IP pool settings.
            # Setting num_ips=1 causes Virtualizor to look for IPs assigned to the
            # specific serid (localhost), which fails when the pool is "All Servers".
            # Virtualizor will assign an IP from whatever pool is configured for the plan.
        elif ip_to_assign:
            payload["ips[0]"] = ip_to_assign
        # No plan + no specific IP: let Virtualizor handle assignment automatically

        if st_uuid:
            payload["space[0][size]"] = disk_gb
            payload["space[0][st_uuid]"] = st_uuid
        else:
            payload["space[0][size]"] = disk_gb

        data = await self._request("addvs", payload)

        # NOTE: data["title"] == "Add Virtual Server" appears on BOTH success and failure
        # (it is just the page title — confirmed against the official docs, where a
        # successful response is {"title":"Add Virtual Server","error":[],"vs_info":{...}}).
        # So title is NOT a failure signal. A real failure has a non-empty `error`, which
        # _request() already raises on. Success is detected by finding a vpsid below.
        vs_info = data.get("vs_info") or {}
        vpsid = vs_info.get("vpsid") if isinstance(vs_info, dict) else None
        taskid = data.get("taskid")

        if not vpsid:
            vpsid = data.get("vpsid")
        if not vpsid:
            # This Virtualizor version returns the new vpsid directly in `done` as a
            # numeric string (e.g. "201"), with the config echoed under `newvs` — there
            # is no `vs_info`. Older/other versions put it in a `done` dict. Handle both.
            done_raw = data.get("done")
            if isinstance(done_raw, dict):
                vpsid = done_raw.get("vpsid")
                taskid = taskid or done_raw.get("taskid")
            elif isinstance(done_raw, (int, float)) and done_raw:
                vpsid = int(done_raw)
            elif isinstance(done_raw, str) and done_raw.strip().isdigit() and int(done_raw) > 0:
                vpsid = int(done_raw)
        if not vpsid:
            newvs = data.get("newvs")
            if isinstance(newvs, dict):
                vpsid = newvs.get("vpsid") or newvs.get("vps_id")
        if not vpsid:
            addvs_raw = data.get("addvs")
            if isinstance(addvs_raw, dict):
                vpsid = addvs_raw.get("vpsid")
                taskid = taskid or addvs_raw.get("taskid")
            elif isinstance(addvs_raw, (int, float)) and addvs_raw:
                vpsid = int(addvs_raw)

        if not vpsid and taskid:
            for _ in range(18):
                await _asyncio.sleep(5)
                try:
                    t_data = await self._request("tasks", {"actid": taskid})
                    tasks = t_data.get("tasks", {})
                    for task in tasks.values():
                        tid_vps = task.get("vpsid")
                        if tid_vps and str(tid_vps) != "0":
                            vpsid = tid_vps
                            break
                    if vpsid:
                        break
                except Exception:
                    pass

        if not vpsid:
            import json as _json2
            # Strip the bulky add-form payload so the meaningful keys (done, error,
            # vs_info, taskid, ...) are actually visible instead of being truncated away.
            _form_noise = {
                "ips", "ips6", "ips6_subnet", "ips_int", "ostemplates", "oslist",
                "plans", "storage", "servers", "globals", "mediagroups", "mediadetails",
                "vpsgroups", "iso", "isos", "scripts", "groups", "users",
            }
            meaningful = {k: v for k, v in data.items() if k not in _form_noise}
            snippet = _json2.dumps(meaningful, ensure_ascii=False)[:600]
            raise RuntimeError(
                "Virtualizor VPS ساخته نشد (فرم دوباره برگشت، بدون vs_info).\n"
                f"پارامترهای ارسالی: osid={payload.get('osid')!r}, "
                f"serid={payload.get('serid')!r}, ram={payload.get('ram')!r}, "
                f"cores={payload.get('cores')!r}, uid={payload.get('uid')!r}, "
                f"ip={payload.get('ips[0]') or ('num_ips=' + str(payload.get('num_ips')))!r}, "
                f"st_uuid={payload.get('space[0][st_uuid]')!r}\n"
                f"پاسخ Virtualizor: {snippet}"
            )

        # Capture the Virtualizor uid this create used/created so the caller can persist
        # it and reuse the same account next time (uid=0 inline-creation makes a NEW user
        # otherwise). newvs.uid is the freshly created user; else fall back to a real uid
        # we sent.
        newvs = data.get("newvs") if isinstance(data.get("newvs"), dict) else {}
        sent_uid = payload.get("uid")
        created_uid = newvs.get("uid") or (sent_uid if str(sent_uid or "0") not in ("0", "") else None)

        # The VPS exists now but is locked/offline while its OS-install task runs, so a
        # full lookup may not be queryable yet. Don't fail the whole creation over that —
        # return what we already know so the server (and its vpsid) is recorded. Status
        # sync later moves it BUILDING → ACTIVE once the build finishes.
        try:
            info = await self.get_server(str(vpsid))
            if created_uid and not info.extra_data.get("uid"):
                info.extra_data["uid"] = created_uid
            return info
        except Exception:
            return ServerInfo(
                provider_server_id=str(vpsid),
                name=params.name,
                status="building",
                ip_address=ip_to_assign,
                ram=int(payload.get("ram", 0) or 0),
                cpu=int(payload.get("cores", 0) or 0),
                disk=int(disk_gb or 0),
                extra_data={"vpsid": str(vpsid), "uid": created_uid},
            )

    async def get_server(self, server_id: str) -> ServerInfo:
        data = await self._request("vs", query={"vpsid": server_id})
        vs_list = data.get("vs", {}) or {}
        if not vs_list:
            raise RuntimeError(f"Server {server_id} not found")
        vs = dict(vs_list.get(str(server_id)) or next(iter(vs_list.values())))

        # The plain listvs `machine_status` is stale/uncomputed (it read "0" for a VM
        # that was actually online). The LIVE power state comes from a separate call,
        # `act=vs&vs_status=<vpsid>` → {"<vpsid>":{"status":N}} where N: 1=on, 0=off,
        # 2=suspended. Overlay it so _parse_vs reports the real state.
        live = await self._get_live_status(server_id)
        if live is not None:
            vs["machine_status"] = "1" if live == 1 else "0"
            if live == 2:
                vs["suspended"] = "1"

        return self._parse_vs(vs)

    async def _get_live_status(self, server_id: str) -> Optional[int]:
        """Live power state via act=vs&vs_status=<vpsid>. Returns 1=on/0=off/2=suspended,
        or None if it couldn't be determined."""
        try:
            data = await self._request("vs", query={"vs_status": server_id})
        except Exception:
            return None
        # The status map may sit at the top level keyed by vpsid, or under "status"/"vs_status".
        for container in (data.get("status"), data.get("vs_status"), data):
            if not isinstance(container, dict):
                continue
            entry = container.get(str(server_id))
            if isinstance(entry, dict) and entry.get("status") is not None:
                try:
                    return int(entry["status"])
                except (ValueError, TypeError):
                    continue
            if isinstance(entry, (int, str)) and str(entry).strip() not in ("", "None"):
                try:
                    return int(entry)
                except (ValueError, TypeError):
                    continue
        return None

    async def start_server(self, server_id: str) -> bool:
        data = await self._request("vs", query={"action": "start", "vpsid": server_id})
        return bool(data.get("done"))

    async def stop_server(self, server_id: str) -> bool:
        data = await self._request("vs", query={"action": "stop", "vpsid": server_id})
        return bool(data.get("done"))

    async def restart_server(self, server_id: str) -> bool:
        data = await self._request("vs", query={"action": "restart", "vpsid": server_id})
        return bool(data.get("done"))

    async def delete_server(self, server_id: str) -> bool:
        data = await self._request("vs", query={"delete": server_id})
        return bool(data.get("done"))

    async def suspend_server(self, server_id: str) -> bool:
        data = await self._request("vs", query={"suspend": server_id})
        return bool(data.get("done"))

    async def unsuspend_server(self, server_id: str) -> bool:
        data = await self._request("vs", query={"unsuspend": server_id})
        return bool(data.get("done"))

    async def get_traffic(self, server_id: str) -> float:
        data = await self._request("vs", query={"vpsid": server_id})
        vs_list = data.get("vs", {}) or {}
        vs = vs_list.get(str(server_id)) or (next(iter(vs_list.values())) if vs_list else {})
        return float(vs.get("used_bandwidth", 0) or 0)

    async def rebuild_server(self, server_id: str, os_id: str, rootpass: str = "") -> bool:
        # Rebuild is its OWN admin action `act=rebuild`. The PHP SDK passes everything
        # (including vpsid) in the POST body — putting vpsid only in the URL query made
        # Virtualizor reject it with "Please choose a VPS / invalid VPS ID". So vpsid
        # goes in the body. Params: vpsid, osid (NOT newos!), newpass, conf, reos=1.
        # Success → {"title":"Rebuild Virtual Server","done":1,...}.
        payload: dict = {"vpsid": server_id, "reos": 1, "osid": os_id}
        if rootpass:
            payload["newpass"] = rootpass
            payload["conf"] = rootpass          # confirm-password field must equal newpass
        data = await self._request("rebuild", payload, query={"vpsid": server_id})
        done_val = data.get("done")
        return bool(done_val) if not isinstance(done_val, dict) else bool(done_val.get("done"))

    async def change_root_password(self, server_id: str, new_password: str) -> bool:
        # vpsid in URL query; editvps=1 submit trigger + rootpass in POST body
        data = await self._request("managevps", {"editvps": 1, "rootpass": new_password}, query={"vpsid": server_id})
        done_val = data.get("done")
        return bool(done_val) if not isinstance(done_val, dict) else bool(done_val.get("done"))

    async def list_nodes(self) -> list[dict]:
        data = await self._request("servers")
        raw = data.get("servs") or data.get("servers") or []
        if isinstance(raw, dict):
            items = list(raw.values())
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        result = []
        for s in items:
            result.append({
                "serid": int(s.get("serid", 0)),
                "name": s.get("server_name", ""),
                "ip": s.get("ip", ""),
                "online": str(s.get("status", "1")) == "1",
                "os": s.get("os", ""),
                "cpu": s.get("cpu", "") or s.get("cpu_model", ""),
                "cpu_load": s.get("cpu_load", "") or s.get("load", ""),
                "ram_total_mb": float(s.get("total_ram", s.get("ram", 0)) or 0),
                "ram_used_mb": float(s.get("ram", 0) or 0),
                "hdd": s.get("hdd", "") or s.get("disks", ""),
                "virt_type": s.get("virt", ""),
            })
        return result

    async def _get_default_node(self) -> Optional[int]:
        try:
            nodes = await self.list_nodes()
            online = [n for n in nodes if n["online"]]
            if online:
                return online[0]["serid"]
            if nodes:
                return nodes[0]["serid"]
        except Exception:
            pass
        return None

    async def list_ips(self, serid: int = 0) -> list[str]:
        # serid goes in the URL query string per the API reference table
        data = await self._request("ips", query={"serid": serid})
        ips_raw = data.get("ips") or data.get("freeips") or {}

        def _is_free(v: dict) -> bool:
            # Free IPs have vpsid="0" (string zero), assigned ones have a real vpsid
            return str(v.get("vpsid", "0")) in ("0", "")

        if isinstance(ips_raw, dict):
            return [v.get("ip", k) for k, v in ips_raw.items() if isinstance(v, dict) and _is_free(v)]
        if isinstance(ips_raw, list):
            return [i if isinstance(i, str) else i.get("ip", "") for i in ips_raw]
        return []

    async def list_storages(self) -> list[dict]:
        data = await self._request("storage")
        storage_raw = data.get("storage", {})
        result = []
        for stid, st in storage_raw.items():
            result.append({
                "stid": str(stid),
                "st_uuid": st.get("st_uuid", ""),
                "name": st.get("name", ""),
                "free_gb": float(st.get("free", 0)),
                "total_gb": float(st.get("size", 0)),
                "type": st.get("type", ""),
                "is_primary": str(st.get("primary_storage", "0")) == "1",
            })
        return result

    async def get_primary_storage_uuid(self) -> Optional[str]:
        try:
            storages = await self.list_storages()
            primary = next((s for s in storages if s["is_primary"]), None)
            if not primary and storages:
                primary = storages[0]
            return primary["st_uuid"] if primary else None
        except Exception:
            return None

    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        data = await self._request("plans")
        plans_raw = data.get("plans") or data.get("plansdata") or {}
        if not isinstance(plans_raw, dict):
            plans_raw = {}
        plans = []
        for pid, plan in plans_raw.items():
            plans.append(PlanInfo(
                provider_plan_id=str(pid),
                name=plan.get("plan_name", ""),
                ram=int(plan.get("ram", 0) or 0),
                cpu=int(plan.get("cores", 0) or 0),
                disk=int(plan.get("space", 0) or 0),
                bandwidth=int(plan.get("bandwidth", 0) or 0),
            ))
        return plans

    async def list_os_templates(self) -> list[dict]:
        """قالب‌های OS قابل استفاده روی این پنل.

        ⚠️ دو نکته‌ای که قبلاً لیست را خالی/نامعتبر می‌کرد:
          • کلیدِ پاسخ در این نسخه‌ها `oses` است نه `ostemplates` (هر دو + `oslist`
            پشتیبانی می‌شود). با کلید اشتباه لیست خالی می‌شد و ساختِ سرور با
            osid خالی خطا می‌داد.
          • هر سیستم‌عامل به‌ازای هر نوع مجازی‌سازی osid جدا دارد (`type`: kvm،
            proxk، vzk، …). فرستادن osidِ نوعِ دیگر = خطای ساخت. پس فقط قالب‌های
            هم‌نوع با مجازی‌سازیِ این اکانت برگردانده می‌شوند.
        """
        data = await self._request("ostemplates")
        os_list = {}
        for key in ("oses", "ostemplates", "oslist"):
            val = data.get(key)
            if isinstance(val, dict) and val:
                os_list = val
                break
        want = (self.virt_type or "kvm").strip().lower()
        out, fallback = [], []
        for oid, tmpl in os_list.items():
            if not isinstance(tmpl, dict):
                continue
            if str(tmpl.get("active", "1")) == "0":
                continue
            row = {
                "id": str(tmpl.get("osid") or oid),
                "name": tmpl.get("name") or tmpl.get("filename") or "",
                "type": str(tmpl.get("type") or tmpl.get("Nvirt") or "").lower(),
                "distro": tmpl.get("distro") or "",
            }
            if not row["name"]:
                continue
            (out if row["type"] == want else fallback).append(row)
        # اگر هیچ قالبی دقیقاً هم‌نوع نبود، به‌جای لیست خالی همه را بده تا
        # فلوی خرید قفل نشود (پنل خودش نامعتبر بودن را گزارش می‌دهد)
        return out or fallback

    async def list_users(self) -> list[dict]:
        data = await self._request("users")
        users_raw = data.get("users", {})
        result = []
        for uid, u in (users_raw.items() if isinstance(users_raw, dict) else []):
            result.append({
                "uid": str(uid),
                "email": u.get("email", ""),
                "username": u.get("username", u.get("email", "")),
                "type": u.get("type", ""),
            })
        return result

    async def create_user(self, email: str, password: str) -> int:
        username = email.split("@")[0]
        data = await self._request("adduser", {
            "adduser": 1,
            "priority": 0,
            "newemail": email,
            "newpass": password,
            "fname": username,
            "lname": "User",
        })
        uid = data.get("done") or data.get("uid") or (data.get("adduser") or {}).get("uid")
        if not uid:
            raise RuntimeError(f"Virtualizor adduser failed: {list(data.keys())}")
        return int(uid)

    async def find_user_by_email(self, email: str) -> Optional[int]:
        try:
            users = await self.list_users()
            for u in users:
                if u.get("email", "").lower() == email.lower():
                    return int(u["uid"])
        except Exception:
            pass
        return None

    async def edit_server(self, server_id: str, ram: Optional[int] = None,
                          cpu: Optional[int] = None, disk: Optional[int] = None) -> bool:
        # vpsid in URL query; editvps=1 is the submit trigger (same pattern as addvps=1 for addvs)
        payload: dict = {"editvps": 1}
        if ram is not None:
            payload["ram"] = ram
        if cpu is not None:
            payload["cores"] = cpu
        if disk is not None:
            payload["space"] = disk
        data = await self._request("managevps", payload, query={"vpsid": server_id})
        done_val = data.get("done")
        return bool(done_val) if not isinstance(done_val, dict) else bool(done_val.get("done"))

    async def apply_resources(self, server_id: str, ram: Optional[int] = None,
                              cores: Optional[int] = None,
                              disk: Optional[int] = None) -> dict:
        """ارتقاء منابعِ یک VPS موجود — **بدون هیچ تغییری در ترافیک**.

        ⚠️ چرا «اعمال پلن» (plid) ممنوع است: اگر پلن روی VPS اعمال شود،
        ویرچولایزور سهمیه و مصرفِ ترافیک را هم از نو می‌نویسد، یعنی مشتری‌ای که
        ۱۵ ترابایت مصرف کرده ناگهان دوباره ۲۰ ترابایتِ دست‌نخورده می‌گیرد.
        پس فقط فیلدهای منابع فرستاده می‌شوند و `plid`/`bandwidth` هرگز.

        محافظ‌ها:
          • دیسک فقط بزرگ می‌شود؛ کوچک‌کردن = نابودی داده → نادیده گرفته می‌شود.
          • آرایه‌ی `space` فقط وقتی ارسال می‌شود که بتوان **همه‌ی** دیسک‌های
            موجود را شمرد (مستند: هر دیسکی که در آرایه نباشد حذف می‌شود).
            اگر شمردن قطعی نبود، دیسک دست‌نخورده می‌ماند و در خروجی گزارش می‌شود.
          • بعد از اعمال، سهمیه‌ی ترافیک بازخوانی و اگر عوض شده بود برگردانده
            می‌شود (پنل ممکن است بی‌خبر آن را تغییر دهد).

        خروجی: {"ram":int|None, "cores":int|None, "disk":int|None,
                 "disk_skipped":bool, "bandwidth_restored":bool}
        """
        def _i(v) -> int:
            try:
                return int(float(v or 0))
            except (TypeError, ValueError):
                return 0

        before = await self._vs_row(server_id)
        if not before:
            # قبل از هر نوشتنی خطا بده تا caller بتواند تمیز وجه را برگرداند —
            # «خوانده نشد» هرگز نباید با «اعمال شد» اشتباه گرفته شود
            raise RuntimeError("خواندن وضعیت فعلی سرویس از پنل ممکن نشد.")
        bw_before = _i(before.get("bandwidth"))
        used_before = float(before.get("used_bandwidth") or 0)
        cur_ram, cur_cores = _i(before.get("ram")), _i(before.get("cores"))

        payload: dict = {"vpsid": server_id, "editvps": 1}
        want: dict = {}
        # فقط-افزایشی: منابع واقعیِ VM ممکن است از پلن بیشتر باشد (ویرایش دستیِ
        # ادمین) — نوشتن کورکورانه‌ی مقدار پلن آن VM را «تنزل» می‌داد
        if ram and int(ram) > cur_ram:
            payload["ram"] = want["ram"] = int(ram)
        if cores and int(cores) > cur_cores:
            payload["cores"] = want["cores"] = int(cores)

        disk_skipped = False
        disk_req = 0
        if disk:
            entries = await self._vps_disks(server_id, before)
            if not entries:
                # شمارشِ قطعیِ دیسک‌ها ممکن نشد → آرایه‌ی space فرستاده نمی‌شود
                # (هر دیسکی که در آرایه نباشد توسط پنل حذف می‌شود = نابودی داده)
                disk_skipped = True
                logger.warning("apply_resources: disks not enumerable for vps %s — "
                               "disk untouched", server_id)
            else:
                prim_idx = next((i for i, d in enumerate(entries) if d["primary"]), 0)
                prim_size = entries[prim_idx]["size"]
                if int(disk) > prim_size:
                    disk_req = int(disk)
                    for idx, d in enumerate(entries):
                        # فقط دیسکِ اصلی بزرگ می‌شود؛ بقیه عیناً بازفرستاده
                        # می‌شوند تا حذف نشوند
                        payload[f"space[{idx}][size]"] = disk_req if idx == prim_idx else d["size"]
                        if d["st_uuid"]:
                            payload[f"space[{idx}][st_uuid]"] = d["st_uuid"]
                        if d["bus_driver"]:
                            payload[f"space[{idx}][bus_driver]"] = d["bus_driver"]
                            payload[f"space[{idx}][bus_driver_num]"] = d["bus_driver_num"]

        if len(payload) <= 2:      # فقط vpsid+editvps ⇒ چیزی برای تغییر نیست
            return {"ram": cur_ram, "cores": cur_cores, "disk": _i(before.get("space")),
                    "disk_req": 0, "disk_skipped": disk_skipped, "changed": False,
                    "unverified": [], "traffic_alert": ""}

        await self._request("managevps", payload, query={"vpsid": server_id})

        # ── از این نقطه به بعد نوشتن روی پنل انجام شده: هیچ استثنایی نباید بالا
        #    برود، وگرنه caller وجه را برمی‌گرداند در حالی که منابع اعمال شده‌اند
        result = {"ram": cur_ram, "cores": cur_cores, "disk": _i(before.get("space")),
                  "disk_req": disk_req, "disk_skipped": disk_skipped, "changed": True,
                  "unverified": list(want.keys()), "traffic_alert": ""}
        after: dict = {}
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2)
            try:
                after = await self._vs_row(server_id)
            except Exception:
                after = {}
                continue
            if after:
                break
        if not after:
            logger.warning("apply_resources: verify read failed for vps %s", server_id)
            return result

        a_ram, a_cores = _i(after.get("ram")), _i(after.get("cores"))
        result["ram"], result["cores"] = a_ram or cur_ram, a_cores or cur_cores
        result["disk"] = _i(after.get("space")) or result["disk"]
        result["unverified"] = [
            k for k, v in want.items()
            if (a_ram if k == "ram" else a_cores) < v
        ]

        # ── ترافیک: نه سهمیه نه مصرف نباید عوض شده باشد ──
        bw_after = _i(after.get("bandwidth"))
        used_after = float(after.get("used_bandwidth") or 0)
        if bw_after != bw_before:
            # شامل حالت «نامحدود» (صفر) هم می‌شود: اگر VMِ نامحدود ناگهان سقف
            # گرفت باید برگردانده شود
            try:
                await self._request(
                    "managevps",
                    {"vpsid": server_id, "editvps": 1, "bandwidth": bw_before},
                    query={"vpsid": server_id})
                logger.warning("apply_resources: bandwidth %s→%s on vps %s — restored",
                               bw_before, bw_after, server_id)
            except Exception:
                logger.exception("apply_resources: bandwidth restore FAILED vps %s", server_id)
                result["traffic_alert"] = (
                    f"سهمیه‌ی ترافیک از {bw_before} به {bw_after} تغییر کرد و "
                    "بازگرداندن آن ناموفق بود")
        if used_before > 0 and used_after < used_before * 0.9:
            # شمارنده‌ی مصرف عقب رفته = ریستِ ناخواسته (قابل بازگرداندن نیست)
            logger.error("apply_resources: used_bandwidth dropped %.2f→%.2f on vps %s",
                         used_before, used_after, server_id)
            result["traffic_alert"] = (
                f"شمارنده‌ی مصرف ترافیک از {used_before:.0f} به {used_after:.0f} "
                "گیگ افت کرد")
        return result

    async def _vps_disks(self, server_id: str, vs_row: dict | None = None) -> list[dict]:
        """همه‌ی دیسک‌های یک VPS با اندازه/استوریج. لیست خالی = شمارش قطعی نشد
        (در آن حالت نباید آرایه‌ی space فرستاده شود، وگرنه دیسک حذف می‌شود)."""
        raw = None
        for src in (vs_row or {}, ):
            if isinstance(src.get("disks"), (list, dict)):
                raw = src["disks"]
                break
        if raw is None:
            try:
                mv = await self._request("managevps", {}, query={"vpsid": server_id})
                if isinstance(mv.get("disks"), (list, dict)):
                    raw = mv["disks"]
            except Exception:
                raw = None
        if raw is None:
            return []
        items = list(raw.values()) if isinstance(raw, dict) else list(raw)
        out: list[dict] = []
        for i, d in enumerate(items):
            if not isinstance(d, dict):
                return []          # شکل ناشناخته → ریسک نکن
            try:
                size = int(float(d.get("size") or 0))
            except (TypeError, ValueError):
                return []
            if size <= 0:
                return []
            out.append({
                "size": size,
                "primary": str(d.get("primary", "1" if i == 0 else "0")) in ("1", "True", "true"),
                "st_uuid": d.get("st_uuid") or d.get("uuid") or "",
                "bus_driver": d.get("bus_driver") or "",
                "bus_driver_num": d.get("bus_driver_num") if d.get("bus_driver_num") is not None else i,
            })
        if not out:
            return []          # ظرفِ خالی = شمارش قطعی نشد
        # دقیقاً یک دیسکِ اصلی: نبودش → اولی؛ بیش از یکی → فقط اولی
        # (وگرنه همه‌ی دیسک‌های primary تا اندازه‌ی کاملِ پلن بزرگ می‌شوند)
        _seen = False
        for d in out:
            if d["primary"] and not _seen:
                _seen = True
            elif d["primary"]:
                d["primary"] = False
        if not _seen:
            out[0]["primary"] = True
        return out

    async def change_ip(self, server_id: str) -> str:
        """Assign a new free IP taken ONLY from this VPS's allowed pool.

        The right source is the VPS's own Manage page `ips` list — i.e. the IPs shown
        in that VM's Network section, which Virtualizor has already scoped to the
        plan's permitted pool(s). The node-wide `act=ips` listing was wrong: it returns
        IPs from every pool on the node (other subnets the plan can't use), so the new
        IP could land outside the plan's range (old bug) or filter to nothing (the
        "No free IP" bug). Sourcing from managevps fixes both."""
        import random as _random

        # Load the Manage VPS page (no editvps) → its `ips` are this VPS's allowed pool.
        data = await self._request("managevps", {}, query={"vpsid": server_id})
        ips_raw = data.get("ips") or {}
        entries = list(ips_raw.values()) if isinstance(ips_raw, dict) else (ips_raw or [])
        entries = [e for e in entries if isinstance(e, dict) and e.get("ip")]

        def _pool(e: dict) -> str:
            return str(e.get("ippid") or e.get("ippoolid") or e.get("ipp_id") or "")

        # Current IP = the entry already assigned to this VPS.
        current = next((e for e in entries if str(e.get("vpsid", "0")) == str(server_id)), None)
        cur_pool = _pool(current) if current else ""
        cur_ip = current.get("ip") if current else None
        cur_subnet = (
            cur_ip.rsplit(".", 1)[0] if cur_ip and str(cur_ip).count(".") == 3 else None
        )

        free = [e for e in entries if str(e.get("vpsid", "0")) in ("0", "")]
        # Every entry here already belongs to the plan's allowed pool; when the pool/
        # subnet of the current IP is identifiable, prefer staying inside it.
        candidates = [e for e in free if cur_pool and _pool(e) == cur_pool]
        if not candidates and cur_subnet:
            candidates = [e for e in free if str(e.get("ip", "")).rsplit(".", 1)[0] == cur_subnet]
        if not candidates:
            candidates = free

        ip_values = [e.get("ip") for e in candidates if e.get("ip")]
        if not ip_values:
            raise RuntimeError("هیچ آی‌پی آزادی در pool مجاز این پلن یافت نشد")

        new_ip = _random.choice(ip_values)
        # vpsid in URL query; editvps=1 is the submit trigger; ips array in POST body
        resp = await self._request("managevps", {"editvps": 1, "ips[0]": new_ip}, query={"vpsid": server_id})
        done_val = resp.get("done")
        # done can be a nested dict {done: true} or a scalar true/1
        ok = bool(done_val) if not isinstance(done_val, dict) else bool(done_val.get("done"))
        if not ok:
            raise RuntimeError(f"Virtualizor IP change failed — response keys: {list(resp.keys())}")
        return new_ip

    async def _vps_ipv4s(self, server_id: str) -> list[str]:
        """IPv4های فعلیِ یک VPS از act=vs (منبع معتبر طبق مستندات: فیلد ips = ipid→ip).

        خواندن IPهای فعلی از پاسخ managevps برای همه‌ی نصب‌ها ساختار یکسان ندارد
        (باگ before=set() از همین بود که current تشخیص داده نمی‌شد)."""
        data = await self._request("vs", query={"vpsid": server_id})
        vs_list = data.get("vs", {}) or {}
        vs = vs_list.get(str(server_id)) or (next(iter(vs_list.values())) if vs_list else {})
        ips_field = vs.get("ips")
        result: list[str] = []
        if isinstance(ips_field, dict):
            result = [str(v) for v in ips_field.values() if v]
        elif isinstance(ips_field, list):
            result = [str(v) for v in ips_field if v]
        return [ip for ip in result if ip.count(".") == 3]  # فقط IPv4

    async def add_extra_ip(self, server_id: str) -> str:
        """Attach ONE additional free IP to the VPS, keeping all current IPs.

        Reads current IPs from act=vs (authoritative), picks a free IP (vpsid==0)
        from the VPS's allowed pool, then submits managevps with the FULL explicit
        ips[] list (current + new). No num_ips — that flag REPLACES the list with
        auto-assigned IPs instead of extending it (root cause of the swap bug)."""
        import random as _random

        # IPهای فعلی از منبع معتبر
        current_ips = await self._vps_ipv4s(server_id)
        before = set(current_ips)
        cur_ip = current_ips[0] if current_ips else None
        cur_subnet = cur_ip.rsplit(".", 1)[0] if cur_ip else None

        # کاندیدهای آزاد از pool مجاز همین VPS (managevps → ips با vpsid==0)
        data = await self._request("managevps", {}, query={"vpsid": server_id})
        ips_raw = data.get("ips") or {}
        entries = list(ips_raw.values()) if isinstance(ips_raw, dict) else (ips_raw or [])
        entries = [e for e in entries if isinstance(e, dict) and e.get("ip")]

        def _pool(e: dict) -> str:
            return str(e.get("ippid") or e.get("ippoolid") or e.get("ipp_id") or "")

        cur_entry = next((e for e in entries if e.get("ip") == cur_ip), None)
        cur_pool = _pool(cur_entry) if cur_entry else ""

        free = [e for e in entries if str(e.get("vpsid", "0")) in ("0", "")]
        candidates = [e for e in free if cur_pool and _pool(e) == cur_pool]
        if not candidates and cur_subnet:
            candidates = [e for e in free if str(e.get("ip", "")).rsplit(".", 1)[0] == cur_subnet]
        if not candidates:
            candidates = free
        ip_values = [e.get("ip") for e in candidates
                     if e.get("ip") and e.get("ip") not in before]
        if not ip_values:
            raise RuntimeError("هیچ آی‌پی آزادی در pool مجاز این سرور یافت نشد")

        new_ip = _random.choice(ip_values)
        logger.info("add_extra_ip vps=%s: current=%s candidate=%s", server_id, current_ips, new_ip)

        async def _revert() -> None:
            if not current_ips:
                return
            revert: dict = {"editvps": 1}
            for i, ip in enumerate(current_ips):
                revert[f"ips[{i}]"] = ip
            try:
                await self._request("managevps", revert, query={"vpsid": server_id})
            except Exception as exc:
                logger.warning("add_extra_ip: revert failed vps=%s: %s", server_id, exc)

        # لیست صریحِ کامل (فعلی‌ها + جدید) — بدون num_ips
        all_ips = current_ips + [new_ip]
        payload: dict = {"editvps": 1}
        for i, ip in enumerate(all_ips):
            payload[f"ips[{i}]"] = ip
        resp = await self._request("managevps", payload, query={"vpsid": server_id})
        done_val = resp.get("done")
        ok = bool(done_val) if not isinstance(done_val, dict) else bool(done_val.get("done"))
        if not ok:
            raise RuntimeError(f"Virtualizor add-IP failed — response keys: {list(resp.keys())}")

        # تأیید از منبع معتبر: باید همه‌ی IPهای فعلی + جدید حاضر باشند
        assigned = set(await self._vps_ipv4s(server_id))
        logger.info("add_extra_ip vps=%s: after assigned=%s", server_id, assigned)
        if before.issubset(assigned) and new_ip in assigned:
            return new_ip

        # اگر افزوده نشد → برگشت و خطای شفاف
        await _revert()
        raise RuntimeError("افزودن آی‌پی دوم برای این سرور توسط پنل انجام نشد")

    async def list_all_vps(self) -> list[dict]:
        """همه‌ی VPSهای پنل: [{vpsid, hostname, ip, serid, bandwidth, used_bandwidth}].

        برای بازنگاشتِ بعد از مهاجرت لازم است — مهاجرت در ویرچولایزور vpsid جدید
        می‌دهد، پس رکوردهای ربات باید با hostname دوباره به VM واقعی وصل شوند.
        صفحه‌بندی: reslen بزرگ گرفته می‌شود و تا خالی‌شدن صفحه ادامه می‌یابد."""
        out: list[dict] = []
        seen: set[str] = set()
        page = 1
        _RESLEN = 200
        while True:
            if page > 20:
                # لیست ناقص خطرناک‌تر از خطاست: با نبودِ یک VM، گاردِ «چند VM
                # هم‌نام» دور زده می‌شود و رکورد به VMِ اشتباه وصل می‌شود
                raise RuntimeError("لیست VMهای پنل بیش از حد انتظار بزرگ است — "
                                   "بازنگاشت خودکار انجام نشد.")
            data = await self._request("vs", query={"reslen": _RESLEN, "page": page})
            rows = data.get("vs") or {}
            if not rows:
                if page == 1:
                    raise RuntimeError("پنل هیچ VMی برنگرداند — بازنگاشت انجام نشد.")
                break
            items = rows.values() if isinstance(rows, dict) else rows
            fresh = 0
            for vs in items:
                if not isinstance(vs, dict):
                    continue
                vid = str(vs.get("vpsid") or "")
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                fresh += 1
                out.append({
                    "vpsid": vid,
                    "hostname": (vs.get("hostname") or "").strip(),
                    "name": (vs.get("vps_name") or "").strip(),
                    "ips": self._ips_of(vs),
                    "uid": str(vs.get("uid") or ""),
                    "serid": vs.get("serid"),
                    "bandwidth": vs.get("bandwidth"),
                    "used_bandwidth": vs.get("used_bandwidth"),
                })
            if fresh == 0:
                # پنل پارامتر page را نادیده گرفته (همان صفحه دوباره) — اگر صفحه‌ی
                # اول پر بوده، یعنی احتمالاً VMهایی جا مانده‌اند
                if len(out) >= _RESLEN:
                    raise RuntimeError("پنل صفحه‌بندی را پشتیبانی نمی‌کند و لیست "
                                       "ممکن است ناقص باشد — بازنگاشت انجام نشد.")
                break
            page += 1
        return out

    @staticmethod
    def _ips_of(vs: dict) -> list[str]:
        """همه‌ی IPv4های یک رکورد VPS (فیلد ips نگاشت ipid→ip است)."""
        raw = vs.get("ips") or {}
        vals = list(raw.values()) if isinstance(raw, dict) else list(raw)
        return [str(v) for v in vals if isinstance(v, str) and v.count(".") == 3]

    async def _vs_row(self, server_id: str) -> dict:
        """رکورد خام یک VPS از act=vs (کلید = vpsid)."""
        data = await self._request("vs", query={"vpsid": server_id})
        vs_list = data.get("vs", {}) or {}
        return vs_list.get(str(server_id)) or (next(iter(vs_list.values())) if vs_list else {})

    async def bandwidth_info(self, server_id: str) -> tuple[int, float]:
        """(سهمیه، مصرف) زنده از پنل — هر دو GB. سهمیه‌ی صفر = نامحدود.

        نمایش به کاربر باید همیشه از همین‌جا بیاید (باقی‌مانده = سهمیه − مصرف)؛
        شمارنده‌ی مستقل در دیتابیس نگه داشته نمی‌شود، چون ماژول WHMCS سرِ
        سالگردِ ماهانه‌ی هر VM سقف را «سقف منهای مصرف دوره» می‌کند و هر کپیِ
        محلی بلافاصله کهنه می‌شود."""
        vs = await self._vs_row(server_id)
        if not vs:
            # VM روی پنل نیست (حذف‌شده/نامعتبر). اگر اینجا صفر برگردانیم، caller
            # آن را «نامحدود» تعبیر می‌کند و سقف ترافیک سرور پاک می‌شود
            raise RuntimeError(f"VPS {server_id} در پنل یافت نشد")
        try:
            quota = int(float(vs.get("bandwidth") or 0))
        except (TypeError, ValueError):
            quota = 0
        try:
            used = float(vs.get("used_bandwidth") or 0)
        except (TypeError, ValueError):
            used = 0.0
        return quota, used

    async def get_bandwidth_quota(self, server_id: str) -> int:
        """سهمیه‌ی ترافیک فعلی (GB). صفر = نامحدود."""
        quota, _ = await self.bandwidth_info(server_id)
        return quota

    async def add_traffic(self, server_id: str, gb: int) -> bool:
        """افزودن ترافیک به سهمیه‌ی فعلی (شارژِ یک‌باره‌ی استخر).

        مدل عملیاتی این پنل: ماژول WHMCS «استخر کاهشی» دارد — سرِ سالگردِ ماهانه‌ی
        هر VM (لنگر = تاریخ ساخت) خودش سقف را به «سقف منهای مصرف دوره» کاهش
        می‌دهد. پس اینجا فقط یک‌بار سقف بالا می‌رود؛ **هیچ تسک برگرداندن/ریست
        نباید ساخته شود** و مصرف ماه بعد خودکار کسر می‌شود.

        قواعد سختِ نوشتن (هر تخطی = خرابیِ برگشت‌ناپذیر):
          • تنها write مجاز: act=managevps با فقط vpsid + bandwidth
            (editvps=1 صرفاً تریگرِ ثبتِ فرم است، نه فیلد پیکربندی). POSTِ ناقصِ
            managevps با فیلدهای دیگر می‌تواند تنظیمات دیسک را خراب کند.
          • هرگز act=bwreset — گرافِ تاریخیِ VM را برای همیشه پاک می‌کند.
          • هرگز تاریخ ساخت VM دست نخورد (لنگر سالگرد است).
          • هرگز bandwidth=0 نوشته نشود — صفر یعنی نامحدود (حداقل ۱).
          • بعد از نوشتن حتماً بازخوانی و تأیید.

        ⚠️ `bandwidth` مقدار **کل** است نه افزایشی → مقدار فعلی خوانده و مجموع
        نوشته می‌شود.
        """
        gb = int(gb)
        self.last_add_traffic_unverified = False
        if gb <= 0:
            return True
        current = await self.get_bandwidth_quota(server_id)
        if current <= 0:
            # صفر = نامحدود؛ نوشتن gb سهمیه را از نامحدود به gb محدود می‌کرد
            raise RuntimeError("ترافیک این سرویس نامحدود است — نیازی به خرید ترافیک نیست.")
        target = max(1, current + gb)   # هرگز صفر (=نامحدود) نوشته نشود
        # payload حداقلی: vpsid + bandwidth (+ تریگر ثبت). هیچ فیلد دیگری اضافه نشود.
        await self._request("managevps",
                            {"vpsid": server_id, "editvps": 1, "bandwidth": target},
                            query={"vpsid": server_id})
        # تأیید از منبع معتبر (پاسخ managevps قابل‌اتکا نیست).
        # ⚠️ «خطای خواندن» با «اعمال‌نشدن» فرق دارد: اگر بازخوانی خطا داد یا
        # پنل هنوز کهنه بود، دوباره تلاش می‌کنیم؛ خطا فقط وقتی داده می‌شود که
        # یک خواندنِ موفق واقعاً مقدار کمتر برگرداند (وگرنه caller وجه را
        # برمی‌گرداند در حالی که ترافیک روی پنل اضافه شده = ترافیک مجانی).
        last_read: int | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2)
            try:
                last_read = await self.get_bandwidth_quota(server_id)
            except Exception:
                last_read = None
                continue
            if last_read >= target:
                self.last_bandwidth_quota = last_read
                return True
        if last_read is not None and last_read < target:
            raise RuntimeError(
                f"افزودن ترافیک تأیید نشد (سهمیه: {last_read}GB به‌جای {target}GB).")
        # بازخوانی قطعی نشد — احتمال زیاد نوشتن انجام شده؛ موفق در نظر می‌گیریم
        # (برگرداندن وجه در این حالت یعنی ترافیک مجانی). فلگ برای هشدار به ادمین
        # تا موارد تأییدنشده دستی بررسی شوند.
        self.last_add_traffic_unverified = True
        logger.warning("add_traffic: verify read failed for vps %s (target %sGB)",
                       server_id, target)
        return True

