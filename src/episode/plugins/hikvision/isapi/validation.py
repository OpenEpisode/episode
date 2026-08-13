from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlunsplit

import httpx

from episode.domain.models import Device


async def validate_device(
    device: Device,
    checked_at: str,
    timeout: float,
) -> dict[str, Any]:
    """Probe Hikvision device information without activating the Event stream."""
    config = device.get_config("isapi")
    protocol = config.protocol if config and config.protocol else "http"
    port = config.port if config else 80
    default_port = 443 if protocol == "https" else 80
    port_part = f":{port}" if port and port != default_port else ""
    url = urlunsplit(
        (protocol, f"{device.ip_address}{port_part}", "/ISAPI/System/deviceInfo", "", "")
    )
    auth = httpx.DigestAuth(device.username, device.password) if device.username else None
    try:
        async with httpx.AsyncClient(
            auth=auth,
            timeout=httpx.Timeout(min(timeout, 8)),
            follow_redirects=False,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        root = ET.fromstring(response.content)
        details = {
            "manufacturer": _xml_text(root, "manufacturer"),
            "model": _xml_text(root, "model"),
            "firmware_version": _xml_text(root, "firmwareVersion"),
        }
        return _result(
            "supported",
            "ISAPI device information responded · Event stream not tested",
            checked_at,
            capabilities=["device-information"],
            details={key: value for key, value in details.items() if value},
        )
    except ET.ParseError:
        return _result(
            "unavailable",
            "ISAPI endpoint returned an unexpected response",
            checked_at,
        )
    except Exception as error:
        return _failure(error, checked_at)


def _failure(error: Exception, checked_at: str) -> dict[str, Any]:
    if isinstance(error, asyncio.TimeoutError | httpx.TimeoutException):
        return _result(
            "unavailable",
            "ISAPI did not respond before the validation timeout",
            checked_at,
        )
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in (401, 403):
            return _result(
                "authentication_failed",
                "ISAPI rejected the configured credentials",
                checked_at,
            )
        if status in (404, 405, 501):
            return _result(
                "unsupported",
                "ISAPI endpoint is not supported at the configured path",
                checked_at,
            )
        return _result(
            "unavailable",
            f"ISAPI returned HTTP {status}",
            checked_at,
        )
    if isinstance(error, httpx.ConnectError):
        return _result(
            "unreachable",
            "ISAPI endpoint could not be reached",
            checked_at,
        )
    return _result(
        "unavailable",
        f"ISAPI validation failed ({error.__class__.__name__})",
        checked_at,
    )


def _xml_text(root: ET.Element, name: str) -> str:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == name:
            return element.text or ""
    return ""


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
