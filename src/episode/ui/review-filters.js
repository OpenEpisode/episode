import { escHtml } from "./dom.js";

export function filterValues(items, field, defaults, selected = "") {
  return [...new Set([
    ...defaults,
    ...items.map(item => item[field]).filter(Boolean),
    ...(selected ? [selected] : []),
  ])].sort();
}

export function option(value, label, selected) {
  return `<option value="${escHtml(value)}" ${value === selected ? "selected" : ""}>${escHtml(label)}</option>`;
}

export function filteredHash(view, filters) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) query.set(key, value);
  }
  return `#${view}${query.size ? `?${query}` : ""}`;
}
