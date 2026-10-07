from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


@dataclass
class ConnectorConfig:
    type: str = ""
    enabled: bool = True
    settings: dict = field(default_factory=dict)


@dataclass
class ExternalPluginConfig:
    """Explicit activation and scoped configuration for an installed plugin."""

    id: str = ""
    enabled: bool = True
    device_ids: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", self.id):
            raise ValueError("plugin id must use lowercase letters, numbers, dots, _ or -")
        if not isinstance(self.device_ids, list) or not all(
            isinstance(device_id, str) and device_id for device_id in self.device_ids
        ):
            raise ValueError(f"plugin {self.id!r} device_ids must be non-empty strings")
        if len(set(self.device_ids)) != len(self.device_ids):
            raise ValueError(f"plugin {self.id!r} contains duplicate device_ids")
        if not isinstance(self.settings, dict):
            raise ValueError(f"plugin {self.id!r} settings must be an object")


@dataclass
class SnapshotActionConfig:
    enabled: bool = False
    event_types: tuple[str, ...] = ("doorbell",)

    def __post_init__(self) -> None:
        if not isinstance(self.event_types, (tuple, list)):
            raise ValueError("snapshot event_types must be a list of canonical event types")
        event_types = tuple(self.event_types)
        if len(event_types) > 64 or any(
            not isinstance(event_type, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", event_type)
            for event_type in event_types
        ):
            raise ValueError("snapshot event_types must contain valid canonical event types")
        if len(set(event_types)) != len(event_types):
            raise ValueError("snapshot event_types cannot contain duplicates")
        self.event_types = event_types


@dataclass
class RecordingActionConfig:
    fragment_seconds: int = 4

    def __post_init__(self):
        if not 1 <= self.fragment_seconds <= 30:
            raise ValueError("recording fragment_seconds must be between 1 and 30")


@dataclass
class ActionsConfig:
    snapshot: SnapshotActionConfig = field(default_factory=SnapshotActionConfig)
    recording: RecordingActionConfig = field(default_factory=RecordingActionConfig)


@dataclass
class EpisodeConfig:
    data_dir: str = "/var/episode/data"
    plugins_dir: str = "/opt/episode/plugins"
    db_path: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8989
    episode_timeout: int = 30
    snapshot_window: int = 1
    log_level: str = "INFO"
    actions: ActionsConfig = field(default_factory=ActionsConfig)
    connectors: list[ConnectorConfig] = field(default_factory=list)
    plugins: list[ExternalPluginConfig] = field(default_factory=list)

    def __post_init__(self):
        if not self.db_path:
            self.db_path = os.path.join(self.data_dir, "episode.db")
        if isinstance(self.actions, dict):
            snapshot = self.actions.get("snapshot", {})
            recording = self.actions.get("recording", {})
            self.actions = ActionsConfig(
                snapshot=SnapshotActionConfig(**snapshot)
                if isinstance(snapshot, dict)
                else snapshot,
                recording=RecordingActionConfig(**recording)
                if isinstance(recording, dict)
                else recording,
            )
        if self.plugins and isinstance(self.plugins[0], dict):
            self.plugins = [
                ExternalPluginConfig(**plugin) if isinstance(plugin, dict) else plugin
                for plugin in self.plugins
            ]
        plugin_ids = [plugin.id for plugin in self.plugins]
        if len(set(plugin_ids)) != len(plugin_ids):
            raise ValueError("plugin configuration contains duplicate ids")


def load_config(path: str | None = None) -> EpisodeConfig:
    if path is None:
        path = os.environ.get("EPISODE_CONFIG", "")
    if path and os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
        raw["connectors"] = [ConnectorConfig(**c) for c in raw.pop("connectors", [])]
        raw["plugins"] = [ExternalPluginConfig(**p) for p in raw.pop("plugins", [])]
        try:
            return EpisodeConfig(**raw)
        except TypeError as error:
            raise ValueError(f"Invalid Episode configuration: {error}") from error
    return EpisodeConfig()
