import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");

const source = await readFile(
  new URL("../../src/episode/ui/player-controls.js", import.meta.url),
  "utf8",
);

// Stub media-player.js (imported by player-controls.js).
const mediaUrl = moduleUrl(`
  export function attachMediaSource(video, url, opts) {
    globalThis.mediaAttachCalls = globalThis.mediaAttachCalls || [];
    globalThis.mediaAttachCalls.push({ url, live: opts?.live });
    globalThis.mediaStateCallbacks = globalThis.mediaStateCallbacks || [];
    if (opts?.onState) globalThis.mediaStateCallbacks.push(opts.onState);
    opts?.onState?.({ state: "loading", message: "Loading recording…" });
    return () => {};
  }
  export function evidenceMediaUrl(evidence) {
    return evidence?.metadata?.format === "hls-fmp4"
      ? "/api/v1/recordings/" + encodeURIComponent(evidence.id) + "/index.m3u8"
      : "/api/v1/evidence/" + encodeURIComponent(evidence.id) + "/file";
  }
  export function updateMediaStatus(el, opts) {
    if (el) {
      const visible = !["ready", "idle"].includes(opts.state);
      el.className = "media-playback-status media-state-" + opts.state + (visible ? "" : " hidden");
      el.textContent = opts.message || "";
    }
  }
`);

const playerUrl = moduleUrl(
  source.replace('"./media-player.js?v=8"', JSON.stringify(mediaUrl)),
);

const {
  seekOffset,
  findSegment,
  formatTime,
  renderControls,
  mountPlayer,
  SPEEDS,
} = await import(playerUrl);

const SEG_A = { start: 1000, end: 5000, id: "seg-a", metadata: { format: "hls-fmp4" } };
const SEG_B = { start: 8000, end: 12000, id: "seg-b", metadata: { format: "hls-fmp4" } };

test("seekOffset returns 0 when playhead is before segment start", () => {
  assert.equal(seekOffset(500, 1000), 0);
  assert.equal(seekOffset(0, 1000), 0);
});

test("seekOffset returns correct seconds when playhead is inside segment", () => {
  assert.equal(seekOffset(3000, 1000), 2); // 2 s in
  assert.equal(seekOffset(1000, 1000), 0); // at start
  assert.equal(seekOffset(4999, 1000), 3.999); // near end
});

test("seekOffset clamps to 0 for negative offsets", () => {
  assert.equal(seekOffset(500, 1000), 0);
  assert.equal(seekOffset(-100, 0), 0);
});

test("findSegment returns the containing segment", () => {
  assert.equal(findSegment([SEG_A, SEG_B], 3000), SEG_A);
  assert.equal(findSegment([SEG_A, SEG_B], 9000), SEG_B);
});

test("findSegment returns nearest segment when between gaps", () => {
  // 6500 is 1500 from A.end (5000) and 1500 from B.start (8000) → either is fine
  const result = findSegment([SEG_A, SEG_B], 6500);
  assert.ok(result === SEG_A || result === SEG_B);
  // 5100 is 100 from A.end and 2900 from B.start → A
  assert.equal(findSegment([SEG_A, SEG_B], 5100), SEG_A);
  // 7900 is 2900 from A.end and 100 from B.start → B
  assert.equal(findSegment([SEG_A, SEG_B], 7900), SEG_B);
});

test("findSegment returns null for empty segments", () => {
  assert.equal(findSegment([], 3000), null);
  assert.equal(findSegment(null, 3000), null);
  assert.equal(findSegment(undefined, 3000), null);
});

test("formatTime formats seconds correctly", () => {
  assert.equal(formatTime(0), "0:00");
  assert.equal(formatTime(5), "0:05");
  assert.equal(formatTime(60), "1:00");
  assert.equal(formatTime(65), "1:05");
  assert.equal(formatTime(3600), "1:00:00");
  assert.equal(formatTime(3665), "1:01:05");
  assert.equal(formatTime(-1), "0:00");
  assert.equal(formatTime(NaN), "0:00");
});

test("SPEEDS contains the expected playback rates", () => {
  assert.deepEqual(SPEEDS, [0.5, 1, 2, 4, 8, 16]);
});

test("renderControls produces a toolbar with all buttons", () => {
  const html = renderControls({ isPlaying: false, speed: 1 });
  assert.ok(html.includes('role="toolbar"'));
  assert.ok(html.includes('data-ctl="playpause"'));
  assert.ok(html.includes('data-ctl="back10"'));
  assert.ok(html.includes('data-ctl="fwd10"'));
  assert.ok(html.includes('data-ctl="speed"'));
  assert.ok(html.includes('data-ctl="mute"'));
  assert.ok(html.includes('data-ctl="fullscreen"'));
  assert.ok(html.includes('data-ctl="export"'));
  assert.ok(!html.includes('data-ctl="live"'));
  assert.ok(html.includes('data-ctl-time'));
  // Play icon when not playing
  assert.ok(html.includes('icons.svg#play'));
  assert.ok(!html.includes('icons.svg#pause'));
});

test("renderControls toggles play/pause icon and active states", () => {
  const playing = renderControls({ isPlaying: true, isMuted: true, speed: 4 });
  assert.ok(playing.includes('icons.svg#pause'));
  assert.ok(!playing.includes('icons.svg#play'));
  assert.ok(playing.includes('class="tl-ctl-btn active" data-ctl="mute"'));
  assert.ok(playing.includes('value="4" selected'));
  assert.ok(!playing.includes('data-ctl="live"'));
});

test("clicking the video gives it keyboard focus for playback shortcuts", () => {
  const listeners = new Map();
  const focusCalls = [];
  const video = {
    paused: true,
    currentTime: 0,
    playbackRate: 1,
    muted: false,
    addEventListener: (type, listener) => listeners.set(type, listener),
    removeEventListener: type => listeners.delete(type),
    focus: options => focusCalls.push(options),
    removeAttribute() {},
    load() {},
  };
  const classList = { add() {}, remove() {} };
  const emptyMessage = { textContent: "" };
  const empty = { classList, querySelector: () => emptyMessage };
  const controls = { addEventListener() {}, querySelector: () => null, innerHTML: "" };
  const episodeLink = { classList };
  const stage = { offsetHeight: 0, style: { removeProperty() {} } };
  const container = {
    innerHTML: "",
    querySelector(selector) {
      return {
        ".tl-player-video": video,
        ".tl-media-status": {},
        ".tl-controls-wrap": controls,
        ".tl-player-empty": empty,
        ".tl-episode-link": episodeLink,
        ".tl-player-stage": stage,
      }[selector] || null;
    },
  };
  const oldGlobals = {
    document: globalThis.document,
    setInterval: globalThis.setInterval,
    clearInterval: globalThis.clearInterval,
  };
  globalThis.document = {
    fullscreenElement: null,
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.setInterval = () => 1;
  globalThis.clearInterval = () => {};

  try {
    const player = mountPlayer(container);
    assert.match(container.innerHTML, /<video[^>]*tabindex="0"/);
    listeners.get("click")();
    assert.deepEqual(focusCalls, [{ preventScroll: true }]);
    player.cleanup();
    assert.equal(listeners.has("click"), false);
  } finally {
    for (const [key, value] of Object.entries(oldGlobals)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
});

test("clearing the selected recording hides stale loading status", () => {
  const listeners = new Map();
  const video = {
    paused: true,
    currentTime: 0,
    playbackRate: 1,
    muted: false,
    addEventListener: (type, listener) => listeners.set(type, listener),
    removeEventListener: type => listeners.delete(type),
    focus() {},
    pause() { this.paused = true; },
    removeAttribute() {},
    load() {},
  };
  const status = { className: "hidden", textContent: "" };
  const classList = { add() {}, remove() {} };
  const emptyMessage = { textContent: "" };
  const empty = { classList, querySelector: () => emptyMessage };
  const controls = { addEventListener() {}, querySelector: () => null, innerHTML: "" };
  const episodeLink = { classList };
  const stage = { offsetHeight: 0, style: { removeProperty() {} } };
  const container = {
    innerHTML: "",
    querySelector(selector) {
      return {
        ".tl-player-video": video,
        ".tl-media-status": status,
        ".tl-controls-wrap": controls,
        ".tl-player-empty": empty,
        ".tl-episode-link": episodeLink,
        ".tl-player-stage": stage,
      }[selector] || null;
    },
  };
  const oldGlobals = {
    document: globalThis.document,
    setInterval: globalThis.setInterval,
    clearInterval: globalThis.clearInterval,
    mediaStateCallbacks: globalThis.mediaStateCallbacks,
  };
  globalThis.mediaStateCallbacks = [];
  globalThis.document = {
    fullscreenElement: null,
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.setInterval = () => 1;
  globalThis.clearInterval = () => {};

  try {
    const player = mountPlayer(container);
    player.loadSegment(SEG_A, SEG_A.start);
    const staleStateCallback = globalThis.mediaStateCallbacks.at(-1);
    assert.match(status.className, /media-state-loading/);
    assert.equal(status.textContent, "Loading recording…");

    player.loadSegment(null);
    player.setEmptyMessage("No recordings available for the selected cameras");
    assert.match(status.className, /media-state-idle hidden/);
    assert.equal(status.textContent, "");
    assert.equal(emptyMessage.textContent, "No recordings available for the selected cameras");
    staleStateCallback({ state: "loading", message: "Loading recording…" });
    assert.match(status.className, /media-state-idle hidden/);
    assert.equal(status.textContent, "");
    player.cleanup();
  } finally {
    for (const [key, value] of Object.entries(oldGlobals)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
});
