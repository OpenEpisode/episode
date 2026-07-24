from episode.config import EpisodeConfig
from episode.connectors.onvif.connector import ONVIFConnector
from episode.domain.models import Device
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
