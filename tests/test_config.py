import json

import pytest

from episode.config import EpisodeConfig, load_config


def test_all_event_snapshot_action_is_disabled_by_default():
    assert EpisodeConfig().actions.snapshot.enabled is False
    assert EpisodeConfig().actions.snapshot.event_types == ("doorbell",)


def test_all_event_snapshot_action_can_be_enabled_explicitly():
    config = EpisodeConfig(actions={"snapshot": {"enabled": True}})

    assert config.actions.snapshot.enabled is True


def test_snapshot_event_types_can_be_configured():
    config = EpisodeConfig(actions={"snapshot": {"event_types": ["doorbell", "tamper_detection"]}})

    assert config.actions.snapshot.event_types == ("doorbell", "tamper_detection")


@pytest.mark.parametrize(
    "event_types",
    (["Doorbell"], [""], ["doorbell", "doorbell"], ["x" * 65], [None]),
)
def test_snapshot_event_types_must_be_valid_unique_canonical_types(event_types):
    with pytest.raises(ValueError, match="snapshot event_types"):
        EpisodeConfig(actions={"snapshot": {"event_types": event_types}})


def test_recording_fragments_default_to_four_seconds():
    assert EpisodeConfig().actions.recording.fragment_seconds == 4


def test_recording_fragment_duration_can_be_configured():
    config = EpisodeConfig(actions={"recording": {"fragment_seconds": 6}})

    assert config.actions.recording.fragment_seconds == 6


def test_recording_fragment_duration_must_be_bounded():
    with pytest.raises(ValueError, match="fragment_seconds must be between 1 and 30"):
        EpisodeConfig(actions={"recording": {"fragment_seconds": 0}})


def test_obsolete_inventory_configuration_is_rejected(tmp_path):
    path = tmp_path / "episode.json"
    path.write_text(json.dumps({"areas": []}))

    with pytest.raises(ValueError, match="unexpected keyword argument 'areas'"):
        load_config(str(path))
