from __future__ import annotations

import pytest
import pytest_asyncio

from episode.config import EpisodeConfig
from episode.domain.models import Area, CapabilityConfig, Device, DeviceDiscoveryUpdate
from episode.storage.repository import Repository


def _device(*, enabled: bool = True, setup_state: str = "ready", configs=None) -> Device:
    return Device(
        id="camera-1",
        name="Camera",
        device_type="camera",
        area_id="area-1",
        capabilities=["camera"],
        ip_address="192.0.2.10",
        username="operator",
        password="secret",
        configs=configs if configs is not None else {"onvif": CapabilityConfig()},
        activity_window_seconds=45,
        metadata={"operator": {"note": "keep"}},
        enabled=enabled,
        setup_state=setup_state,
        event_filter=["motion"],
    )


@pytest_asyncio.fixture
async def repository(tmp_path) -> Repository:
    repository = Repository(
        EpisodeConfig(
            data_dir=str(tmp_path / "data"),
            db_path=str(tmp_path / "data" / "episode.db"),
        )
    )
    await repository.initialize()
    await repository.upsert_area(Area(id="area-1", name="Area"))
    try:
        yield repository
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_discovery_merges_into_latest_operator_row(repository):
    await repository.upsert_device(_device())

    # Simulate an operator edit completed after a plugin took its discovery
    # snapshot. The stale discovery must not restore any old inventory field.
    edited = _device()
    edited.name = "Renamed camera"
    edited.ip_address = "192.0.2.20"
    edited.username = "new-user"
    edited.password = "new-secret"
    edited.activity_window_seconds = 90
    await repository.upsert_device(edited)

    merged = await repository.apply_device_discovery(
        DeviceDiscoveryUpdate(
            device_id="camera-1",
            integration_type="onvif",
            capabilities=("video", "events"),
            metadata={"model": "Model X"},
        )
    )

    assert merged is not None
    stored = await repository.get_device("camera-1")
    assert stored is not None
    assert stored.name == "Renamed camera"
    assert stored.ip_address == "192.0.2.20"
    assert stored.username == "new-user"
    assert stored.password == "new-secret"
    assert stored.activity_window_seconds == 90
    assert stored.event_filter == ["motion"]
    assert stored.capabilities == ["camera", "video", "events"]
    assert stored.metadata == {"operator": {"note": "keep"}, "onvif": {"model": "Model X"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "setup_state"),
    [(False, "ready"), (True, "needs_setup")],
)
async def test_discovery_ignores_nonparticipating_device(repository, enabled, setup_state):
    await repository.upsert_device(_device(enabled=enabled, setup_state=setup_state))

    result = await repository.apply_device_discovery(
        DeviceDiscoveryUpdate(
            device_id="camera-1",
            integration_type="onvif",
            capabilities=("events",),
            metadata={"model": "Should not persist"},
        )
    )

    assert result is None
    stored = await repository.get_device("camera-1")
    assert stored is not None
    assert stored.metadata == {"operator": {"note": "keep"}}
    assert stored.capabilities == ["camera"]


@pytest.mark.asyncio
async def test_discovery_does_not_resurrect_deleted_or_removed_integration(repository):
    await repository.upsert_device(_device())
    await repository.delete_device("camera-1")

    update = DeviceDiscoveryUpdate(
        device_id="camera-1",
        integration_type="onvif",
        capabilities=("events",),
        metadata={"model": "Should not resurrect"},
    )
    assert await repository.apply_device_discovery(update) is None
    assert await repository.get_device("camera-1") is None

    await repository.upsert_device(_device())
    removed = _device(configs={})
    await repository.upsert_device(removed)
    assert await repository.apply_device_discovery(update) is None
    stored = await repository.get_device("camera-1")
    assert stored is not None
    assert stored.configs == {}
    assert stored.metadata == {"operator": {"note": "keep"}}


@pytest.mark.asyncio
async def test_discovery_preserves_manual_video_endpoint_and_settings(repository):
    await repository.upsert_device(
        _device(
            configs={
                "reolink": CapabilityConfig(),
                "video": CapabilityConfig(
                    protocol="rtsp",
                    port=8554,
                    path="/operator-selected",
                    settings={
                        "recording_mode": "on_episode",
                        "recording_source_id": "reolink:rtsp:sub",
                    },
                ),
            }
        )
    )

    await repository.apply_device_discovery(
        DeviceDiscoveryUpdate(
            device_id="camera-1",
            integration_type="reolink",
            video_if_unconfigured=CapabilityConfig(
                protocol="rtsp",
                port=554,
                path="/camera-discovered",
                settings={"recording_mode": "on_event", "origin": "reolink"},
            ),
        )
    )

    stored = await repository.get_device("camera-1")
    assert stored is not None
    video = stored.get_config("video")
    assert video is not None
    assert video.protocol == "rtsp"
    assert video.port == 8554
    assert video.path == "/operator-selected"
    assert video.settings == {
        "recording_mode": "on_episode",
        "recording_source_id": "reolink:rtsp:sub",
    }


@pytest.mark.asyncio
async def test_reolink_discovery_fills_only_an_unconfigured_video(repository):
    await repository.upsert_device(
        _device(
            configs={
                "reolink": CapabilityConfig(),
                "video": CapabilityConfig(
                    settings={
                        "recording_mode": "on_episode",
                        "recording_source_id": "reolink:native:main",
                    }
                ),
            }
        )
    )

    await repository.apply_device_discovery(
        DeviceDiscoveryUpdate(
            device_id="camera-1",
            integration_type="reolink",
            metadata={"model": "Reolink"},
            video_if_unconfigured=CapabilityConfig(
                protocol="rtsp",
                port=554,
                path="/discovered",
                settings={"recording_mode": "on_event", "origin": "reolink"},
            ),
        )
    )

    stored = await repository.get_device("camera-1")
    assert stored is not None
    video = stored.get_config("video")
    assert video is not None
    assert (video.protocol, video.port, video.path) == ("rtsp", 554, "/discovered")
    assert video.settings == {
        "recording_mode": "on_episode",
        "recording_source_id": "reolink:native:main",
        "origin": "reolink",
    }


@pytest.mark.asyncio
async def test_onvif_discovery_never_creates_video_config(repository):
    await repository.upsert_device(_device(configs={"onvif": CapabilityConfig()}))

    await repository.apply_device_discovery(
        DeviceDiscoveryUpdate(
            device_id="camera-1",
            integration_type="onvif",
            capabilities=("video", "events"),
            metadata={"profile_token": "main"},
        )
    )

    stored = await repository.get_device("camera-1")
    assert stored is not None
    assert stored.get_config("video") is None
    assert stored.metadata["onvif"] == {"profile_token": "main"}
