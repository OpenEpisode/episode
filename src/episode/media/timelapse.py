from __future__ import annotations

from episode.domain.models import Evidence


def is_timelapse_eligible(evidence: Evidence) -> bool:
    """Return whether snapshot evidence may contribute frames to a timelapse."""
    eligible = evidence.metadata.get("timelapse_eligible", True)
    return evidence.evidence_type == "snapshot" and eligible is not False
