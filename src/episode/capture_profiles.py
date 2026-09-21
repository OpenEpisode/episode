from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from episode.domain.event_filter import (
    FILTER_SOURCE_DEVICE,
    FILTER_SOURCE_PROFILE,
    FILTERED_REASON,
    LEGACY_FILTER_CLASSES,
    event_class,
    is_filterable,
    normalize_selector,
    validate_selector,
)
from episode.domain.models import (
    CaptureProfile,
    CaptureProfileChange,
    Event,
    EventState,
    ParticipationDecision,
    make_event_dedup_key,
)
from episode.storage.capture_profiles import (
    ALL_DEVICES_PROFILE_ID,
)

MAX_CAPTURE_PROFILE_NAME = 100
MAX_CAPTURE_PROFILE_DEVICES = 500
MAX_CAPTURE_PROFILE_CHANGES = 100
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def resolve_profile_event_filter(
    event_filter: list[str] | None,
    filter_generic_events: bool | None,
) -> list[str] | None:
    """Resolve a profile selector request, preferring the class field.

    ``None`` for both means "leave the stored selector alone" on update and "no
    filtering" on create. The deprecated ``beta.7`` boolean is translated to its
    historical class equivalent, which suppressed motion, status, and audio but
    not ``security``.
    """
    if event_filter is not None:
        return list(event_filter)
    if filter_generic_events is None:
        return None
    return sorted(LEGACY_FILTER_CLASSES) if filter_generic_events else []


class CaptureProfileError(ValueError):
    """Expected operator error while managing a capture profile."""


class CaptureProfileNotFoundError(CaptureProfileError):
    pass


class CaptureProfileConflictError(CaptureProfileError):
    pass


class CaptureProfileService:
    """Core capture participation policy and profile management.

    The service deliberately contains only one bounded decision: whether a
    Device's newly canonicalized active Event can affect Episode capture. It
    does not interpret plugin metadata or expose a general rule engine.
    """

    def __init__(self, repository) -> None:
        self._repository = repository
        self._lock = asyncio.Lock()

    async def list_profiles(self) -> list[CaptureProfile]:
        profiles = await self._repository.list_capture_profiles()
        active = await self._repository.get_active_capture_profile()
        active_id = active.id if active else ALL_DEVICES_PROFILE_ID
        return [self._with_active(profile, profile.id == active_id) for profile in profiles]

    async def get_profile(self, profile_id: str) -> CaptureProfile | None:
        try:
            self._validate_profile_id(profile_id)
        except CaptureProfileError:
            return None
        profile = await self._repository.get_capture_profile(profile_id)
        if profile is None:
            return None
        active = await self._repository.get_active_capture_profile()
        return self._with_active(profile, bool(active and active.id == profile.id))

    async def active_response(
        self, *, limit: int = 20
    ) -> tuple[CaptureProfile, list[CaptureProfileChange]]:
        active = await self._repository.get_active_capture_profile()
        if active is None:
            # Repository initialization creates this row. Keeping the error
            # explicit avoids silently inventing mutable runtime state if a
            # damaged database is opened.
            raise RuntimeError("No active capture profile is persisted")
        bounded = max(1, min(int(limit), MAX_CAPTURE_PROFILE_CHANGES))
        return (
            self._with_active(active, True),
            await self._repository.list_capture_profile_changes(limit=bounded),
        )

    async def create_profile(
        self,
        name: str,
        device_ids: list[str] | None = None,
        *,
        event_filter: list[str] | None = None,
    ) -> CaptureProfile:
        normalized_name = self._validate_name(name)
        normalized_filter = self._validate_event_filter(event_filter)
        async with self._lock:
            normalized_ids = await self._validate_device_ids(device_ids or [])
            existing = await self._repository.list_capture_profiles()
            if any(item.name.casefold() == normalized_name.casefold() for item in existing):
                raise CaptureProfileConflictError("A capture profile with that name already exists")
            profile = CaptureProfile(
                id=self._new_profile_id(),
                name=normalized_name,
                include_all_devices=False,
                device_ids=normalized_ids,
                builtin=False,
                event_filter=normalized_filter,
            )
            try:
                return await self._repository.create_capture_profile(profile)
            except Exception as error:
                # A concurrent creator can win the unique name constraint.
                if "UNIQUE" in str(error).upper():
                    raise CaptureProfileConflictError(
                        "A capture profile with that name already exists"
                    ) from error
                raise

    async def update_profile(
        self,
        profile_id: str,
        name: str,
        device_ids: list[str] | None = None,
        *,
        event_filter: list[str] | None = None,
    ) -> CaptureProfile:
        normalized_name = self._validate_name(name)
        # ``None`` means "leave the stored selector alone"; an empty list is the
        # explicit operator choice to stop filtering.
        normalized_filter = (
            None if event_filter is None else self._validate_event_filter(event_filter)
        )
        async with self._lock:
            self._validate_profile_id(profile_id)
            existing = await self._repository.get_capture_profile(profile_id)
            if existing is None:
                raise CaptureProfileNotFoundError("Capture profile not found")
            if existing.builtin:
                raise CaptureProfileConflictError(
                    "The built-in All Devices profile cannot be edited"
                )
            normalized_ids = await self._validate_device_ids(device_ids or [])
            profiles = await self._repository.list_capture_profiles()
            if any(
                item.id != profile_id and item.name.casefold() == normalized_name.casefold()
                for item in profiles
            ):
                raise CaptureProfileConflictError("A capture profile with that name already exists")
            updated = replace(
                existing,
                name=normalized_name,
                device_ids=normalized_ids,
                event_filter=(
                    normalized_filter if normalized_filter is not None else existing.event_filter
                ),
                updated_at=datetime.now(tz=timezone.utc),
            )
            try:
                saved = await self._repository.update_capture_profile(updated)
                active = await self._repository.get_active_capture_profile()
                return self._with_active(saved, bool(active and active.id == saved.id))
            except Exception as error:
                if "UNIQUE" in str(error).upper():
                    raise CaptureProfileConflictError(
                        "A capture profile with that name already exists"
                    ) from error
                raise

    async def delete_profile(self, profile_id: str) -> None:
        async with self._lock:
            self._validate_profile_id(profile_id)
            profile = await self._repository.get_capture_profile(profile_id)
            if profile is None:
                raise CaptureProfileNotFoundError("Capture profile not found")
            if profile.builtin:
                raise CaptureProfileConflictError(
                    "The built-in All Devices profile cannot be deleted"
                )
            active = await self._repository.get_active_capture_profile()
            if active and active.id == profile_id:
                raise CaptureProfileConflictError("The active capture profile cannot be deleted")
            await self._repository.delete_capture_profile(profile_id)

    async def activate_profile(
        self, profile_id: str, *, source: str = "api"
    ) -> tuple[CaptureProfile, CaptureProfileChange, list[CaptureProfileChange]]:
        if not source or len(source) > 64:
            raise CaptureProfileError("Activation source must be a bounded non-empty value")
        self._validate_profile_id(profile_id)
        async with self._lock:
            profile = await self._repository.get_capture_profile(profile_id)
            if profile is None:
                raise CaptureProfileNotFoundError("Capture profile not found")
            change = await self._repository.activate_capture_profile(profile_id, source=source)
            active = await self._repository.get_active_capture_profile()
            if active is None:
                raise RuntimeError("Capture profile activation did not persist an active profile")
            changes = await self._repository.list_capture_profile_changes(limit=20)
            return self._with_active(active, True), change, changes

    async def evaluate_event(self, event: Event) -> tuple[ParticipationDecision, list[str]]:
        """Evaluate and snapshot targets for one newly canonicalized Event."""
        if event.event_state != EventState.ACTIVE:
            raise ValueError("Capture participation decisions apply only to active Events")
        async with self._lock:
            return await self._evaluate_event(event)

    async def canonicalize_event(self, event: Event) -> tuple[Event, bool]:
        """Persist an active Event and its capture decision as one row."""
        if event.event_state != EventState.ACTIVE:
            raise ValueError("Capture participation decisions apply only to active Events")
        if not event.dedup_key:
            event.dedup_key = make_event_dedup_key(
                event.device_id,
                event.timestamp,
                event.event_type,
                event.event_state,
            )
        async with self._lock:
            existing = await self._repository.find_event_by_dedup_key(event.dedup_key)
            if existing is not None:
                return existing, False
            decision, targets = await self._evaluate_event(event)
            event.participation = decision
            event.eligible_recording_device_ids = targets
            return await self._repository.canonicalize_event(event)

    async def _evaluate_event(
        self,
        event: Event,
    ) -> tuple[ParticipationDecision, list[str]]:
        profile = await self._repository.get_active_capture_profile()
        if profile is None:
            raise RuntimeError("No active capture profile is persisted")
        device = await self._repository.get_device(event.device_id)
        allowed = bool(
            device
            and device.can_participate
            and (profile.include_all_devices or event.device_id in profile.device_ids)
        )
        if device is None:
            reason = "device_not_found"
        elif device.setup_state == "needs_setup":
            reason = "device_needs_setup"
        elif not device.enabled:
            reason = "device_disabled"
        elif profile.include_all_devices:
            reason = "all_devices_profile"
        elif event.device_id in profile.device_ids:
            reason = "device_in_profile"
        else:
            reason = "device_not_in_profile"
        filtered_event_type: str | None = None
        filtered_event_class: str | None = None
        filter_source: str | None = None
        if allowed:
            device_override = device.event_filter if device else None
            selector = normalize_selector(
                device_override if device_override is not None else profile.event_filter
            )
            if selector and is_filterable(event.event_type, selector):
                allowed = False
                reason = FILTERED_REASON
                filtered_event_type = event.event_type
                filtered_event_class = event_class(event.event_type)
                filter_source = (
                    FILTER_SOURCE_DEVICE if device_override is not None else FILTER_SOURCE_PROFILE
                )
                # ``attachment`` stays unset here: the Engine records whether it
                # linked the Event to an open Episode or found none, so the
                # outcome is never persisted ahead of the link itself.
        decision = ParticipationDecision(
            allowed=allowed,
            profile_id=profile.id,
            profile_name=profile.name,
            reason=reason,
            evaluated_at=datetime.now(tz=timezone.utc),
            filtered_event_type=filtered_event_type,
            filtered_event_class=filtered_event_class,
            filter_source=filter_source,
        )
        targets = await self._resolve_target_ids(event, profile) if allowed else []
        return decision, targets

    async def _resolve_target_ids(
        self,
        event: Event,
        profile: CaptureProfile,
    ) -> list[str]:
        """Snapshot current eligible video targets for an allowed Event."""
        devices = await self._repository.list_devices(area_id=event.area_id)
        target_ids: list[str] = []
        for device in devices:
            if not device.can_participate:
                continue
            if not profile.include_all_devices and device.id not in profile.device_ids:
                continue
            video = device.get_config("video")
            if video is None:
                continue
            mode = video.settings.get("recording_mode", "on_event")
            if mode == "on_episode" or (mode == "on_event" and device.id == event.device_id):
                target_ids.append(device.id)
        return sorted(set(target_ids))

    @staticmethod
    def _validate_event_filter(event_filter: list[str] | None) -> list[str]:
        """Validate a profile-level selector.

        A profile offers exactly the classes a Device offers: one selector means
        one thing whatever it names and whoever it is applied to. Suppressing a
        security class across a group is a costly decision, so it is made
        legible in the profile summary and the Device list marker rather than
        forbidden by validation. Operator errors (an unknown class name, a
        duplicate) surface as ``CaptureProfileError`` (HTTP 400) instead of
        silently narrowing the selector.
        """
        try:
            return validate_selector(event_filter, level="profile")
        except ValueError as error:
            raise CaptureProfileError(str(error)) from error

    async def _validate_device_ids(self, device_ids: list[str]) -> list[str]:
        if not isinstance(device_ids, list):
            raise CaptureProfileError("device_ids must be an array")
        if len(device_ids) > MAX_CAPTURE_PROFILE_DEVICES:
            raise CaptureProfileError(
                f"A capture profile may include at most {MAX_CAPTURE_PROFILE_DEVICES} Devices"
            )
        normalized: list[str] = []
        for value in device_ids:
            if not isinstance(value, str) or not value or len(value) > 128:
                raise CaptureProfileError("Capture profile Device IDs must be non-empty strings")
            if value in normalized:
                raise CaptureProfileError("Capture profile Device IDs must be unique")
            normalized.append(value)
        missing = [
            value for value in normalized if await self._repository.get_device(value) is None
        ]
        if missing:
            raise CaptureProfileError(
                "Capture profile references unknown Device IDs: " + ", ".join(missing[:5])
            )
        return sorted(normalized)

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str):
            raise CaptureProfileError("Capture profile name must be a string")
        normalized = " ".join(name.strip().split())
        if not normalized:
            raise CaptureProfileError("Capture profile name is required")
        if len(normalized) > MAX_CAPTURE_PROFILE_NAME:
            raise CaptureProfileError(
                f"Capture profile name must be at most {MAX_CAPTURE_PROFILE_NAME} characters"
            )
        return normalized

    @staticmethod
    def _new_profile_id() -> str:
        profile_id = f"capture-{uuid4().hex}"
        if not _PROFILE_ID.fullmatch(profile_id):
            raise RuntimeError("Generated capture profile ID failed validation")
        return profile_id

    @staticmethod
    def _validate_profile_id(profile_id: str) -> None:
        if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
            raise CaptureProfileError("Invalid capture profile ID")

    @staticmethod
    def _with_active(profile: CaptureProfile, active: bool) -> CaptureProfile:
        return replace(profile, active=active)
