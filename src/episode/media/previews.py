from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol


class SnapshotSource(Protocol):
    def get(self, device_id: str): ...

    async def fetch_snapshot(self, device_id: str) -> tuple[bytes, str]: ...


class ActiveRecordingSource(Protocol):
    def active_device_ids(self, episode_id: str) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class CurrentViewDescriptor:
    device_id: str
    mode: str
    refresh_interval_seconds: int


@dataclass(frozen=True)
class _CachedPreview:
    content: bytes
    media_type: str
    fetched_at: float


class CurrentViewService:
    """Serve short-lived camera previews without turning them into Evidence."""

    def __init__(
        self,
        snapshots: SnapshotSource,
        recordings: ActiveRecordingSource,
        *,
        refresh_interval_seconds: int = 3,
        max_cached_devices: int = 64,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be greater than zero")
        if max_cached_devices <= 0:
            raise ValueError("max_cached_devices must be greater than zero")
        self._snapshots = snapshots
        self._recordings = recordings
        self._refresh_interval = refresh_interval_seconds
        self._max_cached_devices = max_cached_devices
        self._cache: dict[str, _CachedPreview] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def describe(self, episode_id: str) -> tuple[CurrentViewDescriptor, ...]:
        return tuple(
            CurrentViewDescriptor(
                device_id=device_id,
                mode="snapshot" if self._has_snapshot(device_id) else "unavailable",
                refresh_interval_seconds=self._refresh_interval,
            )
            for device_id in self._recordings.active_device_ids(episode_id)
        )

    async def fetch(self, episode_id: str, device_id: str) -> tuple[bytes, str]:
        if device_id not in self._recordings.active_device_ids(episode_id):
            raise LookupError("Device is not recording this Episode")
        if not self._has_snapshot(device_id):
            raise LookupError("Device has no current-view provider")

        cached = self._fresh_cache_entry(device_id)
        if cached:
            return cached.content, cached.media_type

        lock = self._locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            cached = self._fresh_cache_entry(device_id)
            if cached:
                return cached.content, cached.media_type
            content, media_type = await self._snapshots.fetch_snapshot(device_id)
            self._make_cache_room(device_id)
            self._cache[device_id] = _CachedPreview(content, media_type, time.monotonic())
            return content, media_type

    def _has_snapshot(self, device_id: str) -> bool:
        source = self._snapshots.get(device_id)
        return bool(source and source.snapshot_uri)

    def _fresh_cache_entry(self, device_id: str) -> _CachedPreview | None:
        cached = self._cache.get(device_id)
        if cached and time.monotonic() - cached.fetched_at < self._refresh_interval:
            return cached
        return None

    def _make_cache_room(self, device_id: str) -> None:
        if device_id in self._cache or len(self._cache) < self._max_cached_devices:
            return
        oldest_device = min(
            self._cache,
            key=lambda cached_device: self._cache[cached_device].fetched_at,
        )
        self._cache.pop(oldest_device, None)
        lock = self._locks.get(oldest_device)
        if lock is not None and not lock.locked():
            self._locks.pop(oldest_device, None)
