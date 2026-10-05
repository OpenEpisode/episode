import { API, api, apiAll, apiBlob } from "./api.js?v=3";
import {
  detailMetric,
  episodeStateBadge,
  episodeTriggerBadge,
  eventBadge,
  eventSourceBadges,
  pageControls,
  pageHeader,
  sectionHeading,
  stateBadge,
} from "./components.js?v=6";
import { activity } from "./activity-page.js?v=1";
import { closeDeliveryViewer, openDeliveryViewer } from "./delivery-viewer.js?v=1";
import { escHtml } from "./dom.js";
import {
  activateCurrentViews,
  deactivateCurrentViews,
  renderCurrentViews,
} from "./current-views.js?v=12";
import {
  episodeDisplayEnd,
  episodeRailTime,
  groupEpisodesByTime,
} from "./episode-list.js?v=3";
import {
  attachMediaSource,
  evidenceMediaUrl,
  isHlsEvidence,
  isPlayableVideoEvidence,
  updateMediaStatus,
} from "./media-player.js?v=8";
import {
  originBadge,
  renderEvidenceArchive,
  renderEpisodeEvidence,
  renderEvidenceGrid,
  showCarousel,
} from "./evidence-gallery.js?v=9";
import { activateEpisodeWorkspace, renderEpisodeWorkspace } from "./episode-view.js?v=15";
import {
  fmt,
  fmtBytes,
  fmtDuration,
  fmtShort,
  plural,
  titleCase,
  trunc,
} from "./format.js?v=3";
import {
  groupEvidenceBundlesByDay,
  groupEvidenceByEpisode,
} from "./review-lists.js?v=3";
import { updateRecentEpisodes } from "./sidebar.js?v=5";
import {
  eventParticipationBadge,
  eventParticipationNotice,
  participationAttachmentNote,
} from "./review-participation.js?v=1";
import { filterValues, filteredHash, option } from "./review-filters.js?v=1";
import { calendarTimeBounds, TIME_RANGE_OPTIONS } from "./time-range.js?v=1";
import { showContent, showError, showLoading } from "./view.js?v=1";
import { eventTitle } from "./timeline.js?v=6";

const PAGE_SIZES = Object.freeze({ episodes: 48, evidence: 60 });
const COMMON_EVIDENCE_TYPES = ["recording", "snapshot", "payload", "event_attachment"];
let eventDeliveries = [];

function incompleteRecordingGuidance(item) {
  if (item.evidence_type !== "incomplete_recording") return null;
  const invalidPlaylist = item.metadata?.playlist_validation?.valid === false;
  const reason = {
    invalid_hls_playlist: "The HLS playlist failed validation and was not marked playable.",
    startup_recovery: "The application restarted before this recording could be finalized.",
    retry_limit_exceeded: "The camera stream could not be recovered after repeated attempts.",
    episode_closed: "The Episode closed while the camera stream was reconnecting.",
    incomplete_hls_finalization: "FFmpeg left temporary output or the capture ended before finalization completed.",
    application_shutdown: "The application stopped while this recording was active.",
    recording_task_cancelled: "The recording task ended before capture was finalized.",
  }[item.metadata?.reason] || "Capture ended before this recording could be finalized.";
  const legacy = !isHlsEvidence(item);
  return {
    legacy,
    invalidPlaylist,
    reason,
    format: legacy ? "Legacy partial MP4" : "Partial HLS recording",
    explanation: invalidPlaylist
      ? "The playlist or one of its referenced components was incomplete. The original fragments and component manifest were preserved for diagnosis; playback is unavailable."
      : legacy
      ? "The incomplete MP4 index was never written, so browsers cannot play this older partial file. The original bytes remain available for forensic recovery, but no action is required."
      : "Completed HLS fragments were preserved and can be reviewed below. No action is required.",
  };
}

function payloadFieldLabel(key) {
  return String(key).replace(/_/g, " ").replace(/\b\w/g, character => character.toUpperCase());
}

function payloadFieldValue(key, value) {
  if (value === null || value === undefined || value === "") return "-";
  if (key === "sdk_command" && Number.isInteger(value)) {
    return `${value} (0x${value.toString(16).toUpperCase()})`;
  }
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function renderPayloadInterpretation(metadata) {
  const entries = Object.entries(metadata || {});
  if (!entries.length) return "";
  return `<div class="payload-interpretation"><dl class="payload-fields">
    ${entries.map(([key, value]) => `
      <dt>${escHtml(payloadFieldLabel(key))}</dt>
      <dd>${escHtml(payloadFieldValue(key, value))}</dd>`).join("")}
  </dl></div>`;
}

function hasEmbeddedEventPicture(metadata) {
  const descriptor = metadata?.embedded_picture;
  return Boolean(descriptor && Number.isInteger(descriptor.byte_size) && descriptor.byte_size > 0);
}

function eventConditionBadge(state) {
  const normalized = String(state || "").toLowerCase();
  if (normalized === "active") return '<span class="badge badge-active">Reported active</span>';
  if (normalized === "inactive") return '<span class="badge badge-inactive">Reported ended</span>';
  return `<span class="badge badge-neutral">${escHtml(titleCase(state || "Unknown condition"))}</span>`;
}

export function closeReviewOverlays() {
  deactivateCurrentViews();
  closeDeliveryViewer();
  eventDeliveries = [];
}

export async function episodes(page = 1) {
  showLoading();
  try {
    const pageSize = PAGE_SIZES.episodes;
    const offset = (page - 1) * pageSize;
    const [result, areas] = await Promise.all([
      api(`/episodes?limit=${pageSize + 1}&offset=${offset}`),
      api("/areas?include_disabled=true"),
    ]);
    const hasNext = result.length > pageSize;
    const list = result.slice(0, pageSize);
    const areaNames = new Map(areas.map(area => [area.id, area.name]));
    const episodeIds = list.map(item => item.id).join(",");
    const covers = episodeIds ? await api("/covers?ids=" + encodeURIComponent(episodeIds)) : {};
    const groups = groupEpisodesByTime(list);

    showContent(`
      ${pageHeader({
        eyebrow: "Review",
        title: "Episodes",
        description: "Correlated activity and its preserved evidence, organized by Area.",
      })}
      ${list.length === 0 ? '<div class="empty">No episodes yet</div>' : `
      <div class="episode-history">
        ${groups.map(group => `
          <section class="episode-period ${group.active ? "is-active" : ""}">
            <header class="episode-period-heading">
              <span>${escHtml(group.label)}</span>
              <small>${plural(group.episodes.length, "episode")}</small>
            </header>
            <div class="episode-rail-list">
              ${group.episodes.map(item => `
                <div class="episode-rail-item">
                  <time datetime="${escHtml(item.start_time)}">${escHtml(episodeRailTime(item.start_time))}</time>
                  <div class="episode-rail-track"><span></span></div>
                  <a href="#episode/${item.id}" class="episode-history-card">
                    ${covers[item.id]
                      ? `<div class="episode-history-cover"><img src="${API}/evidence/${covers[item.id]}/thumbnail" loading="lazy" decoding="async" alt="" onerror="this.onerror=null;this.src='${API}/evidence/${covers[item.id]}/file'"></div>`
                      : '<div class="episode-history-cover episode-cover-placeholder"><img src="/logo.svg" alt=""><span>No snapshot</span></div>'}
                    <div class="episode-history-body">
                      <div class="episode-card-heading">
                        <div class="episode-card-area">
                          <svg><use href="icons.svg?v=2#areas"></use></svg>
                          <div>
                            <span class="episode-area-kicker">Area</span>
                            <h3>${escHtml(trunc(areaNames.get(item.primary_area_id) || item.primary_area_id || "Unknown", 36))}</h3>
                          </div>
                        </div>
                        <div class="episode-card-badges">
                          ${episodeTriggerBadge(item.trigger_type)}
                          ${episodeStateBadge(item.state)}
                        </div>
                      </div>
                      <div class="episode-history-summary">
                        <span title="Activity"><svg><use href="icons.svg?v=2#activity"></use></svg>${plural(item.event_count, "event")}</span>
                        <span title="Evidence"><svg><use href="icons.svg?v=2#evidence"></use></svg>${plural(item.evidence_count, "evidence")}</span>
                        <span title="Duration"><svg><use href="icons.svg?v=2#clock"></use></svg>${fmtDuration(item.start_time, episodeDisplayEnd(item)) || "Ongoing"}</span>
                      </div>
                      <div class="episode-history-range">
                        <svg><use href="icons.svg?v=2#clock"></use></svg>
                        <span>${fmtShort(item.start_time)}
                          ${episodeDisplayEnd(item) ? `→ ${fmtShort(episodeDisplayEnd(item))}` : ""}</span>
                      </div>
                    </div>
                  </a>
                </div>`).join("")}
            </div>
          </section>`).join("")}
      </div>`}
      ${pageControls("#episodes", page, list.length, hasNext)}`);
    updateRecentEpisodes(page === 1 ? list : null);
  } catch (error) {
    showError(error.message);
  }
}

export {
  activity,
  eventParticipationBadge,
  eventParticipationNotice,
  participationAttachmentNote,
};

export async function episode(id) {
  showLoading();
  try {
    const item = await api("/episodes/" + id);
    const active = !["closed", "archived"].includes(item.state);
    const [events, evidence, currentViews, areas, devices] = await Promise.all([
      apiAll("/episodes/" + id + "/events"),
      apiAll("/episodes/" + id + "/evidence"),
      active ? api("/episodes/" + id + "/current-views") : Promise.resolve([]),
      api("/areas?include_disabled=true"),
      api("/devices?include_disabled=true"),
    ]);
    const areaNames = new Map(areas.map(area => [area.id, area.name || area.id]));
    const deviceNames = new Map(devices.map(device => [device.id, device.name || device.id]));
    const areaName = areaNames.get(item.primary_area_id) || item.primary_area_id || "Unknown Area";
    const duration = fmtDuration(item.start_time, item.end_time || item.last_event_time);
    const timelapseDevices = item.state === "closed"
      ? [...new Set(evidence
        .filter(entry => entry.evidence_type === "snapshot"
          && entry.metadata?.timelapse_eligible !== false)
        .map(entry => entry.device_id)
        .filter(Boolean))]
      : [];
    const chronologicalEvents = [...events]
      .sort((left, right) => new Date(left.timestamp) - new Date(right.timestamp));
    const supportingContent = `
      <section class="section episode-secondary review-disclosure">
        <button type="button" class="collapse-header collapsed" onclick="toggleCollapse(this)">
          <span><svg><use href="icons.svg?v=2#evidence"></use></svg><span><strong>All evidence</strong><small>Browse every artifact preserved in this Episode</small></span></span>
          <span class="review-disclosure-count">${evidence.length}</span>
        </button>
        <div class="collapse-body collapsed">${renderEpisodeEvidence(evidence)}</div>
      </section>
      <section class="section episode-secondary review-disclosure">
        <button type="button" class="collapse-header collapsed" onclick="toggleCollapse(this)">
          <span><svg><use href="icons.svg?v=2#activity"></use></svg><span><strong>Raw activity</strong><small>Inspect the normalized Events and their sources</small></span></span>
          <span class="review-disclosure-count">${events.length}</span>
        </button>
        <div class="collapse-body collapsed">
          ${events.length === 0 ? '<div class="empty">No activity</div>' : `
          <div class="table-wrap"><table>
            <thead><tr><th>Type</th><th>Device</th><th>State</th><th>Time</th><th>Source</th></tr></thead>
            <tbody>${chronologicalEvents.map(event => `
              <tr class="clickable" onclick="location='#event/${event.id}'">
                <td>${eventBadge(event.event_type)}</td>
                <td>${escHtml(trunc(deviceNames.get(event.device_id) || event.device_id, 24))}</td>
                <td>${stateBadge(event.event_state)}</td>
                <td>${fmtShort(event.timestamp)}</td>
                <td>${eventSourceBadges(event)}</td>
              </tr>`).join("")}</tbody>
          </table></div>`}
        </div>
      </section>`;
    const workspace = renderEpisodeWorkspace(
      item,
      events,
      evidence,
      timelapseDevices,
      deviceNames,
      supportingContent,
    );

    showContent(`
      <div class="breadcrumbs"><a href="#episodes">Episodes</a> <span class="sep">›</span> <span>${escHtml(trunc(areaName, 40))}</span></div>
      <header class="review-detail-hero episode-detail-header">
        <div class="review-detail-identity">
          <div class="review-detail-icon"><svg><use href="icons.svg?v=2#episodes"></use></svg></div>
          <div>
            <div class="eyebrow">Episode</div>
            <h2>${escHtml(trunc(areaName, 48))}</h2>
            <code>${escHtml(item.id)}</code>
          </div>
        </div>
        <div class="review-detail-badges">
          ${episodeTriggerBadge(item.trigger_type)}
          ${stateBadge(item.state)}
        </div>
        <div class="review-detail-metrics">
          ${detailMetric("clock", "Started", fmt(item.start_time))}
          ${detailMetric("clock", "Duration", duration || (active ? "In progress" : "—"))}
          ${detailMetric("activity", "Activity", plural(item.event_count, "Event"))}
          ${detailMetric("evidence", "Evidence", plural(item.evidence_count, "artifact"))}
        </div>
      </header>
      ${active ? renderCurrentViews(currentViews) : ""}
      ${workspace.html}`);
    activateEpisodeWorkspace(workspace.model, item, deviceNames);
    if (active) activateCurrentViews(id, currentViews);
  } catch (error) {
    showError(error.message);
  }
}

window.applyReviewFilters = (form, view) => {
  const parameters = new URLSearchParams(new FormData(form));
  for (const [key, value] of [...parameters.entries()]) {
    if (!value) parameters.delete(key);
  }
  if (view === "activity" || view === "evidence") {
    const timeRange = parameters.get("time_range");
    if (timeRange !== "custom") {
      parameters.delete("custom_from");
      parameters.delete("custom_to");
    }
    if (timeRange === "all") parameters.delete("time_range");
  }
  location.hash = `${view}${parameters.size ? `?${parameters}` : ""}`;
};

export async function event(id) {
  showLoading();
  try {
    const item = await api("/events/" + id);
    const [nearbyEvidence, closest, deliveries, devices, areas] = await Promise.all([
      item.episode_id
        ? apiAll("/episodes/" + item.episode_id + "/evidence")
        : Promise.resolve([]),
      item.episode_id
        ? api("/events/" + id + "/closest-snapshot").catch(() => null)
        : Promise.resolve(null),
      api("/receipts?event_id=" + encodeURIComponent(id) + "&limit=100"),
      api("/devices?include_disabled=true"),
      api("/areas?include_disabled=true"),
    ]);
    const deviceNames = new Map(devices.map(device => [device.id, device.name || device.id]));
    const areaNames = new Map(areas.map(area => [area.id, area.name || area.id]));
    const deviceName = deviceNames.get(item.device_id) || item.device_id || "Unknown Device";
    const areaName = areaNames.get(item.area_id) || item.area_id || "Unknown Area";
    const related = nearbyEvidence
      .filter(evidence => evidence.device_id === item.device_id && evidence.evidence_type !== "payload")
      .sort((left, right) => new Date(left.timestamp) - new Date(right.timestamp));

    eventDeliveries = deliveries.filter(delivery => delivery.has_artifact);
    let snapshotHtml = "";
    let targetBadge = "";
    if (closest?.snapshot) {
      const snapshot = closest.snapshot;
      const box = closest.bounding_box;
      if (closest.target_type) {
        targetBadge = `<span class="badge badge-neutral">${escHtml(titleCase(closest.target_type))}</span>`;
      }
      const snapshotIndex = related.findIndex(evidence => evidence.id === snapshot.id);
      snapshotHtml = `<article class="review-media-card">
        ${sectionHeading(
          "evidence",
          "Closest snapshot",
          `Captured ${fmt(snapshot.timestamp)}`,
          box ? `<label class="review-overlay-control"><input type="checkbox" checked onchange="document.getElementById('event-bbox-overlay').style.display=this.checked?'block':'none'"> Detection overlay</label>` : "",
        )}
        <div class="review-media-frame">
          <div class="review-media-image">
            <img src="${API}/evidence/${snapshot.id}/file" alt="Snapshot captured near this Event">
            ${box ? `<svg id="event-bbox-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-label="Detection region">
              <rect x="${box.x}" y="${box.y}" width="${box.width}" height="${box.height}"></rect>
            </svg>` : ""}
          </div>
        </div>
        <footer class="review-media-footer">
          <span>${targetBadge || "Closest visual evidence from this Device"}</span>
          <nav>
            ${snapshotIndex >= 0 ? `<button type="button" onclick="showCarousel(null, ${snapshotIndex})">Open slideshow</button>` : ""}
            <a href="#evidence/${snapshot.id}">Evidence details</a>
          </nav>
        </footer>
      </article>`;
    }

    const lockName = String(item.metadata?.lock_name || "").trim();
    const eventPicture = item.has_raw_payload && hasEmbeddedEventPicture(item.metadata) ? `
      <article class="review-media-card">
        ${sectionHeading("evidence", "Event picture", "Embedded in the original vendor callback")}
        <div class="review-media-frame">
          <img src="${API}/events/${encodeURIComponent(item.id)}/picture" alt="${escHtml(lockName ? `Unlock record for ${lockName}` : "Door unlock record")}">
        </div>
        <footer class="review-media-footer">
          <span>The original delivery remains unchanged.</span>
          <nav><a href="${API}/events/${encodeURIComponent(item.id)}/picture" target="_blank" rel="noopener">Open image</a></nav>
        </footer>
      </article>` : "";

    const deliveryRows = deliveries.length ? `<section class="review-panel section">
      ${sectionHeading("file", "Original deliveries", "Raw inputs that contributed to this normalized Event", `<span class="review-section-count">${deliveries.length}</span>`)}
      <div class="resource-list">${deliveries.map(delivery => `
        <div class="resource-row event-delivery-row">
          <div class="resource-main"><strong>${escHtml(delivery.source)}</strong><span>${titleCase(delivery.transport || "unknown")} transport · received ${fmt(delivery.received_at)}</span></div>
          ${stateBadge(delivery.status)}
          ${delivery.has_artifact
            ? `<button class="button button-ghost" type="button" onclick="openEventDelivery(${eventDeliveries.findIndex(candidate => candidate.id === delivery.id)})">View original</button>`
            : '<span class="meta">No artifact</span>'}
        </div>`).join("")}</div>
      </section>` : "";
    const visuals = eventPicture || snapshotHtml
      ? `<section class="event-visuals section">
          ${sectionHeading("evidence", "Visual evidence", "Images received with, or correlated to, this Event")}
          <div class="event-visual-grid">${eventPicture}${snapshotHtml}</div>
        </section>`
      : "";
    showContent(`
      <div class="breadcrumbs"><a href="#activity">Activity</a> <span class="sep">›</span> <span>Event</span></div>
      <header class="review-detail-hero event-detail-header">
        <div class="review-detail-identity">
          <div class="review-detail-icon"><svg><use href="icons.svg?v=2#activity"></use></svg></div>
          <div>
            <div class="eyebrow">Event</div>
            <h2>${escHtml(eventTitle(item))}</h2>
            <code>${escHtml(item.id)}</code>
          </div>
        </div>
        <div class="review-detail-badges">${eventConditionBadge(item.event_state)}${eventParticipationBadge(item.participation)}</div>
        <div class="review-detail-metrics">
          ${detailMetric("devices", "Device", deviceName)}
          ${detailMetric("areas", "Area", areaName)}
          ${detailMetric("clock", "Occurred", fmt(item.timestamp))}
          ${item.episode_id
            ? detailMetric("episodes", "Episode", trunc(item.episode_id, 18), `#episode/${item.episode_id}`)
            : detailMetric("episodes", "Episode", "Not associated")}
        </div>
        <div class="review-detail-sources"><small>Received through</small><div>${eventSourceBadges(item)}</div></div>
      </header>
      ${eventParticipationNotice(item.participation)}
      ${visuals}
      <section class="review-panel section">
        ${sectionHeading("evidence", "Related evidence", `Artifacts from ${deviceName} in the same Episode`, `<span class="review-section-count">${related.length}</span>`)}
        ${related.length ? renderEvidenceGrid(related) : '<div class="empty">No evidence linked to this Event yet</div>'}
      </section>
      ${Object.keys(item.metadata || {}).length ? `<section class="review-panel section">
        ${sectionHeading("activity", "Interpreted details", "Normalized fields decoded by the source integration")}
        ${renderPayloadInterpretation(item.metadata)}
      </section>` : ""}
      ${deliveryRows}`);
  } catch (error) {
    showError(error.message);
  }
}

window.openEventDelivery = index => openDeliveryViewer(eventDeliveries, index);

export async function evidence(deviceId, page = 1, parameters = new URLSearchParams()) {
  showLoading();
  try {
    const selected = {
      device_id: parameters.get("device_id") || deviceId || "",
      area_id: parameters.get("area_id") || "",
      evidence_type: parameters.get("evidence_type") || "",
      association: parameters.get("association") || "",
      time_range: parameters.get("time_range") || "",
      custom_from: parameters.get("custom_from") || "",
      custom_to: parameters.get("custom_to") || "",
    };
    const timeBounds = calendarTimeBounds(selected);
    const pageSize = PAGE_SIZES.evidence;
    const offset = (page - 1) * pageSize;
    const query = new URLSearchParams({ limit: pageSize + 1, offset });
    for (const key of ["device_id", "area_id", "evidence_type"]) {
      if (selected[key]) query.set(key, selected[key]);
    }
    if (timeBounds.valid) {
      if (timeBounds.from) query.set("captured_from", timeBounds.from);
      if (timeBounds.before) query.set("captured_before", timeBounds.before);
    }
    if (selected.association === "episode") query.set("has_episode", "true");
    if (selected.association === "unassigned") query.set("has_episode", "false");
    const [devices, areas, result] = await Promise.all([
      api("/devices?include_disabled=true"),
      api("/areas?include_disabled=true"),
      api(`/evidence?${query}`),
    ]);
    const hasNext = result.length > pageSize;
    const list = result.slice(0, pageSize);
    const deviceNames = new Map(devices.map(device => [device.id, device.name || device.id]));
    const areaNames = new Map(areas.map(area => [area.id, area.name || area.id]));
    const evidenceTypes = filterValues(
      list,
      "evidence_type",
      COMMON_EVIDENCE_TYPES,
      selected.evidence_type,
    );
    const groups = groupEvidenceBundlesByDay(groupEvidenceByEpisode(list));
    const base = filteredHash("evidence", selected);
    showContent(`
      ${pageHeader({
        eyebrow: "Review",
        title: "Evidence",
        description: "Browse preserved recordings, snapshots, payloads, and other captures in chronological context.",
      })}
      <form class="review-filter-bar" onchange="applyReviewFilters(this, 'evidence')">
        <label><span>Device</span><select name="device_id">
          ${option("", "All Devices", selected.device_id)}
          ${devices.map(device => option(device.id, device.name || device.id, selected.device_id)).join("")}
        </select></label>
        <label><span>Area</span><select name="area_id">
          ${option("", "All Areas", selected.area_id)}
          ${areas.map(area => option(area.id, area.name || area.id, selected.area_id)).join("")}
        </select></label>
        <label><span>Artifact</span><select name="evidence_type">
          ${option("", "All artifact types", selected.evidence_type)}
          ${evidenceTypes.map(type => option(type, titleCase(type), selected.evidence_type)).join("")}
        </select></label>
        <label><span>Episode</span><select name="association">
          ${option("", "Any association", selected.association)}
          ${option("episode", "Linked to an Episode", selected.association)}
          ${option("unassigned", "Not linked to an Episode", selected.association)}
        </select></label>
        <label><span>Time</span><select name="time_range">
          ${TIME_RANGE_OPTIONS.map(([value, label]) => option(value, label, selected.time_range || "all")).join("")}
        </select></label>
        ${selected.time_range === "custom" ? `
          <label class="review-custom-date"><span>From</span><input type="date" name="custom_from" value="${escHtml(selected.custom_from)}"></label>
          <label class="review-custom-date"><span>Through</span><input type="date" name="custom_to" value="${escHtml(selected.custom_to)}"></label>
          ${timeBounds.valid ? "" : '<small class="review-filter-status">Choose a valid start and end date to apply this range.</small>'}
        ` : ""}
        <a class="filter-reset" href="#evidence">Reset</a>
      </form>
      ${list.length
        ? renderEvidenceArchive(groups, deviceNames, areaNames)
        : '<div class="empty-state"><h3>No matching evidence</h3><p>Try changing the filters or wait for a new capture.</p></div>'}
      ${pageControls(base, page, list.length, hasNext)}`);
  } catch (error) {
    showError(error.message);
  }
}

export async function evidenceDetail(id) {
  showLoading();
  try {
    const item = await api("/evidence/" + id);
    const isVideo = isPlayableVideoEvidence(item);
    const isImage = item.mime_type?.startsWith("image/");
    const isText = item.mime_type?.startsWith("text/") || item.mime_type === "application/xml";
    const expired = item.availability === "expired";
    const interruption = incompleteRecordingGuidance(item);
    const associationPromise = (async () => {
      if (!expired && isImage && item.episode_id) {
        try {
          const closest = await api("/evidence/" + id + "/closest-event");
          if (closest?.event) return closest;
        } catch {}
      }
      if (!item.event_id) return null;
      try {
        const event = await api("/events/" + item.event_id);
        return {
          event,
          bounding_box: event.metadata?.bounding_box || null,
          target_type: event.metadata?.target_type || "",
        };
      } catch {
        return null;
      }
    })();
    const peersPromise = !expired && (isImage || isVideo) && item.episode_id
      ? apiAll("/episodes/" + item.episode_id + "/evidence")
      : Promise.resolve([item]);
    const textPromise = !expired && isText
      ? apiBlob("/evidence/" + item.id + "/file").then(blob => blob.text()).catch(() => "")
      : Promise.resolve("");
    const [devices, areas, association, peers, textContent] = await Promise.all([
      api("/devices?include_disabled=true"),
      api("/areas?include_disabled=true"),
      associationPromise,
      peersPromise,
      textPromise,
    ]);
    const deviceNames = new Map(devices.map(device => [device.id, device.name || device.id]));
    const areaNames = new Map(areas.map(area => [area.id, area.name || area.id]));
    const deviceName = deviceNames.get(item.device_id) || item.device_id || "Unknown Device";
    const areaName = areaNames.get(item.area_id) || item.area_id || "Unknown Area";
    const event = association?.event || null;
    const box = association?.bounding_box || null;
    const peerMedia = peers.filter(evidence => evidence.evidence_type !== "payload");
    const peerIndex = peerMedia.findIndex(evidence => evidence.id === item.id);

    let media = "";
    if (expired) {
      media = `<div class="evidence-detail-file"><svg><use href="icons.svg?v=2#clock"></use></svg><strong>Expired</strong><span>Visual Evidence expired under the retention policy.</span></div>`;
    } else if (isVideo) {
      media = `<video controls preload="metadata"></video><div class="media-playback-status hidden" role="status"></div>`;
    } else if (interruption) {
      media = `<div class="evidence-detail-file evidence-interrupted-file">
        <svg><use href="icons.svg?v=2#clock"></use></svg>
        <strong>${interruption.invalidPlaylist ? "No playable playlist" : "Partial recording preserved"}</strong>
        <span>${interruption.invalidPlaylist
          ? "The original media components and manifest remain available for diagnosis."
          : "This legacy capture is not directly playable."}</span>
      </div>`;
    } else if (isImage) {
      media = `<div class="review-media-image">
        <img src="${API}/evidence/${item.id}/file" alt="Preserved ${escHtml(titleCase(item.evidence_type))}">
        ${box ? `<svg id="evidence-bbox-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-label="Detection region">
          <rect x="${box.x}" y="${box.y}" width="${box.width}" height="${box.height}"></rect>
        </svg>` : ""}
      </div>`;
    } else if (isText && textContent) {
      media = `<pre class="payload-xml evidence-text-preview">${escHtml(textContent)}</pre>`;
    } else {
      media = `<div class="evidence-detail-file"><svg><use href="icons.svg?v=2#file"></use></svg><strong>${escHtml(titleCase(item.evidence_type))}</strong><span>${escHtml(item.mime_type || "Unknown format")}</span></div>`;
    }

    let associationHtml = "";
    if (event) {
      const difference = new Date(item.timestamp) - new Date(event.timestamp);
      const differenceSeconds = difference / 1000;
      const differenceLabel = Number.isFinite(differenceSeconds)
        ? `${differenceSeconds >= 0 ? "+" : ""}${differenceSeconds.toFixed(1)}s`
        : "—";
      const eventDeviceName = deviceNames.get(event.device_id) || event.device_id || "Unknown Device";
      associationHtml = `<section class="review-panel section evidence-association">
        ${sectionHeading(
          "activity",
          "Associated Event",
          eventTitle(event),
          association.target_type ? `<span class="badge badge-neutral">${escHtml(titleCase(association.target_type))}</span>` : "",
        )}
        <dl class="review-fact-grid">
          <div><dt>Device</dt><dd>${escHtml(eventDeviceName)}</dd></div>
          <div><dt>Occurred</dt><dd>${fmt(event.timestamp)}</dd></div>
          <div><dt>Capture offset</dt><dd>${differenceLabel}</dd></div>
          <div><dt>Condition</dt><dd>${eventConditionBadge(event.event_state)}</dd></div>
        </dl>
        <div class="evidence-association-footer">
          <div><small>Received through</small>${eventSourceBadges(event)}</div>
          <a class="button button-ghost" href="#event/${escHtml(event.id)}">Open Event</a>
        </div>
      </section>`;
    }

    const breadcrumbs = ['<a href="#evidence">Evidence</a>'];
    if (item.event_id) breadcrumbs.push(`<a href="#event/${item.event_id}">Activity</a>`);
    if (item.episode_id) breadcrumbs.push(`<a href="#episode/${item.episode_id}">Episode</a>`);
    breadcrumbs.push(`<span>${trunc(item.evidence_type, 24)}</span>`);
    showContent(`
      <div class="breadcrumbs">${breadcrumbs.map((crumb, index) => `${index ? ' <span class="sep">›</span> ' : ""}${crumb}`).join("")}</div>
      <header class="review-detail-hero evidence-detail-header">
        <div class="review-detail-identity">
          <div class="review-detail-icon"><svg><use href="icons.svg?v=2#evidence"></use></svg></div>
          <div>
            <div class="eyebrow">Evidence</div>
            <h2>${escHtml(interruption ? "Interrupted recording" : titleCase(item.evidence_type))}</h2>
            <code>${escHtml(item.id)}</code>
          </div>
        </div>
        <div class="review-detail-badges">${originBadge(item)}${interruption ? '<span class="badge badge-degraded">Interrupted</span>' : ""}<span class="badge badge-neutral">${escHtml(interruption?.format || item.mime_type || "Unknown format")}</span></div>
        <div class="review-detail-metrics">
          ${detailMetric("devices", "Device", deviceName)}
          ${detailMetric("areas", "Area", areaName)}
          ${detailMetric("clock", "Captured", fmt(item.timestamp))}
          ${item.episode_id
            ? detailMetric("episodes", "Episode", trunc(item.episode_id, 18), `#episode/${item.episode_id}`)
            : detailMetric("episodes", "Episode", "Not associated")}
        </div>
      </header>
      ${interruption ? `<section class="recording-interruption-note section">
        <div><strong>${escHtml(interruption.reason)}</strong><span>${escHtml(interruption.explanation)}</span></div>
        ${item.episode_id ? `<a href="#episode/${encodeURIComponent(item.episode_id)}">View Episode</a>` : ""}
      </section>` : ""}
      <section class="review-media-card evidence-detail-media section">
        ${sectionHeading(
          "evidence",
          expired ? "Expired artifact" : interruption ? "Preserved partial capture" : "Preserved artifact",
          expired
            ? "Visual Evidence expired under the retention policy"
            : item.original_filename || item.mime_type || "Original filename unavailable",
          box ? `<label class="review-overlay-control"><input type="checkbox" checked onchange="document.getElementById('evidence-bbox-overlay').style.display=this.checked?'block':'none'"> Detection overlay</label>` : "",
        )}
        <div class="review-media-frame">${media}</div>
        <footer class="review-media-footer">
          <span>${expired
            ? `Expired ${fmt(item.expired_at)}`
            : interruption
            ? interruption.legacy
              ? "Original partial bytes preserved · not directly playable"
              : interruption.invalidPlaylist
                ? "Original media components preserved · no playable playlist"
                : "Completed recording fragments preserved"
            : item.sha256
            ? isHlsEvidence(item) ? "Bundle manifest fingerprint recorded" : "SHA-256 fingerprint recorded"
            : "No integrity fingerprint recorded"}</span>
          ${expired ? "" : `<nav>
            ${peerIndex >= 0 && (isImage || isVideo) ? '<button id="evidence-open-viewer" type="button">Open viewer</button>' : ""}
            ${isHlsEvidence(item)
              ? `<a href="${API}/recordings/${item.id}/manifest.json" download>Download component manifest</a>`
              : `<a href="${API}/evidence/${item.id}/file" download>${interruption ? "Download partial data" : "Download file"}</a>`}
          </nav>`}
        </footer>
      </section>
      ${associationHtml}
      <section class="review-panel section">
        ${sectionHeading("file", "File and integrity", "Technical facts used to identify and verify this artifact")}
        <dl class="review-fact-grid evidence-file-facts">
          <div><dt>Original filename</dt><dd>${escHtml(item.original_filename || "Not reported")}</dd></div>
          <div><dt>Media type</dt><dd>${escHtml(item.mime_type || "Unknown")}</dd></div>
          <div><dt>File size</dt><dd>${fmtBytes(item.byte_size)}</dd></div>
          <div class="integrity-fact"><dt>${isHlsEvidence(item) ? "Bundle manifest SHA-256" : "SHA-256 fingerprint"}</dt><dd>${item.sha256 ? `<code>${escHtml(item.sha256)}</code><small>${isHlsEvidence(item) ? "Verifies the component inventory that fingerprints every recording fragment." : "Use this fingerprint to verify that the stored bytes have not changed."}</small>` : "Not indexed"}</dd></div>
        </dl>
      </section>`);
    if (!expired && isVideo) {
      const video = document.querySelector(".evidence-detail-media video");
      if (video) attachMediaSource(video, evidenceMediaUrl(item), {
        onState: state => updateMediaStatus(
          document.querySelector(".evidence-detail-media .media-playback-status"),
          state,
        ),
      });
    }
    document.getElementById("evidence-open-viewer")?.addEventListener("click", () => {
      showCarousel(peerMedia, peerIndex);
    });
  } catch (error) {
    showError(error.message);
  }
}
