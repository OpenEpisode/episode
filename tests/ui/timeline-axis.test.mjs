import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../../src/episode/ui/timeline-axis.js", import.meta.url),
  "utf8",
);
const moduleUrl = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const {
  ZOOM_LEVELS,
  ZOOM_LABELS,
  LABEL_W,
  timeToY,
  yToTime,
  canvasHeight,
  renderAxis,
} = await import(moduleUrl);

const HOUR = 3600000;
const DAY_START = new Date("2026-06-10T00:00:00").getTime();

test("zoom levels are five increasing pixels-per-hour steps", () => {
  assert.equal(ZOOM_LEVELS.length, 5);
  assert.deepEqual(ZOOM_LABELS, ["24h", "12h", "6h", "1h", "15min"]);
  for (let i = 1; i < ZOOM_LEVELS.length; i += 1) {
    assert.ok(ZOOM_LEVELS[i] > ZOOM_LEVELS[i - 1]);
  }
  assert.ok(LABEL_W > 0);
});

test("time<->pixel mapping round-trips", () => {
  for (const zoom of [0, 2, 4]) {
    for (const offsetMs of [0, 15 * 60000, HOUR, 12 * HOUR, 23.5 * HOUR]) {
      const time = DAY_START + offsetMs;
      const y = timeToY(time, DAY_START, zoom);
      assert.ok(Math.abs(yToTime(y, DAY_START, zoom) - time) < 0.001);
    }
  }
});

test("canvasHeight is 24 hours at the chosen zoom", () => {
  const dayEnd = DAY_START + 24 * HOUR;
  assert.equal(canvasHeight(DAY_START, dayEnd, 0), 24 * ZOOM_LEVELS[0]);
  assert.equal(canvasHeight(DAY_START, dayEnd, 4), 24 * ZOOM_LEVELS[4]);
});

function mockCtx() {
  const calls = { fillRect: [], arc: [], fillText: [] };
  return {
    calls,
    fillStyle: "",
    strokeStyle: "",
    font: "",
    textBaseline: "",
    lineWidth: 1,
    clearRect() {},
    beginPath() {},
    moveTo() {},
    lineTo() {},
    stroke() {},
    fill() {},
    arc(x, y, r) {
      calls.arc.push({ x, y, r, color: this.fillStyle });
    },
    fillRect(x, y, w, h) {
      calls.fillRect.push({ x, y, w, h, color: this.fillStyle });
    },
    setLineDash() {},
    fillText(text, x, y) {
      calls.fillText.push(text);
    },
    setTransform() {},
  };
}

test("renderAxis draws a blue blob per recording segment and no detection dots", () => {
  const palette = {
    accent: "#111",
    muted: "#222",
    border: "#333",
    playhead: "#444",
    detection: { human_detection: "#555" },
  };
  const model = {
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 12 * HOUR,
    playhead: DAY_START + 6 * HOUR,
    segments: [
      { start: DAY_START + HOUR, end: DAY_START + 2 * HOUR, id: "s1" },
      { start: DAY_START + 5 * HOUR, end: DAY_START + 6 * HOUR, id: "s2" },
    ],
    detections: [
      { time: DAY_START + 3 * HOUR, type: "human_detection", id: "d1" },
      { time: DAY_START + 4 * HOUR, type: "unknown", id: "d2" },
    ],
  };

  const ctx = mockCtx();
  renderAxis(ctx, 130, model, 0, palette);

  // One 6px-radius blob per video segment, in the playhead (brand blue) color.
  const blobs = ctx.calls.arc;
  assert.equal(blobs.length, 2);
  for (const blob of blobs) {
    assert.equal(blob.r, 6);
    assert.equal(blob.color, "#444");
  }

  // No thin 4px-wide segment bars remain.
  assert.equal(ctx.calls.fillRect.filter(rect => rect.w === 4).length, 0);

  // Hour labels are drawn (24 of them, "00:00".."23:00").
  assert.equal(ctx.calls.fillText.filter(text => /^\d\d:00$/.test(text)).length, 24);

  // NOW marker: a 31x16 label pill sits right after the blob track on the
  // left side of the axis, so it is never clipped by the canvas edge.
  const pill = ctx.calls.fillRect.find(rect => rect.w === 31 && rect.h === 16);
  assert.ok(pill, "expected the NOW label pill");
  assert.ok(pill.x >= LABEL_W, "NOW pill stays right of the hour-label column");
  assert.ok(pill.x < 130 - 40, "NOW pill stays clear of the canvas right edge");
  assert.ok(ctx.calls.fillText.includes("NOW"));
});

test("renderAxis clamps the playhead handle within the canvas", () => {
  const palette = { accent: "#111", muted: "#222", border: "#333", playhead: "#444", detection: {} };
  const model = {
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 25 * HOUR, // now is outside the day -> no NOW pill
    playhead: DAY_START + 30 * HOUR, // playhead beyond the day -> clamped
    segments: [],
    detections: [],
  };
  const ctx = mockCtx();
  renderAxis(ctx, 130, model, 0, palette);
  // Playhead handle is a 6px-wide fillRect at the clamped bottom edge.
  const handle = ctx.calls.fillRect.find(rect => rect.w === 6);
  assert.ok(handle, "expected the playhead handle");
  const maxHandleY = canvasHeight(model.dayStart, model.dayEnd, 0);
  assert.ok(handle.y <= maxHandleY + 1);
});
