function pad(value) {
  return String(value).padStart(2, "0");
}

export function fmt(value) {
  if (!value) return "-";
  const date = new Date(value);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function fmtShort(value) {
  return fmt(value);
}

export function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function fmtDuration(start, end) {
  if (!start || !end) return "";
  const elapsed = new Date(end) - new Date(start);
  if (elapsed < 0) return "-";
  const totalSeconds = Math.floor(elapsed / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(seconds)}`
    : `${minutes}:${pad(seconds)}`;
}

export function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

export function trunc(value, length = 40) {
  return value && value.length > length ? value.slice(0, length) + "\u2026" : value;
}

export function titleCase(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, letter => letter.toUpperCase());
}
