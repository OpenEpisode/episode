const TIME_RANGE_OPTIONS = Object.freeze([
  ["all", "All time"],
  ["today", "Today"],
  ["yesterday", "Yesterday"],
  ["last_7_days", "Last 7 days"],
  ["last_30_days", "Last 30 days"],
  ["custom", "Custom range"],
]);

function localDayStart(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function localDateStart(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  if (
    date.getFullYear() !== Number(match[1])
    || date.getMonth() !== Number(match[2]) - 1
    || date.getDate() !== Number(match[3])
  ) return null;
  return date;
}

function addLocalDays(date, days) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

function utcBounds(start, end) {
  return { from: start.toISOString(), before: end.toISOString(), valid: true };
}

/** Return UTC bounds for a browser-local calendar selection. */
export function calendarTimeBounds(selection = {}, now = new Date()) {
  const range = selection.time_range || "all";
  const today = localDayStart(now);
  if (range === "all") return { from: "", before: "", valid: true };
  if (range === "today") return utcBounds(today, addLocalDays(today, 1));
  if (range === "yesterday") return utcBounds(addLocalDays(today, -1), today);
  if (range === "last_7_days") return utcBounds(addLocalDays(today, -6), addLocalDays(today, 1));
  if (range === "last_30_days") return utcBounds(addLocalDays(today, -29), addLocalDays(today, 1));
  if (range === "custom") {
    const start = localDateStart(selection.custom_from);
    const end = localDateStart(selection.custom_to);
    if (!start || !end || end < start) return { from: "", before: "", valid: false };
    return utcBounds(start, addLocalDays(end, 1));
  }
  return { from: "", before: "", valid: false };
}

export { TIME_RANGE_OPTIONS };
