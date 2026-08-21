import { escHtml } from "./dom.js";
import { plural } from "./format.js?v=3";

function badgeClass(value) {
  return String(value || "unknown")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-");
}

export function stateBadge(state) {
  return `<span class="badge badge-${badgeClass(state)}">${escHtml(state || "Unknown")}</span>`;
}

export function episodeStateBadge(state) {
  const normalized = String(state || "").toLowerCase();
  if (normalized === "closed") return "";
  if (["new", "active", "quiescent"].includes(normalized)) return stateBadge("Active");
  return stateBadge(state);
}

export function eventBadge(type) {
  return `<span class="badge badge-${badgeClass(type)}">${escHtml(type || "Unknown")}</span>`;
}

export function episodeTriggerBadge(triggerType) {
  const triggers = {
    doorbell: ["Doorbell", "Triggered by a Doorbell Event"],
    motion: ["Motion", "Triggered by a motion Event"],
    manual: ["Manual", "Triggered by a manual Event"],
  };
  const trigger = triggers[triggerType];
  if (!trigger) return "";
  return `<span class="badge badge-${triggerType} episode-trigger" title="${trigger[1]}">${trigger[0]}</span>`;
}

export function sourceBadges(sources) {
  if (!Array.isArray(sources)) return escHtml(sources || "");
  const displayKey = source => {
    if (!source || typeof source !== "object") return String(source || "").toLowerCase();
    return `${source.kind || "source"} ${source.name || source.id || source.source || ""}`
      .toLowerCase();
  };
  const ordered = [...sources].sort((left, right) => {
    const leftKey = displayKey(left);
    const rightKey = displayKey(right);
    return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
  });
  return ordered.map(source => {
    if (source && typeof source === "object") {
      return `<span class="source-chip source-chip-${badgeClass(source.kind)}" title="${escHtml(source.source || source.id)}">
        <span class="source-chip-kind">${escHtml(source.kind || "source")}</span>
        <span>${escHtml(source.name || source.id || "Unknown")}</span>
      </span>`;
    }
    return `<span class="label">${escHtml(source)}</span>`;
  }).join(" ");
}

export function eventSourceBadges(event) {
  return sourceBadges(event?.origins?.length ? event.origins : event?.sources);
}

export function pageHeader({ eyebrow = "", title, description = "", actions = "", status = "" }) {
  return `<div class="page-header">
    <div class="page-heading">
      ${eyebrow ? `<div class="eyebrow">${escHtml(eyebrow)}</div>` : ""}
      <h2>${escHtml(title)}</h2>
      ${description ? `<p>${escHtml(description)}</p>` : ""}
    </div>
    ${actions || status ? `<div class="page-actions">${status}${actions}</div>` : ""}
  </div>`;
}

export function detailMetric(icon, label, value, href = "") {
  const content = `<svg><use href="icons.svg?v=2#${icon}"></use></svg>
    <span><small>${escHtml(label)}</small><strong>${escHtml(value)}</strong></span>`;
  return href
    ? `<a class="review-detail-metric" href="${escHtml(href)}">${content}</a>`
    : `<div class="review-detail-metric">${content}</div>`;
}

export function sectionHeading(icon, title, description = "", aside = "") {
  return `<header class="review-section-heading">
    <div class="review-section-title">
      <svg><use href="icons.svg?v=2#${icon}"></use></svg>
      <div><h3>${escHtml(title)}</h3>${description ? `<p>${escHtml(description)}</p>` : ""}</div>
    </div>
    ${aside}
  </header>`;
}

export function pageControls(base, page, itemCount, hasNext) {
  if (page === 1 && !hasNext) return "";
  const separator = base.includes("?") ? "&" : "?";
  const href = target => `${base}${separator}page=${target}`;
  return `<nav class="pagination" aria-label="Page navigation">
    ${page > 1
      ? `<a class="button button-ghost" href="${href(page - 1)}">\u2190 Newer</a>`
      : '<span class="button button-ghost pagination-disabled">\u2190 Newer</span>'}
    <span class="pagination-summary">Page ${page} \u00b7 ${plural(itemCount, "item")}</span>
    ${hasNext
      ? `<a class="button button-ghost" href="${href(page + 1)}">Older \u2192</a>`
      : '<span class="button button-ghost pagination-disabled">Older \u2192</span>'}
  </nav>`;
}
