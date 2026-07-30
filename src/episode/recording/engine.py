from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from episode.domain.models import Device, EpisodeState, Event, Evidence
from episode.engine.bus import EventBus, Message
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
    start_time: datetime | None = None
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None


class RecordingEngine:
    def __init__(
        self,
        repo: Repository,
        bus: EventBus,
        data_dir: str,
        media=None,
        target_resolver: RecordingTargetResolver | None = None,
    ):
        self._repo = repo
        self._bus = bus
        self._data_dir = data_dir
        self._media = media
        self._target_resolver = target_resolver or AreaRecordingTargetResolver(repo)
        self._active_tasks: set[asyncio.Task] = set()
        self._recordings: dict[tuple[str, str], _EpisodeRecording] = {}
        self._running = False

    def _rec_key(self, episode_id: str, device_id: str) -> tuple[str, str]:
        return (episode_id, device_id)

    async def start(self):
        self._running = True
        self._bus.subscribe("event.received", self._on_event)
        self._bus.subscribe("episode.updated", self._on_episode_updated)

    async def stop(self):
        self._running = False
        for rec in list(self._recordings.values()):
            await self._stop_recording(rec)
        if self._active_tasks:
            _, pending = await asyncio.wait(self._active_tasks, timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _on_event(self, msg: Message):
        if msg.data.get("canonical_event_created") is False:
            return
        event = Event(**msg.data["event"])
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

        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        safe_device_id = re.sub(r"[^A-Za-z0-9._-]+", "-", device.id).strip("._-")
        safe_device_id = (safe_device_id or "device")[:64]
        recording_id = uuid.uuid4().hex[:12]
        filename = f"rec_{safe_device_id}_{ts}_{recording_id}.mp4"
        output = os.path.join(self._data_dir, "episodes", episode_id, "recordings", filename)
        working = f"{output}.part"

        rec = _EpisodeRecording(
            episode_id=episode_id,
            device_id=device.id,
            area_id=device.area_id,
            output_path=output,
            working_path=working,
            start_time=datetime.now(tz=timezone.utc),
        )
        self._recordings[key] = rec

        rec.task = asyncio.create_task(self._record_episode(rec, rtsp_url))
        self._active_tasks.add(rec.task)
        rec.task.add_done_callback(self._active_tasks.discard)

        logger.info(
            "Started recording %s for episode %s camera %s",
            filename,
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
                "mp4",
                rec.working_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rec.proc = proc
            await proc.wait()
        except asyncio.CancelledError:
            self._recordings.pop(key, None)
            await self._terminate_process(rec)
            self._remove_file(rec.working_path)
            raise
        except Exception:
            self._recordings.pop(key, None)
            logger.exception(
                "Recording failed for episode %s camera %s", rec.episode_id[:8], rec.device_id
            )
            self._remove_file(rec.working_path)
            return

        self._recordings.pop(key, None)

        if proc.returncode not in (0, -15, -9, 255):
            if _retries < 3:
                episode = await self._repo.get_episode(rec.episode_id)
                if episode and episode.state != EpisodeState.CLOSED:
                    logger.warning(
                        "Recording failed for episode %s camera %s "
                        "(ffmpeg exit %d), retrying (%d/3)...",
                        rec.episode_id[:8],
                        rec.device_id,
                        proc.returncode,
                        _retries + 1,
                    )
                    self._remove_file(rec.working_path)
                    self._recordings[key] = rec
                    await asyncio.sleep(2)
                    if self._recordings.get(key) is not rec:
                        return
                    episode = await self._repo.get_episode(rec.episode_id)
                    if not episode or episode.state == EpisodeState.CLOSED:
                        self._recordings.pop(key, None)
                        return
                    await self._record_episode(rec, rtsp_url, _retries + 1)
                    return

            logger.error(
                "Recording failed for episode %s camera %s (ffmpeg exit %d)",
                rec.episode_id[:8],
                rec.device_id,
                proc.returncode,
            )
            self._remove_file(rec.working_path)
            return

        if (
            not os.path.exists(rec.working_path)
            or os.path.getsize(rec.working_path) < 4096
            or not await self._has_video_stream(rec.working_path)
        ):
            logger.warning(
                "Recording invalid for episode %s camera %s, discarding",
                rec.episode_id[:8],
                rec.device_id,
            )
            self._remove_file(rec.working_path)
            return

        os.replace(rec.working_path, rec.output_path)

        duration = None
        if rec.start_time:
            duration = int((datetime.now(tz=timezone.utc) - rec.start_time).total_seconds())
        evidence = Evidence(
            device_id=rec.device_id,
            area_id=rec.area_id,
            timestamp=datetime.now(tz=timezone.utc),
            evidence_type="recording",
            file_path=rec.output_path,
            mime_type="video/mp4",
            episode_id=rec.episode_id,
            metadata={"origin": "recording", "duration_seconds": duration}
            if duration
            else {"origin": "recording"},
        )

        await self._bus.publish(
            Message(type="evidence.received", data={"evidence": asdict(evidence)})
        )

        logger.info(
            "Recording complete for episode %s camera %s: %s (%.1f KB)",
            rec.episode_id[:8],
            rec.device_id,
            os.path.basename(rec.output_path),
            os.path.getsize(rec.output_path) / 1024,
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
        }
