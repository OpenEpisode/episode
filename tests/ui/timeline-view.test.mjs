import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/timeline-view.js", import.meta.url),
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
  export const SPEEDS = [1];
  export function recordingBounds() { return { start: 0, end: 1 }; }
  export function mountDetectionGrid() { return {}; }
  export const DETECTION_TYPE_LABELS = {};
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
const { isTimelineRecordingPlayable } = await import(timelineViewUrl);

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
