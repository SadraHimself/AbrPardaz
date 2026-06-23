"""Virtualizor KVM API client."""
from __future__ import annotations

import hashlib
import random
from typing import Optional

import aiohttp

from .base import BaseProvider, CreateServerParams, PlanInfo, ServerInfo


def _make_api_pass(api_key: str, api_pass: str) -> tuple[str, str]:
    """Virtualizor Admin API auth: random_key + md5(api_pass + random_key), all lowercase"""
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    random_key = "".join(random.choices(chars, k=8))
    auth_pass = random_key + hashlib.md5(f"{api_pass}{random_key}".encode()).hexdigest()
    return random_key, auth_pass


def _encode_form(params: dict) -> str:
    """Build application/x-www-form-urlencoded string keeping [ ] unencoded.
    PHP parses space[0][size]=20 as $_POST['space'][0]['size'] = '20'.
    Standard urllib.parse.urlencode encodes brackets → PHP sees literal key instead of array."""
    from urllib.parse import quote
    parts = []
    for k, v in params.items():
        ek = quote(str(k), safe="[]")
        ev = quote(str(v), safe="")
        parts.append(f"{ek}={ev}")
    return "&".join(parts)


class VirtualizorProvider(BaseProvider):
    """
    Virtualizor API v2 — KVM مجازی‌ساز

    Docs: https://my.virtualizor.com/docs/api/
    """

    def __init__(self, panel_url: str, api_key: str, api_pass: str):
        self.panel_url = panel_url.rstrip("/")
        self.api_key = api_key
        self.api_pass = api_pass

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _request(self, act: str, params: Optional[dict] = None) -> dict:
        import json as _json
        random_key, auth_pass = _make_api_pass(self.api_key, self.api_pass)

        # Routing: act + api in URL query string (Virtualizor may read via $_GET['act']).
        # Auth + action params: POST body with _encode_form so PHP array notation
        # (space[0][size]) isn't percent-encoded and $_POST parses it as nested array.
        url_qs = f"?act={act}&api=json"
        post_params: dict = {
            "adminapikey": self.api_key,
            "adminapipass": auth_pass,
        }
        if params:
            post_params.update(params)
        form_body = _encode_form(post_params)

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30, connect=5)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        # Try stored URL first, then http↔https fallback if panel returns HTML
        candidates = [self.panel_url]
        if self.panel_url.startswith("http://"):
            candidates.append(self.panel_url.replace("http://", "https://", 1))
        elif self.panel_url.startswith("https://"):
            candidates.append(self.panel_url.replace("https://", "http://", 1))

        last_error = None
        async with aiohttp.ClientSession(connector=connector) as session:
            for base_url in candidates:
                url = f"{base_url}/index.php{url_qs}"
                try:
                    async with session.post(
                        url, data=form_body, headers=headers,
                        timeout=timeout, allow_redirects=False,
                    ) as resp:
                        if resp.status in (301, 302, 303):
                            location = resp.headers.get("Location", "")
                            if "login" in location:
                                raise RuntimeError(
                                    "API credentials رد شد — در پنل Virtualizor:\n"
                                    "Configuration → API → Enable API را فعال کنید\n"
                                    "و API Key/Pass را مجدداً کپی کنید"
                                )
                            last_error = f"ریدایرکت {resp.status} به: {location}"
                            continue
                        raw = await resp.text()
                        if not raw or not raw.strip():
                            last_error = "پنل پاسخ خالی برگرداند"
                            continue
                        if raw.strip().startswith("<") or "window.location" in raw:
                            last_error = f"پنل HTML برگرداند — امتحان با: {base_url}"
                            continue
                        try:
                            data = _json.loads(raw)
                        except _json.JSONDecodeError:
                            preview = raw[:150].replace("\n", " ")
                            last_error = f"پاسخ JSON نیست: {preview}"
                            continue
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
    def _vs_status(raw: str) -> str:
        mapping = {"1": "active", "0": "off", "2": "suspended"}
        return mapping.get(str(raw), raw)

    def _parse_vs(self, vs: dict) -> ServerInfo:
        return ServerInfo(
            provider_server_id=str(vs.get("vpsid", "")),
            name=vs.get("hostname", ""),
            status=self._vs_status(vs.get("status", "0")),
            ip_address=vs.get("ip", {}).get("0") if isinstance(vs.get("ip"), dict) else vs.get("ip"),
            ram=int(vs.get("ram", 0)),
            cpu=int(vs.get("cores", 0)),
            disk=int(vs.get("space", 0)),
            bandwidth=int(vs.get("bandwidth", 0)),
            os_name=vs.get("os_name"),
            extra_data={"vpsid": vs.get("vpsid"), "node": vs.get("server_name")},
        )

    # ── BaseProvider implementation ───────────────────────────────────────────

    async def create_server(self, params: CreateServerParams) -> ServerInfo:
        import asyncio as _asyncio

        payload: dict = {
            "hostname": params.name,
            "rootpass": params.extra.get("root_password", "TeleCloud@2024"),
            "osid": params.os_id,
            "bandwidth": params.extra.get("bandwidth", 1000),
            "ram": params.extra.get("ram", 1024),
            "cores": params.extra.get("cpu", 1),
            "virt": params.extra.get("virt_type", "kvm"),
            "num_ips": 1,
        }

        # uid: Virtualizor user who owns the VPS. Must be a regular (non-admin) user.
        # Admin configures this via provider extra_config → virtualizor_uid.
        # If not set, omit uid entirely and let Virtualizor use its default.
        uid_val = params.extra.get("virtualizor_uid")
        if uid_val:
            payload["uid"] = uid_val

        plan_id_str = str(params.plan_id).strip()
        if plan_id_str.isdigit() and int(plan_id_str) > 0:
            payload["plid"] = int(plan_id_str)

        # Storage: explicit override > primary storage from Virtualizor > size-only fallback
        st_uuid = params.extra.get("st_uuid") or await self.get_primary_storage_uuid()
        disk_gb = params.extra.get("disk", 20)
        if st_uuid:
            payload["space[0][size]"] = disk_gb
            payload["space[0][st_uuid]"] = st_uuid
        else:
            # Virtualizor should use its own default; send just size
            payload["space[0][size]"] = disk_gb

        node_id = params.extra.get("node_id")
        if node_id:
            payload["node_select"] = node_id

        data = await self._request("addvs", payload)

        # Response format per docs: vs_info.vpsid (not addvs.vpsid)
        vs_info = data.get("vs_info") or {}
        vpsid = vs_info.get("vpsid") if isinstance(vs_info, dict) else None
        taskid = data.get("taskid")

        # Fallbacks for older Virtualizor versions
        if not vpsid:
            vpsid = data.get("vpsid")
        if not vpsid:
            addvs_raw = data.get("addvs")
            if isinstance(addvs_raw, dict):
                vpsid = addvs_raw.get("vpsid")
                taskid = taskid or addvs_raw.get("taskid")
            elif isinstance(addvs_raw, (int, float)) and addvs_raw:
                vpsid = int(addvs_raw)

        # Async creation: poll act=tasks until vpsid appears (max 90 sec)
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
            snippet = _json2.dumps({k: data[k] for k in list(data.keys())[:8]}, ensure_ascii=False)[:300]
            raise RuntimeError(
                f"Virtualizor did not return vpsid. Response: {snippet}"
            )
        return await self.get_server(str(vpsid))

    async def delete_server(self, server_id: str) -> bool:
        data = await self._request("deletevs", {"svs": server_id, "conf": "1"})
        return bool(data.get("done"))

    async def get_server(self, server_id: str) -> ServerInfo:
        data = await self._request("listvs", {"svs": server_id})
        vs_list = data.get("vs", {})
        if not vs_list:
            raise RuntimeError(f"Server {server_id} not found")
        vs = vs_list.get(server_id) or next(iter(vs_list.values()))
        return self._parse_vs(vs)

    async def start_server(self, server_id: str) -> bool:
        data = await self._request("startvs", {"svs": server_id})
        return bool(data.get("done"))

    async def stop_server(self, server_id: str) -> bool:
        data = await self._request("stopvs", {"svs": server_id})
        return bool(data.get("done"))

    async def restart_server(self, server_id: str) -> bool:
        data = await self._request("restartvs", {"svs": server_id})
        return bool(data.get("done"))

    async def rebuild_server(self, server_id: str, os_id: str) -> bool:
        data = await self._request("rebuilddisk", {"svs": server_id, "osid": os_id, "conf": "1"})
        return bool(data.get("done"))

    async def suspend_server(self, server_id: str) -> bool:
        data = await self._request("vs_suspend", {"svs": server_id})
        return bool(data.get("done"))

    async def unsuspend_server(self, server_id: str) -> bool:
        data = await self._request("vs_unsuspend", {"svs": server_id})
        return bool(data.get("done"))

    async def get_traffic(self, server_id: str) -> float:
        data = await self._request("vs_stats", {"svs": server_id})
        stats = data.get("vstats", {})
        used_bytes = float(stats.get("tx_bytes", 0)) + float(stats.get("rx_bytes", 0))
        return used_bytes / (1024 ** 3)

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
        """Return st_uuid of the primary/default storage, or None on failure."""
        try:
            storages = await self.list_storages()
            primary = next((s for s in storages if s["is_primary"]), None)
            if not primary and storages:
                primary = storages[0]  # fallback to first
            return primary["st_uuid"] if primary else None
        except Exception:
            return None

    async def list_plans(self, location: Optional[str] = None) -> list[PlanInfo]:
        data = await self._request("listplans")
        plans_raw = data.get("plans", {})
        plans = []
        for pid, plan in plans_raw.items():
            plans.append(PlanInfo(
                provider_plan_id=str(pid),
                name=plan.get("plan_name", ""),
                ram=int(plan.get("ram", 0)),
                cpu=int(plan.get("cores", 0)),
                disk=int(plan.get("space", 0)),
                bandwidth=int(plan.get("bandwidth", 0)),
            ))
        return plans

    async def list_os_templates(self) -> list[dict]:
        data = await self._request("listos")
        os_list = data.get("ostemplates", {})
        return [{"id": oid, "name": tmpl.get("name", "")} for oid, tmpl in os_list.items()]

    async def list_users(self) -> list[dict]:
        """List Virtualizor users — admin needs this to find the correct virtualizor_uid."""
        data = await self._request("listusers")
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

    async def edit_server(self, server_id: str, ram: Optional[int] = None,
                          cpu: Optional[int] = None, disk: Optional[int] = None) -> bool:
        payload: dict = {"svs": server_id}
        if ram is not None:
            payload["ram"] = ram
        if cpu is not None:
            payload["cores"] = cpu
        if disk is not None:
            payload["space"] = disk
        data = await self._request("editvs", payload)
        return bool(data.get("done"))

    async def change_ip(self, server_id: str) -> Optional[str]:
        data = await self._request("changeip", {"svs": server_id})
        return data.get("newip")

    async def add_traffic(self, server_id: str, gb: int) -> bool:
        data = await self._request("addvsbw", {"svs": server_id, "bw": gb})
        return bool(data.get("done"))

    async def get_vnc(self, server_id: str) -> dict:
        """Return dict with host, port, password for VNC connection."""
        data = await self._request("vnc", {"svs": server_id})
        vnc = data.get("novnc", data.get("vnc", {}))
        if isinstance(vnc, dict):
            return {
                "host": vnc.get("host", ""),
                "port": vnc.get("port", ""),
                "password": vnc.get("passwd", vnc.get("password", "")),
            }
        return {"host": "", "port": str(vnc), "password": ""}
