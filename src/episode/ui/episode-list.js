const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(value) {
  const date = new Date(value);
  date.setHours(0, 0, 0, 0);
  return date;
}

function startOfWeek(value) {
  const date = startOfDay(value);
  const daysSinceMonday = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() - daysSinceMonday);
  return date;
}

function periodFor(episode, now) {
  if (!["closed", "archived"].includes(episode.state)) {
    return { key: "active", label: "Happening now", active: true };
  }

  const occurred = new Date(episode.start_time);
  const today = startOfDay(now);
  const week = startOfWeek(now);
  const occurredDay = startOfDay(occurred);
  if (occurredDay >= today) return { key: "today", label: "Today", active: false };
  if (occurredDay >= week) {
    return { key: "this-week", label: "Earlier this week", active: false };
  }
  if (occurredDay >= new Date(week.getTime() - 7 * DAY_MS)) {
    return { key: "last-week", label: "Last week", active: false };
  }
  if (occurred.getFullYear() === now.getFullYear()
    && occurred.getMonth() === now.getMonth()) {
    return { key: "this-month", label: "Earlier this month", active: false };
  }
  if (occurred.getFullYear() === now.getFullYear()) {
    return {
      key: `month-${occurred.getMonth()}`,
      label: occurred.toLocaleDateString(undefined, { month: "long" }),
      active: false,
    };
  }
  return { key: `year-${occurred.getFullYear()}`, label: String(occurred.getFullYear()), active: false };
}

export function groupEpisodesByTime(episodes, now = new Date()) {
  const groups = [];
  const byKey = new Map();
  for (const episode of episodes) {
    const period = periodFor(episode, now);
    let group = byKey.get(period.key);
    if (!group) {
      group = { ...period, episodes: [] };
      groups.push(group);
      byKey.set(period.key, group);
    }
    group.episodes.push(episode);
  }
  return groups.sort((left, right) => Number(right.active) - Number(left.active));
}

export function episodeRailTime(value, now = new Date()) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "–";
  const sameDay = startOfDay(date).getTime() === startOfDay(now).getTime();
  return date.toLocaleString(undefined, sameDay
    ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }
    : {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      });
}
