from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from episode.api.inventory import DeviceWriteRequest, device_from_request
from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import CapabilityConfig, Device, Event
from episode.inventory import InventoryService
from episode.inventory.validation import DeviceValidationService
from episode.media import CameraMedia, MediaRegistry, VideoMode, VideoSourceDescriptor
from episode.plugins.registry import builtin_plugin_registry
from episode.storage.repository import Repository


def test_manual_recording_source_requires_manual_endpoint():
    with pytest.raises(ValidationError, match="Select a discovered source"):
        DeviceWriteRequest(
            name="Camera",
            area_id="entrance",
            ip_address="192.0.2.10",
            video={"enabled": True, "recording_source_id": "manual"},
            onvif={"enabled": True},
        )


def test_video_can_be_enabled_before_plugin_media_discovery():
    request = DeviceWriteRequest(
        name="Reolink camera",
        area_id="entrance",
        ip_address="192.0.2.10",
        video={"enabled": True},
        onvif={"enabled": False},
        reolink={"enabled": True, "media_enabled": True},
    )

    assert request.video.enabled is True
    assert request.reolink.media_enabled is True


def test_device_configuration_persists_a_pinned_recording_source():
    request = DeviceWriteRequest(
        id="camera-1",
        name="Reolink camera",
        area_id="entrance",
        ip_address="192.0.2.10",
        video={"enabled": True, "recording_source_id": "  reolink:native:sub  "},
        onvif={"enabled": False},
        reolink={"enabled": True, "media_enabled": True},
    )

    device = device_from_request(request.id, request)

    assert device.get_config("video").settings["recording_source_id"] == "reolink:native:sub"


@pytest.mark.asyncio
async def test_device_detail_projects_safe_runtime_video_sources(tmp_path):
    device = Device(
        id="camera-1",
        name="Front camera",
        device_type="camera",
        area_id="entrance",
        ip_address="192.0.2.10",
        username="viewer",
        password="secret",
        capabilities=["video"],
        configs={
            "video": CapabilityConfig(
                protocol="",
                port=None,
                path="",
                settings={"origin": "onvif"},
            )
        },
    )

    class InMemoryRepository:
        async def get_device(self, device_id):
            return device if device_id == device.id else None

        async def device_usage(self, _device_id):
            return {"episodes": 0, "events": 0, "evidence": 0}

    media = MediaRegistry()
    media.register(
        CameraMedia(
            device_id=device.id,
            stream_uri="rtsp://viewer:secret@192.0.2.10/private/main",
            source="onvif",
            video_source=VideoSourceDescriptor(
                id="onvif:profile-main",
                name="Main profile",
                provider="ONVIF",
                protocol="rtsp",
                metadata_kind="configured",
                width=1920,
                height=1080,
                frame_rate=25,
                codec="H264",
                default=True,
            ),
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_api(InMemoryRepository(), str(tmp_path), media=media)
        ),
        base_url="http://test",
    )
    try:
        response = await client.get("/api/v1/devices/camera-1")
    finally:
        await client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["video_sources"][0]["id"] == "onvif:profile-main"
    assert body["video_sources"][0]["width"] == 1920
    assert body["configuration"]["video"]["recording_source_id"] == ""
    assert "secret" not in response.text
    assert "rtsp://" not in response.text


@pytest_asyncio.fixture
async def inventory_api(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    inventory = InventoryService(repository)
    app = create_api(repository, str(tmp_path), inventory=inventory)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield repository, inventory, client
    finally:
        await client.aclose()
        await repository.close()


@pytest.mark.asyncio
async def test_isapi_defaults_to_interpreting_every_vendor_event(inventory_api):
    """Ignored Events is empty by default.

    A non-empty default stops the plugin *interpreting* the message, so the
    Raw Artifact and Receipt exist but no canonical Event ever does, and the
    core event class filter is never consulted. ``videoloss`` was in the
    ``beta.7`` default, which silently removed the ``security`` class from
    those cameras, so a new Device must start with nothing ignored.
    """
    _repository, _inventory, client = inventory_api
    area = await client.post("/api/v1/areas", json={"name": "Front gate"})
    assert area.status_code == 201

    created = await client.post(
        "/api/v1/devices",
        json={
            "name": "Gate camera",
            "area_id": area.json()["id"],
            "ip_address": "192.0.2.10",
            "username": "admin",
            "password": "top-secret",
            "isapi": {"enabled": True},
        },
    )
    assert created.status_code == 201
    assert created.json()["configuration"]["isapi"]["ignore_events"] == []

    stored = await _repository.get_device("gate-camera")
    assert stored.get_config("isapi").settings["ignore_events"] == []


@pytest.mark.asyncio
async def test_area_and_device_crud_keeps_credentials_write_only(inventory_api):
    repository, inventory, client = inventory_api

    area_response = await client.post(
        "/api/v1/areas",
        json={"name": "Front Door", "location": "Main entrance"},
    )
    assert area_response.status_code == 201
    assert area_response.json()["id"] == "front-door"

    device_response = await client.post(
        "/api/v1/devices",
        json={
            "name": "Door camera",
            "area_id": "front-door",
            "ip_address": "192.0.2.10",
            "username": "admin",
            "password": "top-secret",
            "manufacturer": "Hikvision",
            "episode_policy": {
                "activity_window_seconds": 90,
            },
            "isapi": {"enabled": True},
        },
    )
    assert device_response.status_code == 201
    body = device_response.json()
    assert body["id"] == "door-camera"
    assert body["configuration"]["username_configured"] is True
    assert body["configuration"]["password_configured"] is True
    assert "top-secret" not in json.dumps(body)
    assert "admin" not in json.dumps(body)
    stored = await repository.get_device("door-camera")
    assert stored.username == "admin"
    assert stored.password == "top-secret"
    assert stored.activity_window_seconds == 90
    assert body["capture_policy"]["activity_window_seconds"] == 90
    assert body["configuration"]["episode_policy"] == {
        "activity_window_seconds": 90,
        "event_filter": None,
    }
    assert body["configuration"]["manufacturer"] == "Hikvision"
    assert body["identity"]["manufacturer"] == "Hikvision"
    assert {"video", "onvif", "isapi"}.issubset(stored.configs)
    assert not stored.capabilities

    update = await client.put(
        "/api/v1/devices/door-camera",
        json={
            "name": "Door camera renamed",
            "area_id": "front-door",
            "ip_address": "192.0.2.10",
            "username": None,
            "password": None,
            "episode_policy": {
                "activity_window_seconds": 60,
            },
            "onvif": {"enabled": True, "events_enabled": True, "relaxed_xml": True},
            "isapi": {"enabled": False},
        },
    )
    assert update.status_code == 200
    stored = await repository.get_device("door-camera")
    assert stored.username == "admin"
    assert stored.password == "top-secret"
    assert stored.activity_window_seconds == 60
    assert "isapi" not in stored.configs
    assert stored.get_config("onvif").settings["events_enabled"] is True
    assert stored.get_config("onvif").settings["relaxed_xml"] is True
    assert update.json()["configuration"]["onvif"]["relaxed_xml"] is True


@pytest.mark.asyncio
async def test_device_api_lists_safe_video_choices_and_persists_selection(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    media = MediaRegistry()
    media.register(
        CameraMedia(
            device_id="camera-1",
            stream_uri="rtsp://user:secret@192.0.2.10/private/main",
            source="onvif",
            video_source=VideoSourceDescriptor(
                id="onvif:profile-main",
                name="Main profile",
                provider="ONVIF",
                protocol="rtsp",
                metadata_kind="configured",
                width=1920,
                height=1080,
                frame_rate=25,
                codec="H264",
                modes=(VideoMode(width=1920, height=1080, frame_rates=(25,), codec="H264"),),
                default=True,
            ),
        )
    )
    app = create_api(repository, str(tmp_path), inventory=InventoryService(repository), media=media)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )
    try:
        area = await client.post("/api/v1/areas", json={"id": "entrance", "name": "Entrance"})
        assert area.status_code == 201
        created = await client.post(
            "/api/v1/devices",
            json={
                "id": "camera-1",
                "name": "Front camera",
                "area_id": "entrance",
                "ip_address": "192.0.2.10",
                "username": "user",
                "password": "secret",
                "video": {
                    "enabled": True,
                    "manual_endpoint": True,
                    "protocol": "rtsp",
                    "port": 554,
                    "path": "/manual",
                    "recording_source_id": "onvif:profile-main",
                },
                "onvif": {"enabled": False},
            },
        )

        assert created.status_code == 201
        body = created.json()
        assert body["configuration"]["video"]["recording_source_id"] == "onvif:profile-main"
        assert body["video_sources"][0]["width"] == 1920
        assert body["video_sources"][0]["frame_rate"] == 25
        assert "private/main" not in json.dumps(body)
        assert "secret" not in json.dumps(body)
        stored = await repository.get_device("camera-1")
        assert stored.get_config("video").settings["recording_source_id"] == "onvif:profile-main"
    finally:
        await client.aclose()
        await repository.close()


@pytest.mark.asyncio
async def test_device_catalog_and_validation_selection_are_bounded(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()

    async def fake_onvif(_device, _checked_at, _timeout):
        return {
            "status": "supported",
            "summary": "ONVIF works",
            "capabilities": ["discovery"],
        }

    validator = DeviceValidationService(
        integration_validators={"onvif": fake_onvif},
        integration_registrations=builtin_plugin_registry().device_integrations(),
    )
    app = create_api(
        repository,
        str(tmp_path),
        inventory=InventoryService(repository),
        validator=validator,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            catalog = await client.get(
                "/api/v1/devices/integrations/catalog",
                params={"manufacturer": "Hikvision", "device_type": "camera"},
            )
            assert catalog.status_code == 200
            assert {entry["id"] for entry in catalog.json()} == {
                "onvif",
                "hikvision-isapi",
                "hikvision-sdk",
            }

            await client.post("/api/v1/areas", json={"id": "yard", "name": "Yard"})
            created = await client.post(
                "/api/v1/devices",
                json={
                    "id": "camera",
                    "name": "Camera",
                    "area_id": "yard",
                    "ip_address": "192.0.2.50",
                },
            )
            assert created.status_code == 201
            stored = await repository.get_device("camera")
            stored.metadata["integration_support"] = {
                "isapi": {"status": "supported", "summary": "ISAPI works"}
            }
            await repository.upsert_device(stored)

            payload = {
                "id": "camera",
                "name": "Camera",
                "area_id": "yard",
                "ip_address": "192.0.2.50",
                "integration_ids": [],
            }
            no_probe = await client.post("/api/v1/devices/validate", json=payload)
            assert no_probe.status_code == 200
            assert no_probe.json()["results"] == {}

            invalid = await client.post(
                "/api/v1/devices/validate",
                json={**payload, "integration_ids": ["ftp"]},
            )
            assert invalid.status_code == 422
            assert "Unknown or unavailable" in invalid.text

            initial = await client.post(
                "/api/v1/devices/validate",
                json={**payload, "integration_ids": None},
            )
            assert initial.status_code == 200
            assert list(initial.json()["results"]) == ["onvif"]
            stored = await repository.get_device("camera")
            assert set(stored.metadata["integration_support"]) == {"isapi", "onvif"}
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_manual_video_validation_does_not_persist_device(tmp_path, monkeypatch):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()

    async def fake_video_validation(device):
        assert device.ip_address == "192.0.2.51"
        assert device.username == "admin"
        assert device.password == "secret"
        return {"status": "supported", "summary": "Stream works", "details": {"codec": "h264"}}

    monkeypatch.setattr(
        "episode.api.endpoints.inventory.validate_manual_video",
        fake_video_validation,
    )
    app = create_api(repository, str(tmp_path), inventory=InventoryService(repository))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/devices/validate-video",
                json={
                    "name": "Manual camera",
                    "area_id": "yard",
                    "ip_address": "192.0.2.51",
                    "username": "admin",
                    "password": "secret",
                    "onvif": {"enabled": False},
                    "video": {
                        "enabled": True,
                        "manual_endpoint": True,
                        "path": "/stream",
                    },
                },
            )
            assert response.status_code == 200
            assert response.json()["status"] == "supported"
            assert await repository.get_device("manual-camera") is None

            await client.post("/api/v1/areas", json={"id": "yard", "name": "Yard"})
            saved = await client.post(
                "/api/v1/devices",
                json={
                    "id": "manual-camera",
                    "name": "Manual camera",
                    "area_id": "yard",
                    "ip_address": "192.0.2.51",
                    "username": "admin",
                    "password": "secret",
                    "onvif": {"enabled": False},
                    "video": {"enabled": True, "manual_endpoint": True, "path": "/stream"},
                },
            )
            assert saved.status_code == 201
            with_saved_credentials = await client.post(
                "/api/v1/devices/validate-video",
                json={
                    "id": "manual-camera",
                    "name": "Manual camera",
                    "area_id": "yard",
                    "ip_address": "192.0.2.51",
                    "onvif": {"enabled": False},
                    "video": {"enabled": True, "manual_endpoint": True, "path": "/stream"},
                },
            )
            assert with_saved_credentials.status_code == 200
            assert with_saved_credentials.json()["status"] == "supported"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_device_writes_reconcile_runtime_integrations_automatically(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    reconciliations: list[str] = []

    async def reconcile_device_integrations(device_id: str):
        reconciliations.append(device_id)

    inventory = InventoryService(
        repository,
        on_device_configuration_changed=reconcile_device_integrations,
    )
    app = create_api(repository, str(tmp_path), inventory=inventory)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/v1/areas", json={"id": "gate", "name": "Gate"})
            created = await client.post(
                "/api/v1/devices",
                json={
                    "id": "sensor",
                    "name": "Sensor",
                    "device_type": "sensor",
                    "area_id": "gate",
                    "video": {"enabled": False},
                    "onvif": {"enabled": False},
                },
            )
            assert created.status_code == 201
            assert reconciliations == ["sensor"]

            updated = await client.put(
                "/api/v1/devices/sensor",
                json={
                    "name": "Sensor renamed",
                    "device_type": "sensor",
                    "area_id": "gate",
                    "video": {"enabled": False},
                    "onvif": {"enabled": False},
                },
            )
            assert updated.status_code == 200
            assert reconciliations == ["sensor", "sensor"]

            deleted = await client.delete("/api/v1/devices/sensor")
            assert deleted.status_code == 204
            assert reconciliations == ["sensor", "sensor", "sensor"]
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_inventory_api_archives_used_resources_and_deletes_unused_ones(inventory_api):
    repository, _inventory, client = inventory_api
    await client.post("/api/v1/areas", json={"id": "gate", "name": "Gate"})
    await client.post(
        "/api/v1/devices",
        json={
            "id": "camera",
            "name": "Camera",
            "area_id": "gate",
            "ip_address": "192.0.2.20",
        },
    )
    await repository.create_event(Event(device_id="camera", area_id="gate", event_type="motion"))

    assert (await client.delete("/api/v1/devices/camera")).status_code == 409
    archived = await client.put(
        "/api/v1/devices/camera",
        json={
            "name": "Camera",
            "area_id": "gate",
            "enabled": False,
            "ip_address": "192.0.2.20",
        },
    )
    assert archived.status_code == 200
    assert archived.json()["state"] == "disabled"
    assert (await client.get("/api/v1/devices")).json() == []
    assert len((await client.get("/api/v1/devices?include_disabled=true")).json()) == 1

    disabled_area = await client.put(
        "/api/v1/areas/gate",
        json={"name": "Gate", "enabled": False},
    )
    assert disabled_area.status_code == 200
    assert disabled_area.json()["enabled"] is False
    assert (await client.delete("/api/v1/areas/gate")).status_code == 409

    await client.post("/api/v1/areas", json={"id": "unused", "name": "Unused"})
    assert (await client.delete("/api/v1/areas/unused")).status_code == 204
    assert (await client.get("/api/v1/areas/unused")).status_code == 404


@pytest.mark.asyncio
async def test_device_type_controls_doorbell_capability_and_rejects_vendor_as_type(
    inventory_api,
):
    repository, _inventory, client = inventory_api
    await client.post("/api/v1/areas", json={"id": "entrance", "name": "Entrance"})

    created = await client.post(
        "/api/v1/devices",
        json={
            "name": "Entry intercom",
            "device_type": "doorbell",
            "area_id": "entrance",
            "ip_address": "192.0.2.30",
            "hikvision_sdk": {"enabled": True},
        },
    )
    assert created.status_code == 201
    assert created.json()["device_type"] == "doorbell"
    assert "doorbell" not in created.json()["capabilities"]
    assert (await repository.get_device("entry-intercom")).device_type == "doorbell"

    changed_role = await client.put(
        "/api/v1/devices/entry-intercom",
        json={
            "name": "Entry camera",
            "device_type": "camera",
            "area_id": "entrance",
            "ip_address": "192.0.2.30",
        },
    )
    assert changed_role.status_code == 200
    assert changed_role.json()["device_type"] == "camera"
    assert "doorbell" not in changed_role.json()["capabilities"]

    invalid = await client.post(
        "/api/v1/devices",
        json={
            "name": "Vendor is not a role",
            "device_type": "hikvision",
            "area_id": "entrance",
            "ip_address": "192.0.2.31",
        },
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_video_can_wait_for_a_discovered_plugin_source_without_onvif(inventory_api):
    _repository, _inventory, client = inventory_api
    await client.post("/api/v1/areas", json={"id": "yard", "name": "Yard"})

    response = await client.post(
        "/api/v1/devices",
        json={
            "name": "Offline discovery camera",
            "area_id": "yard",
            "ip_address": "192.0.2.40",
            "onvif": {"enabled": False},
            "video": {"enabled": True, "manual_endpoint": False},
            "reolink": {"enabled": True, "media_enabled": True},
        },
    )

    assert response.status_code == 201
    assert response.json()["configuration"]["video"]["enabled"] is True
    assert response.json()["configuration"]["reolink"]["media_enabled"] is True


@pytest.mark.asyncio
async def test_reolink_events_and_media_roundtrip(inventory_api):
    repository, inventory, client = inventory_api

    area_response = await client.post(
        "/api/v1/areas",
        json={"name": "Yard", "location": "Back"},
    )
    assert area_response.status_code == 201
    area_id = area_response.json()["id"]

    device_response = await client.post(
        "/api/v1/devices",
        json={
            "name": "Yard camera",
            "area_id": area_id,
            "ip_address": "192.0.2.20",
            "username": "admin",
            "password": "secret",
            "reolink": {
                "enabled": True,
                "media_enabled": True,
                "events_enabled": True,
                "native_video": True,
                "preview_variant": "sub",
                "preview_timeout": 4.0,
            },
        },
    )
    assert device_response.status_code == 201
    stored = await repository.get_device(device_response.json()["id"])
    reolink = stored.get_config("reolink")
    assert reolink is not None
    assert reolink.settings["media_enabled"] is True
    assert reolink.settings["events_enabled"] is True
    assert reolink.settings["native_video"] is True
    assert reolink.settings["preview_variant"] == "sub"
    assert reolink.settings["preview_timeout"] == 4.0

    # Round-trip back through editable configuration
    body = device_response.json()
    assert body["configuration"]["reolink"]["media_enabled"] is True
    assert body["configuration"]["reolink"]["events_enabled"] is True
    assert body["configuration"]["reolink"]["native_video"] is True
    assert body["configuration"]["reolink"]["preview_variant"] == "sub"
    assert body["configuration"]["reolink"]["preview_timeout"] == 4.0

    # Disable events on update
    update = await client.put(
        f"/api/v1/devices/{device_response.json()['id']}",
        json={
            "name": "Yard camera",
            "area_id": area_id,
            "ip_address": "192.0.2.20",
            "username": None,
            "password": None,
            "reolink": {"enabled": True, "media_enabled": True, "events_enabled": False},
        },
    )
    assert update.status_code == 200
    stored = await repository.get_device(device_response.json()["id"])
    assert stored.get_config("reolink").settings["events_enabled"] is False
