from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from episode.domain.models import Device, EpisodeState, Evidence
from episode.engine.bus import EventBus, Message

if TYPE_CHECKING:
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

MAX_RECORDING_SECONDS = 600


@dataclass
class _EpisodeRecording:
    episode_id: str
    device_id: str
    area_id: str
    output_path: str
    start_time: datetime | None = None
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    stop_timer: asyncio.Task | None = None


class RecordingEngine:
    def __init__(
        self,
        repo: Repository,
        bus: EventBus,
        data_dir: str,
        pre_seconds: int = 0,
        post_seconds: int = 30,
        media=None,
    ):
        self._repo = repo
        self._bus = bus
        self._data_dir = data_dir
        self._pre_seconds = pre_seconds
        self._post_seconds = post_seconds
        self._media = media
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
        for task in list(self._active_tasks):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    async def _on_event(self, msg: Message):
        if msg.data.get("canonical_event_created") is False:
            return
        event_data = msg.data["event"]
        if event_data.get("event_state", "active") != "active":
            return
        episode_id = event_data.get("episode_id", "")
        if not episode_id:
            return
        device_id = event_data.get("device_id", "")
        device = await self._repo.get_device(device_id)
        if not device or "video" not in device.capabilities:
            return
        discovered = self._media.get(device.id) if self._media else None
        cap = device.get_config("video")
        url = (
            discovered.authenticated_stream_uri()
            if discovered
            else cap.build_url(device.ip_address, device.username, device.password)
            if cap
            else ""
        )
        if not url:
            return

        key = self._rec_key(episode_id, device.id)
        if key in self._recordings:
            await self._extend_recording(episode_id, device.id)
            return

        is_first_for_episode = not any(k[0] == episode_id for k in self._recordings)

        await self._start_recording(episode_id, device, url)

        # Start companion recordings for on_episode devices on the same area
        if is_first_for_episode:
            await self._start_on_episode_recordings(episode_id, device.area_id)

    async def _start_on_episode_recordings(self, episode_id: str, area_id: str):
        devices = await self._repo.list_devices(area_id=area_id)
        for other in devices:
            if "video" not in other.capabilities:
                continue
            cap = other.get_config("video")
            if not cap:
                continue
            mode = cap.settings.get("recording_mode", "on_event")
            if mode != "on_episode":
                continue
            key = self._rec_key(episode_id, other.id)
            if key in self._recordings:
                continue
            discovered = self._media.get(other.id) if self._media else None
            url = (
                discovered.authenticated_stream_uri()
                if discovered
                else cap.build_url(other.ip_address, other.username, other.password)
            )
            if not url:
                continue
            await self._start_recording(episode_id, other, url, arm_timer=False)

    async def _start_recording(
        self, episode_id: str, device: Device, rtsp_url: str, *, arm_timer: bool = True
    ):
        key = self._rec_key(episode_id, device.id)

        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"rec_{device.id[:8]}_{ts}.mp4"
        output = os.path.join(self._data_dir, "episodes", episode_id, "recordings", filename)

        rec = _EpisodeRecording(
            episode_id=episode_id,
            device_id=device.id,
            area_id=device.area_id,
            output_path=output,
            start_time=datetime.now(tz=timezone.utc),
        )
        self._recordings[key] = rec

        rec.task = asyncio.create_task(self._record_episode(rec, rtsp_url))
        self._active_tasks.add(rec.task)

        if arm_timer:
            await self._arm_stop_timer(rec)

        logger.info(
            "Started recording %s for episode %s camera %s",
            filename,
            episode_id[:8],
            device.id[:8],
        )

    async def _extend_recording(self, episode_id: str, device_id: str):
        key = self._rec_key(episode_id, device_id)
        rec = self._recordings.get(key)
        if not rec:
            return
        if rec.stop_timer and not rec.stop_timer.done():
            rec.stop_timer.cancel()
        await self._arm_stop_timer(rec)
        logger.debug(
            "Extended recording for episode %s camera %s (+%ss)",
            episode_id[:8],
            device_id[:8],
            self._post_seconds,
        )

    async def _arm_stop_timer(self, rec: _EpisodeRecording):
        key = self._rec_key(rec.episode_id, rec.device_id)

        async def _stop_after_timeout():
            await asyncio.sleep(self._post_seconds)
            if key in self._recordings:
                self._recordings.pop(key, None)
                await self._stop_recording(rec)

        rec.stop_timer = asyncio.create_task(_stop_after_timeout())

    async def _on_episode_updated(self, msg: Message):
        episode_id = msg.data.get("episode_id", "")
        state = msg.data.get("state", "")
        if state == "closed":
            to_stop = [k for k, r in self._recordings.items() if k[0] == episode_id]
            for key in to_stop:
                rec = self._recordings.pop(key, None)
                if rec:
                    if rec.stop_timer and not rec.stop_timer.done():
                        rec.stop_timer.cancel()
                    await self._stop_recording(rec)

    async def _stop_recording(self, rec: _EpisodeRecording):
        key = self._rec_key(rec.episode_id, rec.device_id)
        self._recordings.pop(key, None)
        if rec.stop_timer and not rec.stop_timer.done():
            rec.stop_timer.cancel()
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
                "-y",
                "-rtsp_transport",
                "tcp",
                "-i",
                rtsp_url,
                "-t",
                str(MAX_RECORDING_SECONDS),
                "-map",
                "0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                rec.output_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rec.proc = proc
            await proc.wait()
        except Exception:
            logger.exception(
                "Recording failed for episode %s camera %s", rec.episode_id[:8], rec.device_id[:8]
            )
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
                        rec.device_id[:8],
                        proc.returncode,
                        _retries + 1,
                    )
                    if os.path.exists(rec.output_path):
                        os.remove(rec.output_path)
                    self._recordings[key] = rec
                    if rec.stop_timer and not rec.stop_timer.done():
                        rec.stop_timer.cancel()
                    await self._arm_stop_timer(rec)
                    await asyncio.sleep(2)
                    await self._record_episode(rec, rtsp_url, _retries + 1)
                    return

            logger.error(
                "Recording failed for episode %s camera %s (ffmpeg exit %d)",
                rec.episode_id[:8],
                rec.device_id[:8],
                proc.returncode,
            )
            if os.path.exists(rec.output_path):
                os.remove(rec.output_path)
            return

        if not os.path.exists(rec.output_path) or os.path.getsize(rec.output_path) < 4096:
            logger.warning(
                "Recording too small for episode %s camera %s, discarding",
                rec.episode_id[:8],
                rec.device_id[:8],
            )
            if os.path.exists(rec.output_path):
                os.remove(rec.output_path)
            return

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
            rec.device_id[:8],
            os.path.basename(rec.output_path),
            os.path.getsize(rec.output_path) / 1024,
        )

    def status(self) -> dict:
        cameras = len(set(k[1] for k in self._recordings))
        return {
            "running": self._running,
            "active_recordings": len(self._recordings),
            "cameras": cameras,
        }
