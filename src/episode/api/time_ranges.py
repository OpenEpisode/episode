from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException


def normalize_datetime_range(
    start: datetime | None,
    end: datetime | None,
    *,
    start_name: str,
    end_name: str,
) -> tuple[datetime | None, datetime | None]:
    """Validate timezone-aware bounds and normalize them to UTC for a query."""
    for name, value in ((start_name, start), (end_name, end)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(422, f"{name} must include a timezone offset")
    if start is not None and end is not None and end <= start:
        raise HTTPException(422, f"{end_name} must be later than {start_name}")
    return (
        start.astimezone(timezone.utc) if start is not None else None,
        end.astimezone(timezone.utc) if end is not None else None,
    )
