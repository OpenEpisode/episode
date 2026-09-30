import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source => "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/episode-history-player.js", import.meta.url),
  "utf8",
);
const apiUrl = moduleUrl(`export const API = "/api/v1"; export async function api() { throw new Error("unexpected API call"); }`);
const domUrl = moduleUrl(`
  export function escHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
`);
const mediaUrl = moduleUrl(`
  globalThis.__historyAttachments = [];
  globalThis.__historyDetachments = [];
  export function attachMediaSource(video, url, options) {
    globalThis.__historyAttachments.push({ video, url, options });
    if (globalThis.__historyImmediateState) options.onState(globalThis.__historyImmediateState);
    return () => globalThis.__historyDetachments.push({ video, url });
  }
  export function evidenceMediaUrl(evidence) { return "/media/" + evidence.id; }
  export function updateMediaStatus(element, state) {
    if (element) element.mediaState = state;
  }
`);
const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./media-player.js?v=7"', JSON.stringify(mediaUrl)),
));

function fakeNode({ hidden = true } = {}) {
  const listeners = new Map();
  return {
    hidden,
    disabled: false,
    innerHTML: "",
    textContent: "",
    value: "",
    muted: false,
    autoplay: true,
    controls: false,
    poster: "",
    classList: { toggle() {}, remove() {} },
    addEventListener(name, handler) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(handler);
    },
    removeEventListener(name, handler) { listeners.get(name)?.delete(handler); },
    dispatch(name) { listeners.get(name)?.forEach(handler => handler({ target: this })); },
    pause() { this.paused = true; },
    setAttribute(name, value) { this[name] = value; },
    listeners,
  };
}

function playerRoot() {
  const panel = fakeNode();
  const cover = fakeNode({ hidden: false });
  const nodes = {
    "[data-recording-retry]": fakeNode({ hidden: true }),
    "[data-recording-picker]": fakeNode({ hidden: true }),
    "[data-recording-select]": fakeNode({ hidden: false }),
    "[data-recording-player-stage]": fakeNode({ hidden: true }),
    "[data-recording-player]": fakeNode({ hidden: false }),
    "[data-recording-state]": fakeNode({ hidden: false }),
    "[data-recording-player-status]": fakeNode({ hidden: true }),
    "[data-recording-camera]": fakeNode({ hidden: false }),
  };
  panel.querySelector = selector => nodes[selector] || null;
  return {
    root: {
      isConnected: true,
      dataset: { selectedEpisode: "episode-1", detailToken: "1" },
      querySelector: selector => selector === "[data-episode-recordings]"
        ? panel
        : selector === "[data-preview-cover]" ? cover : null,
    },
    cover,
    panel,
    nodes,
  };
}

const recording = (id, deviceId = "camera-1") => ({
  id,
  device_id: deviceId,
  timestamp: "2026-09-29T12:00:00Z",
  mime_type: "video/mp4",
  availability: "available",
});

test("recording options omit expired or non-video evidence and cap the bounded list", () => {
  const recordings = Array.from({ length: 52 }, (_, index) => recording(`recording-${index}`));
  recordings.push({ ...recording("expired"), availability: "expired" });
  recordings.push({ ...recording("payload"), mime_type: "application/json" });

  const result = module.recordingSourceOptions(recordings, [], new Map([["camera-1", "Front camera"]]));

  assert.equal(result.options.length, 50);
  assert.equal(result.capped, true);
  assert.equal(result.expiredCount, 1);
  assert.ok(result.options.every(option => !option.id.includes("expired")));
  assert.match(result.options[0].label, /^Front camera/);
});

test("the panel has one paused muted viewport and no explicit Watch step", () => {
  const html = module.renderEpisodeRecordingPanel({ cover: '<div data-preview-cover></div>' });
  assert.doesNotMatch(html, /Watch recording|data-watch-recordings|Recordings<\/h/);
  assert.match(html, /data-recording-player/);
  assert.match(html, /muted/);
  assert.match(html, /playsinline/);
  assert.doesNotMatch(html, /autoplay/);
  assert.match(html, /data-recording-picker hidden/);
  assert.match(html, /data-recording-retry hidden/);
});

test("selected Episodes automatically load and attach one source while staying paused", async () => {
  globalThis.__historyAttachments.length = 0;
  globalThis.__historyDetachments.length = 0;
  const rendered = playerRoot();
  const requests = [];
  const cleanup = module.bindEpisodeHistoryPlayer(
    rendered.root,
    { id: "episode-1", state: "active" },
    async () => [{ id: "camera-1", name: "Front camera" }],
    1,
    async path => {
      requests.push(path);
      if (path.startsWith("/evidence?")) return [recording("recording-1")];
      if (path.startsWith("/episodes/")) return [];
      throw new Error(`unexpected ${path}`);
    },
  );
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(globalThis.__historyAttachments.length, 1);
  assert.equal(globalThis.__historyAttachments[0].url, "/media/recording-1");
  assert.equal(rendered.nodes["[data-recording-player]"].autoplay, false);
  assert.equal(rendered.nodes["[data-recording-player]"].muted, true);
  assert.equal(rendered.nodes["[data-recording-picker]"].hidden, true);
  assert.equal(rendered.nodes["[data-recording-camera]"].textContent, "Front camera");
  assert.ok(requests.some(path => path.includes("limit=51")));
  assert.ok(requests.some(path => path.endsWith("/current-views")));
  assert.equal(rendered.cover.hidden, false);

  rendered.nodes["[data-recording-player]"].dispatch("loadedmetadata");
  assert.equal(rendered.cover.hidden, true);
  assert.equal(rendered.nodes["[data-recording-player-status]"].mediaState.state, "idle");
  cleanup();
  assert.equal(globalThis.__historyDetachments.length, 1);
  assert.equal(rendered.cover.hidden, false);
  assert.equal(rendered.nodes["[data-recording-player]"].paused, true);
});

test("completed Episodes load bounded recordings without requesting current views", async () => {
  const rendered = playerRoot();
  const requests = [];
  const cleanup = module.bindEpisodeHistoryPlayer(
    rendered.root,
    { id: "episode-1", state: "closed" },
    async () => [],
    1,
    async path => {
      requests.push(path);
      return path.startsWith("/evidence?") ? [recording("recording-1")] : [];
    },
  );
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(requests.some(path => path.includes("limit=51")));
  assert.doesNotMatch(requests.join("\n"), /current-views/);
  cleanup();
});

test("multiple sources expose a selectable camera footer and detach on switch", async () => {
  globalThis.__historyAttachments.length = 0;
  globalThis.__historyDetachments.length = 0;
  const rendered = playerRoot();
  const cleanup = module.bindEpisodeHistoryPlayer(
    rendered.root,
    { id: "episode-1", state: "active" },
    async () => [{ id: "camera-1", name: "Front camera" }, { id: "camera-2", name: "Garage camera" }],
    1,
    async path => path.startsWith("/evidence?")
      ? [recording("one", "camera-1"), recording("two", "camera-2")]
      : [],
  );
  await new Promise(resolve => setImmediate(resolve));
  const select = rendered.nodes["[data-recording-select]"];
  assert.equal(rendered.nodes["[data-recording-picker]"].hidden, false);
  assert.equal(select.disabled, false);
  select.value = "recording:two";
  select.dispatch("change");
  assert.equal(globalThis.__historyAttachments.length, 2);
  assert.equal(globalThis.__historyDetachments.length, 1);
  cleanup();
  assert.equal(globalThis.__historyDetachments.length, 2);
});

test("cleanup invalidates a pending response with an explicit disposed guard", async () => {
  globalThis.__historyAttachments.length = 0;
  const rendered = playerRoot();
  let resolveEvidence;
  const pending = new Promise(resolve => { resolveEvidence = resolve; });
  const cleanup = module.bindEpisodeHistoryPlayer(
    rendered.root,
    { id: "episode-1", state: "closed" },
    async () => [],
    1,
    async path => path.startsWith("/evidence?") ? pending : [],
  );
  cleanup();
  resolveEvidence([recording("late")]);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(globalThis.__historyAttachments.length, 0);
});

test("empty, expired, starting, and failed sources keep the cover and expose only useful retries", async () => {
  const cases = [
    { result: [], message: /No playable recordings/, retry: false },
    { result: [{ ...recording("expired"), availability: "expired" }], message: /expired under the retention policy/, retry: false },
    { result: [{ device_id: "camera-1", mode: "snapshot" }], message: /starting or the live stream/, retry: true },
    { result: new Error("unavailable"), message: /temporarily unavailable/, retry: true },
  ];
  for (const currentCase of cases) {
    const rendered = playerRoot();
    const cleanup = module.bindEpisodeHistoryPlayer(
      rendered.root,
      { id: "episode-1", state: "active" },
      async () => [],
      1,
      async path => {
        if (path.startsWith("/evidence?")) {
          if (currentCase.result instanceof Error) throw currentCase.result;
          return currentCase.result;
        }
        if (path.startsWith("/episodes/")) {
          return currentCase.result[0]?.mode === "snapshot" ? currentCase.result : [];
        }
        return [];
      },
    );
    await new Promise(resolve => setImmediate(resolve));
    assert.match(rendered.nodes["[data-recording-state]"].textContent, currentCase.message);
    assert.equal(rendered.cover.hidden, false);
    assert.equal(rendered.nodes["[data-recording-retry]"].hidden, !currentCase.retry);
    cleanup();
  }
});

test("a playable source remains usable when the other source query fails", async () => {
  globalThis.__historyAttachments.length = 0;
  for (const live of [true, false]) {
    const rendered = playerRoot();
    const cleanup = module.bindEpisodeHistoryPlayer(
      rendered.root,
      { id: "episode-1", state: "active" },
      async () => [],
      1,
      async path => {
        if (path.startsWith("/evidence?")) {
          if (live) throw new Error("evidence unavailable");
          return [recording("recording-1")];
        }
        if (live) return [{ device_id: "camera-1", mode: "hls", stream_url: "/live/index.m3u8", recording_state: "recording" }];
        throw new Error("live unavailable");
      },
    );
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(globalThis.__historyAttachments.length, live ? 1 : 2);
    cleanup();
  }
});

test("history player CSS keeps hidden nodes hidden and media proportions natural", async () => {
  const css = await readFile(new URL("../../src/episode/ui/episode-history.css", import.meta.url), "utf8");
  assert.match(css, /\.episode-history-player-stage video\s*\{[^}]*height: auto/);
  assert.match(css, /object-fit: contain/);
  assert.match(css, /\.episode-history-player-stage\[hidden\]/);
  assert.match(css, /\.episode-preview-cover\[hidden\]/);
  assert.match(css, /grid-template-columns: minmax\(240px, 280px\) minmax\(0, 1fr\)/);
});

test("synchronous playback failure stays visible and releases the returned media cleanup", async () => {
  globalThis.__historyDetachments.length = 0;
  globalThis.__historyImmediateState = { state: "unavailable", message: "Playback support unavailable" };
  const rendered = playerRoot();
  try {
    const cleanup = module.bindEpisodeHistoryPlayer(
      rendered.root, { id: "episode-1", state: "closed" }, async () => [], 1,
      async () => [recording("one")],
    );
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(rendered.nodes["[data-recording-state]"].textContent, "Playback support unavailable");
    assert.equal(rendered.nodes["[data-recording-retry]"].hidden, false);
    assert.equal(rendered.cover.hidden, false);
    assert.equal(globalThis.__historyDetachments.length, 1);
    cleanup();
    assert.equal(globalThis.__historyDetachments.length, 1);
  } finally {
    delete globalThis.__historyImmediateState;
  }
});

test("retry loads an available recording after a transient query failure", async () => {
  const rendered = playerRoot();
  let calls = 0;
  const cleanup = module.bindEpisodeHistoryPlayer(
    rendered.root, { id: "episode-1", state: "closed" }, async () => [], 1,
    async () => { if (++calls === 1) throw new Error("temporarily offline"); return [recording("one")]; },
  );
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(rendered.nodes["[data-recording-retry]"].hidden, false);
  rendered.nodes["[data-recording-retry]"].dispatch("click");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 2);
  assert.equal(rendered.nodes["[data-recording-retry]"].hidden, true);
  assert.equal(rendered.nodes["[data-recording-state]"].textContent, "");
  cleanup();
});

test("changing the selected Episode ignores an outstanding old recording query", async () => {
  globalThis.__historyAttachments.length = 0;
  const rendered = playerRoot();
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const cleanup = module.bindEpisodeHistoryPlayer(
    rendered.root, { id: "episode-1", state: "closed" }, async () => [], 1,
    async () => pending,
  );
  rendered.root.dataset.selectedEpisode = "episode-2";
  rendered.root.dataset.detailToken = "2";
  resolve([recording("old")]);
  await new Promise(done => setImmediate(done));
  assert.equal(globalThis.__historyAttachments.length, 0);
  cleanup();
});
