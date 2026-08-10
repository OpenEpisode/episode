from types import SimpleNamespace

import pytest

from episode.config import EpisodeConfig
from episode.connectors.onvif.connector import ONVIFConnector
from episode.domain.models import CapabilityConfig, Device
from episode.engine.bus import EventBus
from episode.media.registry import MediaRegistry


def _connector(settings=None):
    return ONVIFConnector(
        "ONVIF:Test",
        EventBus(),
        settings or {},
        EpisodeConfig(),
        Device(
            id="camera-test",
            name="Test camera",
            area_id="test-area",
            ip_address="192.0.2.10",
            username="user",
            password="password",
        ),
        repo=object(),
        media=MediaRegistry(),
    )


def test_onvif_events_are_disabled_by_default():
    connector = _connector()

    assert connector._events_enabled is False


def test_onvif_events_can_be_enabled_explicitly():
    connector = _connector({"events_enabled": True})

    assert connector._events_enabled is True


@pytest.mark.asyncio
async def test_onvif_discovery_keeps_manual_rtsp_fallback_separate():
    connector = _connector()
    connector._configured_device.configs["video"] = CapabilityConfig(
        protocol="rtsp",
        port=8554,
        path="/manual",
        settings={"recording_mode": "on_episode"},
    )
    connector._onvif_device = SimpleNamespace(
        manufacturer="Example",
        model="Camera",
        firmware_version="1.0",
        event_topics=[],
        profiles=[],
        services={},
    )

    class Repository:
        saved = None

        async def upsert_device(self, device):
            self.saved = device

    repository = Repository()
    connector._repo = repository
    profile = SimpleNamespace(
        token="auto-main",
        stream_uri="rtsp://192.0.2.10/discovered",
        snapshot_uri="http://192.0.2.10/snapshot",
    )

    await connector._apply_discovered_capabilities(profile)

    video = connector._configured_device.get_config("video")
    assert video.build_url("192.0.2.10") == "rtsp://192.0.2.10:8554/manual"
    assert video.settings == {"recording_mode": "on_episode"}
    assert connector._configured_device.metadata["onvif"]["profile_token"] == "auto-main"
    assert repository.saved is connector._configured_device
