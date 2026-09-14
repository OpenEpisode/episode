from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from episode.api.thumbnails import ThumbnailCache
from episode.domain.models import Event, Evidence
from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

RETENTION_POLICY_SETTING = "visual_evidence_retention_policy"
DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool = True
    retention_days: int = DEFAULT_RETENTION_DAYS
    confirmed_at: datetime | None = None

    @property
    def state(self) -> str:
        if not self.enabled:
            return "disabled"
        return "configured" if self.confirmed_at else "unconfirmed"


class RetentionService:
    """Expire Episode-managed visual material under one global policy."""

    def __init__(
        self,
        repository: Repository,
        data_dir: str,
        thumbnails: ThumbnailCache,
        *,
        active_paths: Callable[[], set[str]] | None = None,
        interval_seconds: float = 3600,
    ) -> None:
        self._repository = repository
        self._data_dir = data_dir
        self._thumbnails = thumbnails
        self._active_paths = active_paths or set
        self._interval_seconds = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
        self._cleanup_lock = asyncio.Lock()
        self._last_cleanup_at: datetime | None = None
        self._last_error: str | None = None
        self._expired_count = 0
        self._failure_count = 0
        self._policy = RetentionPolicy()
        self._initial_cleanup_state = "pending"

    async def start(self) -> None:
        if self._running:
            return
        # Load the policy before returning from startup.  The potentially
        # expensive filesystem sweep is supervised by the background task
        # below, after the recorder has completed its recovery step.
        await self.get_policy()
        self._running = True
        self._initial_cleanup_state = "pending"
        self._task = asyncio.create_task(self._cleanup_loop(), name="visual-retention-loop")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def get_policy(self) -> RetentionPolicy:
        value = await self._repository.get_system_setting(RETENTION_POLICY_SETTING)
        if value is None:
            self._policy = RetentionPolicy()
            return self._policy
        try:
            stored = json.loads(value)
            enabled = stored["enabled"]
            days = stored["retention_days"]
            confirmed_value = stored["confirmed_at"]
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean")
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
                raise ValueError("retention_days must be between 1 and 3650")
            confirmed_at = (
                datetime.fromisoformat(confirmed_value) if confirmed_value is not None else None
            )
            if confirmed_at and confirmed_at.tzinfo is None:
                confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
            self._policy = RetentionPolicy(
                enabled=enabled,
                retention_days=days,
                confirmed_at=confirmed_at,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.error("Invalid stored visual Evidence retention policy; using default")
            self._policy = RetentionPolicy()
        return self._policy

    async def get_retention_days(self) -> int:
        return (await self.get_policy()).retention_days

    async def set_policy(self, *, enabled: bool, retention_days: int) -> RetentionPolicy:
        days = retention_days
        if days < 1 or days > 3650:
            raise ValueError("Retention period must be between 1 and 3650 days")
        policy = RetentionPolicy(
            enabled=enabled,
            retention_days=days,
            confirmed_at=datetime.now(tz=timezone.utc),
        )
        await self._repository.set_system_setting(
            RETENTION_POLICY_SETTING,
            json.dumps(
                {
                    "enabled": policy.enabled,
                    "retention_days": policy.retention_days,
                    "confirmed_at": policy.confirmed_at.isoformat(),
                },
                separators=(",", ":"),
            ),
        )
        self._policy = policy
        if enabled:
            await self.run_once()
        return policy

    async def run_once(self, *, now: datetime | None = None) -> int:
        async with self._cleanup_lock:
            observed_at = now or datetime.now(tz=timezone.utc)
            policy = await self.get_policy()
            if not policy.enabled:
                return 0
            cutoff = observed_at - timedelta(days=policy.retention_days)
            expired = 0
            errors: list[str] = []

            for evidence in await self._repository.list_visual_evidence_before(cutoff):
                try:
                    if await self._expire_evidence(evidence, observed_at):
                        expired += 1
                except Exception as error:
                    logger.exception("Could not expire Evidence %s", evidence.id)
                    errors.append(f"Evidence {evidence.id}: {error}")

            try:
                expired += await self._expire_embedded_event_pictures(cutoff, observed_at)
            except Exception as error:
                logger.exception("Could not expire embedded Event pictures")
                errors.append(f"Embedded Event pictures: {error}")

            self._last_cleanup_at = observed_at
            self._expired_count += expired
            if errors:
                self._failure_count += len(errors)
                self._last_error = errors[0][:240]
            else:
                self._last_error = None
            return expired

    async def _expire_evidence(self, evidence: Evidence, expired_at: datetime) -> bool:
        artifacts = await self._repository.visual_artifacts_for_evidence(evidence.id)
        paths = {evidence.file_path, *(artifact.file_path for artifact in artifacts)} - {""}
        if (
            evidence.metadata.get("format") == "hls-fmp4"
            and evidence.file_path
            and evidence.episode_id
            and not evidence.file_path.startswith("expired:")
        ):
            bundle_root = Path(evidence.file_path).parent.resolve()
            expected_root = Path(
                self._data_dir,
                "episodes",
                evidence.episode_id,
                "recordings",
                evidence.id,
            ).resolve()
            if bundle_root == expected_root:
                paths.add(str(bundle_root))
        if self._contains_active_path(paths):
            return False

        await self._remove_paths(paths)
        await self._thumbnails.discard(evidence)
        if evidence.episode_id:
            timelapse_dir = Path(
                self._data_dir,
                "episodes",
                evidence.episode_id,
                "timelapses",
            )
            await asyncio.to_thread(shutil.rmtree, timelapse_dir, True)
        await self._repository.mark_evidence_expired(
            evidence,
            expired_at=expired_at,
            artifact_ids=[artifact.id for artifact in artifacts],
        )
        return True

    async def _expire_embedded_event_pictures(
        self,
        cutoff: datetime,
        expired_at: datetime,
    ) -> int:
        expired = 0
        offset = 0
        while True:
            events = await self._repository.list_events(limit=500, offset=offset)
            if not events:
                break
            for event in events:
                if event.timestamp >= cutoff or not self._has_embedded_picture(event):
                    continue
                artifacts = await self._repository.visual_artifacts_for_event(event.id)
                paths = {event.raw_payload_path or "", *(item.file_path for item in artifacts)} - {
                    ""
                }
                if self._contains_active_path(paths):
                    continue
                await self._remove_paths(paths)
                await self._repository.mark_event_visual_expired(
                    event,
                    expired_at=expired_at,
                    artifact_ids=[artifact.id for artifact in artifacts],
                )
                expired += 1
            if len(events) < 500:
                break
            offset += len(events)
        return expired

    @staticmethod
    def _has_embedded_picture(event: Event) -> bool:
        descriptor = event.metadata.get("embedded_picture")
        return bool(
            event.raw_payload_path
            and isinstance(descriptor, dict)
            and isinstance(descriptor.get("byte_size"), int)
            and descriptor["byte_size"] > 0
        )

    def _contains_active_path(self, paths: set[str]) -> bool:
        active = {os.path.abspath(path) for path in self._active_paths()}
        return any(
            os.path.abspath(path) in active for path in paths if not path.startswith("expired:")
        )

    @staticmethod
    async def _remove_paths(paths: set[str]) -> None:
        for path in sorted(paths, key=lambda value: len(Path(value).parts), reverse=True):
            if path.startswith("expired:"):
                continue
            try:
                if os.path.isdir(path):
                    await asyncio.to_thread(shutil.rmtree, path)
                else:
                    await asyncio.to_thread(os.remove, path)
            except FileNotFoundError:
                continue

    async def _cleanup_loop(self) -> None:
        self._initial_cleanup_state = "running"
        try:
            await self.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._initial_cleanup_state = "failed"
            self._failure_count += 1
            self._last_error = str(error)[:240]
            logger.exception("Initial visual Evidence retention cleanup failed")
        else:
            self._initial_cleanup_state = "failed" if self._last_error else "completed"

        while self._running:
            try:
                await asyncio.sleep(self._interval_seconds)
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failure_count += 1
                self._last_error = str(error)[:240]
                logger.exception("Visual Evidence retention cleanup failed")

    def status(self) -> dict:
        return {
            "running": self._running,
            "state": "degraded"
            if self._last_error
            else "healthy"
            if self._running
            else "unavailable",
            "enabled": self._policy.enabled,
            "retention_days": self._policy.retention_days,
            "policy_state": self._policy.state,
            "confirmed_at": (
                self._policy.confirmed_at.isoformat() if self._policy.confirmed_at else None
            ),
            "last_cleanup_at": self._last_cleanup_at.isoformat() if self._last_cleanup_at else None,
            "initial_cleanup": self._initial_cleanup_state,
            "expired_count": self._expired_count,
            "failure_count": self._failure_count,
            "last_error": self._last_error,
        }
