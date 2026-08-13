from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from episode.domain.models import CapabilityConfig, Device
from episode.inventory.validation import DeviceValidationService
from episode.plugins.hikvision_isapi.validation import validate_device as validate_isapi


@pytest.mark.asyncio
async def test_validation_reports_protocol_evidence_without_enabling_integrations(
    monkeypatch,
):
    class FakeONVIFClient:
        def __init__(self, *args, **kwargs):
            pass

        async def discover(self):
            return SimpleNamespace(
                manufacturer="Example",
                model="Camera",
                firmware_version="1.2",
                profiles=[SimpleNamespace(snapshot_uri="http://camera/snapshot")],
                event_topics=["Motion"],
            )

        async def close(self):
            pass

    class FakeResponse:
        content = (
            b"<DeviceInfo><manufacturer>Example</manufacturer>"
            b"<model>Camera</model><firmwareVersion>1.2</firmwareVersion></DeviceInfo>"
        )

        def raise_for_status(self):
            pass

    class FakeHTTPClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, _url):
            return FakeResponse()

    monkeypatch.setattr(
        "episode.inventory.validation.ONVIFClient",
        FakeONVIFClient,
    )
    monkeypatch.setattr(
        "episode.plugins.hikvision_isapi.validation.httpx.AsyncClient",
        FakeHTTPClient,
    )

    device = Device(
        id="door",
        name="Door",
        device_type="doorbell",
        area_id="entrance",
        ip_address="192.0.2.10",
        username="viewer",
        password="secret",
        configs={
            "onvif": CapabilityConfig(
                protocol="http",
                port=80,
                path="/onvif/device_service",
            ),
            "isapi": CapabilityConfig(protocol="http", port=80),
        },
    )
    service = DeviceValidationService(
        runtime_integrations=lambda _device: [
            {
                "type": "hikvision_sdk",
                "state": "healthy",
                "name": "HCNetSDK",
                "capabilities": ["events"],
            }
        ],
        integration_validators={"isapi": validate_isapi},
    )

    results = await service.validate(device)

    assert results["onvif"]["status"] == "supported"
    assert results["onvif"]["capabilities"] == [
        "discovery",
        "media",
        "snapshots",
        "events",
    ]
    assert results["isapi"]["status"] == "supported"
    assert results["isapi"]["capabilities"] == ["device-information"]
    assert results["hikvision_sdk"]["status"] == "supported"
    assert device.capabilities == []


def test_validation_failure_states_do_not_call_timeouts_unsupported():
    service = DeviceValidationService()

    timeout = service._failure(TimeoutError(), "ONVIF", "now")
    assert timeout["status"] == "unavailable"

    request = httpx.Request("GET", "http://camera/onvif/device_service")
    response = httpx.Response(404, request=request)
    unsupported = service._failure(
        httpx.HTTPStatusError("not found", request=request, response=response),
        "ONVIF",
        "now",
    )
    assert unsupported["status"] == "unsupported"

    auth_response = httpx.Response(401, request=request)
    authentication = service._failure(
        httpx.HTTPStatusError(
            "unauthorized",
            request=request,
            response=auth_response,
        ),
        "ONVIF",
        "now",
    )
    assert authentication["status"] == "authentication_failed"
