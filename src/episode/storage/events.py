from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import aiosqlite

from episode.domain.event_filter import FILTERED_REASON
from episode.domain.models import Event, EventState, ParticipationDecision, make_event_dedup_key

_RECOVERY_INGRESS_SQL = """COALESCE(
    (SELECT MIN(r.received_at) FROM ingestion_receipts r WHERE r.event_id = e.id),
    CASE WHEN json_valid(e.participation)
         THEN json_extract(e.participation, '$.evaluated_at') END,
    e.timestamp
)"""


def _utc_iso(value: datetime) -> str:
    normalized = (
        value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    )
    return normalized.isoformat(timespec="microseconds")


def _participation_json(decision: ParticipationDecision | None) -> str | None:
    if decision is None:
        return None
    data = asdict(decision)
    data["evaluated_at"] = _utc_iso(decision.evaluated_at)
    return json.dumps(data, separators=(",", ":"))


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
                raw_payload_path, metadata, participation,
                eligible_recording_device_ids, episode_id
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                _participation_json(event.participation),
                (
                    json.dumps(event.eligible_recording_device_ids)
                    if event.eligible_recording_device_ids is not None
                    else None
                ),
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
            "SELECT * FROM events WHERE id = ?",
            (event_id,),
        )
        return self._row_to_event(rows[0]) if rows else None

    async def find_by_dedup_key(self, dedup_key: str) -> Event | None:
        rows = await self._connection.execute_fetchall(
            "SELECT * FROM events WHERE dedup_key = ? LIMIT 1",
            (dedup_key,),
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
        observed_from: datetime | None = None,
        observed_before: datetime | None = None,
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
        if observed_from is not None:
            if observed_from.tzinfo is None or observed_from.utcoffset() is None:
                raise ValueError("observed_from must be timezone-aware")
            clauses.append("timestamp >= ?")
            params.append(_utc_iso(observed_from))
        if observed_before is not None:
            if observed_before.tzinfo is None or observed_before.utcoffset() is None:
                raise ValueError("observed_before must be timezone-aware")
            clauses.append("timestamp < ?")
            params.append(_utc_iso(observed_before))
        if (
            observed_from is not None
            and observed_before is not None
            and observed_before <= observed_from
        ):
            raise ValueError("observed_before must be later than observed_from")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._connection.execute_fetchall(
            f"""SELECT * FROM events{where}
                  ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        return [self._row_to_event(row) for row in rows]

    async def list_unassigned_active(
        self,
        *,
        limit: int = 200,
        after: tuple[datetime, str] | None = None,
        order_by_ingress: bool = True,
    ) -> list[Event]:
        """Return a page of active Events with an incomplete stored decision."""
        clauses = [
            "event_state = ?",
            "episode_id IS NULL",
            "area_id != ''",
            "eligible_recording_device_ids IS NOT NULL",
            "CASE WHEN json_valid(eligible_recording_device_ids) THEN "
            "json_type(eligible_recording_device_ids) = 'array' ELSE 0 END = 1",
            """CASE WHEN json_valid(participation) THEN
                   CASE
                     WHEN json_extract(participation, '$.allowed') = 1 THEN 1
                     WHEN json_extract(participation, '$.allowed') = 0
                       AND json_extract(participation, '$.reason') = ?
                       AND COALESCE(json_type(participation, '$.attachment'), 'null') = 'null'
                     THEN 1
                     ELSE 0
                   END
                 ELSE 0
               END = 1""",
        ]
        params: list[object] = [EventState.ACTIVE.value, FILTERED_REASON]
        ingress_sql = _RECOVERY_INGRESS_SQL if order_by_ingress else "e.timestamp"
        if after is not None:
            clauses.append(
                f"(julianday({ingress_sql}) > julianday(?) "
                f"OR (julianday({ingress_sql}) = julianday(?) AND e.id > ?))"
            )
            after_timestamp = _utc_iso(after[0])
            params.extend((after_timestamp, after_timestamp, after[1]))
        rows = await self._connection.execute_fetchall(
            f"""SELECT e.* FROM events e
                WHERE {" AND ".join(clauses)}
                ORDER BY julianday({ingress_sql}) ASC, e.id ASC LIMIT ?""",
            [*params, max(1, min(int(limit), 500))],
        )
        return [self._row_to_event(row) for row in rows]

    async def find_recent_by_device(self, device_id: str, since: datetime) -> list[Event]:
        rows = await self._connection.execute_fetchall(
            """SELECT * FROM events
               WHERE device_id = ? AND timestamp >= ? ORDER BY timestamp ASC""",
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

    async def update_participation(self, event_id: str, decision: ParticipationDecision) -> None:
        """Re-persist the participation blob for one Event.

        Used when the engine learns something the decision could not know at
        canonicalization time, such as whether an Episode was open to attach to.
        Only the derived decision changes; the canonical observation does not.
        """
        await self._connection.execute(
            "UPDATE events SET participation = ? WHERE id = ?",
            (_participation_json(decision), event_id),
        )
        await self._connection.commit()

    async def update_episode(self, event_id: str, episode_id: str) -> None:
        await self._connection.execute(
            "UPDATE events SET episode_id = ? WHERE id = ?", (episode_id, event_id)
        )
        await self._connection.commit()

    @staticmethod
    def _row_to_event(row: aiosqlite.Row) -> Event:
        participation = None
        raw_participation = row["participation"] if "participation" in row.keys() else None
        if raw_participation:
            try:
                participation = json.loads(raw_participation)
            except (TypeError, json.JSONDecodeError):
                participation = None
        eligible_ids = None
        raw_eligible_ids = (
            row["eligible_recording_device_ids"]
            if "eligible_recording_device_ids" in row.keys()
            else None
        )
        if raw_eligible_ids:
            try:
                parsed_ids = json.loads(raw_eligible_ids)
                if isinstance(parsed_ids, list):
                    eligible_ids = [str(item) for item in parsed_ids]
            except (TypeError, json.JSONDecodeError):
                eligible_ids = None
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
            participation=participation,
            eligible_recording_device_ids=eligible_ids,
        )
