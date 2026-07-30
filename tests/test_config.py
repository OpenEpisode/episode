import pytest

from episode.config import EpisodeConfig


def test_onvif_snapshot_action_is_disabled_by_default():
    assert EpisodeConfig().actions.snapshot.enabled is False


def test_onvif_snapshot_action_can_be_enabled_explicitly():
    config = EpisodeConfig(actions={"snapshot": {"enabled": True}})

    assert config.actions.snapshot.enabled is True


def test_recording_segments_default_to_ten_minutes():
    assert EpisodeConfig().actions.recording.segment_seconds == 600


def test_recording_segment_duration_can_be_configured():
    config = EpisodeConfig(actions={"recording": {"segment_seconds": 120}})

    assert config.actions.recording.segment_seconds == 120


def test_recording_segment_duration_must_be_positive():
    with pytest.raises(ValueError, match="segment_seconds must be greater than zero"):
        EpisodeConfig(actions={"recording": {"segment_seconds": 0}})
