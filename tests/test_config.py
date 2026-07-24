from episode.config import EpisodeConfig


def test_onvif_snapshot_action_is_disabled_by_default():
    assert EpisodeConfig().actions.snapshot.enabled is False


def test_onvif_snapshot_action_can_be_enabled_explicitly():
    config = EpisodeConfig(actions={"snapshot": {"enabled": True}})

    assert config.actions.snapshot.enabled is True
