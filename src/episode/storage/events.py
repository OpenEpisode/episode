from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from episode.domain.models import Event, EventState, make_event_dedup_key


def _utc_iso(value: datetime) -> str:
    normalized = (
        value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    )
    return normalized.isoformat(timespec="microseconds")


class EventStore:
    """Persist and query canonical Events, including delivery deduplication."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def create(self, event: Event) -> Event:
        if not event.dedup_key:
            event.dedup_key = make_event_dedup_key(
                event.device_id, event.timestamp, event.event_type, event.event_state
            )
        await self._connection.execute(
            """INSERT INTO events (
                id, device_id, area_id, timestamp,
                event_type, event_state, source, dedup_key,
                raw_payload_path, metadata, episode_id
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.id,
                event.device_id,
                event.area_id,
                _utc_iso(event.timestamp),
                event.event_type,
                event.event_state.value,
                event.source,
                event.dedup_key,
                event.raw_payload_path,
                json.dumps(event.metadata),
                event.episode_id,
            ),
        )
        await self._connection.commit()
        return event

    async def canonicalize(self, event: Event) -> tuple[Event, bool]:
        if not event.dedup_key:
            event.dedup_key = make_event_dedup_key(
                event.device_id, event.timestamp, event.event_type, event.event_state
            )
        existing = await self.find_by_dedup_key(event.dedup_key)
        if existing:
            return existing, False
        try:
            return await self.create(event), True
        except aiosqlite.IntegrityError:
            existing = await self.find_by_dedup_key(event.dedup_key)
            if existing:
                return existing, False
            raise

    async def get(self, event_id: str) -> Event | None:
        rows = await self._connection.execute_fetchall(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        )
        return self._row_to_event(rows[0]) if rows else None

    async def find_by_dedup_key(self, dedup_key: str) -> Event | None:
        rows = await self._connection.execute_fetchall(
            "SELECT * FROM events WHERE dedup_key = ? LIMIT 1", (dedup_key,)
        )
        return self._row_to_event(rows[0]) if rows else None

    async def list(
        self,
        episode_id: str | None = None,
        area_id: str | None = None,
        device_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        *,
        event_type: str | None = None,
        event_state: str | None = None,
        has_episode: bool | None = None,
    ) -> list[Event]:
        clauses: list[str] = []
        params: list[str | int] = []
        if episode_id:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        if area_id:
            clauses.append("area_id = ?")
            params.append(area_id)
        if device_id:
            clauses.append("device_id = ?")
            params.append(device_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if event_state:
            clauses.append("event_state = ?")
            params.append(event_state)
        if has_episode is not None:
            clauses.append("episode_id IS NOT NULL" if has_episode else "episode_id IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._connection.execute_fetchall(
            f"SELECT * FROM events{where} ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [self._row_to_event(row) for row in rows]

    async def find_recent_by_device(self, device_id: str, since: datetime) -> list[Event]:
        rows = await self._connection.execute_fetchall(
            "SELECT * FROM events WHERE device_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
            (device_id, _utc_iso(since)),
        )
        return [self._row_to_event(row) for row in rows]

    async def find_preceding_transition(self, event: Event) -> Event | None:
        """Return the preceding state transition in the same semantic stream."""
        rows = await self._connection.execute_fetchall(
            """SELECT * FROM events
               WHERE id != ?
                 AND device_id = ?
                 AND area_id = ?
                 AND event_type = ?
                 AND timestamp <= ?
               ORDER BY timestamp DESC, id DESC
               LIMIT 1""",
            (
                event.id,
                event.device_id,
                event.area_id,
                event.event_type,
                _utc_iso(event.timestamp),
            ),
        )
        return self._row_to_event(rows[0]) if rows else None

    async def update_episode(self, event_id: str, episode_id: str) -> None:
        await self._connection.execute(
            "UPDATE events SET episode_id = ? WHERE id = ?", (episode_id, event_id)
        )
        await self._connection.commit()

    @staticmethod
    def _row_to_event(row: aiosqlite.Row) -> Event:
        return Event(
            id=row["id"],
            device_id=row["device_id"],
            area_id=row["area_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            event_type=row["event_type"],
            event_state=EventState(row["event_state"]),
            source=row["source"],
            dedup_key=row["dedup_key"] or "",
            raw_payload_path=row["raw_payload_path"],
            metadata=json.loads(row["metadata"]),
            episode_id=row["episode_id"],
        )
