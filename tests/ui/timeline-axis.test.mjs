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
  timeToX,
  yToTime,
  canvasHeight,
  normalizeTimeRange,
  renderAxis,
  renderAxisHorizontal,
  tickPlan,
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
  const calls = { fillRect: [], arc: [], fillText: [], moveTo: [] };
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
    moveTo(x, y) {
      calls.moveTo.push({ x, y });
    },
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

test("renderAxis draws an thin blue marker per recording segment", () => {
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

  // One thin rectangular marker per video segment, in the playhead (brand
  // blue) color: 16px across the time axis, 4px along it. No circles.
  assert.equal(ctx.calls.arc.length, 0);
  const markers = ctx.calls.fillRect.filter(rect => rect.w === 16 && rect.h === 4);
  assert.equal(markers.length, 2);
  for (const marker of markers) assert.equal(marker.color, "#444");
  const markerCenters = markers.map(marker => marker.y + marker.h / 2);
  assert.ok(markerCenters[0] > markerCenters[1], "earlier segments sit lower on the vertical axis");

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

test("tickPlan refines labels toward 5 minutes and ticks toward 1 minute as zoom increases", () => {
  // Vertical labels need ~22px, horizontal labels ~44px.
  assert.deepEqual(
    [0, 1, 2, 3, 4].map(zoom => tickPlan(zoom, 22)),
    [
      { labelMinutes: 60, minorMinutes: 10 },
      { labelMinutes: 30, minorMinutes: 5 },
      { labelMinutes: 10, minorMinutes: 5 },
      { labelMinutes: 5, minorMinutes: 5 },
      { labelMinutes: 5, minorMinutes: 1 },
    ],
  );
  assert.deepEqual(
    [0, 1, 2, 3, 4].map(zoom => tickPlan(zoom, 44).labelMinutes),
    [60, 60, 30, 10, 5],
  );
});

test("renderAxis adds minute labels between the hours when zoomed in", () => {
  const palette = { accent: "#111", muted: "#222", border: "#333", playhead: "#444", detection: {} };
  const model = {
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 23 * HOUR,
    playhead: DAY_START + 10 * HOUR,
    segments: [],
    detections: [],
  };
  const labelsAt = zoom => {
    const ctx = mockCtx();
    renderAxis(ctx, 130, model, zoom, palette);
    return ctx.calls.fillText.filter(text => /^10:\d\d$/.test(text));
  };
  assert.deepEqual(labelsAt(0), ["10:00"]);
  assert.deepEqual(labelsAt(1), ["10:00", "10:30"]);
  assert.equal(labelsAt(2).length, 6, "one label per 10 minutes");
  assert.equal(labelsAt(4).length, 12, "one label per 5 minutes");
});

test("renderAxis draws a tick for every minute at the highest zoom", () => {
  const palette = { accent: "#111", muted: "#222", border: "#333", playhead: "#444", detection: {} };
  const model = {
    dayStart: DAY_START + 10 * HOUR,
    dayEnd: DAY_START + 11 * HOUR,
    now: DAY_START + 12 * HOUR,
    playhead: DAY_START + 10 * HOUR,
    segments: [],
    detections: [],
  };
  const ticksAt = zoom => {
    const ctx = mockCtx();
    renderAxis(ctx, 130, model, zoom, palette);
    return ctx.calls.moveTo.filter(point => point.x === LABEL_W).length;
  };
  assert.equal(ticksAt(4), 60);
  assert.equal(ticksAt(3), 12, "every 5 minutes one zoom level lower");
});

test("renderAxisHorizontal adds minute labels between the hours when zoomed in", () => {
  const palette = { accent: "#111", muted: "#222", border: "#333", playhead: "#444", detection: {} };
  const model = {
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 23 * HOUR,
    playhead: DAY_START + 10 * HOUR,
    segments: [],
    detections: [],
  };
  const labelsAt = zoom => {
    const ctx = mockCtx();
    renderAxisHorizontal(ctx, Math.round(24 * ZOOM_LEVELS[zoom]), model, zoom, palette);
    return ctx.calls.fillText.filter(text => /^10:\d\d$/.test(text));
  };
  assert.deepEqual(labelsAt(1), ["10:00"]);
  assert.deepEqual(labelsAt(2), ["10:00", "10:30"]);
  assert.equal(labelsAt(3).length, 6, "one label per 10 minutes");
  assert.equal(labelsAt(4).length, 12, "one label per 5 minutes");
});

test("ctrl-wheel zoom keeps the time under the pointer and blocks native scrolling", () => {
  const model = {
    dayStart: DAY_START,
    dayEnd: DAY_START + 24 * HOUR,
    now: DAY_START + 24 * HOUR,
    playhead: DAY_START + 6 * HOUR,
    segments: [],
    detections: [],
  };
  const zooms = [];
  const mounted = mountAxisWithMockDom(model, { zoom: 0, onZoom: zoom => zooms.push(zoom) });
  try {
    mounted.container.scrollTop = 0;
    const canvas = mounted.container.child.child;
    let prevented = 0;
    const wheel = (overrides) => canvas.listeners.wheel({
      ctrlKey: true,
      deltaY: -100,
      clientX: 60,
      clientY: 390, // 10h above the day's end at 39px/h
      preventDefault() { prevented += 1; },
      ...overrides,
    });

    wheel();
    // 10h from the end is 780px at 78px/h; the pointer stayed at 390px.
    assert.equal(mounted.container.scrollTop, 390);
    assert.deepEqual(zooms, [1]);
    assert.equal(prevented, 1);

    // Already at the top zoom level: no change, still no native scroll.
    for (let i = 0; i < 5; i += 1) wheel({ clientY: 0 });
    const scrollAtMax = mounted.container.scrollTop;
    wheel({ clientY: 0 });
    assert.equal(mounted.container.scrollTop, scrollAtMax);
    assert.equal(prevented, 7);

    // Plain wheel scrolls natively and does not zoom.
    const before = zooms.length;
    canvas.listeners.wheel({ ctrlKey: false, metaKey: false, deltaY: -100, preventDefault() { prevented += 1; } });
    assert.equal(zooms.length, before);
    assert.equal(prevented, 7);
  } finally {
    mounted.restore();
  }
});

test("renderAxis labels rolling windows at actual local hour boundaries", () => {
  const palette = { accent: "#111", muted: "#222", border: "#333", playhead: "#444", detection: {} };
  const dayStart = new Date("2026-06-10T11:15:00").getTime();
  const dayEnd = new Date("2026-06-11T11:15:00").getTime();
  const ctx = mockCtx();
  renderAxis(ctx, 130, {
    dayStart,
    dayEnd,
    now: new Date("2026-06-11T10:15:00").getTime(),
    playhead: dayStart,
    segments: [],
    detections: [],
  }, 0, palette);

  const labels = ctx.calls.fillText.filter(text => /^\d\d:00$/.test(text));
  assert.equal(labels[0], "12:00");
  assert.equal(labels.at(-1), "11:00");
  assert.equal(labels.length, 24);
  assert.ok(ctx.calls.fillRect.some(rect => rect.w === 31 && rect.y === 31), "NOW is one hour below the top edge");
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

test("dragging a horizontal timeline never selects a range (native scroll owns the drag)", () => {
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
    horizontal: true,
    onRangeChange: range => selectedRanges.push(range),
    onSeek: time => soughtTimes.push(time),
  });
  const wrap = container.child;
  const startX = timeToX(DAY_START + 2 * HOUR, DAY_START, 0);
  const endX = timeToX(DAY_START + 3 * HOUR, DAY_START, 0);
  wrap.listeners.pointerdown({ pointerId: 1, clientX: startX, clientY: 10 });
  wrap.listeners.pointermove({ pointerId: 1, clientX: endX, clientY: 10 });
  wrap.listeners.pointerup({ pointerId: 1, clientX: endX, clientY: 10 });

  assert.deepEqual(selectedRanges, []);
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
