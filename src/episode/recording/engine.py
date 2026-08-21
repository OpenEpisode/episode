from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from episode.domain.models import Device, EpisodeState, Evidence
from episode.engine.bus import EventBus, Message
from episode.engine.engine import CanonicalEventResult
from episode.recording.targets import AreaRecordingTargetResolver, RecordingTargetResolver

if TYPE_CHECKING:
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class _EpisodeRecording:
    episode_id: str
    device_id: str
    area_id: str
    output_path: str
    working_path: str
    session_id: str
    start_time: datetime | None = None
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    next_segment_index: int = 0
    segment_started_at: dict[int, datetime] = field(default_factory=dict)


class RecordingEngine:
    def __init__(
        self,
        repo: Repository,
        bus: EventBus,
        data_dir: str,
        segment_seconds: int = 600,
        media=None,
        target_resolver: RecordingTargetResolver | None = None,
    ):
        self._repo = repo
        self._bus = bus
        self._data_dir = data_dir
        if segment_seconds <= 0:
            raise ValueError("segment_seconds must be greater than zero")
        self._segment_seconds = segment_seconds
        self._media = media
        self._target_resolver = target_resolver or AreaRecordingTargetResolver(repo)
        self._active_tasks: set[asyncio.Task] = set()
        self._recordings: dict[tuple[str, str], _EpisodeRecording] = {}
        self._running = False

    def _rec_key(self, episode_id: str, device_id: str) -> tuple[str, str]:
        return (episode_id, device_id)

    def active_device_ids(self, episode_id: str) -> tuple[str, ...]:
        """Return Devices currently recording one Episode without exposing stream details."""
        return tuple(
            sorted(
                recording.device_id
                for recording in self._recordings.values()
                if recording.episode_id == episode_id
            )
        )

    async def start(self):
        self._running = True
        self._bus.subscribe("event.canonicalized", self._on_event)
        self._bus.subscribe("episode.updated", self._on_episode_updated)

    async def stop(self):
        self._running = False
        self._bus.unsubscribe("event.canonicalized", self._on_event)
        self._bus.unsubscribe("episode.updated", self._on_episode_updated)
        for rec in list(self._recordings.values()):
            await self._stop_recording(rec)
        if self._active_tasks:
            _, pending = await asyncio.wait(self._active_tasks, timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _on_event(self, msg: Message):
        result = msg.data.get("result")
        if not isinstance(result, CanonicalEventResult) or not result.created:
            return
        event = result.event
        if event.event_state.value != "active":
            return
        episode_id = event.episode_id or ""
        if not episode_id:
            return

        for device in await self._target_resolver.resolve(event):
            key = self._rec_key(episode_id, device.id)
            if key in self._recordings:
                continue
            try:
                url = self._stream_url(device)
                if not url:
                    logger.warning(
                        "Skipping recording for episode %s camera %s: no stream URL",
                        episode_id[:8],
                        device.id,
                    )
                    continue
                await self._start_recording(episode_id, device, url)
            except Exception:
                logger.exception(
                    "Could not start recording for episode %s camera %s",
                    episode_id[:8],
                    device.id,
                )

    def _stream_url(self, device: Device) -> str:
        discovered = self._media.get(device.id) if self._media else None
        if discovered:
            return discovered.authenticated_stream_uri()
        video = device.get_config("video")
        return video.build_url(device.ip_address, device.username, device.password) if video else ""

    async def _start_recording(self, episode_id: str, device: Device, rtsp_url: str):
        key = self._rec_key(episode_id, device.id)
        if key in self._recordings:
            return

        started_at = datetime.now(tz=timezone.utc)
        ts = started_at.strftime("%Y%m%d_%H%M%S_%f")
        safe_device_id = re.sub(r"[^A-Za-z0-9._-]+", "-", device.id).strip("._-")
        safe_device_id = (safe_device_id or "device")[:64]
        session_id = uuid.uuid4().hex[:12]
        filename = f"rec_{safe_device_id}_{ts}_{session_id}_%06d.mp4"
        output = os.path.join(self._data_dir, "episodes", episode_id, "recordings", filename)
        working = f"{output}.part"

        rec = _EpisodeRecording(
            episode_id=episode_id,
            device_id=device.id,
            area_id=device.area_id,
            output_path=output,
            working_path=working,
            session_id=session_id,
            start_time=started_at,
            segment_started_at={0: started_at},
        )
        self._recordings[key] = rec

        rec.task = asyncio.create_task(self._record_episode(rec, rtsp_url))
        self._active_tasks.add(rec.task)
        rec.task.add_done_callback(self._active_tasks.discard)

        logger.info(
            "Started recording %s for episode %s camera %s",
            filename.replace("%06d", "*"),
            episode_id[:8],
            device.id,
        )

    async def _on_episode_updated(self, msg: Message):
        episode_id = msg.data.get("episode_id", "")
        state = msg.data.get("state", "")
        if state == "closed":
            to_stop = [k for k, r in self._recordings.items() if k[0] == episode_id]
            for key in to_stop:
                rec = self._recordings.pop(key, None)
                if rec:
                    await self._stop_recording(rec)

    async def _stop_recording(self, rec: _EpisodeRecording):
        key = self._rec_key(rec.episode_id, rec.device_id)
        self._recordings.pop(key, None)
        await self._terminate_process(rec)
        if rec.task and rec.task is not asyncio.current_task():
            await asyncio.gather(rec.task, return_exceptions=True)

    async def _terminate_process(self, rec: _EpisodeRecording):
        if rec.proc and rec.proc.returncode is None:
            rec.proc.terminate()
            try:
                await asyncio.wait_for(rec.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                rec.proc.kill()
                await rec.proc.wait()

    async def _record_episode(self, rec: _EpisodeRecording, rtsp_url: str, _retries: int = 0):
        key = self._rec_key(rec.episode_id, rec.device_id)
        os.makedirs(os.path.dirname(rec.output_path), exist_ok=True)
        segments_before = rec.next_segment_index
        wait_task = None
        returncode = -1

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-n",
                "-rtsp_transport",
                "tcp",
                "-i",
                rtsp_url,
                "-map",
                "0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-f",
                "segment",
                "-segment_time",
                str(self._segment_seconds),
                "-reset_timestamps",
                "1",
                "-segment_format",
                "mp4",
                "-segment_start_number",
                str(rec.next_segment_index),
                rec.working_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rec.proc = proc
            wait_task = asyncio.create_task(proc.wait())
            while not wait_task.done():
                done, _ = await asyncio.wait({wait_task}, timeout=1)
                await self._finalize_ready_segments(rec, include_latest=bool(done))
            returncode = await wait_task
        except asyncio.CancelledError:
            await self._terminate_process(rec)
            if wait_task:
                await asyncio.gather(wait_task, return_exceptions=True)
            await self._finalize_ready_segments(rec, include_latest=True)
            self._recordings.pop(key, None)
            self._remove_working_segments(rec)
            raise
        except Exception:
            logger.exception(
                "Recording process failed for episode %s camera %s",
                rec.episode_id[:8],
                rec.device_id,
            )
        finally:
            rec.proc = None

        await self._finalize_ready_segments(rec, include_latest=True)

        if self._recordings.get(key) is not rec:
            return
        episode = await self._repo.get_episode(rec.episode_id)
        if not self._running or not episode or episode.state == EpisodeState.CLOSED:
            self._recordings.pop(key, None)
            return

        retry = 0 if rec.next_segment_index > segments_before else _retries + 1
        if retry > 3:
            self._recordings.pop(key, None)
            self._remove_working_segments(rec)
            logger.error(
                "Recording failed for episode %s camera %s after 3 retries",
                rec.episode_id[:8],
                rec.device_id,
            )
            return

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
            return
        episode = await self._repo.get_episode(rec.episode_id)
        if not episode or episode.state == EpisodeState.CLOSED:
            self._recordings.pop(key, None)
            return
        await self._record_episode(rec, rtsp_url, retry)

    def _segment_entries(self, rec: _EpisodeRecording) -> list[tuple[int, str]]:
        entries = []
        pattern = rec.working_path.replace("%06d", "*")
        for path in sorted(glob.glob(pattern)):
            index_text = path.removesuffix(".mp4.part").rsplit("_", 1)[-1]
            if index_text.isdigit():
                entries.append((int(index_text), path))
        return entries

    async def _finalize_ready_segments(
        self, rec: _EpisodeRecording, *, include_latest: bool
    ) -> None:
        entries = self._segment_entries(rec)
        if not entries:
            return

        observed_at = datetime.now(tz=timezone.utc)
        for index, _ in entries:
            if index == 0 and rec.start_time:
                rec.segment_started_at.setdefault(index, rec.start_time)
            else:
                rec.segment_started_at.setdefault(index, observed_at)

        ready = entries if include_latest else entries[:-1]
        for index, working_path in ready:
            started_at = rec.segment_started_at.get(index, observed_at)
            ended_at = rec.segment_started_at.get(index + 1, observed_at)
            await self._finalize_segment(
                rec,
                index,
                working_path,
                started_at=started_at,
                ended_at=ended_at,
            )
            rec.next_segment_index = max(rec.next_segment_index, index + 1)

    async def _finalize_segment(
        self,
        rec: _EpisodeRecording,
        index: int,
        working_path: str,
        *,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        if not os.path.exists(working_path):
            return
        if os.path.getsize(working_path) < 4096 or not await self._has_video_stream(working_path):
            logger.warning(
                "Recording segment invalid for episode %s camera %s, discarding",
                rec.episode_id[:8],
                rec.device_id,
            )
            self._remove_file(working_path)
            return

        output_path = working_path.removesuffix(".part")
        os.replace(working_path, output_path)
        byte_size = os.path.getsize(output_path)
        duration = max(0, int((ended_at - started_at).total_seconds()))
        evidence = Evidence(
            device_id=rec.device_id,
            area_id=rec.area_id,
            timestamp=started_at,
            evidence_type="recording",
            file_path=output_path,
            mime_type="video/mp4",
            episode_id=rec.episode_id,
            metadata={
                "origin": "recording",
                "recording_session_id": rec.session_id,
                "segment_index": index,
                "segment_seconds": self._segment_seconds,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_seconds": duration,
            },
        )
        await self._bus.publish(
            Message(type="evidence.received", data={"evidence": asdict(evidence)})
        )
        logger.info(
            "Recording segment %d complete for episode %s camera %s: %s (%.1f KB)",
            index,
            rec.episode_id[:8],
            rec.device_id,
            os.path.basename(output_path),
            byte_size / 1024,
        )

    def _remove_working_segments(self, rec: _EpisodeRecording) -> None:
        for _, path in self._segment_entries(rec):
            self._remove_file(path)

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
            logger.warning("Could not validate recording %s", os.path.basename(path))
            return False

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0 and stdout.strip() == b"video"
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            logger.warning("Could not validate recording %s", os.path.basename(path))
            return False

    @staticmethod
    def _remove_file(path: str):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def status(self) -> dict:
        cameras = len(set(k[1] for k in self._recordings))
        return {
            "running": self._running,
            "active_recordings": len(self._recordings),
            "cameras": cameras,
            "segment_seconds": self._segment_seconds,
        }
