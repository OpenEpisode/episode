import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/timeline-view.js", import.meta.url),
  "utf8",
);
const stylesheet = await readFile(
  new URL("../../src/episode/ui/timeline-view.css", import.meta.url),
  "utf8",
);
const emptyUrl = moduleUrl(`
  export const $ = () => null;
  export const escHtml = value => String(value ?? "");
  export async function api() { return []; }
  export function showContent() {}
  export function showLoading() {}
  export function showError() {}
  export function mountCameraList() { return () => {}; }
  export function mountTimelineAxis() { return {}; }
  export function normalizeTimeRange(start, end) { return { start: Math.min(start, end), end: Math.max(start, end) }; }
  export const ZOOM_LEVELS = [1];
  export const ZOOM_LABELS = ["24h"];
  export function mountPlayer() { return {}; }
  export function findSegment() { return null; }
  export function selectRecordingForTime() { return null; }
  export const SPEEDS = [1];
  export function recordingBounds() { return { start: 0, end: 1 }; }
  export function mountDetectionGrid() { return {}; }
  export const DETECTION_TYPE_LABELS = {};
  export const DEFAULT_DETECTION_LIMIT = 50;
  export const DETECTION_LIMIT_OPTIONS = [25, 50, 100, 200, "ALL"];
`);
const mediaUrl = moduleUrl(`
  export function isPlayableVideoEvidence(item) {
    if (item?.mime_type?.startsWith("video/")) return true;
    return item?.metadata?.format === "hls-fmp4"
      && item.metadata?.playlist_validation?.valid !== false;
  }
`);
const timelineViewUrl = moduleUrl(
  source
    .replaceAll('"./dom.js"', JSON.stringify(emptyUrl))
    .replaceAll('"./api.js"', JSON.stringify(emptyUrl))
    .replaceAll('"./view.js"', JSON.stringify(emptyUrl))
    .replaceAll('"./camera-list.js"', JSON.stringify(emptyUrl))
    .replaceAll('"./timeline-axis.js"', JSON.stringify(emptyUrl))
    .replaceAll('"./player-controls.js"', JSON.stringify(emptyUrl))
    .replaceAll('"./timeline.js"', JSON.stringify(emptyUrl))
    .replaceAll('"./detection-grid.js"', JSON.stringify(emptyUrl))
    .replaceAll('"./media-player.js?v=8"', JSON.stringify(mediaUrl)),
);

const browserHarnessUrl = moduleUrl(`
  const elements = new Map();
  const calls = { api: [], markup: "", player: null, cameraOptions: null };
  class Element {
    constructor() {
      this.dataset = {};
      this.style = {};
      this.listeners = new Map();
      this.attributes = new Map();
      this.classList = { toggle() {} };
      this.offsetHeight = 44;
      this.value = "";
      this.disabled = false;
    }
    addEventListener(name, callback) {
      const listeners = this.listeners.get(name) || [];
      listeners.push(callback);
      this.listeners.set(name, listeners);
    }
    removeEventListener(name, callback) {
      const listeners = this.listeners.get(name) || [];
      const remaining = listeners.filter(listener => listener !== callback);
      if (remaining.length) this.listeners.set(name, remaining);
      else this.listeners.delete(name);
    }
    async dispatch(name, event) {
      for (const callback of this.listeners.get(name) || []) await callback(event);
    }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    closest(selector) {
      return selector === "[data-tl-action]" && this.dataset.tlAction ? this : null;
    }
    showPicker() {}
  }
  const view = new Element();
  for (const selector of [
    "[data-tl-view]", ".timeline-view", "[data-tl-cams]", "[data-tl-timeline]",
    "[data-tl-player]", "[data-tl-detections]", ".tl-player-stage",
    ".tl-episode-context", "[data-tl-date]", "[data-tl-date-picker]",
    "[data-tl-date-cal]", "[data-tl-zoom-label]",
  ]) elements.set(selector, selector === "[data-tl-view]" || selector === ".timeline-view" ? view : new Element());
  let apiHandler = async () => [];
  let cameraOptions = null;
  export function $(selector) { return elements.get(selector) || null; }
  export function escHtml(value) { return String(value ?? ""); }
  export function element(selector) { return elements.get(selector) || null; }
  export function setApiHandler(handler) { apiHandler = handler; }
  export async function api(path) { calls.api.push(path); return apiHandler(path); }
  export function apiCalls() { return [...calls.api]; }
  export function showContent(markup) { calls.markup = markup; }
  export function markup() { return calls.markup; }
  export function showLoading() {}
  export function showError(error) { calls.error = error; }
  export function mountCameraList(_element, options) {
    cameraOptions = options;
    return () => {};
  }
  export function cameraListOptions() { return cameraOptions; }
  export function mountTimelineAxis() { return { setModel() {}, setZoom() {}, cleanup() {} }; }
  export function normalizeTimeRange(start, end) { return { start: Math.min(start, end), end: Math.max(start, end) }; }
  export const ZOOM_LEVELS = [1];
  export const ZOOM_LABELS = ["24h"];
  export const SPEEDS = [1];
  export function mountPlayer() {
    calls.player = { segments: [], emptyMessages: [], current: null };
    return {
      currentSegment: () => calls.player.current,
      loadSegment(segment, time) {
        calls.player.segments.push({ segment, time });
        calls.player.current = segment;
      },
      setEmptyMessage(message) { calls.player.emptyMessages.push(message); },
      cleanup() {},
    };
  }
  export function playerState() { return calls.player; }
  export function recordingBounds(item) {
    const start = new Date(item.metadata?.started_at || item.timestamp).getTime();
    const end = item.metadata?.ended_at
      ? new Date(item.metadata.ended_at).getTime()
      : start + Number(item.metadata?.duration_seconds || 1) * 1000;
    return { start, end };
  }
  export function selectRecordingForTime() { return null; }
  export function selectRecordingForRange() { return null; }
  export function selectRecordingForTimeInRange() { return null; }
  export function mountDetectionGrid() {
    return { update() {}, setTimeRange() {}, setLimit() {}, highlightNearest() {}, cleanup() {} };
  }
  export const DETECTION_TYPE_LABELS = {};
  export const DEFAULT_DETECTION_LIMIT = 50;
  export const DETECTION_LIMIT_OPTIONS = [25, 50, 100, 200, "ALL"];
`);
const interactiveTimelineUrl = moduleUrl(
  source
    .replaceAll('"./dom.js"', JSON.stringify(browserHarnessUrl))
    .replaceAll('"./api.js"', JSON.stringify(browserHarnessUrl))
    .replaceAll('"./view.js"', JSON.stringify(browserHarnessUrl))
    .replaceAll('"./camera-list.js"', JSON.stringify(browserHarnessUrl))
    .replaceAll('"./timeline-axis.js"', JSON.stringify(browserHarnessUrl))
    .replaceAll('"./player-controls.js"', JSON.stringify(browserHarnessUrl))
    .replaceAll('"./timeline.js"', JSON.stringify(browserHarnessUrl))
    .replaceAll('"./detection-grid.js"', JSON.stringify(browserHarnessUrl))
    .replaceAll('"./media-player.js?v=8"', JSON.stringify(mediaUrl)),
);
const {
  calculateTopRowHeight,
  fmtDateDMY,
  isTimelineRecordingPlayable,
  renderEpisodeContext,
  timelineBounds,
} = await import(timelineViewUrl);

test("timeline excludes expired and invalid recording evidence", () => {
  assert.equal(isTimelineRecordingPlayable({
    evidence_type: "recording",
    mime_type: "video/mp4",
  }), true);
  assert.equal(isTimelineRecordingPlayable({
    evidence_type: "recording",
    mime_type: "application/vnd.apple.mpegurl",
    metadata: { format: "hls-fmp4", playlist_validation: { valid: true } },
  }), true);
  assert.equal(isTimelineRecordingPlayable({
    evidence_type: "recording",
    mime_type: "video/mp4",
    availability: "expired",
  }), false);
  assert.equal(isTimelineRecordingPlayable({
    evidence_type: "recording",
    mime_type: "application/vnd.apple.mpegurl",
    metadata: { format: "hls-fmp4", playlist_validation: { valid: false } },
  }), false);
  assert.equal(isTimelineRecordingPlayable({
    evidence_type: "incomplete_recording",
    mime_type: "video/mp4",
  }), false);
});

test("episode context renders a compact columns card with only the key details", () => {
  const html = renderEpisodeContext({
    id: "episode-1",
    primary_area_id: "area-1",
    start_time: "2026-10-06T10:00:00Z",
    end_time: "2026-10-06T10:05:00Z",
    state: "closed",
    trigger_type: "human_detection",
    summary: "Person detected near the entrance",
    event_count: 3,
    evidence_count: 5,
  }, "Front door", {
    eventTypes: ["human_detection", "motion_detection"],
    recordingCount: 2,
    snapshotCount: 3,
    deviceNames: ["Porta Principal", "Garagem Interior"],
  });
  assert.ok(html.includes("Episode details"));
  assert.ok(html.includes("Front door")); // area
  assert.ok(html.includes("Porta Principal · Garagem Interior")); // cameras
  assert.ok(html.includes("2026-10-06")); // start date
  assert.ok(html.includes("2026-10-06")); // end date
  assert.ok(html.includes("3 events")); // event count
  assert.ok(html.includes("Human Detection · Motion Detection")); // detection types
  // The compact layout drops the summary, trigger, artifact counts and episode id.
  assert.ok(!html.includes("Person detected near the entrance"));
  assert.ok(!html.includes("5 artifacts"));
  assert.ok(!html.includes("episode-1"));
});

test("detections size to their content and cap at four rows", () => {
  const layoutRule = stylesheet.match(/\.timeline-view\s*\{([^}]*)\}/)?.[1] || "";
  const bodyRule = stylesheet.match(/\.tl-det-body\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(layoutRule, /align-content:\s*start;/);
  assert.match(bodyRule, /height:\s*auto;/);
  assert.match(bodyRule, /max-height:\s*calc\(4 \* var\(--det-row-height/);
  assert.doesNotMatch(bodyRule, /(?:^|\n)\s*height:\s*calc\(4 \* var\(--det-row-height/);
});

test("episode context renders a clear empty state", () => {
  const html = renderEpisodeContext(null);
  assert.ok(html.includes("Select a recording"));
});

test("episode context explains when a selected recording has no Episode", () => {
  const html = renderEpisodeContext(null, null, { recordingSelected: true });
  assert.ok(html.includes("isn't linked to an Episode"));
});

test("top timeline panels keep a usable height when the player has no recording", () => {
  assert.equal(calculateTopRowHeight({ head: 32, stage: 240, controls: 0, context: 70 }), 420);
  assert.equal(calculateTopRowHeight({ head: 32, stage: 500, controls: 48, context: 120 }), 702);
});

test("timeline opens today and Latest recording explicitly jumps to available footage", async () => {
  const originalWindow = globalThis.window;
  const originalDocument = globalThis.document;
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const timers = [];
  const clearedTimers = [];
  const documentListeners = new Map();
  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  globalThis.setInterval = (callback, delay) => {
    timers.push({ callback, delay });
    return timers.length;
  };
  globalThis.clearInterval = id => clearedTimers.push(id);
  globalThis.document = {
    visibilityState: "visible",
    addEventListener(name, callback) { documentListeners.set(name, callback); },
    removeEventListener(name, callback) { documentListeners.delete(name); },
  };

  const harness = await import(browserHarnessUrl);
  const { cleanupTimeline, renderTimeline } = await import(interactiveTimelineUrl);
  const camera = { id: "camera-1", name: "Front camera", device_type: "camera", area_id: "area-1" };
  const recordingDate = new Date();
  recordingDate.setDate(recordingDate.getDate() - 1);
  recordingDate.setHours(12, 0, 0, 0);
  const recordingDateLabel = [
    String(recordingDate.getDate()).padStart(2, "0"),
    String(recordingDate.getMonth() + 1).padStart(2, "0"),
    recordingDate.getFullYear(),
  ].join("/");
  const recording = {
    id: "recording-yesterday",
    device_id: camera.id,
    episode_id: "episode-1",
    evidence_type: "recording",
    mime_type: "video/mp4",
    timestamp: recordingDate.toISOString(),
    metadata: { started_at: recordingDate.toISOString(), duration_seconds: 60 },
  };
  let availableRecordings = [recording];
  harness.setApiHandler(async path => {
    if (path.startsWith("/devices")) return [camera];
    if (path.startsWith("/episodes?")) return [];
    if (path.startsWith("/evidence?evidence_type=recording")) return availableRecordings;
    return [];
  });

  try {
    await renderTimeline();
    await new Promise(resolve => setImmediate(resolve));

    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    const dateInput = harness.element("[data-tl-date]");
    assert.equal(dateInput.value, fmtDateDMY(today));
    assert.equal(timers.length, 1);
    assert.equal(timers[0].delay, 60_000);
    assert.equal(
      harness.apiCalls().some(path => path.startsWith("/evidence?evidence_type=recording")),
      false,
      "loading today's empty timeline should not search backward for an older recording",
    );
    assert.equal(harness.playerState().emptyMessages.at(-1), "No recordings available for the selected cameras");
    assert.match(harness.markup(), /data-tl-action="latest-recording"[^>]*>\s*Latest recording\s*</);

    const episodeRequestCount = () => harness.apiCalls().filter(path => path.startsWith("/episodes?limit=200")).length;
    const beforeHiddenRefresh = episodeRequestCount();
    globalThis.document.visibilityState = "hidden";
    timers[0].callback();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(episodeRequestCount(), beforeHiddenRefresh, "hidden tabs do not refresh");

    globalThis.document.visibilityState = "visible";
    documentListeners.get("visibilitychange")?.();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(episodeRequestCount(), beforeHiddenRefresh + 1, "returning to a visible tab refreshes Today");

    const latestButton = {
      dataset: { tlAction: "latest-recording" },
      disabled: false,
      attributes: new Map(),
      closest: selector => selector === "[data-tl-action]" ? latestButton : null,
      setAttribute(name, value) { this.attributes.set(name, value); },
      removeAttribute(name) { this.attributes.delete(name); },
    };
    const clickTarget = {
      closest: selector => selector === "[data-tl-action]" ? latestButton : null,
    };
    await harness.element("[data-tl-view]").dispatch("click", { target: clickTarget });

    assert.equal(dateInput.value, recordingDateLabel, JSON.stringify(harness.apiCalls()));
    assert.equal(harness.playerState().segments.at(-1).segment.id, recording.id);
    assert.equal(harness.playerState().segments.at(-1).time, new Date(recording.timestamp).getTime());
    assert.equal(latestButton.disabled, false);
    assert.equal(latestButton.attributes.has("aria-busy"), false);

    availableRecordings = [];
    const dateBeforeEmptySearch = dateInput.value;
    await harness.element("[data-tl-view]").dispatch("click", { target: clickTarget });
    assert.equal(dateInput.value, dateBeforeEmptySearch, "an empty latest search leaves the selected day unchanged");
    assert.equal(harness.playerState().emptyMessages.at(-1), "No recordings available for the selected cameras");
    const beforeHistoricalRefresh = episodeRequestCount();
    timers[0].callback();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(episodeRequestCount(), beforeHistoricalRefresh, "historical dates do not auto-refresh");
  } finally {
    cleanupTimeline();
    assert.deepEqual(clearedTimers, [1]);
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
    if (originalSetInterval === undefined) delete globalThis.setInterval;
    else globalThis.setInterval = originalSetInterval;
    if (originalClearInterval === undefined) delete globalThis.clearInterval;
    else globalThis.clearInterval = originalClearInterval;
  }
});

test("empty timeline playback reserves a 16:9 viewing area", () => {
  assert.match(stylesheet, /\.tl-player-stage:has\(\.tl-player-empty:not\(\.hidden\)\)\s*\{[^}]*min-height:\s*240px;[^}]*aspect-ratio:\s*16 \/ 9;/s);
  assert.match(stylesheet, /\.tl-player-stage:has\(\.tl-player-empty:not\(\.hidden\)\) \.tl-player-video\s*\{\s*display:\s*none;/);
  assert.match(stylesheet, /\.tl-player-wrap:has\(\.tl-player-empty:not\(\.hidden\)\) \.tl-controls-row\s*\{\s*display:\s*none;/);
});

test("today uses a rolling 24-hour window with one hour ahead", () => {
  const now = new Date("2026-10-07T15:30:00").getTime();
  const bounds = timelineBounds("2026-10-07", { now, followingToday: true });
  assert.equal(bounds.dayStart, now - 23 * 60 * 60 * 1000);
  assert.equal(bounds.dayEnd, now + 60 * 60 * 1000);
});

test("historical dates retain local calendar-day bounds", () => {
  const now = new Date("2026-10-07T15:30:00").getTime();
  const bounds = timelineBounds("2026-10-06", { now, followingToday: false });
  const start = new Date("2026-10-06T00:00:00").getTime();
  const endDate = new Date("2026-10-06T00:00:00");
  endDate.setDate(endDate.getDate() + 1);
  assert.equal(bounds.dayStart, start);
  assert.equal(bounds.dayEnd, endDate.getTime());
});
