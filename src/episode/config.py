from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class ConnectorConfig:
    type: str = ""
    enabled: bool = True
    settings: dict = field(default_factory=dict)


@dataclass
class SnapshotActionConfig:
    enabled: bool = False


@dataclass
class RecordingActionConfig:
    segment_seconds: int = 600

    def __post_init__(self):
        if self.segment_seconds <= 0:
            raise ValueError("recording segment_seconds must be greater than zero")


@dataclass
class ActionsConfig:
    snapshot: SnapshotActionConfig = field(default_factory=SnapshotActionConfig)
    recording: RecordingActionConfig = field(default_factory=RecordingActionConfig)


@dataclass
class EpisodeConfig:
    data_dir: str = "/var/episode/data"
    orphans_dir: str = ""
    db_path: str = ""
    evidence_dir: str = ""
    events_dir: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8989
    episode_timeout: int = 30
    snapshot_window: int = 1
    log_level: str = "INFO"
    actions: ActionsConfig = field(default_factory=ActionsConfig)
    connectors: list[ConnectorConfig] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)
    areas: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if not self.orphans_dir:
            self.orphans_dir = os.path.join(self.data_dir, "orphans")
        if not self.db_path:
            self.db_path = os.path.join(self.data_dir, "episode.db")
        if not self.evidence_dir:
            self.evidence_dir = os.path.join(self.data_dir, "orphans")
        if not self.events_dir:
            self.events_dir = os.path.join(self.data_dir, "orphans")
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


def load_config(path: str | None = None) -> EpisodeConfig:
    if path is None:
        path = os.environ.get("EPISODE_CONFIG", "")
    if path and os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
        raw.pop("recording", None)
        raw["connectors"] = [ConnectorConfig(**c) for c in raw.pop("connectors", [])]
        return EpisodeConfig(**raw)
    return EpisodeConfig()
