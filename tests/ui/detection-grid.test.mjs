import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");

const source = await readFile(
  new URL("../../src/episode/ui/detection-grid.js", import.meta.url),
  "utf8",
);

// Stub dom.js
const domUrl = moduleUrl(`
  export function escHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
`);

// Stub format.js
const formatUrl = moduleUrl(`
  export function fmtTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    const pad = v => String(v).padStart(2, "0");
    return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }
`);

const gridUrl = moduleUrl(
  source
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js"', JSON.stringify(formatUrl)),
);

const {
  PAGE_SIZE,
  DETECTION_TYPE_LABELS,
  filterDetections,
  relativeTime,
  renderDetectionTile,
  renderFilterBar,
  renderDetectionGrid,
} = await import(gridUrl);

const NOW = 1700000000000;
const DET_A = { id: "ev-1", type: "human_detection", time: NOW - 30000, snapshot: { id: "snap-1" } };
const DET_B = { id: "ev-2", type: "vehicle_detection", time: NOW - 60000, snapshot: null };
const DET_C = { id: "ev-3", type: "motion_detection", time: NOW - 120000 };

test("PAGE_SIZE is 60", () => {
  assert.equal(PAGE_SIZE, 60);
});

test("DETECTION_TYPE_LABELS maps canonical types to display labels", () => {
  assert.equal(DETECTION_TYPE_LABELS.human_detection, "Person");
  assert.equal(DETECTION_TYPE_LABELS.vehicle_detection, "Vehicle");
  assert.equal(DETECTION_TYPE_LABELS.motion_detection, "Motion");
  assert.equal(DETECTION_TYPE_LABELS.doorbell, "Doorbell");
  assert.equal(DETECTION_TYPE_LABELS.door_access, "Door");
  assert.equal(DETECTION_TYPE_LABELS.tamper_detection, "Tamper");
  assert.equal(DETECTION_TYPE_LABELS.manual_trigger, "Manual");
});

test("filterDetections returns only matching types", () => {
  const all = new Set(Object.keys(DETECTION_TYPE_LABELS));
  assert.equal(filterDetections([DET_A, DET_B, DET_C], all).length, 3);
  const onlyHuman = new Set(["human_detection"]);
  const result = filterDetections([DET_A, DET_B, DET_C], onlyHuman);
  assert.equal(result.length, 1);
  assert.equal(result[0].id, "ev-1");
});

test("filterDetections handles empty and null input", () => {
  assert.deepEqual(filterDetections([], new Set(["human_detection"])), []);
  assert.deepEqual(filterDetections(null, new Set(["human_detection"])), []);
  assert.deepEqual(filterDetections(undefined, new Set(["human_detection"])), []);
});

test("relativeTime formats seconds, minutes, hours, days", () => {
  assert.equal(relativeTime(NOW, NOW), "0s ago");
  assert.equal(relativeTime(NOW - 30000, NOW), "30s ago");
  assert.equal(relativeTime(NOW - 60000, NOW), "1m ago");
  assert.equal(relativeTime(NOW - 120000, NOW), "2m ago");
  assert.equal(relativeTime(NOW - 3600000, NOW), "1h ago");
  assert.equal(relativeTime(NOW - 7200000, NOW), "2h ago");
  assert.equal(relativeTime(NOW - 86400000, NOW), "1d ago");
  assert.equal(relativeTime(NOW + 5000, NOW), "upcoming");
});

test("renderDetectionTile produces a button with overlays", () => {
  const html = renderDetectionTile({ ...DET_A, nowMs: NOW });
  assert.ok(html.includes('class="tl-det-tile"'));
  assert.ok(html.includes('data-det-id="ev-1"'));
  assert.ok(html.includes(`data-det-time="${DET_A.time}"`));
  assert.ok(html.includes('data-det-type="human_detection"'));
  assert.ok(html.includes("30s ago"));
  assert.ok(html.includes("Person"));
  assert.ok(html.includes("/api/v1/evidence/snap-1/thumbnail"));
  assert.ok(html.includes('loading="lazy"'));
  assert.ok(html.includes('class="tl-det-time"'));
  assert.ok(html.includes('class="tl-det-type"'));
});

test("renderDetectionTile shows icon fallback when no snapshot", () => {
  const html = renderDetectionTile({ ...DET_B, nowMs: NOW });
  assert.ok(html.includes('data-det-id="ev-2"'));
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("icons.svg#activity"));
  assert.ok(html.includes("Vehicle"));
});

test("renderFilterBar produces toggle buttons with active state", () => {
  const types = ["human_detection", "vehicle_detection"];
  const active = new Set(["human_detection"]);
  const html = renderFilterBar(types, active);
  assert.ok(html.includes('role="group"'));
  assert.ok(html.includes('class="tl-det-filter active" data-filter="human_detection"'));
  assert.ok(html.includes('class="tl-det-filter" data-filter="vehicle_detection"'));
  assert.ok(html.includes('aria-pressed="true"'));
  assert.ok(html.includes('aria-pressed="false"'));
});

test("renderDetectionGrid shows tiles up to visibleCount with sentinel", () => {
  const all = new Set(Object.keys(DETECTION_TYPE_LABELS));
  const html = renderDetectionGrid([DET_A, DET_B, DET_C], all, NOW, { visibleCount: 2 });
  assert.ok(html.includes('role="grid"'));
  assert.ok(html.includes('data-det-id="ev-1"'));
  assert.ok(html.includes('data-det-id="ev-2"'));
  assert.ok(!html.includes('data-det-id="ev-3"'));
  assert.ok(html.includes('data-det-sentinel'));
});

test("renderDetectionGrid shows empty state when no detections match", () => {
  const all = new Set(Object.keys(DETECTION_TYPE_LABELS));
  const html = renderDetectionGrid([], all, NOW);
  assert.ok(html.includes('class="tl-det-empty"'));
  assert.ok(html.includes("No detections"));
});

test("renderDetectionGrid respects active filters", () => {
  const onlyVehicle = new Set(["vehicle_detection"]);
  const html = renderDetectionGrid([DET_A, DET_B, DET_C], onlyVehicle, NOW);
  assert.ok(!html.includes('data-det-id="ev-1"'));
  assert.ok(html.includes('data-det-id="ev-2"'));
  assert.ok(!html.includes('data-det-id="ev-3"'));
});

test("VIRTUAL_THRESHOLD is 200", async () => {
  const { VIRTUAL_THRESHOLD } = await import(gridUrl);
  assert.equal(VIRTUAL_THRESHOLD, 200);
});

test("renderDetectionTile includes data-det-time on time span", () => {
  const html = renderDetectionTile({ ...DET_A, nowMs: NOW });
  assert.ok(html.includes(`data-det-time="${DET_A.time}"`));
});
