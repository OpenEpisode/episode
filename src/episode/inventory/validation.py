from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from episode.domain.models import Device
from episode.plugins.models import PluginRegistration

IntegrationValidator = Callable[[Device, str, float], Awaitable[Mapping[str, Any]]]


class DeviceValidationService:
    """Safely validate integration support without changing runtime configuration."""

    def __init__(
        self,
        *,
        runtime_integrations: Callable[[Device], Sequence[Mapping[str, Any]]] = lambda _device: (),
        integration_validators: Mapping[str, IntegrationValidator] | None = None,
        integration_registrations: Sequence[PluginRegistration] = (),
        timeout: float = 10,
    ) -> None:
        self._runtime_integrations = runtime_integrations
        self._integration_validators = dict(integration_validators or {})
        self._integration_registrations = tuple(
            registration
            for registration in integration_registrations
            if registration.validation_capability and registration.integration
        )
        self._timeout = timeout

    @property
    def integration_types(self) -> tuple[str, ...]:
        return tuple(
            registration.integration.type
            for registration in self._integration_registrations
            if registration.integration
        )

    async def validate(self, device: Device) -> dict[str, dict[str, Any]]:
        checked_at = datetime.now(timezone.utc).isoformat()
        results = await asyncio.gather(
            *(
                self._validate_integration(registration, device, checked_at)
                for registration in self._integration_registrations
            )
        )
        return dict(zip(self.integration_types, results, strict=True))

    async def _validate_integration(
        self,
        registration: PluginRegistration,
        device: Device,
        checked_at: str,
    ) -> dict[str, Any]:
        integration = registration.validation_capability
        validator = self._integration_validators.get(integration)
        if validator is None:
            return self._runtime_support(registration, device, checked_at)
        try:
            result = await validator(device, checked_at, self._timeout)
            return dict(result)
        except Exception as error:
            return self._failure(error, integration.upper(), checked_at)

    def _runtime_support(
        self,
        registration: PluginRegistration,
        device: Device,
        checked_at: str,
    ) -> dict[str, Any]:
        metadata = registration.integration
        if metadata is None:
            raise RuntimeError(f"Plugin {registration.id} has no integration metadata")
        integration = next(
            (
                item
                for item in self._runtime_integrations(device)
                if item.get("type") == metadata.type
            ),
            None,
        )
        if integration and integration.get("state") == "healthy":
            return self._result(
                "supported",
                f"{metadata.name} is connected",
                checked_at,
                capabilities=list(metadata.capabilities),
            )
        if integration:
            return self._result(
                "unavailable",
                str(integration.get("summary") or f"{metadata.name} is unavailable"),
                checked_at,
            )
        return self._result(
            "not_validated",
            (
                f"{metadata.name} is not probed automatically; enable it "
                "explicitly and restart to validate"
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
        str(integration): dict(result)
        for integration, result in value.items()
        if isinstance(integration, str) and isinstance(result, dict)
    }
