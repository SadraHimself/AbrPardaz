"""Identity verification via Zohal (زحل) → Shahkar.

Zohal proxies Iran's Shahkar service, matching a mobile number to a national
code. Docs: https://dashboard.zohal.io/documents

Requires ZOHAL_TOKEN (Bearer) and the server's outbound IP to be registered in
Zohal's whitelist.
"""
from __future__ import annotations

import re

import httpx

from bot.config import settings


def normalize_ir_mobile(raw: str) -> str | None:
    """Return an Iranian mobile as 09XXXXXXXXX, or None if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        return "0" + digits
    return None


def valid_national_code(code: str) -> bool:
    """Validate an Iranian national code (10 digits + checksum)."""
    if not re.fullmatch(r"\d{10}", code or ""):
        return False
    if code == code[0] * 10:  # all identical digits are invalid
        return False
    s = sum(int(code[i]) * (10 - i) for i in range(9))
    r = s % 11
    check = int(code[9])
    return check == r if r < 2 else check == 11 - r


class ShahkarService:

    async def verify(self, phone_number: str, national_code: str) -> bool:
        """True if (mobile, national_code) match in Shahkar (via Zohal).

        Raises RuntimeError("... not configured") when ZOHAL_TOKEN is unset.
        """
        if not settings.ZOHAL_TOKEN:
            raise RuntimeError("ZOHAL_TOKEN is not configured")

        mobile = normalize_ir_mobile(phone_number) or phone_number

        url = f"{settings.ZOHAL_BASE_URL}/services/inquiry/shahkar"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.ZOHAL_TOKEN}",
        }
        payload = {"mobile": mobile, "national_code": national_code}

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)
            # 400 = invalid input, 4xx/5xx service errors → treat as "not matched"
            if resp.status_code != 200:
                return False
            data = resp.json()

        if data.get("result") != 1:
            return False
        body = data.get("response_body") or {}
        return bool((body.get("data") or {}).get("matched"))
