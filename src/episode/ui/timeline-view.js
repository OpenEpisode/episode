// NVR Playback & Detections — Timeline view orchestrator.
// Composes the 4-region layout (camera list, vertical 24h timeline axis,
// player, detection grid) and wires keyboard shortcuts, date selection,
// and zoom. Data is composed client-side from the API: /devices, /episodes,
// /episodes/{id}/evidence, /episodes/{id}/events.
import { $, escHtml } from "./dom.js";
import { api } from "./api.js";
import { showContent, showLoading, showError } from "./view.js";
import { mountCameraList } from "./camera-list.js";
import { mountTimelineAxis, normalizeTimeRange, ZOOM_LEVELS, ZOOM_LABELS } from "./timeline-axis.js";
import { mountPlayer, SPEEDS } from "./player-controls.js";
import * as timelineModel from "./timeline.js";
import * as detectionGridModule from "./detection-grid.js";
import { isPlayableVideoEvidence } from "./media-player.js?v=8";

const {
  mountDetectionGrid,
  DETECTION_TYPE_LABELS,
  DEFAULT_DETECTION_LIMIT = 50,
  DETECTION_LIMIT_OPTIONS = [25, 50, 100, 200, "ALL"],
} = detectionGridModule;

export { DETECTION_TYPE_LABELS };

// The date selector is a text input with DD/MM/YYYY representation
// (<input type="date"> always renders in the browser locale).
export function fmtDateDMY(dateStr) {
  const [y, m, d] = String(dateStr).split("-");
  return `${d}/${m}/${y}`;
}

/** Parse "DD/MM/YYYY" (also accepts single-digit day/month) -> "YYYY-MM-DD", or null. */
export function parseDateDMY(value) {
  const match = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(String(value).trim());
  if (!match) return null;
  const [, d, m, y] = match;
  const iso = `${y}-${m.padStart(2, "0")}-${d.padStart(2, "0")}`;
  const date = new Date(`${iso}T00:00:00`);
  if (date.getFullYear() !== Number(y) || date.getMonth() + 1 !== Number(m) || date.getDate() !== Number(d)) {
    return null; // e.g. 31/02/2026
  }
  return iso;
}

/** Shift the selected date by `deltaDays` (±1) and reload the day. */
function changeDate(deltaDays) {
  const date = new Date(`${state.selectedDate}T00:00:00`);
  date.setDate(date.getDate() + deltaDays);
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  state.selectedDate = `${y}-${m}-${d}`;
  const input = $("[data-tl-date]");
  if (input) input.value = fmtDateDMY(state.selectedDate);
  const picker = $("[data-tl-date-picker]");
  if (picker) picker.value = state.selectedDate;
  loadDay();
}

function defaultState() {
  const now = new Date();
  const localDate = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  return {
    selectedCameraId: null, // compatibility for callers that select one camera
    selectedCameraIds: new Set(),
    selectedDate: localDate,
    playheadTime: null, // ms epoch
    isPlaying: false,
    playbackSpeed: 1,
    isMuted: false,
    isFullscreen: false,
    activeFilters: new Set(Object.keys(DETECTION_TYPE_LABELS)),
    zoomLevel: 0, // 0=24h 1=12h 2=6h 3=1h 4=15min
    detectionLimit: DEFAULT_DETECTION_LIMIT,
    timelineCollapsed: false,
    detectionPage: 0,
    newEventCount: 0,
    cameras: [],
    preferredSegmentId: null,
    initialPlaybackPending: true,
    selectedTimeRange: null,
    rangeClearedPlayer: false,
    episodeContextId: null,
  };
}

/**
 * Map a keyboard event to a timeline action string, or null to ignore.
 * Excludes form elements, buttons, and the timeline axis (which handles
 * its own keys).
 * @param {KeyboardEvent} event
 * @returns {string|null}
 */
export function keyToAction(event) {
  const target = event.target;
  if (target?.closest?.("input, select, textarea, [contenteditable]")) return null;
  if (target?.closest?.("button")) return null;
  // The video stage responds to the timeline keyboard shortcuts too: arrows
  // seek, Shift+arrows step a frame, Up/Down move between events, Space
  // toggles play/pause. handleKeydown calls preventDefault() to stop the
  // native media controls from also handling the same key.
  if (target?.closest?.(".tl-axis")) return null;
  const key = event.key;
  const shift = event.shiftKey;
  if (key === " ") return event.repeat ? null : "play";
  if (key === "ArrowLeft") return shift ? "frame-back" : "back-5";
  if (key === "ArrowRight") return shift ? "frame-fwd" : "fwd-5";
  if (key === "ArrowUp") return "prev-event";
  if (key === "ArrowDown") return "next-event";
  if (key === "f" || key === "F") return "fullscreen";
  if (key === "m" || key === "M") return "mute";
  if (key === "+" || key === "=") return "zoom-in";
  if (key === "-" || key === "_") return "zoom-out";
  if (key === "[") return "speed-down";
  if (key === "]") return "speed-up";
  return null;
}

// Module-level so repeated navigation tears down the previous instance.
let state = defaultState();
let axis = null;
let player = null;
let detectionGrid = null;
let loadToken = 0;
let episodeContextToken = 0;
const episodeCache = new Map();
let areaNamesPromise = null;
const cleanups = [];

function registerCleanup(fn) {
  cleanups.push(fn);
}

function teardownAll() {
  for (const fn of cleanups.splice(0)) {
    try {
      fn();
    } catch {
      /* ignore teardown errors */
    }
  }
}

export function cleanupTimeline() {
  loadToken += 1;
  episodeContextToken += 1;
  teardownAll();
  axis = null;
  player = null;
  detectionGrid = null;
}

async function loadCameras() {
  const devices = await api("/devices?include_disabled=true");
  return devices.filter(device => device.device_type === "camera" || device.device_type === "doorbell");
}

function ts(value) {
  const result = new Date(value).getTime();
  return Number.isFinite(result) ? result : 0;
}

function formatEpisodeTimestamp(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "-";
  const pad = part => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function formatEpisodeLabel(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, letter => letter.toUpperCase());
}

function localDayStart(dateStr) {
  return new Date(`${dateStr}T00:00:00`).getTime();
}

function emptyDayModel(dateStr) {
  const dayStart = localDayStart(dateStr);
  const dayEnd = dayStart + 86400000;
  const now = Date.now();
  const selectEnd = Math.max(dayStart, Math.min(dayEnd, now));
  return { dayStart, dayEnd, now, selectEnd, playhead: selectEnd, segments: [], detections: [] };
}

export function isTimelineRecordingPlayable(item) {
  return item?.evidence_type === "recording"
    && item.availability !== "expired"
    && isPlayableVideoEvidence(item);
}

const SNAPSHOT_ASSOCIATION_WINDOW_MS = 2000;

function episodeWindow(episode) {
  return {
    start: ts(episode.start_time),
    end: ts(episode.end_time || episode.last_event_time || episode.start_time),
  };
}

function nearestEvent(snapshot, events, usedEventIds) {
  const snapshotTime = ts(snapshot.timestamp);
  let best = null;
  let bestDistance = SNAPSHOT_ASSOCIATION_WINDOW_MS + 1;
  for (const event of events) {
    if (usedEventIds.has(event.id)) continue;
    const distance = Math.abs(ts(event.timestamp) - snapshotTime);
    if (distance <= SNAPSHOT_ASSOCIATION_WINDOW_MS && distance < bestDistance) {
      best = event;
      bestDistance = distance;
    }
  }
  if (best) usedEventIds.add(best.id);
  return best;
}

function recordingKey(item, episodeId) {
  return `${episodeId}:${item.device_id}:${item.metadata?.recording_session_id || item.id || item.metadata?.started_at || item.timestamp}`;
}

// Compose one day model for all selected cameras from the real API. Snapshot
// pairing is deliberately presentation-only: the source Evidence and Events
// remain untouched, and a snapshot can decorate at most one displayed Event.
async function buildDayModel(cameras, dateStr) {
  const selectedCameras = Array.isArray(cameras) ? cameras : [cameras];
  const dayStart = localDayStart(dateStr);
  const dayEnd = dayStart + 86400000;
  const episodes = await api("/episodes?limit=200");
  const relevant = episodes.filter(ep => {
    const { start, end } = episodeWindow(ep);
    return end >= dayStart && start <= dayEnd;
  });
  const segments = [];
  const detections = [];
  const episodeDetails = new Map();
  const segmentIds = new Set();
  const selectedIds = new Set(selectedCameras.map(camera => camera?.id).filter(Boolean));
  const selectedAreaIds = new Set(selectedCameras.map(camera => camera?.area_id).filter(Boolean));
  for (const episode of relevant) {
    if (selectedAreaIds.size && episode.primary_area_id && !selectedAreaIds.has(episode.primary_area_id)) continue;
    const [evidence, events] = await Promise.all([
      api(`/episodes/${episode.id}/evidence?limit=500`),
      api(`/episodes/${episode.id}/events?limit=500`),
    ]);
    const snapshotsByDevice = new Map();
    const eventsByDevice = new Map();
    const details = {
      eventTypes: new Set(),
      recordingCount: 0,
      snapshotCount: 0,
      deviceIds: new Set(),
    };
    episodeDetails.set(episode.id, details);
    for (const item of evidence) {
      if (item.device_id) details.deviceIds.add(item.device_id);
      if (item.evidence_type === "snapshot") details.snapshotCount += 1;
      else if (item.evidence_type === "recording") details.recordingCount += 1;
      if (!selectedIds.has(item.device_id)) continue;
      if (item.evidence_type === "snapshot") {
        if (!snapshotsByDevice.has(item.device_id)) snapshotsByDevice.set(item.device_id, []);
        snapshotsByDevice.get(item.device_id).push(item);
      } else if (isTimelineRecordingPlayable(item)) {
        const bounds = timelineModel.recordingBounds(item);
        const key = recordingKey(item, episode.id);
        if (segmentIds.has(key)) continue;
        segmentIds.add(key);
        const start = Math.max(dayStart, bounds.start);
        const end = Math.min(dayEnd, bounds.end);
        if (end > start && start <= Date.now()) {
          segments.push({ start, end, id: item.id, deviceId: item.device_id, metadata: item.metadata, mime_type: item.mime_type, episodeId: episode.id });
        }
      }
    }
    for (const event of events) {
      if (event.device_id) details.deviceIds.add(event.device_id);
      if (event.event_type) details.eventTypes.add(event.event_type);
      if (!selectedIds.has(event.device_id)) continue;
      const time = ts(event.timestamp);
      if (time < dayStart || time > dayEnd || time > Date.now()) continue;
      if (!eventsByDevice.has(event.device_id)) eventsByDevice.set(event.device_id, []);
      eventsByDevice.get(event.device_id).push(event);
    }
    for (const [deviceId, deviceEvents] of eventsByDevice) {
      const snapshots = [...(snapshotsByDevice.get(deviceId) || [])]
        .sort((a, b) => ts(a.timestamp) - ts(b.timestamp));
      const snapshotsByEventId = new Map();
      const snapshotByEvent = new Map();
      const usedSnapshotIds = new Set();
      const usedEventIds = new Set();
      const eventById = new Map(deviceEvents.map(event => [event.id, event]));
      for (const snapshot of snapshots) {
        if (snapshot.event_id && eventById.has(snapshot.event_id) && !usedSnapshotIds.has(snapshot.id)) {
          snapshotsByEventId.set(snapshot.event_id, snapshot);
          usedSnapshotIds.add(snapshot.id);
          usedEventIds.add(snapshot.event_id);
        }
      }
      for (const snapshot of snapshots) {
        if (usedSnapshotIds.has(snapshot.id)) continue;
        const event = nearestEvent(snapshot, deviceEvents, usedEventIds);
        if (event) {
          snapshotByEvent.set(event.id, snapshot);
          usedSnapshotIds.add(snapshot.id);
        }
      }
      for (const event of deviceEvents) {
        const snapshot = snapshotsByEventId.get(event.id) || snapshotByEvent.get(event.id) || null;
        detections.push({ time: ts(event.timestamp), type: event.event_type, id: event.id, deviceId, event, snapshot });
      }
    }
  }
  const now = Date.now();
  const selectEnd = Math.max(dayStart, Math.min(dayEnd, now));
  detections.sort((a, b) => a.time - b.time || String(a.id).localeCompare(String(b.id)));
  segments.sort((a, b) => a.start - b.start || String(a.id).localeCompare(String(b.id)));
  return { dayStart, dayEnd, now, selectEnd, playhead: selectEnd, segments, detections, episodeDetails };
}

function placeholder(icon, text) {
  return `<div class="tl-placeholder"><svg><use href="icons.svg#${icon}"></use></svg><span>${escHtml(text)}</span></div>`;
}

function zoomBar() {
  return `
    <div class="tl-zoom-bar" role="group" aria-label="Timeline zoom">
      <button type="button" class="tl-ctl-btn" data-tl-action="zoom-out" aria-label="Zoom out" title="Zoom out">
        <svg><use href="icons.svg#zoom-out"></use></svg>
      </button>
      <span class="tl-zoom-label" data-tl-zoom-label>${ZOOM_LABELS[0]}</span>
      <button type="button" class="tl-ctl-btn" data-tl-action="zoom-in" aria-label="Zoom in" title="Zoom in">
        <svg><use href="icons.svg#zoom-in"></use></svg>
      </button>
    </div>`;
}

function detectionLimitSelect() {
  return `
    <label class="tl-det-limit-wrap">
      <span class="tl-det-limit-label">Show</span>
      <select class="tl-det-limit" data-tl-det-limit aria-label="Maximum detections to show">
        ${DETECTION_LIMIT_OPTIONS.map(limit => `<option value="${limit}"${limit === DEFAULT_DETECTION_LIMIT ? " selected" : ""}>${limit === "ALL" ? "All" : limit}</option>`).join("")}
      </select>
      <span class="tl-det-limit-label">detections</span>
    </label>`;
}

function episodeContextState(kind, message) {
  const icon = kind === "error" ? "system" : kind === "loading" ? "clock" : "episodes";
  return `<div class="tl-episode-context-state${kind === "error" ? " is-error" : ""}">
    <svg><use href="icons.svg#${icon}"></use></svg><span>${escHtml(message)}</span>
  </div>`;
}

/**
 * Render the Episode details for the recording currently in the player, using
 * the same themed fact-grid table (dl.review-fact-grid) as the Device view.
 */
export function renderEpisodeContext(episode, areaName = null, options = {}) {
  const { recordingSelected = false } = options;
  if (!episode) {
    return episodeContextState(
      "empty",
      recordingSelected
        ? "This recording isn't linked to an Episode"
        : "Select a recording to see its Episode details",
    );
  }
  const area = areaName || episode.primary_area_id || "Unknown area";
  const started = formatEpisodeTimestamp(episode.start_time);
  const ended = episode.end_time
    ? formatEpisodeTimestamp(episode.end_time)
    : ["active", "quiescent"].includes(String(episode.state || "").toLowerCase())
      ? "Ongoing"
      : "—";
  const eventTypes = [...new Set(options.eventTypes || episode.eventTypes || [])].filter(Boolean);
  const deviceNames = [...new Set(options.deviceNames || episode.deviceNames || [])].filter(Boolean);
  const eventTypesText = eventTypes.length
    ? eventTypes.map(formatEpisodeLabel).join(" · ")
    : "No classified events";
  const deviceNamesText = deviceNames.length
    ? deviceNames.join(" · ")
    : "No participating devices listed";
  const eventCount = episode.event_count ?? episode.events?.length ?? 0;
  return `<div class="tl-episode-context-card">
    <div class="tl-episode-context-title">
      <span><svg><use href="icons.svg#episodes"></use></svg><span>Episode details</span></span>
    </div>
    <dl class="review-fact-grid tl-episode-facts">
      <div><dt>Cameras</dt><dd title="${escHtml(deviceNamesText)}">${escHtml(deviceNamesText)}</dd></div>
      <div><dt>Area</dt><dd title="${escHtml(area)}">${escHtml(area)}</dd></div>
      <div><dt>Start</dt><dd>${escHtml(started)}</dd></div>
      <div><dt>End</dt><dd>${escHtml(ended)}</dd></div>
      <div><dt>Events</dt><dd>${eventCount} events</dd></div>
      <div><dt>Detections</dt><dd title="${escHtml(eventTypesText)}">${escHtml(eventTypesText)}</dd></div>
    </dl>
  </div>`;
}

function renderLayout() {
  showContent(`
    <div class="timeline-view" data-tl-view>
      <section class="tl-region tl-cams">
        <div class="tl-region-head"><svg><use href="icons.svg#devices"></use></svg>Cameras</div>
        <div class="tl-region-body" data-tl-cams>${placeholder("devices", "No cameras configured")}</div>
      </section>
      <section class="tl-region tl-timeline">
        <div class="tl-region-head">
          <svg><use href="icons.svg#timeline"></use></svg>
          <div class="tl-date-group">
            <button type="button" class="tl-date-nav" data-tl-date-nav="-1" aria-label="Previous day" title="Previous day">
              <svg><use href="icons.svg#chevron-left"></use></svg>
            </button>
            <input type="text" class="tl-tl-date" data-tl-date inputmode="numeric" placeholder="DD/MM/YYYY" value="${escHtml(fmtDateDMY(state.selectedDate))}" aria-label="Select date (DD/MM/YYYY)">
            <button type="button" class="tl-date-nav" data-tl-date-nav="1" aria-label="Next day" title="Next day">
              <svg><use href="icons.svg#chevron-right"></use></svg>
            </button>
            <button type="button" class="tl-date-cal" data-tl-date-cal aria-label="Open calendar" title="Open calendar">
              <svg><use href="icons.svg#calendar"></use></svg>
            </button>
            <input type="date" class="tl-date-picker-hidden" data-tl-date-picker value="${escHtml(state.selectedDate)}" aria-hidden="true">
          </div>
        </div>
        <div class="tl-region-body" data-tl-timeline>${placeholder("timeline", "Select a camera")}</div>
        ${zoomBar()}
      </section>
      <section class="tl-region tl-player">
        <div class="tl-region-head"><svg><use href="icons.svg#clock"></use></svg>Player</div>
        <div class="tl-region-body">
          <div class="tl-player-media" data-tl-player>${placeholder("clock", "Nothing to play")}</div>
          <section class="tl-episode-context" data-tl-episode-context aria-live="polite">
            ${episodeContextState("empty", "Select a recording to see its Episode details")}
          </section>
        </div>
      </section>
      <section class="tl-region tl-detections">
        <div class="tl-region-head">
          <svg><use href="icons.svg#activity"></use></svg>Detections
          ${detectionLimitSelect()}
        </div>
        <div class="tl-region-body" data-tl-detections>${placeholder("activity", "No detections")}</div>
      </section>
    </div>`);
}

// The zoom bar (and any future data-tl-action button) is delegated at the view
// level so it survives the axis/grid re-rendering their own bodies.
function wireActionButtons() {
  const view = $("[data-tl-view]");
  view?.addEventListener("click", event => {
    const button = event.target.closest("[data-tl-action]");
    if (!button) return;
    handleAction(button.dataset.tlAction);
  });
}

function setSelectedTimeRange(range) {
  state.selectedTimeRange = range
    ? normalizeTimeRange(range.start, range.end)
    : null;
  if (state.dayModel) {
    state.dayModel = { ...state.dayModel, selectedRange: state.selectedTimeRange };
    axis?.setModel(state.dayModel);
  }
  detectionGrid?.setTimeRange(state.selectedTimeRange);
  applySelectedRangePlayback(state.dayModel);
}

function onRangeChange(range) {
  setSelectedTimeRange(range);
}

function clearTimeRange() {
  setSelectedTimeRange(null);
}

function wireRangeSelection() {
  const onOutsideClick = event => {
    if (!state.selectedTimeRange) return;
    if (event.target.closest?.(".tl-axis")) return;
    clearTimeRange();
  };
  // Clear after the clicked control/tile has handled its own action. This
  // keeps detection tiles clickable even though clearing re-renders the grid.
  document.addEventListener("click", onOutsideClick);
  registerCleanup(() => {
    document.removeEventListener("click", onOutsideClick);
  });
}

async function loadAreaNames() {
  if (!areaNamesPromise) {
    areaNamesPromise = api("/areas?include_disabled=true")
      .then(areas => new Map(areas.map(area => [area.id, area.name || area.id])))
      .catch(error => {
        areaNamesPromise = null;
        throw error;
      });
  }
  return areaNamesPromise;
}

function episodeContextDetails(model, episodeId) {
  const raw = model?.episodeDetails?.get(episodeId);
  if (!raw) return null;
  const knownNames = new Map(state.cameras.map(camera => [camera.id, camera.name || camera.id]));
  return {
    eventTypes: [...raw.eventTypes],
    recordingCount: raw.recordingCount,
    snapshotCount: raw.snapshotCount,
    deviceNames: [...raw.deviceIds].map(deviceId => knownNames.get(deviceId)).filter(Boolean),
  };
}

function setEpisodeContext(episodeId, { recordingSelected = false, details = null } = {}) {
  const container = $("[data-tl-episode-context]");
  if (!container) return;
  const normalizedId = episodeId || null;
  if (normalizedId && state.episodeContextId === normalizedId && container.dataset.episodeContextState !== "error") {
    const cached = episodeCache.get(normalizedId);
    if (cached && details) {
      cached.details = details;
      container.innerHTML = renderEpisodeContext(cached.episode, cached.areaName, details);
    }
    return;
  }
  state.episodeContextId = normalizedId;
  const token = ++episodeContextToken;
  if (!normalizedId) {
    container.dataset.episodeContextState = "empty";
    container.innerHTML = renderEpisodeContext(null, null, { recordingSelected });
    return;
  }
  const cached = episodeCache.get(normalizedId);
  if (cached) {
    container.dataset.episodeContextState = "ready";
    cached.details = details || cached.details || {};
    container.innerHTML = renderEpisodeContext(cached.episode, cached.areaName, cached.details);
    return;
  }
  container.dataset.episodeContextState = "loading";
  container.innerHTML = episodeContextState("loading", "Loading Episode details…");
  Promise.all([
    api(`/episodes/${encodeURIComponent(normalizedId)}`),
    loadAreaNames().catch(() => new Map()),
  ]).then(([episode, areas]) => {
    if (token !== episodeContextToken || state.episodeContextId !== normalizedId) return;
    const areaName = areas.get(episode.primary_area_id) || episode.primary_area_id || "Unknown area";
    if (episodeCache.size >= 50) {
      episodeCache.delete(episodeCache.keys().next().value);
    }
    episodeCache.set(normalizedId, { episode, areaName, details: details || {} });
    container.dataset.episodeContextState = "ready";
    container.innerHTML = renderEpisodeContext(episode, areaName, details || {});
  }).catch(error => {
    if (token !== episodeContextToken || state.episodeContextId !== normalizedId) return;
    container.dataset.episodeContextState = "error";
    container.innerHTML = episodeContextState("error", error?.message || "Episode details unavailable");
  });
}

function wireDetectionLimit() {
  const select = $("[data-tl-det-limit]");
  if (!select) return;
  const handler = () => {
    const limit = select.value === "ALL" ? "ALL" : Number(select.value);
    state.detectionLimit = DETECTION_LIMIT_OPTIONS.includes(limit) ? limit : DEFAULT_DETECTION_LIMIT;
    detectionGrid?.setLimit(state.detectionLimit);
  };
  handler();
  select.addEventListener("change", handler);
  registerCleanup(() => select.removeEventListener("change", handler));
}

// The date selector lives in the timeline region head. The text input commits
// on Enter only; the chevron arrows step ±1 day; the calendar button opens the
// native date picker (showPicker) on a hidden <input type="date">.
function wireDate() {
  const input = $("[data-tl-date]");
  const picker = $("[data-tl-date-picker]");
  const calBtn = $("[data-tl-date-cal]");
  const view = $("[data-tl-view]");

  // Text input: commit on Enter only.
  if (input) {
    const onKey = event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const iso = parseDateDMY(input.value);
      if (!iso || iso === state.selectedDate) return;
      input.value = fmtDateDMY(iso);
      if (picker) picker.value = iso;
      state.selectedDate = iso;
      loadDay();
    };
    input.addEventListener("keydown", onKey);
    registerCleanup(() => input.removeEventListener("keydown", onKey));
  }

  // Chevron arrows: prev / next day.
  if (view) {
    const onNavClick = event => {
      const btn = event.target.closest("[data-tl-date-nav]");
      if (!btn) return;
      const delta = Number(btn.dataset.tlDateNav) || 0;
      if (delta) changeDate(delta);
    };
    view.addEventListener("click", onNavClick);
    registerCleanup(() => view.removeEventListener("click", onNavClick));
  }

  // Calendar button: open native picker on the hidden date input.
  if (calBtn && picker) {
    const onCalClick = () => {
      picker.value = state.selectedDate;
      picker.showPicker?.();
    };
    calBtn.addEventListener("click", onCalClick);
    registerCleanup(() => calBtn.removeEventListener("click", onCalClick));

    const onPickerChange = () => {
      if (!picker.value) return;
      state.selectedDate = picker.value;
      if (input) input.value = fmtDateDMY(state.selectedDate);
      loadDay();
    };
    picker.addEventListener("change", onPickerChange);
    registerCleanup(() => picker.removeEventListener("change", onPickerChange));
  }
}

function onCameraSelect(cameraId) {
  state.selectedCameraId = cameraId;
  state.selectedCameraIds = cameraId ? new Set([cameraId]) : new Set();
  clearExcludedPlayer(cameraId ? state.selectedCameraIds : new Set());
  loadDay({ preferGlobalLatest: shouldPreferGlobalLatest(state.selectedCameraIds) });
}

function onCameraSelectionChange(cameraIds) {
  state.selectedCameraIds = new Set(cameraIds);
  state.selectedCameraId = state.selectedCameraIds.values().next().value || null;
  clearExcludedPlayer(state.selectedCameraIds);
  loadDay({ preferGlobalLatest: shouldPreferGlobalLatest(state.selectedCameraIds) });
}

function shouldPreferGlobalLatest(selectedIds) {
  if (!selectedIds.size) return false;
  const current = player?.currentSegment?.();
  return !current || !selectedIds.has(current.deviceId);
}

function clearExcludedPlayer(selectedIds) {
  if (!selectedIds.size) {
    setEpisodeContext(null);
    player?.loadSegment?.(null);
    player?.setEmptyMessage?.("Select at least one camera to view recordings");
    return;
  }
  const segment = player?.currentSegment?.();
  if (segment && !selectedIds.has(segment.deviceId)) {
    setEpisodeContext(null);
    player.loadSegment?.(null);
    player?.setEmptyMessage?.("Loading recordings for the selected cameras…");
  }
}

function movePlayhead(time) {
  state.playheadTime = time;
  const base = state.dayModel ?? emptyDayModel(state.selectedDate);
  state.dayModel = { ...base, playhead: time };
  axis?.setModel(state.dayModel);
}

function applySelectedRangePlayback(model) {
  if (!model || !player) return;
  const range = state.selectedTimeRange;
  if (!range) {
    const current = player.currentSegment?.();
    if (!state.rangeClearedPlayer || current) {
      state.rangeClearedPlayer = false;
      return;
    }
    const around = timelineModel.selectRecordingForTime(
      model.segments,
      model.playhead ?? state.playheadTime,
    );
    if (around) {
      const targetTime = around.exact ? (model.playhead ?? state.playheadTime) : around.segment.start;
      movePlayhead(targetTime);
      setEpisodeContext(around.segment.episodeId, {
        recordingSelected: true,
        details: episodeContextDetails(model, around.segment.episodeId),
      });
      player.loadSegment(around.segment, targetTime);
    } else {
      const targetTime = model.playhead ?? state.playheadTime ?? model.selectEnd;
      movePlayhead(targetTime);
      setEpisodeContext(null);
      player.loadSegment(null, targetTime);
      player.setEmptyMessage?.("No recordings available for the selected cameras");
    }
    state.rangeClearedPlayer = false;
    return;
  }

  const selection = timelineModel.selectRecordingForRange(
    model.segments,
    range,
    player.currentSegment?.(),
    model.playhead ?? state.playheadTime,
  );
  if (!selection) {
    const targetTime = Math.max(
      model.dayStart,
      Math.min(model.selectEnd ?? model.dayEnd, range.start),
    );
    movePlayhead(targetTime);
    setEpisodeContext(null);
    player.loadSegment(null, targetTime);
    player.setEmptyMessage?.("No recordings in the selected period");
    state.rangeClearedPlayer = true;
    return;
  }

  const targetTime = selection.playhead;
  movePlayhead(targetTime);
  setEpisodeContext(selection.segment.episodeId, {
    recordingSelected: true,
    details: episodeContextDetails(model, selection.segment.episodeId),
  });
  if (selection.preservedCurrent) {
    player.pause?.();
    player.seekTo(targetTime);
  } else {
    player.loadSegment(selection.segment, targetTime);
  }
  state.rangeClearedPlayer = false;
}

function onSeek(time) {
  const model = state.dayModel;
  if (!model || time > Date.now() || time > (model.selectEnd ?? model.dayEnd)) return;
  const clamped = Math.max(model.dayStart, Math.min(model.selectEnd ?? model.dayEnd, time));
  const range = state.selectedTimeRange;
  const seekTime = range
    ? Math.max(range.start, Math.min(range.end, clamped))
    : clamped;
  const selection = range
    ? timelineModel.selectRecordingForTimeInRange(model.segments, seekTime, range)
    : timelineModel.selectRecordingForTime(model.segments, seekTime);
  if (selection && player) {
    const segment = selection.segment;
    const targetTime = selection.time ?? (selection.exact ? seekTime : segment.start);
    movePlayhead(targetTime);
    setEpisodeContext(segment.episodeId, {
      recordingSelected: true,
      details: episodeContextDetails(model, segment.episodeId),
    });
    player.playSegment(segment, targetTime);
    detectionGrid?.highlightNearest(targetTime);
    state.rangeClearedPlayer = false;
  } else {
    movePlayhead(seekTime);
    setEpisodeContext(null);
    player?.loadSegment?.(null, seekTime);
    player?.setEmptyMessage?.(
      state.selectedTimeRange
        ? "No recordings in the selected period"
        : "No recording available for the selected time",
    );
    if (state.selectedTimeRange) state.rangeClearedPlayer = true;
    detectionGrid?.highlightNearest(seekTime);
  }
  // Move focus to the video so space/arrows control playback without
  // re-clicking the video (timeline/detection clicks land focus elsewhere).
  player?.focus();
  // A click (or keyboard seek) highlights the matching detection below.
}

function onScrub(time) {
  const model = state.dayModel;
  if (time > Date.now()) return;
  const range = state.selectedTimeRange;
  const lowerBound = range?.start ?? model?.dayStart;
  const upperBound = range?.end ?? (model?.selectEnd ?? model?.dayEnd);
  const clamped = model
    ? Math.max(lowerBound, Math.min(upperBound, time))
    : time;
  movePlayhead(clamped);
  const currentSegment = player?.currentSegment?.();
  // Keep dragging lightweight: switch/clear the video only when the operator
  // commits the selected time in onSeek. This avoids repeatedly tearing down
  // media while crossing gaps or other recordings.
  if (currentSegment && clamped >= currentSegment.start && clamped < currentSegment.end) {
    player.seekTo(clamped);
  }
  detectionGrid?.highlightNearest(clamped);
}

/** Lightweight ±delta seek within the current segment (no HLS re-attach). */
function seekDelta(deltaMs) {
  // The player's live time tracks the video during playback; otherwise fall
  // back to the last committed playhead.
  const base = player?.now?.() ?? state.playheadTime ?? state.dayModel?.playhead ?? 0;
  const model = state.dayModel;
  const target = model
    ? Math.max(
      state.selectedTimeRange?.start ?? model.dayStart,
      Math.min(
        state.selectedTimeRange?.end ?? (model.selectEnd ?? model.dayEnd),
        base + deltaMs,
      ),
    )
    : base + deltaMs;
  movePlayhead(target);
  if (player) player.seekTo(target);
  detectionGrid?.highlightNearest(target);
}

function applyZoom(zoom) {
  state.zoomLevel = zoom;
  axis?.setZoom(zoom);
  const label = $("[data-tl-zoom-label]");
  if (label) label.textContent = ZOOM_LABELS[zoom];
}

function onZoom(zoom) {
  applyZoom(zoom);
}

async function loadDay({ preferGlobalLatest = false } = {}) {
  const token = ++loadToken;
  const cameras = state.cameras.filter(camera => state.selectedCameraIds.has(camera.id));
  const tlBody = $("[data-tl-timeline]");
  if (!cameras.length || !tlBody) {
    const model = emptyDayModel(state.selectedDate);
    model.selectedRange = state.selectedTimeRange;
    state.dayModel = model;
    axis?.setModel(model);
    detectionGrid?.update([]);
    setEpisodeContext(null);
    player?.loadSegment?.(null, model.playhead);
    player?.setEmptyMessage?.("Select at least one camera to view recordings");
    return;
  }

  if (!axis) {
    axis = mountTimelineAxis(tlBody, {
      model: emptyDayModel(state.selectedDate),
      zoom: state.zoomLevel,
      onSeek,
      onScrub,
      onZoom,
      onRangeChange,
    });
    registerCleanup(() => {
      axis?.cleanup();
      axis = null;
    });
  }

  try {
    let globalLatest = null;
    if (preferGlobalLatest) {
      globalLatest = await findLatestPlayableRecording(cameras);
      if (token !== loadToken) return;
      if (globalLatest) {
        const targetTime = globalLatest.recording.bounds.start;
        const targetDate = localDateForTime(targetTime);
        state.selectedDate = targetDate;
        state.playheadTime = targetTime;
        state.preferredSegmentId = globalLatest.recording.item.id;
        const input = $("[data-tl-date]");
        const picker = $("[data-tl-date-picker]");
        if (input) input.value = fmtDateDMY(targetDate);
        if (picker) picker.value = targetDate;
      }
    }
    const model = await buildDayModel(cameras, state.selectedDate);
    if (!axis || token !== loadToken) return; // view was torn down or superseded
    model.selectedRange = state.selectedTimeRange;
    const initialSegment = state.preferredSegmentId
      ? model.segments.find(segment => segment.id === state.preferredSegmentId)
      : null;
    const latestSegment = [...model.segments].sort((a, b) => b.start - a.start).at(0) || null;
    const currentSegment = player?.currentSegment?.();
    const currentStillAvailable = currentSegment
      && model.segments.some(segment => segment.id === currentSegment.id)
      && state.selectedCameraIds.has(currentSegment.deviceId);
    const globalSegment = globalLatest ? segmentFromRecording(globalLatest.recording) : null;
    if (globalSegment && !model.segments.some(segment => segment.id === globalSegment.id)) {
      model.segments.push(globalSegment);
      model.segments.sort((left, right) => left.start - right.start || String(left.id).localeCompare(String(right.id)));
    }
    const autoSegment = currentStillAvailable
      ? currentSegment
      : (globalSegment || (state.initialPlaybackPending ? (initialSegment || latestSegment) : latestSegment));
    model.playhead = currentStillAvailable
      ? (player?.now?.() ?? state.playheadTime ?? currentSegment.start)
      : (autoSegment?.start ?? model.selectEnd);
    model.playhead = Math.max(model.dayStart, Math.min(model.selectEnd, model.playhead));
    state.playheadTime = model.playhead;
    state.dayModel = model;
    axis.setModel(model);
    detectionGrid?.update(model.detections);
    const selectedSegment = autoSegment || (currentStillAvailable ? currentSegment : null);
    setEpisodeContext(selectedSegment?.episodeId || null, {
      recordingSelected: Boolean(selectedSegment),
      details: episodeContextDetails(model, selectedSegment?.episodeId),
    });
    if (state.selectedTimeRange) {
      applySelectedRangePlayback(model);
      state.initialPlaybackPending = false;
    } else if (!currentStillAvailable) {
      if (autoSegment) {
        player?.loadSegment?.(autoSegment, autoSegment.start);
      } else {
        setEpisodeContext(null);
        player?.loadSegment?.(null, model.playhead);
        player?.setEmptyMessage?.("No recordings available for the selected cameras");
      }
      state.initialPlaybackPending = false;
    }
  } catch (error) {
    if (token !== loadToken) return;
    const fallback = emptyDayModel(state.selectedDate);
    state.dayModel = fallback;
    detectionGrid?.update([]);
    setEpisodeContext(null);
    if (axis) axis.setModel(fallback);
    player?.loadSegment?.(null, fallback.playhead);
    player?.setEmptyMessage?.("Unable to load recordings for the selected cameras");
  }
}

function localDateForTime(time) {
  const date = new Date(time);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function segmentFromRecording(recording) {
  if (!recording?.item || !recording.bounds) return null;
  const { item, bounds } = recording;
  return {
    start: bounds.start,
    end: bounds.end,
    id: item.id,
    deviceId: item.device_id,
    metadata: item.metadata,
    mime_type: item.mime_type,
    episodeId: item.episode_id || recording.episodeId,
  };
}

async function findLatestPlayableRecording(cameras) {
  const cameraIds = new Set(cameras.map(camera => camera.id));
  const pageSize = 500;
  let offset = 0;
  while (true) {
    const evidence = await api(`/evidence?evidence_type=recording&limit=${pageSize}&offset=${offset}`);
    const playable = evidence
      .filter(item => cameraIds.has(item.device_id) && isTimelineRecordingPlayable(item))
      .map(item => ({ item, bounds: timelineModel.recordingBounds(item), episodeId: item.episode_id }))
      .filter(recording => Number.isFinite(recording.bounds.start) && recording.bounds.start <= Date.now());
    if (playable.length) return { recording: playable[0] };
    if (evidence.length < pageSize) return null;
    offset += evidence.length;
  }
}

/**
 * Cap the timeline's first grid row at the player's natural content height —
 * the video stage plus the fixed chrome (heads, controls, episode-context
 * band) — so the tall 24h canvas and the camera list scroll inside a row that
 * is exactly as tall as the video. No fixed minimum height, no blank space
 * below the video stage.
 *
 * Every measured input is content-driven and independent of the row height:
 * the video stage is `flex: 0 0 auto` and the episode-context band is
 * `flex: 0 0 auto` (it never stretches with the row). The row is written as a
 * fixed length, not `minmax(_, auto)`, so the tall canvas can only scroll
 * inside it and can never feed back into the measured inputs. The page height
 * is therefore stable and bounded by the video.
 */
function sizeTopRow() {
  const view = $(".timeline-view");
  const player = $(".tl-player");
  if (!view || !player) return;
  const apply = () => {
    // Below the stacking breakpoint the regions are full width, so the
    // side-by-side height match no longer applies and the CSS breakpoints
    // take over.
    if (window.matchMedia?.("(max-width: 767px)").matches) {
      view.style.gridTemplateRows = "";
      return;
    }
    const head = player.querySelector(".tl-region-head")?.offsetHeight || 0;
    const stage = $(".tl-player-stage")?.offsetHeight || 0;
    const controls = player.querySelector(".tl-controls-row")?.offsetHeight || 0;
    // The context band is natural-height (flex: 0 0 auto), so its offsetHeight
    // is its content — it does not stretch with the row and cannot create a
    // resize feedback loop.
    const context = $(".tl-episode-context")?.offsetHeight || 45;
    const row = 2 /* region border */ + head + stage + controls + context;
    view.style.gridTemplateRows = `${row}px auto`;
  };
  apply();
  // The video stage (its height changes as a segment loads or the window
  // resizes) and the episode-context band (its content swaps when the Episode
  // loads) are the only inputs that change; both are natural-height, so
  // observing them cannot feed back into the fixed row height.
  const observer = new ResizeObserver(apply);
  const stage = $(".tl-player-stage");
  if (stage) observer.observe(stage);
  const context = $(".tl-episode-context");
  if (context) observer.observe(context);
  registerCleanup(() => observer.disconnect());
}

function mountPlayerInstance() {
  const body = $("[data-tl-player]");
  if (!body || player) return;
  player = mountPlayer(body, {
    onStateChange: ({ isPlaying }) => {
      state.isPlaying = isPlaying;
      $("[data-ctl='playpause']")?.classList.toggle("active", isPlaying);
    },
    onExport: segment => {
      if (!segment?.id) return;
      window.open(`/api/v1/recordings/${encodeURIComponent(segment.id)}/index.m3u8`, "_blank", "noopener");
    },
  });
  registerCleanup(() => {
    player?.cleanup();
    player = null;
  });
}

function mountDetectionGridInstance() {
  const body = $("[data-tl-detections]");
  if (!body || detectionGrid) return;
  detectionGrid = mountDetectionGrid(body, {
    detections: state.dayModel?.detections || [],
    activeFilters: state.activeFilters,
    timeRange: state.selectedTimeRange,
    nowMs: Date.now(),
    limit: state.detectionLimit,
    onSelect: time => onSeek(time),
    onFilterChange: filters => { state.activeFilters = filters; },
  });
  registerCleanup(() => {
    detectionGrid?.cleanup();
    detectionGrid = null;
  });
}

function refreshForCamera() {
  loadDay();
}

function prevEvent() {
  const model = state.dayModel;
  if (!model?.detections?.length) return;
  const current = state.playheadTime ?? model.playhead;
  const sorted = [...model.detections].sort((a, b) => b.time - a.time);
  const prev = sorted.find(d => d.time < current);
  if (prev) onSeek(prev.time);
}

function nextEvent() {
  const model = state.dayModel;
  if (!model?.detections?.length) return;
  const current = state.playheadTime ?? model.playhead;
  const sorted = [...model.detections].sort((a, b) => a.time - b.time);
  const next = sorted.find(d => d.time > current);
  if (next) onSeek(next.time);
}

function changeSpeed(direction) {
  const idx = SPEEDS.indexOf(state.playbackSpeed);
  const next = Math.max(0, Math.min(SPEEDS.length - 1, (idx < 0 ? 1 : idx) + direction));
  state.playbackSpeed = SPEEDS[next];
  player?.setSpeed(SPEEDS[next]);
}

export function handleKeydown(event) {
  const action = keyToAction(event);
  if (!action) {
    if (event.key === " " && event.repeat && !event.target?.closest?.(".tl-player-video")) {
      event.preventDefault();
    }
    return;
  }
  event.preventDefault();
  // The video's native controls must not also act on the key (e.g. native
  // arrow-seeking or space toggling) once the timeline handler owns it.
  if (event.target?.closest?.(".tl-player-video")) {
    event.stopPropagation();
  }
  switch (action) {
    case "play":
      player?.togglePlay();
      break;
    case "back-5":
      seekDelta(-5000);
      break;
    case "fwd-5":
      seekDelta(5000);
      break;
    case "frame-back":
      player?.pause();
      seekDelta(-1000 / 30);
      break;
    case "frame-fwd":
      player?.pause();
      seekDelta(1000 / 30);
      break;
    case "prev-event":
      prevEvent();
      break;
    case "next-event":
      nextEvent();
      break;
    case "fullscreen":
      player?.toggleFullscreen();
      break;
    case "mute":
      player?.toggleMute();
      break;
    case "zoom-in":
      handleAction("zoom-in");
      break;
    case "zoom-out":
      handleAction("zoom-out");
      break;
    case "speed-down":
      changeSpeed(-1);
      break;
    case "speed-up":
      changeSpeed(1);
      break;
  }
}

function handleAction(action) {
  if (action === "play" && player) {
    player.togglePlay();
  } else if (action === "fullscreen" && player) {
    player.toggleFullscreen();
  } else if (action === "zoom-in" || action === "zoom-out") {
    const next = Math.max(0, Math.min(ZOOM_LEVELS.length - 1, state.zoomLevel + (action === "zoom-in" ? 1 : -1)));
    applyZoom(next);
  }
}

export async function renderTimeline() {
  cleanupTimeline();
  state = defaultState();
  showLoading();
  renderLayout();
  wireActionButtons();
  mountPlayerInstance();
  mountDetectionGridInstance();
  sizeTopRow();
  wireDetectionLimit();
  wireDate();
  wireRangeSelection();

  const view = $("[data-tl-view]");
  view?.addEventListener("keydown", handleKeydown);
  registerCleanup(() => {
    view?.removeEventListener("keydown", handleKeydown);
  });

  try {
    const cameras = await loadCameras();
    state.cameras = cameras;
    state.selectedCameraIds = new Set(cameras.map(camera => camera.id));
    state.selectedCameraId = cameras[0]?.id || null;
    if (cameras.length) {
      try {
        const latest = await findLatestPlayableRecording(cameras);
        if (latest) {
          const targetTime = latest.recording?.bounds.start || 0;
          if (targetTime > 0) {
            state.selectedDate = localDateForTime(targetTime);
            state.playheadTime = targetTime;
          }
          state.preferredSegmentId = latest.recording?.item.id || null;
        }
      } catch {
        // A latest-recording lookup is a startup enhancement. The current local
        // day remains a useful fallback when the evidence endpoint is unavailable.
      }
    }
    const dateInput = $("[data-tl-date]");
    const datePicker = $("[data-tl-date-picker]");
    if (dateInput) dateInput.value = fmtDateDMY(state.selectedDate);
    if (datePicker) datePicker.value = state.selectedDate;
    const camsBody = $("[data-tl-cams]");
    if (camsBody) {
      registerCleanup(mountCameraList(camsBody, {
        cameras,
        selectedIds: state.selectedCameraIds,
        onSelect: onCameraSelect,
        onSelectionChange: onCameraSelectionChange,
      }));
    }
    refreshForCamera();
  } catch (error) {
    showError(error.message);
  }
}
