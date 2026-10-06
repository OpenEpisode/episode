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
const { isTimelineRecordingPlayable, renderEpisodeContext } = await import(timelineViewUrl);

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

test("episode context renders useful details without a duplicate link", () => {
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
  assert.ok(html.includes("episode-1"));
  assert.ok(html.includes("Front door"));
  assert.ok(html.includes("Human Detection"));
  assert.ok(html.includes("Person detected near the entrance"));
  assert.ok(!html.includes("Open details"));
  assert.ok(html.includes("3 events"));
  assert.ok(html.includes("5 artifacts"));
  assert.ok(html.includes("Human Detection · Motion Detection"));
  assert.ok(html.includes("2 recordings"));
  assert.ok(html.includes("3 snapshots"));
  assert.ok(html.includes("Porta Principal · Garagem Interior"));
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

test("empty timeline playback is a compact message rather than a blank player", () => {
  assert.match(stylesheet, /\.tl-player-stage:has\(\.tl-player-empty:not\(\.hidden\)\)\s*\{[^}]*min-height:\s*104px;[^}]*aspect-ratio:\s*auto;/s);
  assert.match(stylesheet, /\.tl-player-stage:has\(\.tl-player-empty:not\(\.hidden\)\) \.tl-player-video\s*\{\s*display:\s*none;/);
  assert.match(stylesheet, /\.tl-player-wrap:has\(\.tl-player-empty:not\(\.hidden\)\) \.tl-controls-row\s*\{\s*display:\s*none;/);
});
