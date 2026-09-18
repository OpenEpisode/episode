from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from episode.domain.models import CapabilityConfig, Device
from episode.inventory.validation import DeviceValidationService
from episode.plugins.hikvision.isapi.validation import validate_device as validate_isapi
from episode.plugins.onvif.validation import validate_device as validate_onvif
from episode.plugins.registry import builtin_plugin_registry


@pytest.mark.asyncio
async def test_validation_reports_protocol_evidence_without_enabling_integrations(
    monkeypatch,
):
    client_options = {}

    class FakeONVIFClient:
        def __init__(self, *args, **kwargs):
            client_options.update(kwargs)

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
        "episode.plugins.onvif.validation.ONVIFClient",
        FakeONVIFClient,
    )
    monkeypatch.setattr(
        "episode.plugins.hikvision.isapi.validation.httpx.AsyncClient",
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
                settings={"relaxed_xml": True},
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
        integration_validators={"onvif": validate_onvif, "isapi": validate_isapi},
        integration_registrations=builtin_plugin_registry().device_integrations(),
    )

    results = await service.validate(
        device,
        integration_ids=["onvif", "hikvision-isapi", "hikvision-sdk"],
    )

    assert results["onvif"]["status"] == "supported"
    assert results["onvif"]["capabilities"] == [
        "discovery",
        "media",
        "snapshots",
        "events",
    ]
    assert client_options["relaxed_xml"] is True
    assert results["isapi"]["status"] == "supported"
    assert results["isapi"]["capabilities"] == ["device-information"]
    assert results["hikvision_sdk"]["status"] == "supported"
    assert device.capabilities == []


@pytest.mark.asyncio
async def test_validation_defaults_to_generic_onvif_only():
    called = []

    async def validate_onvif_only(_device, _checked_at, _timeout):
        called.append("onvif")
        return {"status": "supported", "summary": "ONVIF works"}

    service = DeviceValidationService(
        integration_validators={"onvif": validate_onvif_only},
        integration_registrations=builtin_plugin_registry().device_integrations(),
    )
    device = Device(
        id="camera",
        name="Camera",
        device_type="camera",
        area_id="yard",
        ip_address="192.0.2.10",
    )

    results = await service.validate(device)

    assert tuple(results) == ("onvif",)
    assert called == ["onvif"]


@pytest.mark.asyncio
async def test_validation_rejects_unknown_or_shared_integrations():
    service = DeviceValidationService(
        integration_registrations=builtin_plugin_registry().device_integrations(),
    )
    device = Device(
        id="camera",
        name="Camera",
        device_type="camera",
        area_id="yard",
    )

    with pytest.raises(ValueError, match="Unknown or unavailable"):
        await service.validate(device, integration_ids=["ftp"])

    with pytest.raises(ValueError, match="Unknown or unavailable"):
        await service.validate(device, integration_ids=["not-installed"])


@pytest.mark.asyncio
async def test_validation_deduplicates_registration_id_and_integration_type():
    calls = 0

    async def validate_isapi(_device, _checked_at, _timeout):
        nonlocal calls
        calls += 1
        return {"status": "supported", "summary": "ISAPI works"}

    service = DeviceValidationService(
        integration_validators={"isapi": validate_isapi},
        integration_registrations=builtin_plugin_registry().device_integrations(),
    )
    device = Device(
        id="camera",
        name="Camera",
        device_type="camera",
        area_id="yard",
    )

    result = await service.validate(device, integration_ids=["hikvision-isapi", "isapi"])

    assert list(result) == ["isapi"]
    assert calls == 1


@pytest.mark.asyncio
async def test_validation_bounds_a_hanging_selected_validator():
    async def hanging_validator(_device, _checked_at, _timeout):
        await asyncio.sleep(1)
        return {"status": "supported", "summary": "unexpected"}

    service = DeviceValidationService(
        integration_validators={"onvif": hanging_validator},
        integration_registrations=builtin_plugin_registry().device_integrations(),
        timeout=0.01,
    )
    device = Device(
        id="camera",
        name="Camera",
        device_type="camera",
        area_id="yard",
    )

    result = await service.validate(device)

    assert result["onvif"]["status"] == "unavailable"
    assert "validation timeout" in result["onvif"]["summary"]


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


def test_builtin_catalog_matches_manufacturer_and_keeps_onvif_universal():
    service = DeviceValidationService(
        integration_registrations=builtin_plugin_registry().device_integrations(),
    )

    hikvision = {
        entry["id"] for entry in service.catalog(manufacturer="Hikvision", device_type="camera")
    }
    reolink = {
        entry["id"] for entry in service.catalog(manufacturer="Reolink", device_type="camera")
    }
    unknown = {
        entry["id"] for entry in service.catalog(manufacturer="Unknown", device_type="camera")
    }

    assert hikvision == {"onvif", "hikvision-isapi"}
    assert reolink == {"onvif", "reolink"}
    assert unknown == {"onvif"}
