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

export function fmtBytes(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const bytes = Math.max(0, Number(value));
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  if (bytes === 0) return "0 B";
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const amount = bytes / (1024 ** index);
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

export function trunc(value, length = 40) {
  return value && value.length > length ? value.slice(0, length) + "\u2026" : value;
}

export function titleCase(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, letter => letter.toUpperCase());
}
