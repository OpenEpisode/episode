// NVR Playback & Detections — Timeline view orchestrator.
// Composes the 4-region layout (camera list, vertical 24h timeline axis,
// player, detection grid) and wires keyboard shortcuts, date selection,
// and zoom. Data is composed client-side from the API: /devices, /episodes,
// /episodes/{id}/evidence, /episodes/{id}/events.
import { $, escHtml } from "./dom.js";
import { api } from "./api.js";
import { showContent, showLoading, showError } from "./view.js";
import { mountCameraList } from "./camera-list.js";
import { mountTimelineAxis, ZOOM_LEVELS, ZOOM_LABELS } from "./timeline-axis.js";
import { mountPlayer, findSegment, SPEEDS } from "./player-controls.js";
import { recordingBounds } from "./timeline.js";
import { mountDetectionGrid, DETECTION_TYPE_LABELS } from "./detection-grid.js";

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
  return {
    selectedCameraId: null,
    selectedDate: new Date().toISOString().slice(0, 10),
    playheadTime: null, // ms epoch
    isPlaying: false,
    playbackSpeed: 1,
    isMuted: false,
    isFullscreen: false,
    activeFilters: new Set(Object.keys(DETECTION_TYPE_LABELS)),
    zoomLevel: 0, // 0=24h 1=12h 2=6h 3=1h 4=15min
    detRows: 4, // visible rows in the detections grid (configurable)
    timelineCollapsed: false,
    detectionPage: 0,
    newEventCount: 0,
    cameras: [],
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
  if (target?.closest?.(".tl-axis")) return null;
  const key = event.key;
  const shift = event.shiftKey;
  if (key === " ") return "play";
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

function localDayStart(dateStr) {
  return new Date(`${dateStr}T00:00:00`).getTime();
}

function emptyDayModel(dateStr) {
  const dayStart = localDayStart(dateStr);
  return { dayStart, dayEnd: dayStart + 86400000, now: Date.now(), playhead: dayStart + 86400000, segments: [], detections: [] };
}

// Compose a camera's day model from the real API: episodes overlapping the
// day, then per-episode recording evidence (segments) + events (detections).
async function buildDayModel(camera, dateStr) {
  const dayStart = localDayStart(dateStr);
  const dayEnd = dayStart + 86400000;
  const episodes = await api("/episodes?limit=200");
  const relevant = episodes.filter(ep => {
    const start = ts(ep.start_time);
    const end = ts(ep.end_time || ep.last_event_time || ep.start_time);
    return end >= dayStart && start <= dayEnd;
  });
  const segments = [];
  const detections = [];
  for (const episode of relevant) {
    const [evidence, events] = await Promise.all([
      api(`/episodes/${episode.id}/evidence`),
      api(`/episodes/${episode.id}/events`),
    ]);
    const snapshotMap = new Map();
    for (const item of evidence) {
      if (item.device_id !== camera.id) continue;
      if (item.evidence_type === "snapshot" && item.event_id) {
        snapshotMap.set(item.event_id, item);
      } else if (item.evidence_type === "recording") {
        const bounds = recordingBounds(item);
        segments.push({ start: bounds.start, end: bounds.end, id: item.id, metadata: item.metadata, mime_type: item.mime_type, episodeId: episode.id });
      }
    }
    for (const event of events) {
      if (event.device_id !== camera.id) continue;
      const time = ts(event.timestamp);
      if (time < dayStart || time > dayEnd) continue;
      detections.push({ time, type: event.event_type, id: event.id, snapshot: snapshotMap.get(event.id) || null });
    }
  }
  return { dayStart, dayEnd, now: Date.now(), playhead: dayEnd, segments, detections };
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

const DET_ROWS_OPTIONS = [4, 8, 16, 32, "ALL"];

function detRowsSelect() {
  return `
    <label class="tl-det-rows-wrap">
      <span class="tl-det-rows-label">Rows</span>
      <select class="tl-det-rows" data-tl-det-rows aria-label="Visible detection rows">
        ${DET_ROWS_OPTIONS.map(n => `<option value="${n}"${n === 4 ? " selected" : ""}>${n}</option>`).join("")}
      </select>
    </label>`;
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
        <div class="tl-region-body" data-tl-player>${placeholder("clock", "Nothing to play")}</div>
      </section>
      <section class="tl-region tl-detections">
        <div class="tl-region-head"><svg><use href="icons.svg#activity"></use></svg>Detections${detRowsSelect()}</div>
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

function wireDetRows() {
  const select = $("[data-tl-det-rows]");
  const view = $("[data-tl-view]");
  if (!select || !view) return;
  const handler = () => {
    if (select.value === "ALL") {
      // Show every detection: the pane grows to its content height.
      state.detRows = "ALL";
      view.classList.add("tl-det-all");
      view.style.removeProperty("--det-rows");
      return;
    }
    const rows = Number(select.value) || 4;
    state.detRows = rows;
    view.classList.remove("tl-det-all");
    // Set on the grid container so the detection body height can react.
    view.style.setProperty("--det-rows", String(rows));
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
  loadDay();
}

function movePlayhead(time) {
  state.playheadTime = time;
  const base = state.dayModel ?? emptyDayModel(state.selectedDate);
  state.dayModel = { ...base, playhead: time };
  axis?.setModel(state.dayModel);
}

function onSeek(time) {
  movePlayhead(time);
  const model = state.dayModel;
  if (!model) return;
  const segment = findSegment(model.segments, time);
  if (segment && player) {
    player.playSegment(segment, time);
  }
  // Move focus to the video so space/arrows control playback without
  // re-clicking the video (timeline/detection clicks land focus elsewhere).
  player?.focus();
  // A click (or keyboard seek) highlights the matching detection below.
  detectionGrid?.highlightNearest(time);
}

function onScrub(time) {
  movePlayhead(time);
  if (player) player.seekTo(time);
}

/** Lightweight ±delta seek within the current segment (no HLS re-attach). */
function seekDelta(deltaMs) {
  // The player's live time tracks the video during playback; otherwise fall
  // back to the last committed playhead.
  const base = player?.now?.() ?? state.playheadTime ?? state.dayModel?.playhead ?? 0;
  const target = base + deltaMs;
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

async function loadDay() {
  const cam = state.cameras.find(c => c.id === state.selectedCameraId);
  const tlBody = $("[data-tl-timeline]");
  if (!cam || !tlBody) return;

  if (!axis) {
    axis = mountTimelineAxis(tlBody, {
      model: emptyDayModel(state.selectedDate),
      zoom: state.zoomLevel,
      onSeek,
      onScrub,
      onZoom,
    });
    registerCleanup(() => {
      axis?.cleanup();
      axis = null;
    });
  }

  try {
    const model = await buildDayModel(cam, state.selectedDate);
    if (!axis) return; // view was torn down mid-fetch
    model.playhead = state.playheadTime ?? model.dayEnd;
    state.dayModel = model;
    axis.setModel(model);
    detectionGrid?.update(model.detections);
  } catch (error) {
    if (axis) axis.setModel({ ...emptyDayModel(state.selectedDate), playhead: state.playheadTime ?? Date.now() });
  }
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
    nowMs: Date.now(),
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

function handleKeydown(event) {
  const action = keyToAction(event);
  if (!action) return;
  event.preventDefault();
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
  wireDetRows();
  wireDate();

  const view = $("[data-tl-view]");
  view?.addEventListener("keydown", handleKeydown);
  registerCleanup(() => {
    view?.removeEventListener("keydown", handleKeydown);
  });

  try {
    const cameras = await loadCameras();
    state.cameras = cameras;
    if (cameras.length) state.selectedCameraId = cameras[0].id;
    const camsBody = $("[data-tl-cams]");
    if (camsBody) {
      registerCleanup(mountCameraList(camsBody, {
        cameras,
        selectedId: state.selectedCameraId,
        onSelect: onCameraSelect,
      }));
    }
    refreshForCamera();
  } catch (error) {
    showError(error.message);
  }
}
