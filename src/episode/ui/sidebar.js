import { api } from "./api.js?v=3";
import { episodeStateBadge } from "./components.js?v=5";
import { $ } from "./dom.js";
import { plural, trunc } from "./format.js?v=3";

function statusIndicator(state) {
  if (state === "healthy") return "online";
  if (state === "degraded") return "warning";
  if (state === "disabled" || state === "unknown") return "idle";
  return "offline";
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
    const indicator = statusIndicator(status.state);
    const label = ({
      healthy: "All systems operational",
      degraded: "Attention needed",
      unavailable: "System unavailable",
    }[status.state] || "Status unknown");
    element.innerHTML = `<div class="sidebar-status">
      <span class="dot ${indicator}" title="${label}"></span>
      <span class="label">${label}</span>
      <span class="label" style="margin-left:auto">${status.active_recordings ? plural(status.active_recordings, "rec") : ""}</span>
    </div>`;
  } catch {
    element.innerHTML = '<div class="sidebar-status"><span class="dot offline"></span><span class="label">Offline</span></div>';
  }
}

export function startSidebar() {
  updateSidebarStatus();
  updateRecentEpisodes();
  window.setInterval(updateSidebarStatus, 10000);
}
