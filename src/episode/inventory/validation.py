from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from episode.domain.models import Device

_INTEGRATIONS = ("onvif", "isapi", "hikvision_sdk")
IntegrationValidator = Callable[[Device, str, float], Awaitable[Mapping[str, Any]]]


class DeviceValidationService:
    """Safely validate integration support without changing runtime configuration."""

    def __init__(
        self,
        *,
        runtime_integrations: Callable[[Device], Sequence[Mapping[str, Any]]] = lambda _device: (),
        integration_validators: Mapping[str, IntegrationValidator] | None = None,
        timeout: float = 10,
    ) -> None:
        self._runtime_integrations = runtime_integrations
        self._integration_validators = dict(integration_validators or {})
        self._timeout = timeout

    async def validate(self, device: Device) -> dict[str, dict[str, Any]]:
        checked_at = datetime.now(timezone.utc).isoformat()
        onvif, isapi = await asyncio.gather(
            self._validate_plugin_integration("onvif", device, checked_at),
            self._validate_plugin_integration("isapi", device, checked_at),
        )
        sdk = self._sdk_support(device, checked_at)
        return {"onvif": onvif, "isapi": isapi, "hikvision_sdk": sdk}

    async def _validate_plugin_integration(
        self,
        integration: str,
        device: Device,
        checked_at: str,
    ) -> dict[str, Any]:
        validator = self._integration_validators.get(integration)
        if validator is None:
            return self._result(
                "not_validated",
                f"No validation probe is registered for {integration}",
                checked_at,
            )
        try:
            result = await validator(device, checked_at, self._timeout)
            return dict(result)
        except Exception as error:
            return self._failure(error, integration.upper(), checked_at)

    def _sdk_support(self, device: Device, checked_at: str) -> dict[str, Any]:
        integration = next(
            (
                item
                for item in self._runtime_integrations(device)
                if item.get("type") == "hikvision_sdk"
            ),
            None,
        )
        if integration and integration.get("state") == "healthy":
            return self._result(
                "supported",
                "HCNetSDK worker is connected",
                checked_at,
                capabilities=["events", "device-information"],
            )
        if integration:
            return self._result(
                "unavailable",
                str(integration.get("summary") or "HCNetSDK is configured but unavailable"),
                checked_at,
            )
        return self._result(
            "not_validated",
            (
                "HCNetSDK device login is not probed automatically; "
                "enable it explicitly and restart to validate"
            ),
            checked_at,
        )

    def _failure(self, error: Exception, label: str, checked_at: str) -> dict[str, Any]:
        if isinstance(error, asyncio.TimeoutError | httpx.TimeoutException):
            return self._result(
                "unavailable",
                f"{label} did not respond before the validation timeout",
                checked_at,
            )
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            if status in (401, 403):
                return self._result(
                    "authentication_failed",
                    f"{label} rejected the configured credentials",
                    checked_at,
                )
            if status in (404, 405, 501):
                return self._result(
                    "unsupported",
                    f"{label} endpoint is not supported at the configured path",
                    checked_at,
                )
            return self._result(
                "unavailable",
                f"{label} returned HTTP {status}",
                checked_at,
            )
        if isinstance(error, httpx.ConnectError):
            return self._result(
                "unreachable",
                f"{label} endpoint could not be reached",
                checked_at,
            )
        return self._result(
            "unavailable",
            f"{label} validation failed ({error.__class__.__name__})",
            checked_at,
        )

    @staticmethod
    def _result(
        status: str,
        summary: str,
        checked_at: str,
        *,
        capabilities: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "summary": summary,
            "checked_at": checked_at,
            "capabilities": capabilities or [],
            "details": details or {},
        }


def stored_support(device: Device) -> dict[str, dict[str, Any]]:
    value = device.metadata.get("integration_support", {})
    if not isinstance(value, dict):
        return {}
    return {
        integration: dict(result)
        for integration, result in value.items()
        if integration in _INTEGRATIONS and isinstance(result, dict)
    }
