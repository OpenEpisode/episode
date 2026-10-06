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
  normalizeTimeRange,
  renderAxis,
  mountTimelineAxis,
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

test("vertical mapping places latest time at the top and earliest at the bottom", () => {
  const dayEnd = DAY_START + 24 * HOUR;
  assert.equal(timeToY(dayEnd, DAY_START, 0, dayEnd), 0);
  assert.equal(timeToY(DAY_START, DAY_START, 0, dayEnd), 24 * ZOOM_LEVELS[0]);
  assert.equal(yToTime(0, DAY_START, 0, dayEnd), dayEnd);
  assert.equal(yToTime(24 * ZOOM_LEVELS[0], DAY_START, 0, dayEnd), DAY_START);
});

test("canvasHeight is 24 hours at the chosen zoom", () => {
  const dayEnd = DAY_START + 24 * HOUR;
  assert.equal(canvasHeight(DAY_START, dayEnd, 0), 24 * ZOOM_LEVELS[0]);
  assert.equal(canvasHeight(DAY_START, dayEnd, 4), 24 * ZOOM_LEVELS[4]);
});

test("normalizeTimeRange orders endpoints regardless of drag direction", () => {
  assert.deepEqual(normalizeTimeRange(20, 10), { start: 10, end: 20 });
  assert.deepEqual(normalizeTimeRange(10, 20), { start: 10, end: 20 });
});

function mockCtx() {
  const calls = { fillRect: [], arc: [], fillText: [] };
  return {
    calls,
    fillStyle: "",
    globalAlpha: 1,
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
      calls.fillRect.push({ x, y, w, h, color: this.fillStyle, alpha: this.globalAlpha });
    },
    setLineDash() {},
    fillText(text, x, y) {
      calls.fillText.push(text);
    },
    setTransform() {},
  };
}

function mountAxisWithMockDom(model, options = {}) {
  const ctx = mockCtx();
  const createElement = () => {
    const listeners = {};
    const classes = new Set();
    return {
      listeners,
      classList: {
        add(name) { classes.add(name); },
        remove(name) { classes.delete(name); },
        toggle(name, force) {
          if (force) classes.add(name);
          else classes.delete(name);
          return classes.has(name);
        },
        contains(name) { return classes.has(name); },
      },
      style: {},
      attrs: {},
      clientWidth: 130,
      className: "",
      setAttribute(name, value) { this.attrs[name] = value; },
      addEventListener(type, handler) { listeners[type] = handler; },
      removeEventListener(type) { delete listeners[type]; },
      appendChild(child) { this.child = child; },
      setPointerCapture() {},
      releasePointerCapture() {},
      getContext() { return ctx; },
      getBoundingClientRect() { return { left: 0, top: 0 }; },
    };
  };
  const oldGlobals = {
    document: globalThis.document,
    window: globalThis.window,
    getComputedStyle: globalThis.getComputedStyle,
    requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame,
  };
  globalThis.document = { createElement };
  globalThis.window = { devicePixelRatio: 1 };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
  globalThis.requestAnimationFrame = () => 1;
  globalThis.cancelAnimationFrame = () => {};
  const container = { innerHTML: "", appendChild(child) { this.child = child; } };
  const axis = mountTimelineAxis(container, { model, ...options });
  return {
    axis,
    container,
    restore() {
      for (const [key, value] of Object.entries(oldGlobals)) {
        if (value === undefined) delete globalThis[key];
        else globalThis[key] = value;
      }
    },
  };
}

test("renderAxis draws an easier-to-hit blue marker per recording segment", () => {
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

  // One enlarged blob per video segment, in the playhead (brand blue) color.
  const blobs = ctx.calls.arc;
  assert.equal(blobs.length, 2);
  for (const blob of blobs) {
    assert.equal(blob.r, 8);
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

test("renderAxis shades the selected range without changing the timeline scale", () => {
  const palette = { accent: "#111", muted: "#222", border: "#333", playhead: "#444", detection: {} };
  const model = {
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 12 * HOUR,
    playhead: DAY_START + 6 * HOUR,
    selectedRange: { start: DAY_START + 2 * HOUR, end: DAY_START + 4 * HOUR },
    segments: [],
    detections: [],
  };
  const ctx = mockCtx();
  renderAxis(ctx, 130, model, 0, palette);

  assert.ok(ctx.calls.fillRect.some(rect =>
    rect.x === 0 && rect.y === 20 * ZOOM_LEVELS[0] && rect.w === 130
    && rect.h === 2 * ZOOM_LEVELS[0] && rect.color === "#444" && rect.alpha === 0.14
  ));
  assert.equal(canvasHeight(model.dayStart, model.dayEnd, 0), 24 * ZOOM_LEVELS[0]);
});

test("dragging the timeline commits a period instead of seeking", () => {
  const selectedRanges = [];
  const soughtTimes = [];
  const { axis, container, restore } = mountAxisWithMockDom({
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 12 * HOUR,
    selectEnd: DAY_START + 12 * HOUR,
    playhead: DAY_START,
    segments: [],
    detections: [],
  }, {
    onRangeChange: range => selectedRanges.push(range),
    onSeek: time => soughtTimes.push(time),
  });
  const wrap = container.child;
  const startY = timeToY(DAY_START + 2 * HOUR, DAY_START, 0);
  const endY = timeToY(DAY_START + 3 * HOUR, DAY_START, 0);
  wrap.listeners.pointerdown({ pointerId: 1, clientX: 60, clientY: startY, shiftKey: false });
  wrap.listeners.pointermove({ pointerId: 1, clientX: 60, clientY: endY });
  wrap.listeners.pointerup({ pointerId: 1, clientX: 60, clientY: endY });

  assert.deepEqual(selectedRanges, [{ start: DAY_START + 2 * HOUR, end: DAY_START + 3 * HOUR }]);
  assert.deepEqual(soughtTimes, []);
  axis.cleanup();
  restore();
});

test("clicking near a recording marker snaps to its start", () => {
  const soughtTimes = [];
  const recordingStart = DAY_START + HOUR;
  const { axis, container, restore } = mountAxisWithMockDom({
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 12 * HOUR,
    selectEnd: DAY_START + 12 * HOUR,
    playhead: DAY_START,
    segments: [{ start: recordingStart, end: recordingStart + 30_000 }],
    detections: [],
  }, { onSeek: time => soughtTimes.push(time) });
  const wrap = container.child;
  const markerY = timeToY(recordingStart, DAY_START, 0);
  wrap.listeners.pointerdown({ pointerId: 2, clientX: LABEL_W + 32, clientY: markerY + 8 });
  wrap.listeners.pointerup({ pointerId: 2, clientX: LABEL_W + 32, clientY: markerY + 8 });

  assert.deepEqual(soughtTimes, [recordingStart]);
  axis.cleanup();
  restore();
});

test("clicking outside the selected interval clears it and still seeks", () => {
  const rangeChanges = [];
  const soughtTimes = [];
  const { axis, container, restore } = mountAxisWithMockDom({
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 12 * HOUR,
    selectEnd: DAY_START + 12 * HOUR,
    playhead: DAY_START,
    selectedRange: { start: DAY_START + 2 * HOUR, end: DAY_START + 3 * HOUR },
    segments: [],
    detections: [],
  }, {
    onRangeChange: range => rangeChanges.push(range),
    onSeek: time => soughtTimes.push(time),
  });
  const wrap = container.child;
  const outsideY = timeToY(DAY_START + 5 * HOUR, DAY_START, 0);
  wrap.listeners.pointerdown({ pointerId: 3, clientX: 70, clientY: outsideY });
  wrap.listeners.pointerup({ pointerId: 3, clientX: 70, clientY: outsideY });

  assert.deepEqual(rangeChanges, [null]);
  assert.deepEqual(soughtTimes, [DAY_START + 5 * HOUR]);
  axis.cleanup();
  restore();
});

test("keyboard navigation follows the reversed vertical time direction", () => {
  const scrubbedTimes = [];
  const { axis, container, restore } = mountAxisWithMockDom({
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 12 * HOUR,
    selectEnd: DAY_START + 12 * HOUR,
    playhead: DAY_START + 6 * HOUR,
    segments: [],
    detections: [],
  }, { onScrub: time => scrubbedTimes.push(time) });
  const wrap = container.child;
  const keydown = key => wrap.listeners.keydown({ key, preventDefault() {} });

  keydown("ArrowUp");
  keydown("PageUp");
  keydown("ArrowDown");
  keydown("PageDown");

  assert.deepEqual(scrubbedTimes, [
    DAY_START + 6 * HOUR + 60_000,
    DAY_START + 6 * HOUR + HOUR,
    DAY_START + 6 * HOUR - 60_000,
    DAY_START + 6 * HOUR - HOUR,
  ]);
  axis.cleanup();
  restore();
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
