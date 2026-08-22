from __future__ import annotations

import asyncio
from typing import Any

import httpx

from episode.domain.models import Device
from episode.plugins.onvif.client import ONVIFClient, ONVIFError


async def validate_device(device: Device, checked_at: str, timeout: float) -> dict[str, Any]:
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
        timeout=min(timeout, 8),
        relaxed_validation=bool(config.settings.get("onvif_relaxed_validation", False))
        if config
        else False,
    )
    try:
        discovered = await asyncio.wait_for(client.discover(), timeout=timeout)
        profiles = len(discovered.profiles)
        capabilities = ["discovery"]
        if profiles:
            capabilities.append("media")
        if any(profile.snapshot_uri for profile in discovered.profiles):
            capabilities.append("snapshots")
        if discovered.event_topics:
            capabilities.append("events")
        return _result(
            "supported",
            f"ONVIF responded · {profiles} media profile{'s' if profiles != 1 else ''}",
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
        return _failure(error, checked_at)
    finally:
        await client.close()


def _failure(error: Exception, checked_at: str) -> dict[str, Any]:
    if isinstance(error, asyncio.TimeoutError | httpx.TimeoutException):
        return _result(
            "unavailable", "ONVIF did not respond before the validation timeout", checked_at
        )
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in (401, 403):
            return _result(
                "authentication_failed", "ONVIF rejected the configured credentials", checked_at
            )
        if status in (404, 405, 501):
            return _result(
                "unsupported",
                "ONVIF endpoint is not supported at the configured path",
                checked_at,
            )
        return _result("unavailable", f"ONVIF returned HTTP {status}", checked_at)
    if isinstance(error, httpx.ConnectError):
        return _result("unreachable", "ONVIF endpoint could not be reached", checked_at)
    if isinstance(error, ONVIFError):
        return _result(
            "unavailable",
            f"ONVIF responded but validation failed: {str(error)[:120]}",
            checked_at,
        )
    return _result(
        "unavailable",
        f"ONVIF validation failed ({error.__class__.__name__})",
        checked_at,
    )


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
