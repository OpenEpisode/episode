from __future__ import annotations

import pytest

from episode.media.registry import MediaRegistry
from episode.plugins.onvif.client import ONVIFDevice, ONVIFProfile
from episode.plugins.onvif.device import ONVIFDeviceConnection, device_config


def _device(*, events_enabled: bool = False, **overrides):
    value = {
        "id": "camera-test",
        "name": "Test camera",
        "device_type": "camera",
        "area_id": "test-area",
        "ip_address": "192.0.2.10",
        "username": "user",
        "password": "password",
        "capabilities": ["onvif", "video"],
        "configs": {
            "onvif": {
                "protocol": "http",
                "port": 80,
                "path": "/onvif/device_service",
                "settings": {"events_enabled": events_enabled},
            },
            "video": {
                "protocol": "rtsp",
                "port": 8554,
                "path": "/manual",
                "settings": {"recording_mode": "on_episode"},
            },
        },
    }
    value.update(overrides)
    return value


def test_onvif_events_are_disabled_by_default():
    config, error = device_config(_device())

    assert error is None
    assert config.events_enabled is False


def test_onvif_events_can_be_enabled_explicitly():
    config, error = device_config(_device(events_enabled=True))

    assert error is None
    assert config.events_enabled is True


def test_invalid_onvif_timeout_is_isolated_as_configuration_error():
    value = _device()
    value["configs"]["onvif"]["settings"]["timeout"] = "not-a-number"
    config, error = device_config(value)

    assert config is None


@pytest.mark.asyncio
async def test_onvif_discovery_keeps_manual_rtsp_fallback_separate():
    config, error = device_config(_device())
    assert error is None
    discovered = ONVIFDevice(
        manufacturer="Example",
        model="Camera",
        firmware_version="1.0",
        profiles=[
            ONVIFProfile(
                token="auto-main",
                stream_uri="rtsp://192.0.2.10/discovered",
                snapshot_uri="http://192.0.2.10/snapshot",
            )
        ],
    )

    class Client:
        async def discover(self):
            return discovered

        async def close(self):
            pass

        async def unsubscribe(self, _url):
            pass

    saved = []

    async def save(device):
        saved.append(device)

    async def sink(_delivery):
        pass

    media = MediaRegistry()
    connection = ONVIFDeviceConnection(
        config,
        sink,
        media,
        save,
        client_factory=lambda _config: Client(),
    )

    await connection._discover()

    video = config.device.get_config("video")
    assert video.build_url("192.0.2.10") == "rtsp://192.0.2.10:8554/manual"
    assert video.settings == {"recording_mode": "on_episode"}
    assert config.device.metadata["onvif"]["profile_token"] == "auto-main"
    assert saved == [config.device]
    assert media.get("camera-test").stream_uri == "rtsp://192.0.2.10/discovered"


@pytest.mark.asyncio
async def test_onvif_discovery_does_not_enable_disabled_recording():
    value = _device()
    del value["configs"]["video"]
    config, error = device_config(value)
    assert error is None
    discovered = ONVIFDevice(
        manufacturer="Example",
        model="Camera",
        firmware_version="1.0",
        profiles=[
            ONVIFProfile(
                token="auto-main",
                stream_uri="rtsp://192.0.2.10/discovered",
                snapshot_uri="http://192.0.2.10/snapshot",
            )
        ],
    )

    class Client:
        async def discover(self):
            return discovered

        async def close(self):
            pass

        async def unsubscribe(self, _url):
            pass

    async def sink(_delivery):
        pass

    async def save(_device):
        pass

    connection = ONVIFDeviceConnection(
        config,
        sink,
        MediaRegistry(),
        save,
        client_factory=lambda _config: Client(),
    )

    await connection._discover()

    assert config.device.get_config("video") is None
    assert "video" in config.device.capabilities
