function localDay(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  date.setHours(0, 0, 0, 0);
  return date;
}

function dayLabel(day, now) {
  const today = localDay(now);
  if (!day || !today) return "Unknown date";
  const difference = Math.round((today - day) / (24 * 60 * 60 * 1000));
  if (difference === 0) return "Today";
  if (difference === 1) return "Yesterday";
  return day.toLocaleDateString(undefined, {
    weekday: "long",
    day: "2-digit",
    month: "long",
    year: day.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}

export function groupActivityByDay(events, now = new Date()) {
  const groups = [];
  const byKey = new Map();
  for (const event of events) {
    const day = localDay(event.timestamp);
    const key = day ? day.getTime() : "unknown";
    let group = byKey.get(key);
    if (!group) {
      group = { key, label: dayLabel(day, now), events: [] };
      groups.push(group);
      byKey.set(key, group);
    }
    group.events.push(event);
  }
  return groups;
}

export function groupEvidenceByEpisode(evidence) {
  const unassigned = [];
  const assigned = [];
  const byEpisode = new Map();
  for (const item of evidence) {
    if (!item.episode_id) {
      unassigned.push(item);
      continue;
    }
    let group = byEpisode.get(item.episode_id);
    if (!group) {
      group = { episodeId: item.episode_id, evidence: [], attention: false };
      assigned.push(group);
      byEpisode.set(item.episode_id, group);
    }
    group.evidence.push(item);
  }
  return [
    ...(unassigned.length
      ? [{ episodeId: null, evidence: unassigned, attention: true }]
      : []),
    ...assigned,
  ].map(group => {
    const timestamps = group.evidence
      .map(item => new Date(item.timestamp).getTime())
      .filter(Number.isFinite);
    return {
      ...group,
      deviceCount: new Set(group.evidence.map(item => item.device_id).filter(Boolean)).size,
      firstCaptureAt: timestamps.length ? new Date(Math.min(...timestamps)).toISOString() : null,
      lastCaptureAt: timestamps.length ? new Date(Math.max(...timestamps)).toISOString() : null,
    };
  });
}

export function groupEvidenceBundlesByDay(bundles, now = new Date()) {
  const ordered = [...bundles].sort(
    (left, right) => new Date(right.firstCaptureAt) - new Date(left.firstCaptureAt),
  );
  const periods = [];
  const byKey = new Map();
  for (const bundle of ordered) {
    const day = localDay(bundle.firstCaptureAt);
    const key = day ? day.getTime() : "unknown";
    let period = byKey.get(key);
    if (!period) {
      period = { key, label: dayLabel(day, now), bundles: [] };
      periods.push(period);
      byKey.set(key, period);
    }
    period.bundles.push(bundle);
  }
  return periods;
}
