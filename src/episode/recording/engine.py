from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from episode.domain.models import Device, EpisodeState, EventState, Evidence
from episode.engine.bus import EventBus, Message
from episode.engine.engine import CanonicalEventResult
from episode.media.registry import VIDEO_CODEC_HINTS, VideoStreamHandler
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

# WebKit (Safari), the only browser with broad HEVC playback, requires ``hvc1`` in
# MP4/fMP4 and rejects ``hev1``. ``hvc1`` keeps SPS/PPS/VPS in the init.mp4 sample entry
# (hvcc box) so every fragment is self-contained for decoder init. When the input codec is
# known to be HEVC we retag the output stream so the browser UI can play the bundle.
HEVC_CODEC_TAG = "hvc1"

RecordingEvidenceSink = Callable[[Evidence], Awaitable[Evidence]]


def _evidence_episode_id(value: Evidence | dict[str, object] | None) -> str | None:
    if isinstance(value, dict):
        episode_id = value.get("episode_id")
        return str(episode_id) if episode_id else None
    return value.episode_id if value else None


_RECORDING_PART = re.compile(
    r"^rec_(?P<device>.+)_(?P<started>\d{8}_\d{6}_\d{6})_"
    r"(?P<session>[0-9a-f]{12})_(?P<index>\d{6})\.mp4\.part$"
)


#: Seconds a handler may block writing one chunk before the recorder treats the
#: source as stalled. Bounds "a plugin stall stalls a recording" (see docs/PLUGINS.md).
PIPE_WRITE_TIMEOUT_SECONDS = 10.0


class PipeClosedError(RuntimeError):
    """Raised into a video handler when its recorder child has already gone."""


#: Floor for the recorder's socket timeout on a camera URL, in seconds.
#: RTSP's own option is ``-timeout`` and is measured in **microseconds**; the
#: plan's ``-rw_timeout`` is silently ignored by the RTSP demuxer, so a camera
#: that accepts TCP and then never replies leaves ffmpeg blocked forever without
#: it (measured: still running after 20 s against a silent camera, both with and
#: without ``-rw_timeout``; ``-timeout`` ends it in 3.2 s / 15.2 s at 3 s/15 s).
#: A half-open stream then reaches the existing no-progress watchdog as a normal
#: ffmpeg exit and reconnects, instead of hanging as a live child.
RTSP_SOCKET_TIMEOUT_SECONDS = 15


#: How much redacted ffmpeg stderr a failed attempt keeps for one log line.
FFMPEG_STDERR_TAIL_CHARS = 400


#: ffmpeg prints the input URL verbatim when it fails, including
#: ``rtsp://user:password@host``. Credential-bearing stderr can therefore never
#: be stored or logged before the URL's userinfo has been taken out of it.
_URL_USERINFO = re.compile(r"://[^/@\s]*@")


@dataclass
class _EpisodeRecording:
    episode_id: str
    device_id: str
    area_id: str
    session_id: str
    bundle: HLSRecordingBundle
    start_time: datetime
    rtsp_url: str = ""
    #: Optional plugin-native video source. When set, the recorder still writes the
    #: bundle; only the bytes' origin changes (piped elementary stream instead of RTSP).
    video_handler: VideoStreamHandler | None = None
    codec_hint: str = ""
    #: The recorder's own task driving ``video_handler``; always stopped with the attempt.
    handler_task: asyncio.Task | None = None
    #: Why the plugin source stopped feeding, when it did. It must not be mistaken for a
    #: healthy stream, so it also keeps a source that made progress from retrying forever.
    handler_error: str | None = None
    #: A handler that returned cleanly ended its own source; it must not be retried.
    handler_ended: bool = False
    #: Journal/metadata reason for a recording ended by its plugin source.
    handler_reason: str | None = None
    #: Redacted tail of this attempt's ffmpeg stderr, kept to explain a failed attempt.
    ffmpeg_stderr: str = ""
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
    retry_wakeup: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

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
        evidence_sink: RecordingEvidenceSink | None = None,
    ):
        if fragment_seconds <= 0:
            raise ValueError("fragment_seconds must be greater than zero")
        self._repo = repo
        self._bus = bus
        self._data_dir = data_dir
        self._fragment_seconds = fragment_seconds
        self._media = media
        self._target_resolver = target_resolver or AreaRecordingTargetResolver(repo)
        self._evidence_sink = evidence_sink
        self._active_tasks: set[asyncio.Task] = set()
        self._recordings: dict[tuple[str, str], _EpisodeRecording] = {}
        self._recoverable: dict[tuple[str, str], _EpisodeRecording] = {}
        self._running = False
        self._stall_seconds = max(60, fragment_seconds * 6)
        # A camera socket that stops answering must fail its ffmpeg before the no-progress
        # watchdog would otherwise notice, so the reconnect reason is ffmpeg's own, and the
        # child never sits attached to a dead camera indefinitely.
        self._socket_timeout_seconds = float(
            min(RTSP_SOCKET_TIMEOUT_SECONDS, self._stall_seconds - 1)
        )
        self._retry_initial_seconds = 2.0
        self._retry_max_seconds = 30.0
        self._completed_count = 0
        self._incomplete_count = 0
        self._reconnect_count = 0
        self._failure_count = 0
        self._stalled_count = 0
        self._last_completed_at: datetime | None = None
        self._last_error: str | None = None

    def set_evidence_sink(self, evidence_sink: RecordingEvidenceSink | None) -> None:
        self._evidence_sink = evidence_sink

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
            for recording in (*self._recordings.values(), *self._recoverable.values())
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
        episodes = [
            *await self._repo.list_episodes(state=EpisodeState.ACTIVE, limit=10000),
            *await self._repo.list_episodes(state=EpisodeState.QUIESCENT, limit=10000),
            *await self._repo.list_episodes(state=EpisodeState.FINALIZING, limit=10000),
        ]
        now = datetime.now(tz=timezone.utc)
        for episode in episodes:
            pattern = Path(self._data_dir, "episodes", episode.id, "recordings").glob(
                f"*/{CAPTURE_STATE_NAME}"
            )
            for state_path in sorted(pattern):
                await self._recover_interrupted_bundle(state_path, episode, now)
        await self._recover_legacy_mp4_partials(episodes)

    async def _recover_interrupted_bundle(
        self,
        state_path: Path,
        episode,
        now: datetime,
    ) -> None:
        try:
            bundle = HLSRecordingBundle.load(state_path)
        except (OSError, ValueError, KeyError):
            logger.exception("Could not load interrupted HLS recording %s", state_path)
            return
        state = bundle.state
        if state.episode_id != episode.id:
            logger.warning("Ignoring HLS bundle with mismatched Episode path: %s", state_path)
            return
        existing = await self._repo.get_evidence(state.evidence_id)
        if existing and existing.episode_id == episode.id:
            bundle.complete_publication()
            return
        device = await self._repo.get_device(state.device_id)
        if not device:
            bundle.preserve_temporary_components()
            bundle.refresh_manifest(state="incomplete", ended_at=now, reason="identity_unresolved")
            return
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
            # Finalization belongs to the Episode engine's explicit barrier.
            # Keeping the recovered bundle here lets a publication failure
            # leave the Episode FINALIZING without aborting application startup.
            self._recoverable[self._rec_key(rec.episode_id, rec.device_id)] = rec

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
                stream_url, video_handler, codec_hint = self._video_source(device)
                if not stream_url and video_handler is None:
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
                    video_handler=video_handler,
                    codec_hint=codec_hint,
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
                stream_url, video_handler, codec_hint = self._video_source(device)
                if stream_url or video_handler is not None:
                    await self._start_recording(
                        event.episode_id,
                        replace(device, area_id=event.area_id),
                        stream_url,
                        video_handler=video_handler,
                        codec_hint=codec_hint,
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

    def _video_source(self, device: Device) -> tuple[str, VideoStreamHandler | None, str]:
        """Where this Device's video comes from: a URL, or a plugin handler plus its codec.

        A handler takes precedence over a URL because the plugin already holds a session
        that delivers framed video, and acquiring it that way is what removes the keyframe
        wait a second connection pays. Everything after the recorder's input argument stays
        the same either way, so bundle layout, manifest, retention, and finalization remain
        core-owned.
        """
        discovered = self._media.get(device.id) if self._media else None
        if discovered is None or discovered.video_handler is None:
            return self._stream_url(device), None, ""
        codec = discovered.codec_hint if discovered.codec_hint in VIDEO_CODEC_HINTS else ""
        return discovered.authenticated_stream_uri(), discovered.video_handler, codec

    async def _start_recording(
        self,
        episode_id: str,
        device: Device,
        rtsp_url: str,
        *,
        video_handler: VideoStreamHandler | None = None,
        codec_hint: str = "",
    ) -> None:
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
        rec.video_handler = video_handler
        rec.codec_hint = codec_hint if video_handler is not None and codec_hint else ""
        self._observe_progress(rec)
        if rec.continued:
            rec.state = "reconnecting"
        rec.retry_wakeup.clear()
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

    async def finalize_episode(self, episode_id: str) -> None:
        """Stop and publish all recorder output for an Episode.

        This is an explicit success barrier for Episode finalization.  It is
        intentionally narrower than the EventBus: failures are returned to
        the Episode engine, which keeps the Episode in FINALIZING for retry.
        """
        recordings = [
            recording
            for recording in self._recordings.values()
            if recording.episode_id == episode_id
        ]
        await self._stop_recordings(
            recordings,
            raise_on_error=True,
        )
        # A retry task may move its recording into the recoverable map while
        # the active tasks are being stopped. Include that work in the barrier.
        recoverable = [
            recording
            for recording in self._recoverable.values()
            if recording.episode_id == episode_id
        ]
        for recording in recoverable:
            await self._finalize_bundle(recording, reason="episode_finalizing")
            self._recoverable.pop(
                self._rec_key(recording.episode_id, recording.device_id),
                None,
            )

    async def _stop_recording(self, rec: _EpisodeRecording, *, reason: str | None = None) -> None:
        await self._stop_recordings([rec], reason=reason)

    async def _stop_recordings(
        self,
        recordings: list[_EpisodeRecording],
        *,
        reason: str | None = None,
        raise_on_error: bool = False,
    ) -> None:
        if not recordings:
            return
        for rec in recordings:
            self._recordings.pop(self._rec_key(rec.episode_id, rec.device_id), None)
            rec.stop_reason = reason
            rec.retry_wakeup.set()
            self._signal_process(rec)
        if reason:
            await asyncio.gather(
                *(self._journal_interruption(rec, reason) for rec in recordings),
                return_exceptions=True,
            )
        results = await asyncio.gather(
            *(self._finish_stop(rec) for rec in recordings), return_exceptions=True
        )
        errors = []
        for recording, result in zip(recordings, results, strict=True):
            if not isinstance(result, BaseException):
                continue
            errors.append(result)
            if raise_on_error and not recording.published:
                self._recoverable[self._rec_key(recording.episode_id, recording.device_id)] = (
                    recording
                )
        if errors and raise_on_error:
            raise RuntimeError(
                f"Recording finalization failed for Episode {recordings[0].episode_id}"
            ) from errors[0]

    @staticmethod
    def _signal_child_process(rec: _EpisodeRecording) -> None:
        if rec.proc and rec.proc.returncode is None:
            try:
                rec.proc.terminate()
            except ProcessLookupError:
                pass

    @staticmethod
    def _signal_process(rec: _EpisodeRecording) -> None:
        RecordingEngine._signal_child_process(rec)
        # A plugin-fed source must stop too, or its camera session outlives the
        # recording it was feeding.
        if rec.handler_task is not None and not rec.handler_task.done():
            rec.handler_task.cancel()

    @staticmethod
    async def _await_handler_stop(rec: _EpisodeRecording) -> None:
        """Stop feeding and wait, so no handler outlives the attempt it was feeding."""
        task = rec.handler_task
        rec.handler_task = None
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _finish_stop(self, rec: _EpisodeRecording) -> None:
        await self._await_handler_stop(rec)
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
                raise error

    async def _terminate_process(self, rec: _EpisodeRecording) -> None:
        self._signal_process(rec)
        await self._await_handler_stop(rec)
        await self._await_process_stop(rec)

    @staticmethod
    def _output_codec_tag(rec: _EpisodeRecording) -> list[str]:
        """Retag HEVC output as ``hvc1`` so WebKit can play the fMP4 bundle.

        A native source (F1) declares its codec through ``codec_hint``; when that hint is
        HEVC, ffmpeg would otherwise write the stream's own (in-band, ``hev1``) tag, which
        Safari rejects. ``hvc1`` stores parameter sets in init.mp4 and is what the browser
        UI needs. Returns an empty list for H.264 or unknown codecs so the copy keeps its
        natural tag.
        """
        if rec.codec_hint == "hevc":
            return ["-tag:v", HEVC_CODEC_TAG]
        return []

    def _piped_command(self, rec: _EpisodeRecording, flags: str) -> list[str]:
        """FFmpeg reading an elementary stream on stdin instead of a network URL.

        Only the input side differs from :meth:`_rtsp_command`; everything from ``-i``
        onwards is the same command, so the bundle a handler produces is the same bundle
        shape the recorder has always written. An elementary stream carries no timestamps,
        so ``-use_wallclock_as_timestamps`` is what keeps ``EXTINF`` real; without it ffmpeg
        emits ``EXTINF:0`` segments that no player can lay out.
        """
        command = ["ffmpeg", "-y"]
        if rec.codec_hint:
            command += ["-f", rec.codec_hint]
        command += [
            # An elementary stream carries no timestamps of its own, so arrival time is the
            # only truth available and it is what the playlist must reflect.
            "-use_wallclock_as_timestamps",
            "1",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            *self._output_codec_tag(rec),
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
        ]
        return command

    def _rtsp_command(self, rec: _EpisodeRecording, rtsp_url: str, flags: str) -> list[str]:
        """FFmpeg pulling a camera URL, exactly as before handler support existed.

        ``-timeout`` is the RTSP demuxer's socket I/O timeout, in microseconds, and is
        what keeps a camera that answers nothing from holding an ffmpeg child open
        forever. It must stay under ``_stall_seconds`` so a half-open stream becomes a
        normal ffmpeg exit and reconnects, rather than a live process that never reports
        progress. ``-rw_timeout`` looks like the right option and is ignored here.
        """
        return [
            "ffmpeg",
            "-y",
            "-rtsp_transport",
            "tcp",
            "-timeout",
            str(int(self._socket_timeout_seconds * 1_000_000)),
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
        ]

    @staticmethod
    async def _await_process_stop(rec: _EpisodeRecording) -> None:
        process = rec.proc
        if process and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    def _start_handler(self, rec: _EpisodeRecording, proc: asyncio.subprocess.Process) -> None:
        """Drive the plugin's video handler, feeding this attempt's ffmpeg stdin.

        The handler is a plugin callback, so it runs on a task the recorder owns and can
        cancel: a plugin that never returns must not keep a camera session open past the
        recording. One chunk in flight keeps memory bounded, and a handler that blocks the
        write longer than ``PIPE_WRITE_TIMEOUT_SECONDS`` is treated as stalled, which ends
        the attempt through the same path a dead network takes.
        """
        handler = rec.video_handler
        stdin = proc.stdin
        rec.handler_error = None
        rec.handler_ended = False
        rec.handler_reason = None

        async def _push(chunk: bytes) -> None:
            if stdin is None or proc.returncode is not None:
                raise PipeClosedError("recorder child process is no longer reading")
            stdin.write(chunk)
            await asyncio.wait_for(stdin.drain(), timeout=PIPE_WRITE_TIMEOUT_SECONDS)

        async def _pump() -> None:
            cancelled = False
            try:
                try:
                    await handler(_push)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except asyncio.TimeoutError:
                    # The plugin could not hand over a chunk, so ffmpeg is not keeping up.
                    # Deliberately not a handler failure: this ends the attempt the way a
                    # dead connection does, so the existing reconnect + discont_start path
                    # still applies to a slow source.
                    rec.last_error = (
                        "Video handler blocked writing for "
                        f"{PIPE_WRITE_TIMEOUT_SECONDS:.0f} seconds"
                    )
                    self._stalled_count += 1
                    self._last_error = rec.last_error
                except Exception as error:
                    # A raising handler, or one writing to a child that already died (a
                    # corrupted elementary stream), is a source failure: the bundle it made
                    # is kept and reported incomplete, never published as a recording.
                    rec.handler_error = f"{type(error).__name__}: {str(error)[:180]}"
                    rec.handler_reason = "video_handler_failed"
                    logger.warning(
                        "Video handler for episode %s camera %s failed: %s",
                        rec.episode_id[:8],
                        rec.device_id,
                        rec.handler_error,
                    )
                else:
                    # Returning means the plugin has nothing more to send: a source that
                    # ended, not a recording that finished. Recorded so the attempt is not
                    # replayed against a plugin that would only say so again.
                    rec.handler_ended = True
                    rec.handler_reason = "video_handler_ended"
            finally:
                # Nothing more can arrive either way. Closing stdin is what lets ffmpeg
                # flush its final fragment and exit 0, so a source that ran out is not
                # punished into a truncated bundle. A child that ignores EOF is still
                # caught by the recorder's own no-progress watchdog. A cancelled attempt
                # is left to the recorder's teardown, which terminates the child on purpose.
                if not cancelled and stdin is not None:
                    try:
                        stdin.close()
                    except Exception:
                        pass

        rec.handler_task = asyncio.create_task(_pump())

    @staticmethod
    def _redact_url_credentials(text: str) -> str:
        """Strip ``scheme://user:password@`` from FFmpeg output before any of it is kept.

        FFmpeg prints the input URL verbatim when it cannot open it, and the recorder's
        URL carries the camera username and password, so a raw stderr tail is a credential
        leak wherever it is written. Only the userinfo part goes: host and port survive,
        which is what actually identifies the camera to the operator.
        """
        return _URL_USERINFO.sub("://[redacted]@", text)

    @staticmethod
    async def _drain_stderr(rec: _EpisodeRecording, proc: asyncio.subprocess.Process) -> None:
        """Keep ffmpeg's stderr pipe empty and keep a bounded, redacted tail.

        Unread stderr deadlocks ffmpeg once the OS pipe fills, so it is consumed on both
        the URL and the piped path; only the tail is kept, and only in redacted form,
        because a stream URL with credentials can appear in it.
        """
        tail = ""
        stream = getattr(proc, "stderr", None)
        if stream is not None:
            while True:
                try:
                    line = await stream.readline()
                except Exception:
                    break
                if not line:
                    break
                tail = (tail + line.decode("utf-8", "replace"))[-FFMPEG_STDERR_TAIL_CHARS * 4 :]
        rec.ffmpeg_stderr = RecordingEngine._redact_url_credentials(tail).strip()[
            -FFMPEG_STDERR_TAIL_CHARS:
        ]

    @staticmethod
    def _stderr_note(rec: _EpisodeRecording) -> str:
        """One bounded ffmpeg-stderr clause for a log line; never empty enough to mislead."""
        tail = rec.ffmpeg_stderr.strip().replace("\n", " | ")
        return f" ffmpeg: {tail[-180:]}" if tail else ""

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

    async def _record_episode(self, rec: _EpisodeRecording, rtsp_url: str) -> None:
        key = self._rec_key(rec.episode_id, rec.device_id)
        retry_attempt = 0
        while True:
            if rec.stop_reason == "application_shutdown" or not self._running:
                rec.bundle.preserve_temporary_components()
                rec.bundle.refresh_manifest(state="interrupted", reason="application_shutdown")
                return
            if self._recordings.get(key) is not rec:
                await self._finalize_from_retry_task(rec)
                return

            segments_before = rec.bundle.next_segment_index()
            observed_segments = segments_before
            last_progress = asyncio.get_running_loop().time()
            rec.state = "reconnecting" if rec.continued or retry_attempt else "starting"
            piped = rec.video_handler is not None
            continuing = rec.continued
            flags = "independent_segments+program_date_time+temp_file"
            # Do not start a fresh playlist with an unnecessary discontinuity. Once
            # a process has appended to this bundle, explicitly mark the reconnect.
            if not piped or continuing:
                flags += "+append_list"
            if continuing:
                flags += "+discont_start"

            returncode = -1
            wait_task: asyncio.Task | None = None
            stderr_task: asyncio.Task | None = None
            stall_signaled = False
            try:
                if piped:
                    proc = await asyncio.create_subprocess_exec(
                        *self._piped_command(rec, flags),
                        cwd=str(rec.bundle.root),
                        stdin=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        *self._rtsp_command(rec, rtsp_url, flags),
                        cwd=str(rec.bundle.root),
                        stdout=subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                rec.proc = proc
                rec.ffmpeg_stderr = ""
                stderr_task = asyncio.create_task(self._drain_stderr(rec, proc))
                if piped:
                    self._start_handler(rec, proc)
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
                        self._signal_child_process(rec)
                returncode = await wait_task
                if stderr_task is not None:
                    await stderr_task
                    stderr_task = None
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
                if stderr_task is not None:
                    # Always drain and join stderr: an unconsumed child pipe can block
                    # FFmpeg; only a bounded, credential-redacted tail is retained.
                    try:
                        await asyncio.wait_for(stderr_task, timeout=5)
                    except asyncio.TimeoutError:
                        stderr_task.cancel()
                        await asyncio.gather(stderr_task, return_exceptions=True)
                    except Exception:
                        pass

            if rec.stop_reason == "application_shutdown" or not self._running:
                rec.bundle.preserve_temporary_components()
                rec.bundle.refresh_manifest(state="interrupted", reason="application_shutdown")
                return
            if self._recordings.get(key) is not rec:
                await self._finalize_from_retry_task(rec)
                return

            episode = await self._repo.get_episode(rec.episode_id)
            if not episode or episode.state not in {
                EpisodeState.ACTIVE,
                EpisodeState.QUIESCENT,
            }:
                self._recordings.pop(key, None)
                await self._finalize_from_retry_task(rec)
                return

            segments_after = rec.bundle.next_segment_index()
            retry_attempt = 0 if segments_after > segments_before else retry_attempt + 1
            rec.last_exit_code = returncode
            rec.reconnect_count += 1
            self._reconnect_count += 1
            stderr_note = "" if segments_after > segments_before else self._stderr_note(rec)

            if piped and (
                rec.handler_error or rec.handler_ended or segments_after == segments_before
            ):
                # Plugin sources are not retried indefinitely after ending or failing.
                # Preserve partial fragments, but never call a truncated source complete.
                source_failed = bool(rec.handler_error) or segments_after == 0
                if source_failed and rec.last_error is None:
                    rec.last_error = (
                        rec.handler_error or "video source produced no decodable frames"
                    )
                self._recordings.pop(key, None)
                rec.state = "failed" if source_failed else rec.state
                if source_failed:
                    self._failure_count += 1
                self._last_error = rec.last_error
                logger.warning(
                    "Plugin video source ended for episode %s camera %s (ffmpeg exit %s): %s%s",
                    rec.episode_id[:8],
                    rec.device_id,
                    returncode,
                    rec.handler_error or rec.last_error or "handler returned",
                    self._stderr_note(rec),
                )
                await self._await_handler_stop(rec)
                await self._finalize_from_retry_task(
                    rec,
                    incomplete=source_failed,
                    reason=rec.handler_reason or "video_handler_failed",
                )
                return

            if piped:
                # Never let a previous feeder overlap the next FFmpeg attempt.
                await self._await_handler_stop(rec)

            retry_delay = self._retry_delay_seconds(retry_attempt)
            rec.state = "reconnecting"
            if not stall_signaled:
                rec.last_error = f"FFmpeg exited with code {returncode}; reconnecting{stderr_note}"
            self._last_error = rec.last_error
            logger.warning(
                "Recording process ended for episode %s camera %s "
                "(ffmpeg exit %s), reconnecting after %.1f seconds%s",
                rec.episode_id[:8],
                rec.device_id,
                returncode,
                retry_delay,
                stderr_note,
            )

            stopped = await self._wait_for_retry(rec, retry_delay)
            if stopped:
                if rec.stop_reason == "application_shutdown" or not self._running:
                    rec.bundle.preserve_temporary_components()
                    rec.bundle.refresh_manifest(state="interrupted", reason="application_shutdown")
                    return
                if self._recordings.get(key) is not rec:
                    await self._finalize_from_retry_task(rec)
                    return

            episode = await self._repo.get_episode(rec.episode_id)
            if not episode or episode.state not in {
                EpisodeState.ACTIVE,
                EpisodeState.QUIESCENT,
            }:
                self._recordings.pop(key, None)
                await self._finalize_from_retry_task(
                    rec,
                    incomplete=True,
                    reason="episode_closed",
                )
                return
            rec.continued = True

    @staticmethod
    async def _wait_for_retry(rec: _EpisodeRecording, delay: float) -> bool:
        """Wait for backoff or a stop signal, whichever arrives first."""
        delay_task = asyncio.create_task(asyncio.sleep(delay))
        wake_task = asyncio.create_task(rec.retry_wakeup.wait())
        tasks = (delay_task, wake_task)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            return wake_task in done
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _retry_delay_seconds(self, retry_attempt: int) -> float:
        """Return exponential reconnect backoff capped at the operational maximum."""
        delay = self._retry_initial_seconds
        for _ in range(max(0, retry_attempt - 1)):
            delay = min(delay * 2, self._retry_max_seconds)
            if delay >= self._retry_max_seconds:
                break
        return delay

    async def _finalize_from_retry_task(
        self,
        rec: _EpisodeRecording,
        *,
        incomplete: bool = False,
        reason: str | None = None,
    ) -> None:
        try:
            await self._finalize_bundle(rec, incomplete=incomplete, reason=reason)
        except Exception:
            self._recoverable[self._rec_key(rec.episode_id, rec.device_id)] = rec
            logger.exception(
                "Could not finalize recording for episode %s camera %s; "
                "retaining the recovery workspace",
                rec.episode_id[:8],
                rec.device_id,
            )

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
        if self._evidence_sink:
            persisted = await self._evidence_sink(evidence)
        else:
            await self._bus.publish(
                Message(type="evidence.received", data={"evidence": asdict(evidence)})
            )
            persisted = await self._repo.get_evidence(rec.evidence_id)
        if not persisted or _evidence_episode_id(persisted) != rec.episode_id:
            raise RuntimeError(
                f"Recording Evidence {rec.evidence_id} was not persisted for Episode "
                f"{rec.episode_id}; "
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

    async def _recover_legacy_mp4_partials(self, episodes) -> None:
        devices = await self._repo.list_devices(include_disabled=True)
        devices_by_safe_id: dict[str, list[Device]] = {}
        for device in devices:
            devices_by_safe_id.setdefault(self._safe_device_id(device.id), []).append(device)
        for episode in episodes:
            pattern = os.path.join(
                self._data_dir,
                "episodes",
                episode.id,
                "recordings",
                "*.mp4.part",
            )
            for working_path in sorted(glob.glob(pattern)):
                match = _RECORDING_PART.fullmatch(os.path.basename(working_path))
                episode_id = episode.id
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
        if self._evidence_sink:
            persisted = await self._evidence_sink(evidence)
        else:
            await self._bus.publish(
                Message(type="evidence.received", data={"evidence": asdict(evidence)})
            )
            persisted = await self._repo.get_evidence(evidence.id)
        if not persisted or _evidence_episode_id(persisted) != episode_id:
            if valid and os.path.exists(output_path):
                os.replace(output_path, working_path)
            raise RuntimeError(
                f"Recovered recording Evidence {evidence.id} was not persisted for Episode "
                f"{episode_id}"
            )
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
