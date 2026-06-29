"""NOWPayments API client."""
from __future__ import annotations

import logging

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.nowpayments.io/v1"


class NOWPaymentsError(Exception):
    pass


class NOWPaymentsClient:

    @property
    def _headers(self) -> dict:
        return {
            "x-api-key": settings.NP_API_KEY,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_BASE_URL}{path}",
                params=params,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    raise NOWPaymentsError(f"HTTP {resp.status}: {data}")
                return data

    async def _post(self, path: str, payload: dict) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_BASE_URL}{path}",
                json=payload,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if resp.status >= 400:
                    raise NOWPaymentsError(f"HTTP {resp.status}: {data}")
                return data

    async def check_status(self) -> dict:
        return await self._get("/status")

    async def get_merchant_coins(self) -> list[str]:
        data = await self._get("/merchant/coins")
        return data.get("selectedCurrencies", [])

    async def create_invoice(
        self,
        amount_usd: float,
        order_id: str,
        description: str,
        ipn_callback_url: str,
        success_url: str,
    ) -> dict:
        return await self._post("/invoice", {
            "price_amount": amount_usd,
            "price_currency": settings.NP_PRICE_CURRENCY,
            "order_id": order_id,
            "order_description": description,
            "ipn_callback_url": ipn_callback_url,
            "success_url": success_url,
            "cancel_url": success_url,
        })

    async def create_payment(
        self,
        amount_usd: float,
        order_id: str,
        description: str,
        ipn_callback_url: str,
        pay_currency: str | None = None,
    ) -> dict:
        payload = {
            "price_amount": amount_usd,
            "price_currency": settings.NP_PRICE_CURRENCY,
            "pay_currency": pay_currency or settings.NP_OUTCOME_CURRENCY,
            "order_id": order_id,
            "order_description": description,
            "ipn_callback_url": ipn_callback_url,
        }
        logger.info("create_payment: order=%s ipn_callback_url=%r pay_currency=%s", order_id, ipn_callback_url, payload["pay_currency"])
        return await self._post("/payment", payload)

    async def get_payment_status(self, payment_id: str) -> dict:
        return await self._get(f"/payment/{payment_id}")
