from __future__ import annotations

import hashlib
import json
import math
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

    def preserve_temporary_components(self) -> list[str]:
        incomplete = self.root / "incomplete"
        preserved = []
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
            preserved.append(target.relative_to(self.root).as_posix())
        return sorted(preserved)

    def validate_playlist(self, *, require_endlist: bool = False) -> dict[str, Any]:
        """Check that the HLS entrypoint references complete, present bundle components."""
        segment_files = sorted(
            path for path in (self.root / "segments").glob("segment-*.m4s") if path.is_file()
        )
        empty_fragment_count = 0
        for path in segment_files:
            try:
                if path.is_file() and path.stat().st_size == 0:
                    empty_fragment_count += 1
            except OSError:
                # An unreferenced fragment must not prevent recording what the
                # playlist says is playable; referenced components are checked below.
                continue
        result: dict[str, Any] = {
            "valid": False,
            "error": None,
            "referenced_fragment_count": 0,
            "unreferenced_fragment_count": len(segment_files),
            "empty_fragment_count": empty_fragment_count,
        }
        playlist = self.resolve_component(PLAYLIST_NAME)
        if playlist is None:
            result["error"] = "playlist_missing"
            return result

        try:
            lines = playlist.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            result["error"] = "playlist_unreadable"
            return result

        if not lines or lines[0].strip() != "#EXTM3U":
            result["error"] = "playlist_header_missing"
            return result

        init_references = []
        segment_references = []
        pending_duration: float | None = None
        endlist_found = False
        map_available = False
        for line in lines[1:]:
            line = line.strip()
            if line.startswith("#EXTINF:"):
                if pending_duration is not None:
                    result["error"] = "playlist_segment_reference_missing"
                    return result
                if not map_available:
                    result["error"] = "playlist_initialization_reference_missing"
                    return result
                raw_duration = line.partition(":")[2].partition(",")[0]
                try:
                    duration = float(raw_duration)
                except ValueError:
                    result["error"] = "playlist_segment_duration_invalid"
                    return result
                if not math.isfinite(duration) or duration <= 0:
                    result["error"] = "playlist_segment_duration_invalid"
                    return result
                pending_duration = duration
            elif line.startswith("#EXT-X-MAP:"):
                match = re.search(r'(?:^|,)URI="([^"]+)"', line.partition(":")[2])
                if not match:
                    result["error"] = "playlist_initialization_reference_missing"
                    return result
                init_references.append(match.group(1))
                map_available = True
            elif line == "#EXT-X-ENDLIST":
                endlist_found = True
            elif line and not line.startswith("#"):
                if pending_duration is None or not line.endswith(".m4s"):
                    result["error"] = "playlist_segment_reference_invalid"
                    return result
                segment_references.append(line)
                pending_duration = None

        result["referenced_fragment_count"] = len(segment_references)
        result["unreferenced_fragment_count"] = max(
            0,
            len(segment_files) - len(segment_references),
        )

        if pending_duration is not None:
            result["error"] = "playlist_segment_reference_missing"
            return result
        if not init_references:
            result["error"] = "playlist_initialization_reference_missing"
            return result
        for reference in init_references:
            component = self.resolve_component(reference)
            if component is None:
                result["error"] = "playlist_initialization_component_missing"
                return result
            try:
                if component.stat().st_size <= 0:
                    result["error"] = "playlist_initialization_component_empty"
                    return result
            except OSError:
                result["error"] = "playlist_initialization_component_missing"
                return result
        if not segment_references:
            result["error"] = "playlist_segments_missing"
            return result
        for reference in segment_references:
            component = self.resolve_component(reference)
            if component is None:
                result["error"] = "playlist_segment_component_missing"
                return result
            try:
                if component.stat().st_size <= 0:
                    result["error"] = "playlist_segment_component_empty"
                    return result
            except OSError:
                result["error"] = "playlist_segment_component_missing"
                return result
        if require_endlist and not endlist_found:
            result["error"] = "playlist_endlist_missing"
            return result

        result.update(
            valid=True,
            error=None,
            referenced_fragment_count=len(segment_references),
            unreferenced_fragment_count=max(0, len(segment_files) - len(segment_references)),
        )
        return result

    def ensure_endlist(self) -> bool:
        if not self.playlist_path.exists():
            return False
        try:
            content = self.playlist_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if "#EXT-X-ENDLIST" in content:
            return True
        if not self.validate_playlist()["valid"]:
            return False
        temporary = self.playlist_path.with_suffix(".m3u8.tmp")
        try:
            temporary.write_text(content.rstrip() + "\n#EXT-X-ENDLIST\n", encoding="utf-8")
            os.replace(temporary, self.playlist_path)
            return True
        except OSError:
            return False

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
            if path.name.startswith(f".{COMPONENT_MANIFEST_NAME}") or (
                path.suffix == ".tmp" and "incomplete" not in path.parts
            ):
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

    def prepare_finalize(
        self,
        *,
        ended_at: datetime,
        reason: str | None = None,
        incomplete: bool = False,
    ) -> dict[str, Any]:
        preserved_temporary = self.preserve_temporary_components()
        validation = self.validate_playlist()
        validation["preserved_temporary_component_count"] = len(preserved_temporary)
        validation["playlist_temporary_preserved"] = any(
            Path(path).name.startswith(f"{PLAYLIST_NAME}.tmp") for path in preserved_temporary
        )
        validation["temporary_components_preserved"] = bool(preserved_temporary)
        if validation["valid"]:
            if not self.ensure_endlist():
                preserved_temporary.extend(self.preserve_temporary_components())
            validation = self.validate_playlist(require_endlist=True)
            validation["preserved_temporary_component_count"] = len(preserved_temporary)
            validation["playlist_temporary_preserved"] = any(
                Path(path).name.startswith(f"{PLAYLIST_NAME}.tmp") for path in preserved_temporary
            )
            validation["temporary_components_preserved"] = bool(preserved_temporary)

        output_complete = (
            validation["valid"]
            and not incomplete
            and not validation["temporary_components_preserved"]
        )
        state = "complete" if output_complete else "incomplete"
        manifest = self.refresh_manifest(state=state, ended_at=ended_at, reason=reason)
        manifest["entrypoint"] = PLAYLIST_NAME if validation["valid"] else None
        manifest["playlist_validation"] = validation
        _atomic_json(self.component_manifest_path, manifest)
        return manifest

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
        try:
            root = self.root.resolve(strict=True)
            candidate = (self.root / component_path).resolve(strict=True)
            candidate.relative_to(root)
            return candidate if candidate.is_file() else None
        except (OSError, RuntimeError, ValueError):
            return None

    def component_manifest_sha256(self) -> str:
        return _sha256(self.component_manifest_path)

    @staticmethod
    def _seal(path: Path) -> None:
        try:
            os.chmod(path, path.stat().st_mode & ~0o222)
        except OSError:
            pass
