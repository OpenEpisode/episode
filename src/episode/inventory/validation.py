from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from episode.domain.models import Device
from episode.plugins.models import PluginRegistration, normalize_manufacturer

IntegrationValidator = Callable[[Device, str, float], Awaitable[Mapping[str, Any]]]


class DeviceValidationService:
    """Safely validate integration support without changing runtime configuration."""

    def __init__(
        self,
        *,
        runtime_integrations: Callable[[Device], Sequence[Mapping[str, Any]]] = lambda _device: (),
        integration_validators: Mapping[str, IntegrationValidator] | None = None,
        integration_registrations: Sequence[PluginRegistration] = (),
        catalog_entries: Sequence[Mapping[str, object]] = (),
        timeout: float = 10,
    ) -> None:
        self._runtime_integrations = runtime_integrations
        self._integration_validators = dict(integration_validators or {})
        self._integration_registrations = tuple(
            registration
            for registration in integration_registrations
            if registration.validation_capability
            and registration.integration
            and registration.integration.device_scoped
        )
        self._catalog_entries = tuple(dict(entry) for entry in catalog_entries)
        self._timeout = timeout

    @property
    def integration_types(self) -> tuple[str, ...]:
        return tuple(
            registration.integration.type
            for registration in self._integration_registrations
            if registration.integration
        )

    def catalog(
        self,
        *,
        manufacturer: str | None = None,
        device_type: str = "camera",
    ) -> list[dict[str, object]]:
        """Return bounded integration metadata for the onboarding UI."""
        if manufacturer is None:
            entries = [
                entry
                for registration in self._integration_registrations
                if (entry := registration.public_catalog_entry()) is not None
            ]
        else:
            entries = [
                entry
                for registration in self._integration_registrations
                if registration.integration
                and registration.integration.matches_device(
                    manufacturer=manufacturer,
                    device_type=device_type,
                )
                and (entry := registration.public_catalog_entry()) is not None
            ]
        reserved_ids = {
            key
            for registration in self._integration_registrations
            for key in (
                registration.id,
                registration.integration.type if registration.integration else "",
            )
            if key
        }
        entries.extend(
            entry
            for entry in self._catalog_entries
            if str(entry.get("id", "")) not in reserved_ids
            and self._catalog_entry_matches(
                entry,
                manufacturer=manufacturer,
                device_type=device_type,
            )
        )
        return entries

    @staticmethod
    def _catalog_entry_matches(
        entry: Mapping[str, object],
        *,
        manufacturer: str | None,
        device_type: str,
    ) -> bool:
        device_types = entry.get("device_types") or []
        if device_types and device_type not in device_types:
            return False
        scope_kind = entry.get("manufacturer_scope_kind", "unspecified")
        if scope_kind == "universal":
            return True
        if manufacturer is None:
            # The unfiltered catalogue is informational.  This keeps legacy
            # manifests visible without treating them as recommendations.
            return True
        if scope_kind != "targeted" or not manufacturer:
            return False
        normalized = normalize_manufacturer(manufacturer)
        return normalized in {
            normalize_manufacturer(str(value)) for value in (entry.get("manufacturer_scope") or [])
        }

    async def validate(
        self,
        device: Device,
        integration_ids: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Probe only the requested integrations.

        The default is the generic ONVIF probe.  Vendor integrations must be
        explicitly selected after discovery; this avoids sending credentials
        to every installed validator during first-time onboarding.
        """
        checked_at = datetime.now(timezone.utc).isoformat()
        registrations = self._selected_registrations(integration_ids)
        results = await asyncio.gather(
            *(
                self._validate_integration(registration, device, checked_at)
                for registration in registrations
            )
        )
        return dict(
            zip(
                (registration.integration.type for registration in registrations),
                results,
                strict=True,
            )
        )

    def _selected_registrations(
        self,
        integration_ids: Sequence[str] | None,
    ) -> tuple[PluginRegistration, ...]:
        by_id = {
            key: registration
            for registration in self._integration_registrations
            for key in {
                registration.id,
                registration.integration.type if registration.integration else "",
            }
            if key
        }
        if integration_ids is None:
            return tuple(
                registration
                for registration in self._integration_registrations
                if registration.integration and registration.integration.type == "onvif"
            )
        requested = tuple(dict.fromkeys(integration_ids))
        unknown = [integration_id for integration_id in requested if integration_id not in by_id]
        if unknown:
            raise ValueError("Unknown or unavailable device integration selected")
        selected: list[PluginRegistration] = []
        selected_registration_ids: set[str] = set()
        for integration_id in requested:
            registration = by_id[integration_id]
            if registration.id in selected_registration_ids:
                continue
            selected_registration_ids.add(registration.id)
            selected.append(registration)
        return tuple(selected)

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
            result = await asyncio.wait_for(
                validator(device, checked_at, self._timeout),
                timeout=self._timeout,
            )
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
                "on the Device to validate it at runtime"
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
