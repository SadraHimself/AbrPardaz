"""Virtualizor KVM API client."""
from __future__ import annotations

from typing import Optional

import aiohttp

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

    def __init__(self, panel_url: str, api_key: str, api_pass: str):
        self.panel_url = panel_url.rstrip("/")
        self.api_key = api_key
        self.api_pass = api_pass

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
        timeout = aiohttp.ClientTimeout(total=30, connect=5)
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

    @staticmethod
    def _running_flag(vs: dict) -> str:
        """Resolve the live power state to "1" (running) / "0" (off) / "" (unknown).

        Different Virtualizor versions expose the running state under different
        keys. Prefer `machine_status`, then `vps_status`, then fall back to the
        generic `status` field — this is why a running VM could read as "off"
        before (only `machine_status` was checked, and this panel may not set it)."""
        for key in ("machine_status", "vps_status"):
            v = vs.get(key)
            if v is not None and str(v) != "":
                return "1" if str(v) == "1" else "0"
        v = vs.get("status")
        if v is not None and str(v) != "":
            return "1" if str(v) == "1" else "0"
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

        # Virtualizor VNC password rules: alphanumeric only ("No Non-Alphanumeric
        # characters are allowed") AND max 8 chars (VNC protocol limit — longer values
        # fail with "VNC password length too long than supported"). Sanitize any provided
        # value to alphanumeric and cap at 8; otherwise generate a random 8-char one.
        import secrets as _secrets
        import string as _string
        vnc_pass = "".join(c for c in str(params.extra.get("vnc_pass") or "") if c.isalnum())[:8]
        if not vnc_pass:
            vnc_pass = "".join(_secrets.choice(_string.ascii_letters + _string.digits) for _ in range(8))

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
            "vnc": 1,
            "vncpass": vnc_pass,  # alphanumeric only — Virtualizor rejects special chars
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
            # With a plan assigned, Virtualizor allocates IPs from the plan's own pool.
            # Manually pre-selecting an IP (ips[0]) bypasses that restriction, so we
            # use num_ips=1 and let Virtualizor pick.
            payload["num_ips"] = 1
        elif ip_to_assign:
            payload["ips[0]"] = ip_to_assign
        else:
            payload["num_ips"] = 1

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
        vs = vs_list.get(str(server_id)) or next(iter(vs_list.values()))
        return self._parse_vs(vs)

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
        # vpsid in URL query; editvps=1 submit trigger + conf=1 for rebuild confirmation
        payload: dict = {"editvps": 1, "newos": os_id, "conf": "1"}
        if rootpass:
            payload["rootpass"] = rootpass
        data = await self._request("managevps", payload, query={"vpsid": server_id})
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
        data = await self._request("ostemplates")
        os_list = data.get("ostemplates", {})
        return [{"id": oid, "name": tmpl.get("name", "")} for oid, tmpl in os_list.items()]

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

    async def change_ip(self, server_id: str) -> str:
        """Assign a new free IP, restricted to the SAME IP pool / subnet the VPS
        currently uses.

        A Virtualizor node can host several IP pools (e.g. 1.1.1.0/24 AND
        2.2.2.0/24), but a plan is only permitted to draw from its own pool. The
        VPS's current IP was assigned from that allowed pool at creation, so we
        scope the new IP to the same pool id (`ippid`) — falling back to the same
        /24 subnet — to guarantee we never hand out an IP the plan can't use."""
        import random as _random

        try:
            info = await self.get_server(server_id)
            serid = int(info.extra_data.get("serid") or 0)
            current_ip = info.ip_address
        except Exception:
            serid = 0
            current_ip = None

        # Full IP listing for this node (includes assigned + free, each with its pool id)
        data = await self._request("ips", query={"serid": serid})
        ips_raw = data.get("ips") or data.get("freeips") or {}
        entries = list(ips_raw.values()) if isinstance(ips_raw, dict) else (ips_raw or [])
        entries = [e for e in entries if isinstance(e, dict)]

        def _pool(e: dict) -> str:
            return str(e.get("ippid") or e.get("ippoolid") or e.get("ipp_id") or e.get("pool") or "")

        def _is_free(e: dict) -> bool:
            return str(e.get("vpsid", "0")) in ("0", "")

        # Identify the pool + subnet of the VPS's current IP = the plan's allowed range.
        current_pool = ""
        for e in entries:
            if current_ip and str(e.get("ip", "")) == str(current_ip):
                current_pool = _pool(e)
                break
        current_subnet = (
            current_ip.rsplit(".", 1)[0]
            if current_ip and str(current_ip).count(".") == 3 else None
        )

        free = [e for e in entries if _is_free(e)]

        # 1) Same IP pool as the current IP (the plan's allowed pool).
        candidates = [e for e in free if current_pool and _pool(e) == current_pool]
        # 2) Fall back to the same /24 subnet as the current IP.
        if not candidates and current_subnet:
            candidates = [e for e in free if str(e.get("ip", "")).rsplit(".", 1)[0] == current_subnet]
        # 3) Only when the current IP can't be identified at all, allow any free IP.
        if not candidates and not current_ip:
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

    async def add_traffic(self, server_id: str, gb: int) -> bool:
        # vpsid in URL query; editvps=1 submit trigger + bandwidth in POST body
        data = await self._request("managevps", {"editvps": 1, "bandwidth": gb}, query={"vpsid": server_id})
        done_val = data.get("done")
        return bool(done_val) if not isinstance(done_val, dict) else bool(done_val.get("done"))

    async def get_vnc(self, server_id: str) -> dict:
        # VNC Info API (admin): act=vnc with novnc=<VPSID> in the POST body.
        # The response is a FLAT object — {"port":"5951","ip":"x.x.x.x",
        # "password":"....","novnc":1} — where `novnc` is just a flag (1), NOT a
        # nested dict. Reading data["novnc"] therefore returned the integer 1 and
        # the screen came back empty; read the fields off the top level instead.
        data = await self._request("vnc", {"novnc": server_id}, query={"vpsid": server_id})
        src = data.get("vnc") if isinstance(data.get("vnc"), dict) else data
        return {
            "host": src.get("ip") or src.get("host") or "",
            "port": src.get("port") or src.get("vncport") or "",
            "password": (
                src.get("password") or src.get("passwd")
                or src.get("vnc_passwd") or src.get("vncpass") or ""
            ),
            # If the panel ever hands back a ready-made link/token, use it directly.
            "url": src.get("url") or data.get("url") or "",
            "token": src.get("token") or data.get("token") or "",
        }

    async def set_vnc_password(self, server_id: str, vnc_pass: str) -> bool:
        """Change the VNC password. Virtualizor rule: alphanumeric only, max 8 chars."""
        vnc_pass = "".join(c for c in str(vnc_pass) if c.isalnum())[:8]
        data = await self._request(
            "managevps", {"editvps": 1, "vnc": 1, "vncpass": vnc_pass}, query={"vpsid": server_id}
        )
        done_val = data.get("done")
        return bool(done_val) if not isinstance(done_val, dict) else bool(done_val.get("done"))
