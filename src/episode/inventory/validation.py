from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlunsplit

import httpx

from episode.connectors.onvif.client import ONVIFClient, ONVIFError
from episode.domain.models import Device

_INTEGRATIONS = ("onvif", "isapi", "hikvision_sdk")


class DeviceValidationService:
    """Safely validate integration support without changing runtime configuration."""

    def __init__(
        self,
        *,
        runtime_integrations: Callable[[Device], Sequence[Mapping[str, Any]]] = lambda _device: (),
        timeout: float = 10,
    ) -> None:
        self._runtime_integrations = runtime_integrations
        self._timeout = timeout

    async def validate(self, device: Device) -> dict[str, dict[str, Any]]:
        checked_at = datetime.now(timezone.utc).isoformat()
        onvif, isapi = await asyncio.gather(
            self._probe_onvif(device, checked_at),
            self._probe_isapi(device, checked_at),
        )
        sdk = self._sdk_support(device, checked_at)
        return {"onvif": onvif, "isapi": isapi, "hikvision_sdk": sdk}

    async def _probe_onvif(self, device: Device, checked_at: str) -> dict[str, Any]:
        config = device.get_config("onvif")
        client = ONVIFClient(
            device.ip_address,
            device.username,
            device.password,
            protocol=config.protocol if config and config.protocol else "http",
            port=config.port if config else 80,
            path=config.path if config and config.path else "/onvif/device_service",
            auth_mode=(
                str(config.settings.get("auth_mode", "digest_wsse")) if config else "digest_wsse"
            ),
            timeout=min(self._timeout, 8),
        )
        try:
            discovered = await asyncio.wait_for(client.discover(), timeout=self._timeout)
            profiles = len(discovered.profiles)
            capabilities = ["discovery"]
            if profiles:
                capabilities.append("media")
            if any(profile.snapshot_uri for profile in discovered.profiles):
                capabilities.append("snapshots")
            if discovered.event_topics:
                capabilities.append("events")
            return self._result(
                "supported",
                (f"ONVIF responded · {profiles} media profile{'s' if profiles != 1 else ''}"),
                checked_at,
                capabilities=capabilities,
                details={
                    "manufacturer": discovered.manufacturer,
                    "model": discovered.model,
                    "firmware_version": discovered.firmware_version,
                    "profiles": profiles,
                    "event_topics": len(discovered.event_topics),
                },
            )
        except Exception as error:
            return self._failure(error, "ONVIF", checked_at)
        finally:
            await client.close()

    async def _probe_isapi(self, device: Device, checked_at: str) -> dict[str, Any]:
        config = device.get_config("isapi")
        protocol = config.protocol if config and config.protocol else "http"
        port = config.port if config else 80
        port_part = f":{port}" if port and port not in (80, 443) else ""
        url = urlunsplit(
            (protocol, f"{device.ip_address}{port_part}", "/ISAPI/System/deviceInfo", "", "")
        )
        auth = httpx.DigestAuth(device.username, device.password) if device.username else None
        try:
            async with httpx.AsyncClient(
                auth=auth,
                timeout=httpx.Timeout(min(self._timeout, 8)),
                follow_redirects=False,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
            root = ET.fromstring(response.content)
            details = {
                "manufacturer": self._xml_text(root, "manufacturer"),
                "model": self._xml_text(root, "model"),
                "firmware_version": self._xml_text(root, "firmwareVersion"),
            }
            return self._result(
                "supported",
                "ISAPI device information responded · Event stream not tested",
                checked_at,
                capabilities=["device-information"],
                details={key: value for key, value in details.items() if value},
            )
        except ET.ParseError:
            return self._result(
                "unavailable",
                "ISAPI endpoint returned an unexpected response",
                checked_at,
            )
        except Exception as error:
            return self._failure(error, "ISAPI", checked_at)

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
        if isinstance(error, ONVIFError):
            return self._result(
                "unavailable",
                f"{label} responded but validation failed: {str(error)[:120]}",
                checked_at,
            )
        return self._result(
            "unavailable",
            f"{label} validation failed ({error.__class__.__name__})",
            checked_at,
        )

    @staticmethod
    def _xml_text(root: ET.Element, name: str) -> str:
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == name:
                return element.text or ""
        return ""

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
