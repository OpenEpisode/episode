from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from episode import __version__
from episode.api.routes import create_api
from episode.api.runtime import OperationalView
from episode.config import EpisodeConfig
from episode.domain.models import Area, CapabilityConfig, Device, Episode, Event
from episode.storage.repository import Repository


def _operations() -> OperationalView:
    connectors = [
        {
            "name": "ISAPI:Gate",
            "type": "isapi",
            "running": True,
            "stream_active": True,
            "device_id": "gate-camera",
        },
        {
            "name": "Alarm Server",
            "type": "alarm_server",
            "running": True,
            "path": "/alarm",
            "port": 8989,
            "requests_handled": 12,
            "requests_rejected": 0,
        },
    ]
    plugins = [
        {
            "id": "onvif",
            "name": "ONVIF",
            "kind": "device-integration",
            "state": "ready",
            "integration": {
                "type": "onvif",
                "name": "ONVIF",
                "device_scoped": True,
                "activation_capability": "onvif",
                "capabilities": ["discovery", "media"],
            },
            "instances": [
                {
                    "id": "gate-camera",
                    "name": "Gate camera",
                    "state": "running",
                    "messages_received": 0,
                    "device_info": {
                        "manufacturer": "Example",
                        "model": "Camera 4K",
                        "firmware_version": "1.2.3",
                    },
                    "summary": "Connected · 1 media profile · Events disabled",
                    "capabilities": ["discovery", "media", "snapshots", "events"],
                    "details": {
                        "connected": True,
                        "subscribed": False,
                        "events_enabled": False,
                        "profiles": [
                            {
                                "token": "main",
                                "name": "Main",
                                "encoding": "H264",
                                "width": 3840,
                                "height": 2160,
                                "snapshot": True,
                            }
                        ],
                        "selected_profile": "main",
                        "event_topics": [f"Topic{index}" for index in range(200)],
                    },
                }
            ],
        },
        {
            "id": "hikvision-sdk",
            "name": "Hikvision HCNetSDK",
            "kind": "native-sdk",
            "state": "ready",
            "integration": {
                "type": "hikvision_sdk",
                "name": "Hikvision HCNetSDK",
                "device_scoped": True,
                "activation_capability": "hikvision_sdk",
                "capabilities": ["events", "device-information"],
            },
            "version": "6.1.9.48",
            "architecture": "amd64",
            "metrics": {"deliveries": 3, "failures": 0},
            "instances": [
                {
                    "id": "gate-camera",
                    "name": "Gate camera",
                    "state": "running",
                    "messages_received": 3,
                    "device_info": {
                        "manufacturer": "Fallback vendor",
                        "model": "Fallback model",
                        "firmware_version": "0.0.1",
                    },
                }
            ],
        },
    ]
    return OperationalView(
        version=__version__,
        engine_status=lambda: {"running": True, "timeout": 30},
        recorder_status=lambda: {
            "running": True,
            "active_recordings": 1,
            "cameras": 1,
            "segment_seconds": 600,
        },
        snapshot_status=lambda: {
            "running": False,
            "captured": 0,
            "failures": 0,
            "suppressed": 0,
            "active": 0,
        },
        connector_statuses=lambda: connectors,
        plugin_statuses=lambda: plugins,
        snapshots_enabled=False,
        restart_required=lambda: False,
    )


@pytest.mark.asyncio
async def test_status_is_compact_and_diagnostics_are_separate():
    app = create_api(object(), operations=_operations())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        status_response = await client.get("/api/v1/status")
        diagnostics_response = await client.get("/api/v1/diagnostics")

    assert status_response.status_code == 200
    status = status_response.json()
    assert status == {
        "version": __version__,
        "state": "healthy",
        "active_recordings": 1,
        "restart_required": False,
        "services": {
            "engine": "healthy",
            "recorder": "healthy",
            "snapshots": "disabled",
        },
        "integrations": {
            "total": 4,
            "healthy": 4,
            "degraded": 0,
            "unavailable": 0,
        },
    }
    assert len(status_response.content) < 500
    diagnostics = diagnostics_response.json()
    assert len(diagnostics["integrations"]) == 4
    onvif = next(item for item in diagnostics["integrations"] if item["type"] == "onvif")
    assert len(onvif["details"]["instances"][0]["details"]["event_topics"]) == 200


@pytest.mark.asyncio
async def test_device_api_owns_identity_integrations_and_safe_capture_policy(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    try:
        await repository.upsert_area(Area(id="gate", name="Gate"))
        await repository.upsert_device(
            Device(
                id="gate-camera",
                name="Gate camera",
                device_type="camera",
                area_id="gate",
                capabilities=["video", "onvif", "isapi", "hikvision_sdk"],
                ip_address="192.0.2.10",
                username="admin",
                password="secret",
                configs={
                    "video": CapabilityConfig(settings={"recording_mode": "on_episode"}),
                    "onvif": CapabilityConfig(settings={"events_enabled": False}),
                },
            )
        )
        app = create_api(repository, config.data_dir, operations=_operations())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            summaries = (await client.get("/api/v1/devices")).json()
            detail = (await client.get("/api/v1/devices/gate-camera")).json()

        assert len(summaries) == 1
        summary = summaries[0]
        assert "ip_address" not in summary
        assert "metadata" not in summary
        assert summary["state"] == "healthy"
        assert summary["capabilities"] == ["video"]
        assert summary["identity"] == {
            "manufacturer": "Example",
            "model": "Camera 4K",
            "firmware_version": "1.2.3",
        }
        assert {item["type"] for item in summary["integrations"]} == {
            "onvif",
            "isapi",
            "hikvision_sdk",
        }
        assert all(item["details"] == {} for item in summary["integrations"])

        assert detail["ip_address"] == "192.0.2.10"
        assert detail["capture_policy"] == {
            "recording": "on_episode",
            "automatic_snapshots": False,
            "onvif_events": False,
        }
        assert "username" not in detail
        assert "password" not in detail
        assert "configs" not in detail
        onvif = next(item for item in detail["integrations"] if item["type"] == "onvif")
        assert onvif["details"]["profiles"][0]["token"] == "main"
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_growing_collections_reject_unbounded_queries():
    app = create_api(object())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.get("/api/v1/episodes?limit=201"),
            await client.get("/api/v1/episodes?offset=-1"),
            await client.get("/api/v1/episodes?state=not-a-state"),
            await client.get("/api/v1/events?limit=501"),
            await client.get("/api/v1/evidence?limit=501"),
            await client.get("/api/v1/receipts?limit=1001"),
        ]

    assert all(response.status_code == 422 for response in responses)


@pytest.mark.asyncio
async def test_episode_api_projects_the_first_active_event_as_its_trigger(tmp_path):
    config = EpisodeConfig(data_dir=str(tmp_path))
    repository = Repository(config)
    await repository.initialize()
    started = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
    try:
        for area_id in ("front", "garden", "garage"):
            await repository.upsert_area(Area(id=area_id, name=area_id))
        for device_id, area_id in (
            ("doorbell", "front"),
            ("camera-front", "front"),
            ("camera-garden", "garden"),
            ("operator", "garage"),
        ):
            await repository.upsert_device(
                Device(id=device_id, name=device_id, device_type="camera", area_id=area_id)
            )
        doorbell_episode = Episode(
            id="doorbell-episode",
            primary_area_id="front",
            start_time=started,
        )
        motion_episode = Episode(
            id="motion-episode",
            primary_area_id="garden",
            start_time=started + timedelta(minutes=1),
        )
        manual_episode = Episode(
            id="manual-episode",
            primary_area_id="garage",
            start_time=started + timedelta(minutes=2),
        )
        for episode in (doorbell_episode, motion_episode, manual_episode):
            await repository.create_episode(episode)

        await repository.create_event(
            Event(
                device_id="doorbell",
                area_id="front",
                timestamp=started,
                event_type="doorbell",
                event_state="active",
                episode_id=doorbell_episode.id,
            )
        )
        await repository.create_event(
            Event(
                device_id="camera-front",
                area_id="front",
                timestamp=started + timedelta(seconds=2),
                event_type="human_detection",
                event_state="active",
                episode_id=doorbell_episode.id,
            )
        )
        await repository.create_event(
            Event(
                device_id="camera-garden",
                area_id="garden",
                timestamp=started + timedelta(minutes=1),
                event_type="vehicle_detection",
                event_state="active",
                episode_id=motion_episode.id,
            )
        )
        await repository.create_event(
            Event(
                device_id="operator",
                area_id="garage",
                timestamp=started + timedelta(minutes=2),
                event_type="manual",
                event_state="active",
                episode_id=manual_episode.id,
            )
        )

        transport = httpx.ASGITransport(app=create_api(repository, config.data_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            episodes = (await client.get("/api/v1/episodes")).json()
            detail = (await client.get("/api/v1/episodes/doorbell-episode")).json()

        triggers = {episode["id"]: episode["trigger_type"] for episode in episodes}
        assert triggers == {
            "doorbell-episode": "doorbell",
            "motion-episode": "motion",
            "manual-episode": None,
        }
        assert detail["trigger_type"] == "doorbell"
    finally:
        await repository.close()
