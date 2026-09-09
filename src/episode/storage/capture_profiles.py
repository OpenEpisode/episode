from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from episode.domain.models import CaptureProfile, CaptureProfileChange

ALL_DEVICES_PROFILE_ID = "all-devices"
ALL_DEVICES_PROFILE_NAME = "All Devices"


def _utc_iso(value: datetime) -> str:
    normalized = (
        value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    )
    return normalized.isoformat(timespec="microseconds")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class CaptureProfileStore:
    """SQLite persistence for capture profiles and active-profile history."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def ensure_defaults(self) -> CaptureProfile:
        now = datetime.now(tz=timezone.utc)
        profile = CaptureProfile(
            id=ALL_DEVICES_PROFILE_ID,
            name=ALL_DEVICES_PROFILE_NAME,
            include_all_devices=True,
            device_ids=[],
            builtin=True,
            created_at=now,
            updated_at=now,
        )
        await self._connection.execute(
            """INSERT OR IGNORE INTO capture_profiles
               (id, name, include_all_devices, device_ids, builtin, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                profile.id,
                profile.name,
                1,
                "[]",
                1,
                _utc_iso(now),
                _utc_iso(now),
            ),
        )
        await self._connection.execute(
            """INSERT OR IGNORE INTO capture_profile_state
               (singleton, active_profile_id) VALUES (1, ?)""",
            (ALL_DEVICES_PROFILE_ID,),
        )
        await self._connection.commit()
        return await self.get(ALL_DEVICES_PROFILE_ID) or profile

    async def list(self) -> list[CaptureProfile]:
        rows = await self._connection.execute_fetchall(
            """SELECT * FROM capture_profiles
               ORDER BY builtin DESC, name COLLATE NOCASE ASC, id ASC"""
        )
        active = await self._active_id()
        return [self._row_to_profile(row, active_id=active) for row in rows]

    async def get(self, profile_id: str) -> CaptureProfile | None:
        rows = await self._connection.execute_fetchall(
            "SELECT * FROM capture_profiles WHERE id = ?", (profile_id,)
        )
        if not rows:
            return None
        return self._row_to_profile(rows[0], active_id=await self._active_id())

    async def active(self) -> CaptureProfile | None:
        rows = await self._connection.execute_fetchall(
            """SELECT p.*
               FROM capture_profile_state s
               JOIN capture_profiles p ON p.id = s.active_profile_id
               WHERE s.singleton = 1"""
        )
        return self._row_to_profile(rows[0], active_id=rows[0]["id"]) if rows else None

    async def create(self, profile: CaptureProfile) -> CaptureProfile:
        await self._connection.execute(
            """INSERT INTO capture_profiles
               (id, name, include_all_devices, device_ids, builtin, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                profile.id,
                profile.name,
                int(profile.include_all_devices),
                json.dumps(sorted(set(profile.device_ids)), separators=(",", ":")),
                int(profile.builtin),
                _utc_iso(profile.created_at),
                _utc_iso(profile.updated_at),
            ),
        )
        await self._connection.commit()
        return profile

    async def update(self, profile: CaptureProfile) -> CaptureProfile:
        await self._connection.execute(
            """UPDATE capture_profiles
               SET name = ?, include_all_devices = ?, device_ids = ?, updated_at = ?
               WHERE id = ?""",
            (
                profile.name,
                int(profile.include_all_devices),
                json.dumps(sorted(set(profile.device_ids)), separators=(",", ":")),
                _utc_iso(profile.updated_at),
                profile.id,
            ),
        )
        await self._connection.commit()
        return profile

    async def delete(self, profile_id: str) -> None:
        await self._connection.execute("DELETE FROM capture_profiles WHERE id = ?", (profile_id,))
        await self._connection.commit()

    async def activate(self, profile_id: str, source: str) -> CaptureProfileChange:
        """Atomically switch the active profile and append one audit row."""
        changed_at = datetime.now(tz=timezone.utc)
        try:
            await self._connection.execute("BEGIN IMMEDIATE")
            rows = await self._connection.execute_fetchall(
                "SELECT id, name FROM capture_profiles WHERE id = ?", (profile_id,)
            )
            if not rows:
                raise KeyError(profile_id)
            previous_rows = await self._connection.execute_fetchall(
                """SELECT p.id, p.name
                   FROM capture_profile_state s
                   JOIN capture_profiles p ON p.id = s.active_profile_id
                   WHERE s.singleton = 1"""
            )
            previous_id = previous_rows[0]["id"] if previous_rows else None
            previous_name = previous_rows[0]["name"] if previous_rows else None
            if previous_id == profile_id:
                # Activation is idempotent.  Repeating the same operator
                # request must not create audit noise or imply a transition.
                await self._connection.commit()
                return CaptureProfileChange(
                    previous_profile_id=previous_id,
                    previous_profile_name=previous_name,
                    new_profile_id=profile_id,
                    new_profile_name=rows[0]["name"],
                    changed_at=changed_at,
                    source=source,
                )
            await self._connection.execute(
                """INSERT INTO capture_profile_state (singleton, active_profile_id)
                   VALUES (1, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                       active_profile_id = excluded.active_profile_id""",
                (profile_id,),
            )
            await self._connection.execute(
                """INSERT INTO capture_profile_changes
                   (previous_profile_id, previous_profile_name,
                    new_profile_id, new_profile_name, changed_at, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    previous_id,
                    previous_name,
                    profile_id,
                    rows[0]["name"],
                    _utc_iso(changed_at),
                    source,
                ),
            )
            await self._connection.commit()
        except BaseException:
            try:
                await self._connection.rollback()
            except Exception:
                # The repository owns the connection lifecycle; preserving the
                # original failure is more useful than masking it here.
                pass
            raise
        return CaptureProfileChange(
            previous_profile_id=previous_id,
            previous_profile_name=previous_name,
            new_profile_id=profile_id,
            new_profile_name=rows[0]["name"],
            changed_at=changed_at,
            source=source,
        )

    async def list_changes(self, limit: int = 20, offset: int = 0) -> list[CaptureProfileChange]:
        rows = await self._connection.execute_fetchall(
            """SELECT previous_profile_id, previous_profile_name,
                      new_profile_id, new_profile_name, changed_at, source
               FROM capture_profile_changes
               ORDER BY changed_at DESC, id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        return [
            CaptureProfileChange(
                previous_profile_id=row["previous_profile_id"],
                previous_profile_name=row["previous_profile_name"],
                new_profile_id=row["new_profile_id"],
                new_profile_name=row["new_profile_name"],
                changed_at=_parse_datetime(row["changed_at"]),
                source=row["source"],
            )
            for row in rows
        ]

    async def _active_id(self) -> str | None:
        rows = await self._connection.execute_fetchall(
            "SELECT active_profile_id FROM capture_profile_state WHERE singleton = 1"
        )
        return str(rows[0]["active_profile_id"]) if rows else None

    @staticmethod
    def _row_to_profile(row: aiosqlite.Row, *, active_id: str | None) -> CaptureProfile:
        try:
            device_ids = json.loads(row["device_ids"] or "[]")
        except (TypeError, json.JSONDecodeError):
            device_ids = []
        if not isinstance(device_ids, list):
            device_ids = []
        return CaptureProfile(
            id=row["id"],
            name=row["name"],
            include_all_devices=bool(row["include_all_devices"]),
            device_ids=[str(item) for item in device_ids],
            builtin=bool(row["builtin"]),
            active=row["id"] == active_id,
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )
