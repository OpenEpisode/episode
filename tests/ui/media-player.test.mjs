import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/media-player.js", import.meta.url),
  "utf8",
);
const media = await import(moduleUrl(source));

function fakeVideo({ nativeHls = false } = {}) {
  const listeners = new Map();
  return {
    src: "",
    currentTime: 0,
    paused: true,
    ended: false,
    readyState: 0,
    seekable: { length: 0, start: () => 0, end: () => 0 },
    play() { this.paused = false; return Promise.resolve(); },
    canPlayType: type => nativeHls && type.includes("mpegurl") ? "probably" : "",
    addEventListener(name, handler) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(handler);
    },
    removeEventListener(name, handler) {
      const handlers = listeners.get(name);
      handlers?.delete(handler);
      if (handlers?.size === 0) listeners.delete(name);
    },
    dispatch(name) {
      listeners.get(name)?.forEach(handler => handler());
    },
    removeAttribute(name) { if (name === "src") this.src = ""; },
    load() {},
    listeners,
  };
}

test("DVR helpers expose the seekable recording window", () => {
  const video = fakeVideo();
  video.seekable = { length: 1, start: () => 12, end: () => 72 };

  assert.deepEqual(media.getSeekableRange(video), { start: 12, end: 72 });
  assert.equal(media.formatLiveOffset(0), "Live");
  assert.match(media.formatLiveOffset(14), /14 seconds behind live/);
  assert.equal(media.seekToBeginning(video), true);
  assert.equal(video.currentTime, 12);
  assert.equal(media.seekToLive(video), true);
  assert.equal(video.currentTime, 71.75);
});

test("legacy MP4 recordings use the native video element", () => {
  globalThis.window = {};
  const video = fakeVideo();
  const states = [];
  const detach = media.attachMediaSource(video, "/api/v1/evidence/one/file", {
    onState: state => states.push(state.state),
  });

  assert.equal(video.src, "/api/v1/evidence/one/file");
  video.dispatch("playing");
  assert.deepEqual(states, ["ready"]);
  detach();
});

test("replacing a native source cleans up the previous attachment", () => {
  globalThis.window = {};
  const video = fakeVideo();

  media.attachMediaSource(video, "/api/v1/evidence/one/file");
  media.attachMediaSource(video, "/api/v1/evidence/two/file");

  assert.equal(video.src, "/api/v1/evidence/two/file");
  assert.equal(video.listeners.size, 9);
  media.detachMediaSource(video);
  assert.equal(video.src, "");
  assert.equal(video.listeners.size, 0);
});

test("missing HLS fallback reports an actionable unavailable state", () => {
  globalThis.window = {};
  const video = fakeVideo();
  const states = [];
  media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", {
    onState: state => states.push(state),
  });

  assert.equal(video.src, "");
  assert.equal(states[0].state, "unavailable");
  assert.match(states[0].message, /internet access/i);
});

test("finalized HLS network failures are reported instead of retried forever", () => {
  class FakeHls {
    static Events = { MANIFEST_PARSED: "manifest", FRAG_BUFFERED: "fragment", ERROR: "error" };
    static ErrorTypes = { NETWORK_ERROR: "network", MEDIA_ERROR: "media" };
    static isSupported() { return true; }

    constructor() {
      this.handlers = new Map();
      this.startCalls = 0;
      FakeHls.instance = this;
    }

    on(name, handler) { this.handlers.set(name, handler); }
    loadSource() {}
    attachMedia() {}
    startLoad() { this.startCalls += 1; }
    recoverMediaError() {}
    destroy() {}
  }
  globalThis.window = { Hls: FakeHls };
  const states = [];
  media.attachMediaSource(fakeVideo(), "/api/v1/recordings/one/index.m3u8", {
    onState: state => states.push(state),
  });
  FakeHls.instance.handlers.get("error")("error", {
    fatal: true,
    type: "network",
  });

  assert.equal(states.at(-1).state, "error");
  assert.match(states.at(-1).message, /incomplete|unavailable/i);
  assert.equal(FakeHls.instance.startCalls, 0);
});

test("live HLS uses a finite DVR timeline and recovers a stalled live edge", async () => {
  class FakeHls {
    static Events = { MANIFEST_PARSED: "manifest", FRAG_BUFFERED: "fragment", ERROR: "error" };
    static ErrorTypes = { NETWORK_ERROR: "network", MEDIA_ERROR: "media" };
    static isSupported() { return true; }

    constructor(config) {
      this.config = config;
      this.handlers = new Map();
      this.startCalls = 0;
      FakeHls.instance = this;
    }

    on(name, handler) { this.handlers.set(name, handler); }
    loadSource() {}
    attachMedia(video) {
      video.seekable = { length: 1, start: () => 0, end: () => 10 };
      video.currentTime = 10;
      video.paused = false;
      video.readyState = 2;
    }
    startLoad() { this.startCalls += 1; }
    recoverMediaError() {}
    destroy() { this.destroyed = true; }
  }
  globalThis.window = { Hls: FakeHls };
  const video = fakeVideo();
  video.paused = false;
  const states = [];
  let detach;
  try {
    detach = media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", {
      live: true,
      stallRecoveryMs: 25,
      onState: state => states.push(state),
    });

    assert.equal(FakeHls.instance.config.liveDurationInfinity, false);
    video.dispatch("waiting");
    await new Promise(resolve => setTimeout(resolve, 550));
    assert.equal(FakeHls.instance.startCalls, 1);
    assert.match(states.at(-1).message, /retrying playback/i);
  } finally {
    detach?.();
  }
  assert.equal(FakeHls.instance.destroyed, true);
});
