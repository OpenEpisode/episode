from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HLS_MIME_TYPE = "application/vnd.apple.mpegurl"
PLAYLIST_NAME = "index.m3u8"
COMPONENT_MANIFEST_NAME = "manifest.json"
CAPTURE_STATE_NAME = "capture.json"

_SEGMENT_INDEX = re.compile(r"segment-(?P<index>\d+)\.m4s$")


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = (
        value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    )
    return normalized.isoformat(timespec="microseconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class HLSCaptureState:
    evidence_id: str
    episode_id: str
    device_id: str
    area_id: str
    session_id: str
    started_at: datetime
    video_source_id: str = ""


class HLSRecordingBundle:
    """Filesystem representation of one logical recording Evidence item."""

    def __init__(self, root: Path, state: HLSCaptureState):
        self.root = root
        self.state = state
        self._checksums: dict[str, tuple[int, int, str]] = {}

    @property
    def playlist_path(self) -> Path:
        return self.root / PLAYLIST_NAME

    @property
    def component_manifest_path(self) -> Path:
        return self.root / COMPONENT_MANIFEST_NAME

    @property
    def capture_state_path(self) -> Path:
        return self.root / CAPTURE_STATE_NAME

    @property
    def segment_pattern(self) -> str:
        return str(self.root / "segments" / "segment-%06d.m4s")

    @classmethod
    def create(cls, root: Path, state: HLSCaptureState) -> HLSRecordingBundle:
        bundle = cls(root, state)
        (root / "segments").mkdir(parents=True, exist_ok=True)
        bundle.write_capture_state()
        bundle.refresh_manifest(state="recording")
        return bundle

    @classmethod
    def load(cls, capture_state_path: Path) -> HLSRecordingBundle:
        raw = json.loads(capture_state_path.read_text(encoding="utf-8"))
        started_at = datetime.fromisoformat(raw["started_at"])
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        state = HLSCaptureState(
            evidence_id=raw["evidence_id"],
            episode_id=raw["episode_id"],
            device_id=raw["device_id"],
            area_id=raw["area_id"],
            session_id=raw["session_id"],
            started_at=started_at,
            video_source_id=str(raw.get("video_source_id", "")),
        )
        return cls(capture_state_path.parent, state)

    @classmethod
    def load_from_evidence(cls, entrypoint: Path, evidence: Any) -> HLSRecordingBundle:
        metadata = evidence.metadata
        started_at = datetime.fromisoformat(metadata["started_at"])
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        return cls(
            entrypoint.parent,
            HLSCaptureState(
                evidence_id=evidence.id,
                episode_id=evidence.episode_id or "",
                device_id=evidence.device_id,
                area_id=evidence.area_id,
                session_id=str(metadata.get("recording_session_id", "")),
                started_at=started_at,
            ),
        )

    def write_capture_state(self) -> None:
        _atomic_json(
            self.capture_state_path,
            {
                "format": "episode.hls-capture",
                "version": 1,
                "evidence_id": self.state.evidence_id,
                "episode_id": self.state.episode_id,
                "device_id": self.state.device_id,
                "area_id": self.state.area_id,
                "session_id": self.state.session_id,
                "started_at": _utc_iso(self.state.started_at),
                "video_source_id": self.state.video_source_id,
            },
        )

    def next_segment_index(self) -> int:
        indexes = []
        for path in (self.root / "segments").glob("segment-*.m4s"):
            match = _SEGMENT_INDEX.fullmatch(path.name)
            if match:
                indexes.append(int(match.group("index")))
        return max(indexes, default=-1) + 1

    def preserve_temporary_components(self) -> None:
        incomplete = self.root / "incomplete"
        for path in self.root.rglob("*.tmp"):
            if incomplete in path.parents:
                continue
            if path.name.startswith(f".{COMPONENT_MANIFEST_NAME}"):
                continue
            incomplete.mkdir(exist_ok=True)
            target = incomplete / path.name
            suffix = 1
            while target.exists():
                target = incomplete / f"{path.name}.{suffix}"
                suffix += 1
            os.replace(path, target)

    def ensure_endlist(self) -> None:
        if not self.playlist_path.exists():
            return
        content = self.playlist_path.read_text(encoding="utf-8")
        if "#EXT-X-ENDLIST" in content:
            return
        temporary = self.playlist_path.with_suffix(".m3u8.tmp")
        temporary.write_text(content.rstrip() + "\n#EXT-X-ENDLIST\n", encoding="utf-8")
        os.replace(temporary, self.playlist_path)

    def refresh_manifest(
        self,
        *,
        state: str,
        ended_at: datetime | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        observations = self._playlist_observations()
        components = []
        total_bytes = 0
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name in {COMPONENT_MANIFEST_NAME, CAPTURE_STATE_NAME}:
                continue
            if path.name.startswith(f".{COMPONENT_MANIFEST_NAME}") or path.suffix == ".tmp":
                continue
            try:
                stat = path.stat()
                size = stat.st_size
                relative = path.relative_to(self.root).as_posix()
                cache_key = relative
                fingerprint = (size, stat.st_mtime_ns)
                cached = self._checksums.get(cache_key)
                checksum = cached[2] if cached and cached[:2] == fingerprint else _sha256(path)
            except FileNotFoundError:
                continue
            self._checksums[cache_key] = (*fingerprint, checksum)
            component: dict[str, Any] = {
                "path": relative,
                "byte_size": size,
                "sha256": checksum,
            }
            match = _SEGMENT_INDEX.fullmatch(path.name)
            if match:
                component["kind"] = "media_segment"
                component["sequence"] = int(match.group("index"))
                component.update(observations.get(relative, {}))
                self._seal(path)
            elif path.name == PLAYLIST_NAME:
                component["kind"] = "playlist"
            elif path.name == "init.mp4":
                component["kind"] = "initialization"
            else:
                component["kind"] = "incomplete" if "incomplete" in path.parts else "component"
            components.append(component)
            total_bytes += size

        segments = [item for item in components if item["kind"] == "media_segment"]
        manifest: dict[str, Any] = {
            "format": "episode.recording-bundle",
            "version": 1,
            "evidence_id": self.state.evidence_id,
            "episode_id": self.state.episode_id,
            "device_id": self.state.device_id,
            "area_id": self.state.area_id,
            "session_id": self.state.session_id,
            "state": state,
            "started_at": _utc_iso(self.state.started_at),
            "ended_at": _utc_iso(ended_at),
            "entrypoint": PLAYLIST_NAME if self.playlist_path.exists() else None,
            "component_count": len(components),
            "fragment_count": len(segments),
            "total_bytes": total_bytes,
            "components": components,
        }
        if reason:
            manifest["reason"] = reason
        _atomic_json(self.component_manifest_path, manifest)
        return manifest

    def _playlist_observations(self) -> dict[str, dict[str, Any]]:
        if not self.playlist_path.exists():
            return {}
        observations: dict[str, dict[str, Any]] = {}
        duration: float | None = None
        program_time: str | None = None
        try:
            lines = self.playlist_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return {}
        for line in lines:
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                program_time = line.partition(":")[2].strip()
            elif line.startswith("#EXTINF:"):
                try:
                    duration = float(line.partition(":")[2].partition(",")[0])
                except ValueError:
                    duration = None
            elif line and not line.startswith("#") and line.endswith(".m4s"):
                observations[line] = {
                    **({"duration_seconds": duration} if duration is not None else {}),
                    **({"started_at": program_time} if program_time else {}),
                }
                duration = None
                program_time = None
        return observations

    def prepare_finalize(self, *, ended_at: datetime, reason: str | None = None) -> dict[str, Any]:
        self.preserve_temporary_components()
        self.ensure_endlist()
        return self.refresh_manifest(state="complete", ended_at=ended_at, reason=reason)

    def complete_publication(self) -> None:
        try:
            self.capture_state_path.unlink()
        except FileNotFoundError:
            pass
        for path in self.root.rglob("*"):
            if path.is_file():
                self._seal(path)

    def finalize(self, *, ended_at: datetime, reason: str | None = None) -> dict[str, Any]:
        manifest = self.prepare_finalize(ended_at=ended_at, reason=reason)
        self.complete_publication()
        return manifest

    def resolve_component(self, component_path: str) -> Path | None:
        if component_path.startswith(("/", ".")):
            return None
        candidate = (self.root / component_path).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def component_manifest_sha256(self) -> str:
        return _sha256(self.component_manifest_path)

    @staticmethod
    def _seal(path: Path) -> None:
        try:
            os.chmod(path, path.stat().st_mode & ~0o222)
        except OSError:
            pass
