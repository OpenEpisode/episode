// Tests for keyToAction pure function in timeline-view.js.
// The function maps KeyboardEvent → action string or null.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source => "data:text/javascript;base64," + Buffer.from(source).toString("base64");

// Stub the imports that timeline-view.js needs.
const domUrl = moduleUrl(`
  export const $ = (s, p) => (p || globalThis.document)?.querySelector?.(s) || null;
  export const $$ = (s, p) => (p || globalThis.document)?.querySelectorAll?.(s) || [];
  export function escHtml(v) { return String(v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
`);
const apiUrl = moduleUrl(`export async function api(p) { return []; }`);
const viewUrl = moduleUrl(`
  export function showContent(h) {}
  export function showLoading() {}
  export function showError(m) {}
`);
const cameraUrl = moduleUrl(`export function mountCameraList() { return () => {}; }`);
const axisUrl = moduleUrl(`export const ZOOM_LEVELS = [39,78,156,312,1248]; export const ZOOM_LABELS = ["24h","12h","6h","1h","15min"]; export function normalizeTimeRange(start,end) { return {start:Math.min(start,end),end:Math.max(start,end)}; } export function mountTimelineAxis() { return { cleanup(){} }; }`);
const playerUrl = moduleUrl(`export const SPEEDS = [0.5,1,2,4,8,16]; export function mountPlayer() { return { cleanup(){} }; } export function findSegment() { return null; }`);
const timelineUrl = moduleUrl(`export function recordingBounds() { return { start: 0, end: 0 }; }`);
const gridUrl = moduleUrl(`export const DETECTION_TYPE_LABELS = { human_detection: "Person" }; export function mountDetectionGrid() { return { cleanup(){} }; }`);
const mediaUrl = moduleUrl(`export function isPlayableVideoEvidence() { return true; }`);

const source = await readFile(new URL("../../src/episode/ui/timeline-view.js", import.meta.url), "utf8");

const tlUrl = moduleUrl(
  source
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./api.js"', JSON.stringify(apiUrl))
    .replace('"./view.js"', JSON.stringify(viewUrl))
    .replace('"./camera-list.js"', JSON.stringify(cameraUrl))
    .replace('"./timeline-axis.js"', JSON.stringify(axisUrl))
    .replace('"./player-controls.js"', JSON.stringify(playerUrl))
    .replace('"./timeline.js"', JSON.stringify(timelineUrl))
    .replace('"./detection-grid.js"', JSON.stringify(gridUrl))
    .replace('"./media-player.js?v=8"', JSON.stringify(mediaUrl)),
);

const { keyToAction, handleKeydown } = await import(tlUrl);

// Helper: create a mock KeyboardEvent.
function key(k, { target, shiftKey = false } = {}) {
  return {
    key: k,
    shiftKey,
    target: target || { closest: () => null },
    preventDefault: () => {},
  };
}

test("Space → play", () => {
  assert.equal(keyToAction(key(" ")), "play");
});

test("Space on the focused video toggles play via the timeline handler", () => {
  const video = { closest: selector => selector === ".tl-player-video" ? video : null };
  assert.equal(keyToAction(key(" ", { target: video })), "play");

  let defaultPrevented = false;
  let stopped = false;
  handleKeydown({
    ...key(" ", { target: video }),
    preventDefault: () => { defaultPrevented = true; },
    stopPropagation: () => { stopped = true; },
  });
  assert.equal(defaultPrevented, true);
  assert.equal(stopped, true);
});

test("ArrowLeft → back-5", () => {
  assert.equal(keyToAction(key("ArrowLeft")), "back-5");
});

test("Shift+ArrowLeft → frame-back", () => {
  assert.equal(keyToAction(key("ArrowLeft", { shiftKey: true })), "frame-back");
});

test("ArrowRight → fwd-5", () => {
  assert.equal(keyToAction(key("ArrowRight")), "fwd-5");
});

test("Shift+ArrowRight → frame-fwd", () => {
  assert.equal(keyToAction(key("ArrowRight", { shiftKey: true })), "frame-fwd");
});

test("ArrowUp → prev-event", () => {
  assert.equal(keyToAction(key("ArrowUp")), "prev-event");
});

test("ArrowDown → next-event", () => {
  assert.equal(keyToAction(key("ArrowDown")), "next-event");
});

test("l / L are not mapped (live view removed)", () => {
  assert.equal(keyToAction(key("l")), null);
  assert.equal(keyToAction(key("L")), null);
});

test("f → fullscreen", () => {
  assert.equal(keyToAction(key("f")), "fullscreen");
});

test("m → mute", () => {
  assert.equal(keyToAction(key("m")), "mute");
});

test("+ → zoom-in", () => {
  assert.equal(keyToAction(key("+")), "zoom-in");
});

test("= → zoom-in (shift+=)", () => {
  assert.equal(keyToAction(key("=")), "zoom-in");
});

test("- → zoom-out", () => {
  assert.equal(keyToAction(key("-")), "zoom-out");
});

test("_ → zoom-out (shift+-)", () => {
  assert.equal(keyToAction(key("_")), "zoom-out");
});

test("[ → speed-down", () => {
  assert.equal(keyToAction(key("[")), "speed-down");
});

test("] → speed-up", () => {
  assert.equal(keyToAction(key("]")), "speed-up");
});

test("unmapped key → null", () => {
  assert.equal(keyToAction(key("a")), null);
});

test("target is input → null", () => {
  const target = { closest: sel => (sel.includes("input") ? "input" : null) };
  assert.equal(keyToAction(key(" ", { target })), null);
});

test("target is select → null", () => {
  const target = { closest: sel => (sel.includes("select") ? "select" : null) };
  assert.equal(keyToAction(key(" ", { target })), null);
});

test("target is button → null", () => {
  const target = { closest: sel => (sel.includes("button") ? "button" : null) };
  assert.equal(keyToAction(key(" ", { target })), null);
});

test("target is .tl-axis → null", () => {
  const target = { closest: sel => (sel.includes(".tl-axis") ? ".tl-axis" : null) };
  assert.equal(keyToAction(key("ArrowLeft", { target })), null);
});

test("date selector uses DD/MM/YYYY representation", async () => {
  const { fmtDateDMY, parseDateDMY } = await import(tlUrl);
  assert.equal(fmtDateDMY("2026-10-05"), "05/10/2026");
  assert.equal(parseDateDMY("05/10/2026"), "2026-10-05");
  assert.equal(parseDateDMY("5/10/26"), null);
  assert.equal(parseDateDMY("5/10/2026"), "2026-10-05");
  assert.equal(parseDateDMY(" 05/10/2026 "), "2026-10-05");
  assert.equal(parseDateDMY("31/02/2026"), null); // invalid calendar date
  assert.equal(parseDateDMY("2026-10-05"), null); // ISO is not accepted
  assert.equal(parseDateDMY("abc"), null);
  assert.equal(parseDateDMY(""), null);
});
