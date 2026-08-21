from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Event
from episode.inventory import InventoryService
from episode.storage.repository import Repository


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
            "onvif": {"enabled": True, "events_enabled": True},
            "isapi": {"enabled": False},
        },
    )
    assert update.status_code == 200
    stored = await repository.get_device("door-camera")
    assert stored.username == "admin"
    assert stored.password == "top-secret"
    assert "isapi" not in stored.configs
    assert stored.get_config("onvif").settings["events_enabled"] is True


@pytest.mark.asyncio
async def test_device_writes_reconcile_runtime_integrations_automatically(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    reconciliations = 0

    async def reconcile_device_integrations():
        nonlocal reconciliations
        reconciliations += 1

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
            assert reconciliations == 1

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
            assert reconciliations == 2

            deleted = await client.delete("/api/v1/devices/sensor")
            assert deleted.status_code == 204
            assert reconciliations == 3
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
async def test_video_without_onvif_requires_a_manual_rtsp_endpoint(inventory_api):
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
        },
    )

    assert response.status_code == 422
    assert "manual RTSP endpoint" in response.text
