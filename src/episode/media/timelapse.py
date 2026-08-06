from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from episode.domain.models import EpisodeState, Evidence

if TYPE_CHECKING:
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

_SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9_.-]+")


class TimelapseNotFoundError(Exception):
    pass


class TimelapseGenerationError(Exception):
    pass


def is_timelapse_eligible(evidence: Evidence) -> bool:
    """Return whether snapshot evidence may contribute frames to a timelapse."""
    eligible = evidence.metadata.get("timelapse_eligible", True)
    return evidence.evidence_type == "snapshot" and eligible is not False


def _cache_component(value: str) -> str:
    return _SAFE_COMPONENT.sub("-", value).strip(".-")[:80] or "device"


class TimelapseService:
    """Own timelapse selection, generation, caching, and background precaching."""

    def __init__(self, repository: Repository, data_dir: str):
        self._repository = repository
        self._data_dir = data_dir
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._precache_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._precache_task and not self._precache_task.done():
            return
        self._precache_task = asyncio.create_task(
            self._precache_closed_episodes(),
            name="timelapse-precache",
        )

    async def stop(self) -> None:
        if not self._precache_task:
            return
        self._precache_task.cancel()
        await asyncio.gather(self._precache_task, return_exceptions=True)
        self._precache_task = None

    async def get_or_create(self, episode_id: str, device_id: str | None = None) -> str:
        episode = await self._repository.get_episode(episode_id)
        if not episode:
            raise TimelapseNotFoundError("Episode not found")

        evidence = await self._repository.list_evidence(episode_id=episode_id, limit=10000)
        snapshots = [
            item
            for item in evidence
            if is_timelapse_eligible(item)
            and item.file_path
            and os.path.isfile(item.file_path)
            and (not device_id or item.device_id == device_id)
        ]
        snapshots.sort(key=lambda item: item.timestamp)
        if not snapshots:
            raise TimelapseNotFoundError("No snapshot evidence for this episode")

        key = (episode_id, device_id or "")
        async with self._locks[key]:
            cache_path = self._cache_path(episode_id, device_id)
            if self._cache_is_current(cache_path, snapshots):
                return cache_path
            await self._generate(snapshots, cache_path)
            return cache_path

    def _cache_path(self, episode_id: str, device_id: str | None) -> str:
        suffix = f"_{_cache_component(device_id)}" if device_id else ""
        return os.path.join(
            self._data_dir,
            "episodes",
            episode_id,
            "timelapses",
            f"timelapse{suffix}.mp4",
        )

    @staticmethod
    def _cache_is_current(cache_path: str, snapshots: list[Evidence]) -> bool:
        if not os.path.isfile(cache_path):
            return False
        latest_snapshot = max(item.timestamp for item in snapshots)
        cache_time = datetime.fromtimestamp(os.path.getmtime(cache_path), tz=timezone.utc)
        return cache_time > latest_snapshot

    async def _generate(self, snapshots: list[Evidence], cache_path: str) -> None:
        cache_dir = os.path.dirname(cache_path)
        os.makedirs(cache_dir, exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary_dir:
            sequence: list[str] = []
            for index, item in enumerate(snapshots):
                extension = Path(item.file_path).suffix or ".jpg"
                link = os.path.join(temporary_dir, f"img{index:06d}{extension}")
                os.symlink(item.file_path, link)
                sequence.append(link)

            concat_file = os.path.join(temporary_dir, "files.txt")
            with open(concat_file, "w", encoding="utf-8") as stream:
                for link in sequence:
                    stream.write(f"file '{link}'\nduration 0.2\n")

            output = os.path.join(temporary_dir, "timelapse.mp4")
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file,
                "-fps_mode",
                "vfr",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                output,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await process.wait()
            if process.returncode != 0 or not os.path.isfile(output):
                raise TimelapseGenerationError("Failed to generate timelapse")

            temporary_cache = f"{cache_path}.tmp"
            try:
                shutil.copy2(output, temporary_cache)
                os.replace(temporary_cache, cache_path)
            finally:
                if os.path.exists(temporary_cache):
                    os.unlink(temporary_cache)

    async def _precache_closed_episodes(self) -> None:
        try:
            episodes = await self._repository.list_episodes(
                state=EpisodeState.CLOSED,
                limit=10000,
            )
            for episode in episodes:
                try:
                    await self.get_or_create(episode.id)
                except TimelapseNotFoundError:
                    continue
                except Exception:
                    logger.exception("Could not precache timelapse for episode %s", episode.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Timelapse precache failed")
