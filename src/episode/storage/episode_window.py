from __future__ import annotations

from datetime import datetime, timezone

# An Episode with no closing time is bounded by its last Event, then its start,
# matching how operators see its extent before it closes.
_EPISODE_END_SQL = "COALESCE(episodes.end_time, episodes.last_event_time, episodes.start_time)"


def episode_overlap_clauses(
    overlaps_from: datetime | None,
    overlaps_before: datetime | None,
) -> tuple[list[str], list[str]]:
    """Return ``episodes`` predicates selecting Episodes that overlap a window.

    The window is ``[overlaps_from, overlaps_before)``: an Episode matches when
    it starts before the exclusive upper bound and ends at or after the
    inclusive lower bound.  Callers embed the clauses either directly in an
    ``episodes`` query or in an ``episode_id IN (SELECT id FROM episodes ...)``
    subquery.
    """
    clauses: list[str] = []
    params: list[str] = []
    for name, value in (("overlaps_from", overlaps_from), ("overlaps_before", overlaps_before)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{name} must be timezone-aware")
    if (
        overlaps_from is not None
        and overlaps_before is not None
        and overlaps_before <= overlaps_from
    ):
        raise ValueError("overlaps_before must be later than overlaps_from")
    if overlaps_before is not None:
        clauses.append("episodes.start_time < ?")
        params.append(_iso(overlaps_before))
    if overlaps_from is not None:
        clauses.append(f"{_EPISODE_END_SQL} >= ?")
        params.append(_iso(overlaps_from))
    return clauses, params


def episode_overlap_subquery(
    column: str,
    overlaps_from: datetime | None,
    overlaps_before: datetime | None,
) -> tuple[list[str], list[str]]:
    """Return a clause restricting ``column`` to Episodes overlapping a window."""
    clauses, params = episode_overlap_clauses(overlaps_from, overlaps_before)
    if not clauses:
        return [], []
    return [f"{column} IN (SELECT id FROM episodes WHERE {' AND '.join(clauses)})"], params


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
