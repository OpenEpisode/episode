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
    dispatch(name) { listeners.get(name)?.forEach(handler => handler()); },
    removeAttribute(name) { if (name === "src") this.src = ""; },
    load() {},
    listeners,
  };
}

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
  assert.equal(video.listeners.size, 10);
  media.detachMediaSource(video);
  assert.equal(video.src, "");
  assert.equal(video.listeners.size, 0);
});

test("native HLS is preferred for both live and completed playback", () => {
  class FakeHls {
    static isSupported() { return true; }
    constructor() { throw new Error("hls.js should not replace native HLS"); }
  }
  globalThis.window = { Hls: FakeHls };
  for (const live of [true, false]) {
    const video = fakeVideo({ nativeHls: true });
    const detach = media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", { live });
    assert.equal(video.src, "/api/v1/recordings/one/index.m3u8");
    detach();
  }
});

test("live native HLS seeks once to the available live edge", () => {
  globalThis.window = {};
  const video = fakeVideo({ nativeHls: true });
  video.seekable = { length: 1, start: () => 12, end: () => 72 };
  const detach = media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", { live: true });

  video.dispatch("loadedmetadata");
  assert.equal(video.currentTime, 71.75);
  video.currentTime = 70;
  video.dispatch("progress");
  assert.equal(video.currentTime, 70);
  detach();
});

test("native live playback retries initial live seek when metadata has no range", () => {
  globalThis.window = {};
  const video = fakeVideo({ nativeHls: true });
  const detach = media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", { live: true });

  video.dispatch("loadedmetadata");
  assert.equal(video.currentTime, 0);
  video.seekable = { length: 1, start: () => 20, end: () => 40 };
  video.dispatch("canplay");
  assert.equal(video.currentTime, 39.75);
  detach();
});

test("user seeking prevents a later initial-live reposition", () => {
  globalThis.window = {};
  const video = fakeVideo({ nativeHls: true });
  const detach = media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", { live: true });

  video.dispatch("loadedmetadata");
  video.dispatch("seeking");
  video.seekable = { length: 1, start: () => 20, end: () => 40 };
  video.dispatch("canplay");
  assert.equal(video.currentTime, 0);
  detach();
});

test("HLS falls back to hls.js when native playback is unavailable", () => {
  class FakeHls {
    static Events = { MANIFEST_PARSED: "manifest", FRAG_BUFFERED: "fragment", ERROR: "error" };
    static ErrorTypes = { NETWORK_ERROR: "network", MEDIA_ERROR: "media" };
    static isSupported() { return true; }
    constructor() { this.handlers = new Map(); FakeHls.instance = this; }
    on(name, handler) { this.handlers.set(name, handler); }
    loadSource(url) { this.loadedUrl = url; }
    attachMedia(video) { this.attachedVideo = video; }
    destroy() { this.destroyed = true; }
  }
  globalThis.window = { Hls: FakeHls };
  const video = fakeVideo();
  const detach = media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", { live: true });

  assert.equal(FakeHls.instance.loadedUrl, "/api/v1/recordings/one/index.m3u8");
  assert.equal(FakeHls.instance.attachedVideo, video);
  assert.equal(video.src, "");
  detach();
  assert.equal(FakeHls.instance.destroyed, true);
});

test("missing HLS support reports an actionable unavailable state", () => {
  globalThis.window = {};
  const video = fakeVideo();
  const states = [];
  media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", {
    live: true,
    onState: state => states.push(state),
  });

  assert.equal(video.src, "");
  assert.equal(states[0].state, "unavailable");
  assert.match(states[0].message, /internet access/i);
});

test("live HLS recovers a stalled hls.js playback", async () => {
  class FakeHls {
    static Events = { MANIFEST_PARSED: "manifest", FRAG_BUFFERED: "fragment", ERROR: "error" };
    static ErrorTypes = { NETWORK_ERROR: "network", MEDIA_ERROR: "media" };
    static isSupported() { return true; }
    constructor() { this.handlers = new Map(); this.startCalls = 0; FakeHls.instance = this; }
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
    destroy() {}
  }
  globalThis.window = { Hls: FakeHls };
  const video = fakeVideo();
  video.paused = false;
  const states = [];
  const detach = media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", {
    live: true,
    stallRecoveryMs: 25,
    onState: state => states.push(state),
  });

  video.dispatch("waiting");
  await new Promise(resolve => setTimeout(resolve, 550));
  assert.equal(FakeHls.instance.startCalls, 1);
  assert.match(states.at(-1).message, /retrying playback/i);
  detach();
});
