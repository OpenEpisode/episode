import { api } from "./api.js";
import { episodeStateBadge } from "./components.js";
import { $ } from "./dom.js";
import { plural, trunc } from "./format.js";

function statusIndicator(state) {
  if (state === "healthy") return "online";
  if (state === "degraded") return "warning";
  if (state === "disabled" || state === "unknown") return "idle";
  return "offline";
}

function hasAlerts(alerts) {
  return Array.isArray(alerts) ? alerts.length > 0 : Number(alerts) > 0;
}

function sidebarAlertContent(mobile = false) {
  return `<span class="dot warning" aria-hidden="true"></span>
    <span class="label">${mobile ? "Alerts" : "Recording alerts need attention"}</span>
    <span class="sidebar-status-action" aria-hidden="true">Review ›</span>`;
}

export function sidebarAlertsView(alerts = []) {
  if (!hasAlerts(alerts)) return "";
  return `<a class="sidebar-status sidebar-alert-link" href="#system/alerts" title="Review recording alerts" aria-label="Recording alerts need attention. Review alerts.">
    ${sidebarAlertContent()}
  </a>`;
}

export function sidebarStatusView(status) {
  const indicator = statusIndicator(status.state);
  const label = ({
    healthy: "All systems operational",
    degraded: "Attention needed",
    unavailable: "System unavailable",
  }[status.state] || "Status unknown");
  const href = status.state === "degraded" ? "#system/integrations" : "#system";
  const title = status.state === "degraded" ? "Review integration health" : "Open System status";
  const recordings = status.active_recordings ? plural(status.active_recordings, "rec") : "";
  return `<a class="sidebar-status sidebar-status-link" href="${href}" title="${title}">
    <span class="dot ${indicator}" aria-hidden="true"></span>
    <span class="label">${label}</span>
    ${recordings ? `<span class="label sidebar-recording-count">${recordings}</span>` : ""}
    <span class="sidebar-status-action" aria-hidden="true">${status.state === "degraded" ? "Review ›" : "›"}</span>
  </a>`;
}

export async function updateRecentEpisodes(list = null) {
  const element = $("#recent-episodes-sidebar");
  try {
    const recent = (list || await api("/episodes?limit=8")).slice(0, 8);
    element.innerHTML = `<div class="label">Recent episodes</div>
      ${recent.length
        ? recent.map(episode => {
            const badge = episodeStateBadge(episode.state);
            return `<a href="#episode/${episode.id}">${badge ? `${badge} ` : ""}${trunc(episode.primary_area_id || "?", 22)}</a>`;
          }).join("")
        : '<span class="sidebar-empty">No episodes yet</span>'}`;
  } catch {
    element.innerHTML = '<div class="label">Recent episodes</div><span class="sidebar-empty">Unavailable</span>';
  }
}

export async function updateSidebarStatus() {
  const element = $("#sidebar-status");
  try {
    const status = await api("/status");
    $("#app-version").textContent = status.version ? `v${status.version}` : "";
    element.innerHTML = sidebarStatusView(status);
  } catch {
    element.innerHTML = `<a class="sidebar-status sidebar-status-link" href="#system" title="Open System status">
      <span class="dot offline" aria-hidden="true"></span><span class="label">Offline</span>
      <span class="sidebar-status-action" aria-hidden="true">Review ›</span></a>`;
  }
}

export async function updateSidebarAlerts() {
  const desktop = $("#sidebar-alerts");
  const mobile = $("#mobile-alert-status");
  if (!desktop && !mobile) return false;
  try {
    const alerts = await api("/alerts?limit=1&offset=0");
    const present = hasAlerts(alerts);
    if (desktop) desktop.innerHTML = sidebarAlertsView(alerts);
    if (mobile) {
      mobile.className = `sidebar-status sidebar-alert-status mobile-alert-status${present ? "" : " hidden"}`;
      mobile.title = present ? "Review recording alerts" : "";
      mobile.setAttribute("aria-label", present
        ? "Recording alerts need attention. Review alerts."
        : "Review recording alerts");
      mobile.innerHTML = present ? sidebarAlertContent(true) : "";
    }
    return present;
  } catch {
    // A failed alerts request must not hide or degrade the existing system
    // health indicator. Leave the alert affordance empty until the next poll.
    if (desktop) desktop.innerHTML = "";
    if (mobile) {
      mobile.className = "sidebar-status sidebar-alert-status mobile-alert-status hidden";
      mobile.innerHTML = "";
    }
    return false;
  }
}

export function startSidebar() {
  updateSidebarStatus();
  updateSidebarAlerts();
  updateRecentEpisodes();
  window.setInterval(updateSidebarStatus, 10000);
  window.setInterval(updateSidebarAlerts, 10000);
}
