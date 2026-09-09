from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from episode.domain.models import Device, EpisodeState, EventState, Evidence
from episode.engine.bus import EventBus, Message
from episode.engine.engine import CanonicalEventResult
from episode.recording.hls import (
    CAPTURE_STATE_NAME,
    HLS_MIME_TYPE,
    HLSCaptureState,
    HLSRecordingBundle,
)
from episode.recording.targets import AreaRecordingTargetResolver, RecordingTargetResolver

if TYPE_CHECKING:
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

_RECORDING_PART = re.compile(
    r"^rec_(?P<device>.+)_(?P<started>\d{8}_\d{6}_\d{6})_"
    r"(?P<session>[0-9a-f]{12})_(?P<index>\d{6})\.mp4\.part$"
)


@dataclass
class _EpisodeRecording:
    episode_id: str
    device_id: str
    area_id: str
    session_id: str
    bundle: HLSRecordingBundle
    start_time: datetime
    rtsp_url: str = ""
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    stop_reason: str | None = None
    continued: bool = False
    published: bool = False
    state: str = "starting"
    fragment_count: int = 0
    last_fragment_at: datetime | None = None
    reconnect_count: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None

    @property
    def evidence_id(self) -> str:
        return self.bundle.state.evidence_id

    @property
    def output_path(self) -> str:
        return str(self.bundle.playlist_path)

    @property
    def working_path(self) -> str:
        return self.bundle.segment_pattern

    @property
    def next_segment_index(self) -> int:
        return self.bundle.next_segment_index()


class RecordingEngine:
    """Capture each Device recording as one crash-recoverable HLS Evidence bundle."""

    def __init__(
        self,
        repo: Repository,
        bus: EventBus,
        data_dir: str,
        fragment_seconds: int = 4,
        media=None,
        target_resolver: RecordingTargetResolver | None = None,
    ):
        if fragment_seconds <= 0:
            raise ValueError("fragment_seconds must be greater than zero")
        self._repo = repo
        self._bus = bus
        self._data_dir = data_dir
        self._fragment_seconds = fragment_seconds
        self._media = media
        self._target_resolver = target_resolver or AreaRecordingTargetResolver(repo)
        self._active_tasks: set[asyncio.Task] = set()
        self._recordings: dict[tuple[str, str], _EpisodeRecording] = {}
        self._recoverable: dict[tuple[str, str], _EpisodeRecording] = {}
        self._running = False
        self._stall_seconds = max(60, fragment_seconds * 6)
        self._completed_count = 0
        self._incomplete_count = 0
        self._reconnect_count = 0
        self._failure_count = 0
        self._stalled_count = 0
        self._last_completed_at: datetime | None = None
        self._last_error: str | None = None

    @staticmethod
    def _rec_key(episode_id: str, device_id: str) -> tuple[str, str]:
        return (episode_id, device_id)

    @staticmethod
    def _safe_device_id(device_id: str) -> str:
        value = re.sub(r"[^A-Za-z0-9._-]+", "-", device_id).strip("._-")
        return (value or "device")[:64]

    def active_device_ids(self, episode_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                recording.device_id
                for recording in self._recordings.values()
                if recording.episode_id == episode_id
            )
        )

    def active_recordings(self, episode_id: str) -> tuple[dict[str, object], ...]:
        return tuple(
            self._recording_diagnostic(recording)
            for recording in self._recordings.values()
            if recording.episode_id == episode_id
        )

    def _observe_progress(self, recording: _EpisodeRecording) -> bool:
        fragment_count = recording.bundle.next_segment_index()
        if fragment_count <= recording.fragment_count:
            return False
        recording.fragment_count = fragment_count
        fragments = list((recording.bundle.root / "segments").glob("segment-*.m4s"))
        if fragments:
            latest = max(fragments, key=lambda path: path.stat().st_mtime_ns)
            recording.last_fragment_at = datetime.fromtimestamp(
                latest.stat().st_mtime,
                tz=timezone.utc,
            )
        recording.state = "recording"
        recording.last_error = None
        return True

    def _recording_diagnostic(self, recording: _EpisodeRecording) -> dict[str, object]:
        self._observe_progress(recording)
        return {
            "evidence_id": recording.evidence_id,
            "episode_id": recording.episode_id,
            "device_id": recording.device_id,
            "started_at": recording.start_time,
            "state": recording.state,
            "ready": recording.bundle.playlist_path.exists(),
            "fragment_count": recording.fragment_count,
            "last_fragment_at": recording.last_fragment_at,
            "reconnect_count": recording.reconnect_count,
            "last_exit_code": recording.last_exit_code,
            "last_error": recording.last_error,
        }

    def active_bundle(self, evidence_id: str) -> HLSRecordingBundle | None:
        return next(
            (
                recording.bundle
                for recording in self._recordings.values()
                if recording.evidence_id == evidence_id
            ),
            None,
        )

    def active_file_paths(self) -> set[str]:
        return {
            str(path)
            for recording in self._recordings.values()
            for path in recording.bundle.root.rglob("*")
            if path.is_file()
        }

    async def start(self) -> None:
        self._running = True
        self._bus.subscribe("event.canonicalized", self._on_event)
        self._bus.subscribe("episode.updated", self._on_episode_updated)

    async def stop(self) -> None:
        self._running = False
        self._bus.unsubscribe("event.canonicalized", self._on_event)
        self._bus.unsubscribe("episode.updated", self._on_episode_updated)
        await self._stop_recordings(list(self._recordings.values()), reason="application_shutdown")
        if self._active_tasks:
            _, pending = await asyncio.wait(self._active_tasks, timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def recover_interrupted_recordings(self) -> None:
        """Discover unfinished HLS bundles and reconcile legacy MP4 partials."""
        pattern = Path(self._data_dir, "episodes").glob(f"*/recordings/*/{CAPTURE_STATE_NAME}")
        now = datetime.now(tz=timezone.utc)
        for state_path in sorted(pattern):
            try:
                bundle = HLSRecordingBundle.load(state_path)
            except (OSError, ValueError, KeyError):
                logger.exception("Could not load interrupted HLS recording %s", state_path)
                continue
            state = bundle.state
            existing = await self._repo.get_evidence(state.evidence_id)
            if existing:
                bundle.complete_publication()
                continue
            device = await self._repo.get_device(state.device_id)
            episode = await self._repo.get_episode(state.episode_id)
            if not device or not episode:
                bundle.preserve_temporary_components()
                bundle.refresh_manifest(
                    state="incomplete", ended_at=now, reason="identity_unresolved"
                )
                continue
            rec = _EpisodeRecording(
                episode_id=state.episode_id,
                device_id=state.device_id,
                area_id=state.area_id,
                session_id=state.session_id,
                bundle=bundle,
                start_time=state.started_at,
                continued=True,
            )
            resumable = episode.state in {
                EpisodeState.ACTIVE,
                EpisodeState.QUIESCENT,
            } and await self._episode_within_capture_horizon(episode, now)
            if resumable:
                self._recoverable[self._rec_key(rec.episode_id, rec.device_id)] = rec
                bundle.preserve_temporary_components()
                bundle.refresh_manifest(state="interrupted", reason="startup_recovery")
            else:
                await self._finalize_bundle(rec, reason="startup_recovery")
        await self._recover_legacy_mp4_partials()

    async def resume_active_episodes(self) -> None:
        now = datetime.now(tz=timezone.utc)
        episodes = [
            *await self._repo.list_episodes(state=EpisodeState.ACTIVE, limit=10000),
            *await self._repo.list_episodes(state=EpisodeState.QUIESCENT, limit=10000),
        ]
        for episode in episodes:
            if not await self._episode_within_capture_horizon(episode, now):
                continue
            events = await self._repo.list_events(episode_id=episode.id, limit=10000)
            targets: dict[str, Device] = {}
            for event in events:
                if event.event_state != EventState.ACTIVE:
                    continue
                for device in await self._target_resolver.resolve(event):
                    targets[device.id] = device
            for device in targets.values():
                stream_url = self._stream_url(device)
                if not stream_url:
                    logger.warning(
                        "Could not resume recording for episode %s camera %s: no stream URL",
                        episode.id[:8],
                        device.id,
                    )
                    continue
                await self._start_recording(
                    episode.id,
                    replace(device, area_id=episode.primary_area_id),
                    stream_url,
                )
                recording = self._recordings[self._rec_key(episode.id, device.id)]
                await self._repo.append_episode_journal(
                    episode.id,
                    "recording.resumed",
                    {
                        "device_id": device.id,
                        "recording_session_id": recording.session_id,
                        "evidence_id": recording.evidence_id,
                        "minimum_end_at": episode.minimum_end_at.isoformat(),
                    },
                )
        for key, recording in list(self._recoverable.items()):
            self._recoverable.pop(key, None)
            await self._finalize_bundle(
                recording,
                reason="active_target_not_reconstructed",
            )

    async def _episode_within_capture_horizon(self, episode, now: datetime) -> bool:
        """Keep recovery aligned with the engine's persisted settling policy."""
        if episode.minimum_end_at is None:
            return True
        grace = await self._repo.get_quiescent_grace_seconds()
        return episode.minimum_end_at + timedelta(seconds=grace) >= now

    async def _on_event(self, msg: Message) -> None:
        result = msg.data.get("result")
        if not isinstance(result, CanonicalEventResult) or not result.created:
            return
        event = result.event
        if event.event_state != EventState.ACTIVE or not event.episode_id:
            return
        if event.participation is not None and not event.participation.allowed:
            return
        for device in await self._target_resolver.resolve(event):
            key = self._rec_key(event.episode_id, device.id)
            if key in self._recordings:
                continue
            try:
                url = self._stream_url(device)
                if url:
                    await self._start_recording(
                        event.episode_id,
                        replace(device, area_id=event.area_id),
                        url,
                    )
                else:
                    logger.warning(
                        "Skipping recording for episode %s camera %s: no stream URL",
                        event.episode_id[:8],
                        device.id,
                    )
            except Exception:
                logger.exception(
                    "Could not start recording for episode %s camera %s",
                    event.episode_id[:8],
                    device.id,
                )

    def _stream_url(self, device: Device) -> str:
        discovered = self._media.get(device.id) if self._media else None
        if discovered:
            return discovered.authenticated_stream_uri()
        video = device.get_config("video")
        return video.build_url(device.ip_address, device.username, device.password) if video else ""

    async def _start_recording(self, episode_id: str, device: Device, rtsp_url: str) -> None:
        key = self._rec_key(episode_id, device.id)
        if key in self._recordings:
            return
        rec = self._recoverable.pop(key, None)
        if rec is None:
            started_at = datetime.now(tz=timezone.utc)
            evidence_id = str(uuid.uuid4())
            session_id = uuid.uuid4().hex[:12]
            root = Path(self._data_dir, "episodes", episode_id, "recordings", evidence_id)
            bundle = HLSRecordingBundle.create(
                root,
                HLSCaptureState(
                    evidence_id=evidence_id,
                    episode_id=episode_id,
                    device_id=device.id,
                    area_id=device.area_id,
                    session_id=session_id,
                    started_at=started_at,
                ),
            )
            rec = _EpisodeRecording(
                episode_id=episode_id,
                device_id=device.id,
                area_id=device.area_id,
                session_id=session_id,
                bundle=bundle,
                start_time=started_at,
            )
        rec.rtsp_url = rtsp_url
        self._observe_progress(rec)
        if rec.continued:
            rec.state = "reconnecting"
        self._recordings[key] = rec
        rec.task = asyncio.create_task(self._record_episode(rec, rtsp_url))
        self._active_tasks.add(rec.task)
        rec.task.add_done_callback(self._active_tasks.discard)
        logger.info(
            "Started HLS recording %s for episode %s camera %s",
            rec.evidence_id[:8],
            episode_id[:8],
            device.id,
        )

    async def _on_episode_updated(self, msg: Message) -> None:
        if msg.data.get("state") != "closed":
            return
        episode_id = msg.data.get("episode_id", "")
        recordings = [
            recording
            for recording in self._recordings.values()
            if recording.episode_id == episode_id
        ]
        await asyncio.gather(*(self._stop_recording(recording) for recording in recordings))

    async def _stop_recording(self, rec: _EpisodeRecording, *, reason: str | None = None) -> None:
        await self._stop_recordings([rec], reason=reason)

    async def _stop_recordings(
        self, recordings: list[_EpisodeRecording], *, reason: str | None = None
    ) -> None:
        if not recordings:
            return
        for rec in recordings:
            self._recordings.pop(self._rec_key(rec.episode_id, rec.device_id), None)
            rec.stop_reason = reason
            self._signal_process(rec)
        if reason:
            await asyncio.gather(
                *(self._journal_interruption(rec, reason) for rec in recordings),
                return_exceptions=True,
            )
        await asyncio.gather(
            *(self._finish_stop(rec) for rec in recordings), return_exceptions=True
        )

    @staticmethod
    def _signal_process(rec: _EpisodeRecording) -> None:
        if rec.proc and rec.proc.returncode is None:
            try:
                rec.proc.terminate()
            except ProcessLookupError:
                pass

    async def _finish_stop(self, rec: _EpisodeRecording) -> None:
        await self._await_process_stop(rec)
        if rec.task and rec.task is not asyncio.current_task():
            results = await asyncio.gather(rec.task, return_exceptions=True)
            if (
                results
                and isinstance(results[0], BaseException)
                and not isinstance(results[0], asyncio.CancelledError)
            ):
                error = results[0]
                logger.error(
                    "Recording finalization failed for episode %s camera %s",
                    rec.episode_id[:8],
                    rec.device_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

    async def _terminate_process(self, rec: _EpisodeRecording) -> None:
        self._signal_process(rec)
        await self._await_process_stop(rec)

    @staticmethod
    async def _await_process_stop(rec: _EpisodeRecording) -> None:
        process = rec.proc
        if process and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _journal_interruption(self, rec: _EpisodeRecording, reason: str) -> None:
        try:
            await self._repo.append_episode_journal(
                rec.episode_id,
                "recording.interrupted",
                {
                    "device_id": rec.device_id,
                    "recording_session_id": rec.session_id,
                    "evidence_id": rec.evidence_id,
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception(
                "Could not journal recording interruption for episode %s camera %s",
                rec.episode_id[:8],
                rec.device_id,
            )

    async def _record_episode(
        self, rec: _EpisodeRecording, rtsp_url: str, _retries: int = 0
    ) -> None:
        key = self._rec_key(rec.episode_id, rec.device_id)
        if rec.stop_reason == "application_shutdown" or not self._running:
            rec.bundle.refresh_manifest(state="interrupted", reason="application_shutdown")
            return
        if self._recordings.get(key) is not rec:
            await self._finalize_bundle(rec)
            return
        segments_before = rec.bundle.next_segment_index()
        observed_segments = segments_before
        last_progress = asyncio.get_running_loop().time()
        rec.state = "reconnecting" if rec.continued or _retries else "starting"
        flags = "independent_segments+program_date_time+temp_file+append_list"
        if rec.continued or _retries:
            flags += "+discont_start"
        returncode = -1
        wait_task: asyncio.Task | None = None
        stall_signaled = False
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-rtsp_transport",
                "tcp",
                "-i",
                rtsp_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-f",
                "hls",
                "-hls_time",
                str(self._fragment_seconds),
                "-hls_list_size",
                "0",
                "-hls_playlist_type",
                "event",
                "-hls_segment_type",
                "fmp4",
                "-hls_fmp4_init_filename",
                "init.mp4",
                "-hls_segment_filename",
                "segments/segment-%06d.m4s",
                "-hls_base_url",
                "segments/",
                "-start_number",
                str(rec.bundle.next_segment_index()),
                "-hls_flags",
                flags,
                "index.m3u8",
                cwd=str(rec.bundle.root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rec.proc = proc
            wait_task = asyncio.create_task(proc.wait())
            while not wait_task.done():
                await asyncio.wait({wait_task}, timeout=1)
                await asyncio.to_thread(rec.bundle.refresh_manifest, state="recording")
                current_segments = rec.bundle.next_segment_index()
                if current_segments > observed_segments:
                    observed_segments = current_segments
                    last_progress = asyncio.get_running_loop().time()
                    self._observe_progress(rec)
                elif (
                    not wait_task.done()
                    and not stall_signaled
                    and asyncio.get_running_loop().time() - last_progress > self._stall_seconds
                ):
                    stall_signaled = True
                    rec.state = "stalled"
                    rec.last_error = (
                        f"No new media fragment for more than {self._stall_seconds} seconds"
                    )
                    self._stalled_count += 1
                    self._last_error = rec.last_error
                    logger.warning(
                        "Recording stalled for episode %s camera %s; restarting FFmpeg",
                        rec.episode_id[:8],
                        rec.device_id,
                    )
                    self._signal_process(rec)
            returncode = await wait_task
        except asyncio.CancelledError:
            await self._terminate_process(rec)
            if wait_task:
                await asyncio.gather(wait_task, return_exceptions=True)
            rec.bundle.preserve_temporary_components()
            rec.bundle.refresh_manifest(state="interrupted", reason="recording_task_cancelled")
            raise
        except Exception:
            await self._terminate_process(rec)
            if wait_task:
                await asyncio.gather(wait_task, return_exceptions=True)
            logger.exception(
                "Recording process failed for episode %s camera %s",
                rec.episode_id[:8],
                rec.device_id,
            )
        finally:
            rec.proc = None

        if rec.stop_reason == "application_shutdown" or not self._running:
            rec.bundle.preserve_temporary_components()
            rec.bundle.refresh_manifest(state="interrupted", reason="application_shutdown")
            return
        if self._recordings.get(key) is not rec:
            await self._finalize_bundle(rec)
            return

        episode = await self._repo.get_episode(rec.episode_id)
        if not episode or episode.state == EpisodeState.CLOSED:
            self._recordings.pop(key, None)
            await self._finalize_bundle(rec)
            return

        segments_after = rec.bundle.next_segment_index()
        retry = 0 if segments_after > segments_before else _retries + 1
        rec.last_exit_code = returncode
        rec.reconnect_count += 1
        self._reconnect_count += 1
        if retry > 3:
            self._recordings.pop(key, None)
            rec.state = "failed"
            rec.last_error = (
                "Recording stalled repeatedly; retry limit exceeded"
                if stall_signaled
                else f"FFmpeg exited with code {returncode}; retry limit exceeded"
            )
            self._failure_count += 1
            self._last_error = rec.last_error
            await self._finalize_bundle(rec, incomplete=True, reason="retry_limit_exceeded")
            logger.error(
                "Recording failed for episode %s camera %s after 3 retries",
                rec.episode_id[:8],
                rec.device_id,
            )
            return
        rec.state = "reconnecting"
        if not stall_signaled:
            rec.last_error = f"FFmpeg exited with code {returncode}; reconnecting"
        self._last_error = rec.last_error
        logger.warning(
            "Recording process ended for episode %s camera %s "
            "(ffmpeg exit %s), reconnecting (%d/3)",
            rec.episode_id[:8],
            rec.device_id,
            returncode,
            retry,
        )
        await asyncio.sleep(2)
        if self._recordings.get(key) is not rec:
            if rec.stop_reason == "application_shutdown" or not self._running:
                rec.bundle.preserve_temporary_components()
                rec.bundle.refresh_manifest(state="interrupted", reason="application_shutdown")
            else:
                await self._finalize_bundle(rec)
            return
        episode = await self._repo.get_episode(rec.episode_id)
        if not episode or episode.state == EpisodeState.CLOSED:
            self._recordings.pop(key, None)
            await self._finalize_bundle(rec, incomplete=True, reason="episode_closed")
            return
        rec.continued = True
        await self._record_episode(rec, rtsp_url, retry)

    async def _finalize_bundle(
        self,
        rec: _EpisodeRecording,
        *,
        incomplete: bool = False,
        reason: str | None = None,
    ) -> None:
        if rec.published:
            return
        ended_at = datetime.now(tz=timezone.utc)
        manifest = rec.bundle.prepare_finalize(ended_at=ended_at, reason=reason)
        playable = manifest["fragment_count"] > 0 and rec.bundle.playlist_path.exists()
        evidence_type = "recording" if playable and not incomplete else "incomplete_recording"
        file_path = (
            str(rec.bundle.playlist_path)
            if rec.bundle.playlist_path.exists()
            else str(rec.bundle.component_manifest_path)
        )
        duration = max(0, int((ended_at - rec.start_time).total_seconds()))
        playlist_sha = next(
            (item["sha256"] for item in manifest["components"] if item["path"] == "index.m3u8"),
            None,
        )
        evidence = Evidence(
            id=rec.evidence_id,
            device_id=rec.device_id,
            area_id=rec.area_id,
            timestamp=rec.start_time,
            evidence_type=evidence_type,
            file_path=file_path,
            mime_type=HLS_MIME_TYPE if playable else "application/json",
            original_filename="index.m3u8" if playable else "manifest.json",
            episode_id=rec.episode_id,
            metadata={
                "origin": "recording",
                "format": "hls-fmp4",
                "recording_session_id": rec.session_id,
                "started_at": rec.start_time.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_seconds": duration,
                "fragment_seconds": self._fragment_seconds,
                "fragment_count": manifest["fragment_count"],
                "component_count": manifest["component_count"],
                "bundle_bytes": manifest["total_bytes"],
                "component_manifest": "manifest.json",
                "component_manifest_sha256": rec.bundle.component_manifest_sha256(),
                "integrity_scope": "recording_bundle_manifest",
                "playlist_sha256": playlist_sha,
                **(
                    {"status": "incomplete", "reason": reason}
                    if evidence_type != "recording"
                    else {}
                ),
            },
        )
        await self._bus.publish(
            Message(type="evidence.received", data={"evidence": asdict(evidence)})
        )
        if not await self._repo.get_evidence(rec.evidence_id):
            raise RuntimeError(
                f"Recording Evidence {rec.evidence_id} was not persisted; "
                "the recovery marker has been retained"
            )
        rec.published = True
        rec.bundle.complete_publication()
        if evidence_type == "recording":
            self._completed_count += 1
        else:
            self._incomplete_count += 1
        self._last_completed_at = ended_at
        await self._repo.append_episode_journal(
            rec.episode_id,
            ("recording.completed" if evidence_type == "recording" else "recording.incomplete"),
            {
                "device_id": rec.device_id,
                "recording_session_id": rec.session_id,
                "evidence_id": rec.evidence_id,
                "fragment_count": manifest["fragment_count"],
                **({"reason": reason} if reason else {}),
            },
        )
        logger.info(
            "Recording Evidence %s complete for episode %s camera %s: %d fragments (%.1f MiB)",
            rec.evidence_id[:8],
            rec.episode_id[:8],
            rec.device_id,
            manifest["fragment_count"],
            manifest["total_bytes"] / (1024 * 1024),
        )

    async def _recover_legacy_mp4_partials(self) -> None:
        devices = await self._repo.list_devices(include_disabled=True)
        devices_by_safe_id: dict[str, list[Device]] = {}
        for device in devices:
            devices_by_safe_id.setdefault(self._safe_device_id(device.id), []).append(device)
        pattern = os.path.join(self._data_dir, "episodes", "*", "recordings", "*.mp4.part")
        for working_path in sorted(glob.glob(pattern)):
            match = _RECORDING_PART.fullmatch(os.path.basename(working_path))
            episode_id = os.path.basename(os.path.dirname(os.path.dirname(working_path)))
            if not match:
                logger.warning("Could not identify interrupted recording %s", working_path)
                continue
            candidates = devices_by_safe_id.get(match.group("device"), [])
            if len(candidates) != 1:
                await self._repo.append_episode_journal(
                    episode_id,
                    "recording.incomplete",
                    {
                        "filename": os.path.basename(working_path),
                        "reason": "device_identity_unresolved",
                    },
                )
                continue
            device = candidates[0]
            started_at = datetime.strptime(match.group("started"), "%Y%m%d_%H%M%S_%f").replace(
                tzinfo=timezone.utc
            )
            ended_at = datetime.fromtimestamp(os.path.getmtime(working_path), tz=timezone.utc)
            await self._finalize_legacy_partial(
                episode_id,
                device,
                working_path,
                match.group("session"),
                int(match.group("index")),
                started_at,
                ended_at,
            )

    async def _finalize_legacy_partial(
        self,
        episode_id: str,
        device: Device,
        working_path: str,
        session_id: str,
        index: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        valid = os.path.getsize(working_path) >= 4096 and await self._has_video_stream(working_path)
        output_path = working_path.removesuffix(".part") if valid else working_path
        if valid:
            os.replace(working_path, output_path)
        evidence = Evidence(
            device_id=device.id,
            area_id=device.area_id,
            timestamp=started_at,
            evidence_type="recording" if valid else "incomplete_recording",
            file_path=output_path,
            mime_type="video/mp4" if valid else "application/octet-stream",
            episode_id=episode_id,
            metadata={
                "origin": "recording",
                "format": "mp4",
                "status": "recovered" if valid else "incomplete",
                "reason": "startup_recovery",
                "recording_session_id": session_id,
                "segment_index": index,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_seconds": max(0, int((ended_at - started_at).total_seconds())),
            },
        )
        await self._bus.publish(
            Message(type="evidence.received", data={"evidence": asdict(evidence)})
        )
        if not await self._repo.get_evidence(evidence.id):
            if valid and os.path.exists(output_path):
                os.replace(output_path, working_path)
            raise RuntimeError(f"Recovered recording Evidence {evidence.id} was not persisted")
        await self._repo.append_episode_journal(
            episode_id,
            "recording.recovered" if valid else "recording.incomplete",
            {
                "device_id": device.id,
                "recording_session_id": session_id,
                "segment_index": index,
            },
        )

    async def _has_video_stream(self, path: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0 and stdout.strip() == b"video"
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            return False

    def status(self) -> dict:
        recordings = tuple(
            self._recording_diagnostic(recording)
            for recording in sorted(
                self._recordings.values(),
                key=lambda item: (item.start_time, item.device_id),
            )
        )
        degraded = any(
            item["state"] in {"stalled", "reconnecting", "failed"} for item in recordings
        )
        return {
            "running": self._running,
            "state": "unavailable" if not self._running else "degraded" if degraded else "healthy",
            "active_recordings": len(self._recordings),
            "cameras": len({key[1] for key in self._recordings}),
            "format": "hls-fmp4",
            "fragment_seconds": self._fragment_seconds,
            "stall_seconds": self._stall_seconds,
            "completed_recordings": self._completed_count,
            "incomplete_recordings": self._incomplete_count,
            "reconnects": self._reconnect_count,
            "failures": self._failure_count,
            "stalled_recordings": self._stalled_count,
            "last_completed_at": self._last_completed_at,
            "last_error": self._last_error,
            "recordings": recordings[:32],
        }
