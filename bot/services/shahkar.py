"""Shahkar (شاهکار) KYC integration.

Shahkar is Iran's national mobile number verification service that maps
phone numbers to national IDs. We call it before granting access to
Iran-located servers as required by regulation.

Endpoint spec: https://shahkar.nict.ir  (use credentials from Shahkar portal)
"""
from __future__ import annotations

import httpx

from bot.config import settings


class ShahkarService:

    async def verify(self, phone_number: str, national_id: str) -> bool:
        """
        Returns True if (phone_number, national_id) is a valid match
        in Shahkar database.

        Phone should be in format 09xxxxxxxxx (11 digits).
        """
        if not settings.SHAHKAR_BASE_URL:
            raise RuntimeError("SHAHKAR_BASE_URL is not configured")

        mobile = phone_number.lstrip("+98").lstrip("98")
        if not mobile.startswith("0"):
            mobile = "0" + mobile

        payload = {
            "serviceNumber": settings.SHAHKAR_SERVICE_ID,
            "mobileNumber": mobile,
            "identificationNo": national_id,
        }
        auth = (settings.SHAHKAR_SERVICE_ID, settings.SHAHKAR_PASSWORD)

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.SHAHKAR_BASE_URL}/api/inquiry/v2/inquiry/",
                json=payload,
                auth=auth,
            )
            resp.raise_for_status()
            data = resp.json()
            # Shahkar returns {"matched": true/false}
            return bool(data.get("matched"))

    async def check_mobile_owner(self, phone_number: str) -> dict | None:
        """
        Optional: get subscriber info from Shahkar (if your plan supports it).
        Returns None if not supported.
        """
        return None
