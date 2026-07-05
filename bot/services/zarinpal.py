"""Zarinpal payment gateway client (v4 REST/JSON).

Flow: request_payment() → redirect user to startpay_url(authority) → on return,
verify(amount, authority). Both code 100 (first verify) and 101 (already
verified) mean success.
"""
from __future__ import annotations

import logging

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)


class ZarinpalError(Exception):
    pass


class ZarinpalClient:
    def __init__(self) -> None:
        self.merchant_id = settings.ZARINPAL_MERCHANT_ID
        host = "https://sandbox.zarinpal.com" if settings.ZARINPAL_SANDBOX else "https://payment.zarinpal.com"
        self._api = f"{host}/pg/v4/payment"
        self._startpay = f"{host}/pg/StartPay"

    def startpay_url(self, authority: str) -> str:
        return f"{self._startpay}/{authority}"

    async def _post(self, path: str, payload: dict) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._api}/{path}",
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                return await resp.json(content_type=None)

    @staticmethod
    def _error_message(resp: dict) -> str:
        errs = resp.get("errors")
        if isinstance(errs, dict) and errs:
            return f"code={errs.get('code')} {errs.get('message', '')}".strip()
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            e = errs[0]
            return f"code={e.get('code')} {e.get('message', '')}".strip()
        return "خطای نامشخص از درگاه"

    async def request_payment(self, amount_toman: int, callback_url: str, description: str,
                              mobile: str | None = None, email: str | None = None,
                              auto_verify: bool | None = None, card_pan: str | None = None) -> str:
        """Create a payment request; return the authority on success.

        `mobile` is required for the Ayan (عیان) identity service — Zarinpal matches
        the paying card's owner against this mobile's national code and flags the
        result on the transaction.
        """
        if not self.merchant_id:
            raise ZarinpalError("ZARINPAL_MERCHANT_ID is not configured")
        payload = {
            "merchant_id": self.merchant_id,
            "amount": int(amount_toman),
            "currency": "IRT",
            "callback_url": callback_url,
            "description": description[:500],
        }
        metadata: dict = {}
        if mobile:
            metadata["mobile"] = mobile
        if email:
            metadata["email"] = email
        if card_pan:
            metadata["card_pan"] = card_pan   # lock: Zarinpal rejects any other card
        if auto_verify is not None:
            metadata["auto_verify"] = auto_verify
        if metadata:
            payload["metadata"] = metadata

        logger.info(
            "zarinpal request: amount=%s mobile=%s card_lock=%s auto_verify=%s",
            int(amount_toman),
            (mobile[:4] + "***" + mobile[-2:]) if mobile else None,
            ("****" + card_pan[-4:]) if card_pan else "OFF",
            auto_verify,
        )
        resp = await self._post("request.json", payload)
        data = resp.get("data") or {}
        if isinstance(data, dict) and data.get("code") == 100 and data.get("authority"):
            return data["authority"]
        raise ZarinpalError(self._error_message(resp))

    async def verify(self, amount_toman: int, authority: str) -> tuple[int, str | None]:
        """Verify a payment. Returns (code, ref_id). code 100/101 = success."""
        payload = {
            "merchant_id": self.merchant_id,
            "amount": int(amount_toman),
            "authority": authority,
        }
        resp = await self._post("verify.json", payload)
        data = resp.get("data") or {}
        if isinstance(data, dict) and data.get("code") in (100, 101):
            ref = data.get("ref_id")
            return int(data["code"]), (str(ref) if ref is not None else None)

        code = data.get("code") if isinstance(data, dict) else None
        if code is None:
            errs = resp.get("errors")
            if isinstance(errs, dict):
                code = errs.get("code")
        return (int(code) if code is not None else -1), None
