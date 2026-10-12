"""Factory to instantiate the configured SMS Provider."""
from typing import Any, Dict

from adapters.base import BaseSmsProvider
from adapters.http import HttpSmsProvider
from adapters.mock import MockProvider


def get_sms_provider(sms_config: Dict[str, Any]) -> BaseSmsProvider:
    provider_type = sms_config.get("provider", "mock").lower()

    if provider_type == "mock":
        return MockProvider(sms_config.get("mock", {}))
    elif provider_type == "http":
        return HttpSmsProvider(sms_config.get("http", {}))
    else:
        raise ValueError(f"Unknown SMS provider '{provider_type}'. Must be 'mock' or 'http'.")
