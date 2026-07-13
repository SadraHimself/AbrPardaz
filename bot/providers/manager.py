"""Factory that returns the right provider instance."""
from __future__ import annotations

from bot.database.models import ProviderAccount, ProviderType
from .base import BaseProvider
from .hetzner import HetznerProvider
from .virtualizor import VirtualizorProvider


def get_provider(account: ProviderAccount) -> BaseProvider:
    if account.provider_type == ProviderType.VIRTUALIZOR:
        return VirtualizorProvider(
            panel_url=account.api_endpoint or "",
            api_key=account.api_key or "",
            api_pass=account.api_secret or "",
        )
    if account.provider_type == ProviderType.HETZNER:
        return HetznerProvider(api_token=account.api_key or "")
    raise ValueError(f"Unsupported provider type: {account.provider_type}")
