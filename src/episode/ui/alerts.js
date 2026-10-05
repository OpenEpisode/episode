import { api } from "./api.js?v=3";
import { pageHeader } from "./components.js?v=4";
import { escHtml } from "./dom.js";
import { fmtShort, plural, titleCase, trunc } from "./format.js?v=4";
import { showContent, showError, showLoading } from "./view.js?v=1";

export const ALERT_PAGE_SIZE = 50;

function systemNavigation(active = "alerts") {
  const sections = [
    ["overview", "Overview"],
    ["recordings", "Recordings"],
    ["integrations", "Integrations"],
    ["alerts", "Alerts"],
    ["notifications", "Notifications"],
    ["capture-profiles", "Capture profiles"],
    ["storage", "Storage"],
  ];
  return `<nav class="system-navigation" aria-label="System sections">
    ${sections.map(([id, label]) => `<a href="#system${id === "overview" ? "" : `/${id}`}" class="${active === id ? "active" : ""}">${label}</a>`).join("")}
  </nav>`;
}

function alertSeverity(alert) {
  return ["warning", "error", "info"].includes(alert?.severity) ? alert.severity : "warning";
}

function alertLabel(alert) {
  return alert?.code ? titleCase(alert.code) : "Recording alert";
}

function alertLinks(alert) {
  const links = [];
  if (alert?.evidence_id) {
    links.push(`<a href="#evidence/${encodeURIComponent(alert.evidence_id)}">Review Evidence</a>`);
  }
  if (alert?.episode_id) {
    links.push(`<a href="#episode/${encodeURIComponent(alert.episode_id)}">Open Episode</a>`);
  }
  return links.length ? `<div class="system-alert-actions">${links.join("")}</div>` : "";
}

function alertDiagnostics(alert) {
  const validation = alert?.playlist_validation;
  const hasValidation = validation && typeof validation === "object";
  const hasExitCode = alert?.ffmpeg_exit_code !== null && alert?.ffmpeg_exit_code !== undefined;
  if (!hasValidation && !hasExitCode) return "";
  const facts = [];
  if (hasExitCode) facts.push(`<span>FFmpeg exit code <strong>${escHtml(alert.ffmpeg_exit_code)}</strong></span>`);
  if (hasValidation) {
    facts.push(`<span>Playlist <strong>${validation.valid === true ? "valid" : "invalid"}</strong></span>`);
    if (validation.referenced_fragment_count !== undefined) {
      facts.push(`<span>${escHtml(validation.referenced_fragment_count)} of ${escHtml(validation.fragment_count ?? "?")} fragments referenced</span>`);
    }
    if (validation.empty_fragment_count) {
      facts.push(`<span>${escHtml(validation.empty_fragment_count)} empty fragments</span>`);
    }
    if (validation.preserved_temporary_component_count) {
      facts.push(`<span>${escHtml(validation.preserved_temporary_component_count)} temporary components preserved</span>`);
    }
    if (validation.error) facts.push(`<span>${escHtml(validation.error)}</span>`);
  }
  return `<details class="system-alert-diagnostics"><summary>Technical details</summary><div>${facts.join("")}</div></details>`;
}

export function renderAlertRow(alert) {
  const severity = alertSeverity(alert);
  const title = alert?.title || alertLabel(alert);
  const message = alert?.message || "Recording finalization needs review.";
  const device = alert?.device_id ? `Device ${alert.device_id}` : "Device unavailable";
  const created = alert?.created_at || "";
  return `<article class="system-alert-row system-alert-row-${severity}">
    <span class="status-indicator ${severity === "error" ? "offline" : "warning"}" aria-hidden="true"></span>
    <div class="system-alert-main">
      <div class="system-alert-heading"><strong>${escHtml(title)}</strong><span class="badge badge-${severity}">${escHtml(alertLabel(alert))}</span></div>
      <p>${escHtml(trunc(String(message), 360))}</p>
      <div class="system-alert-meta"><span>${escHtml(device)}</span>${created ? `<time datetime="${escHtml(created)}">${escHtml(fmtShort(created))}</time>` : ""}</div>
      ${alertDiagnostics(alert)}
    </div>
    ${alertLinks(alert)}
  </article>`;
}

function pagination(page, hasNext, count) {
  const href = target => `#system/alerts?page=${target}`;
  if (page === 1 && !hasNext) return "";
  return `<nav class="pagination system-alert-pagination" aria-label="Alert pages">
    ${page > 1
      ? `<a class="button button-ghost" href="${href(page - 1)}">← Newer</a>`
      : '<span class="button button-ghost pagination-disabled">← Newer</span>'}
    <span class="pagination-summary">Page ${page} · ${plural(count, "alert")}</span>
    ${hasNext
      ? `<a class="button button-ghost" href="${href(page + 1)}">Older →</a>`
      : '<span class="button button-ghost pagination-disabled">Older →</span>'}
  </nav>`;
}

export function renderAlertsPage(alerts = [], page = 1, hasNext = alerts.length > ALERT_PAGE_SIZE) {
  const ordered = [...alerts].sort((left, right) => {
    const leftTime = Date.parse(left?.created_at || "") || 0;
    const rightTime = Date.parse(right?.created_at || "") || 0;
    return rightTime - leftTime;
  });
  const visible = ordered.slice(0, ALERT_PAGE_SIZE);
  const content = ordered.length
    ? `<div class="system-alert-list">${visible.map(renderAlertRow).join("")}</div>`
    : `<section class="section empty-state system-alert-empty">
        <div class="empty-icon">✓</div><h3>No alerts</h3>
        <p>Recording finalization and recovery are operating normally. Alerts remain here while their related Evidence is retained.</p>
      </section>`;
  return `${pageHeader({
    eyebrow: "Operations · System",
    title: "Alerts",
    description: "Review recording finalization and recovery issues that need attention.",
  })}
  <div class="system-layout system-alerts-layout">
    ${systemNavigation("alerts")}
    <div class="system-content">
      <section class="section system-alerts-panel">
        <div class="system-section-heading"><div><h3>Recent alerts</h3><p>Newest alerts appear first. They remain available while the related Evidence is retained.</p></div><span class="badge badge-warning">${visible.length}${hasNext ? "+" : ""}</span></div>
        ${content}
        ${pagination(page, hasNext, visible.length)}
      </section>
    </div>
  </div>`;
}

export async function alerts(page = 1) {
  const currentPage = Number.isInteger(page) && page > 0 ? page : 1;
  showLoading();
  try {
    const offset = (currentPage - 1) * ALERT_PAGE_SIZE;
    const response = await api(`/alerts?limit=${ALERT_PAGE_SIZE}&offset=${offset}`);
    const items = Array.isArray(response) ? response : [];
    // The public endpoint caps page size at 50. Probe one item beyond a full
    // page so "Older" is only shown when another page actually exists.
    const nextPage = items.length === ALERT_PAGE_SIZE
      ? await api(`/alerts?limit=1&offset=${offset + ALERT_PAGE_SIZE}`)
      : [];
    showContent(renderAlertsPage(items, currentPage, Array.isArray(nextPage) && nextPage.length > 0));
  } catch (error) {
    showError(error.message);
  }
}
