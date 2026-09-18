import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const domUrl = moduleUrl('export const escHtml = value => String(value).replaceAll("<", "&lt;").replaceAll(">", "&gt;");');
const apiUrl = moduleUrl("export async function api(path) { return globalThis.__currentViewsApi(path); }");
const mediaUrl = moduleUrl("export function attachMediaSource() { return () => {}; }");
const source = await readFile(
  new URL("../../src/episode/ui/current-views.js", import.meta.url),
  "utf8",
);
const currentViewsUrl = moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./media-player.js?v=7"', JSON.stringify(mediaUrl)),
);
const {
  activateCurrentViews,
  deactivateCurrentViews,
  markEpisodeComplete,
  renderCurrentViews,
} = await import(currentViewsUrl);

test("current view panel distinguishes refreshing and unavailable recordings", () => {
  const html = renderCurrentViews([
    {
      device_id: "camera-a",
      device_name: "Entry camera",
      mode: "snapshot",
      image_url: "/api/v1/preview",
      summary: "Refreshing",
    },
    {
      device_id: "doorbell",
      device_name: "Doorbell",
      mode: "unavailable",
      image_url: null,
      summary: "Recording continues",
    },
  ]);

  assert.match(html, /Ongoing recordings/);
  assert.match(html, /data-preview-url="\/api\/v1\/preview"/);
  assert.match(html, /Preview unavailable/);
  assert.match(html, /recording continues/);
});

test("ongoing HLS recordings use native video controls only", () => {
  const html = renderCurrentViews([{
    device_id: "camera-a",
    device_name: "Entry camera",
    mode: "hls",
    stream_url: "/api/v1/recordings/one/index.m3u8",
    summary: "Streaming",
  }]);

  assert.match(html, /<video muted autoplay playsinline controls aria-label=/);
  assert.doesNotMatch(html, /current-view-controls/);
  assert.doesNotMatch(html, /data-current-view-seek|data-current-view-play/);
  assert.doesNotMatch(html, /current-view-now-button|current-view-position/);
  assert.doesNotMatch(html, /Preparing timeline|Review recordings/);
  assert.match(html, /Live preview/);
});

test("current view labels are escaped", () => {
  const html = renderCurrentViews([{
    device_id: "camera-a",
    device_name: "<script>alert(1)</script>",
    mode: "unavailable",
    image_url: null,
    summary: "Unavailable",
  }]);

  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

function fakeClassList(...initial) {
  const values = new Set(initial);
  return {
    add: (...names) => names.forEach(name => values.add(name)),
    remove: (...names) => names.forEach(name => values.delete(name)),
    contains: name => values.has(name),
  };
}

function fakeCard() {
  const badgeText = { textContent: "Live" };
  return {
    classList: fakeClassList("is-loading"),
    querySelector(selector) {
      if (selector === "[data-current-view-live]") return { lastChild: badgeText };
      if (selector === ".current-view-status") return { textContent: "Streaming" };
      return null;
    },
    badgeText,
  };
}

test("episode completion marks active previews complete without DVR controls", () => {
  const card = fakeCard();
  const state = { replaceChildren(...content) { this.textContent = content.join(""); } };
  const previousDocument = globalThis.document;
  globalThis.document = {
    querySelector: selector => selector === "[data-current-views-state]" ? state : null,
    querySelectorAll: selector => selector === "#current-view-grid .current-view-card" ? [card] : [],
  };

  try {
    markEpisodeComplete();
    assert.equal(card.classList.contains("is-complete"), true);
    assert.equal(card.classList.contains("is-loading"), false);
    assert.equal(card.badgeText.textContent, "Complete");
    assert.equal(state.textContent, "Episode closed · recordings remain available to review");
  } finally {
    globalThis.document = previousDocument;
  }
});

test("transient empty views during finalizing preserve the player until closure", async () => {
  const card = fakeCard();
  const state = { replaceChildren(...content) { this.textContent = content.join(""); } };
  const grid = {
    innerHTML: "retained playable card",
    querySelector: selector => selector === ".current-view-card" ? card : null,
  };
  const pending = [];
  const states = ["finalizing", "closed"];
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  globalThis.window = {
    setTimeout(callback) {
      pending.push(callback);
      return pending.length;
    },
    clearTimeout() {},
  };
  globalThis.document = {
    getElementById: id => id === "current-view-grid" ? grid : null,
    querySelector: selector => selector === "[data-current-views-state]" ? state : null,
    querySelectorAll: selector => selector === "#current-view-grid .current-view-card" ? [card] : [],
  };
  globalThis.__currentViewsApi = async path => path.endsWith("/current-views")
    ? []
    : { state: states.shift() };

  try {
    activateCurrentViews("episode-a", [{
      device_id: "camera-a",
      mode: "hls",
      stream_url: "/api/v1/recordings/one/index.m3u8",
      refresh_interval_seconds: 2,
    }]);
    await pending.shift()();
    assert.equal(grid.innerHTML, "retained playable card");
    assert.equal(card.classList.contains("is-complete"), false);
    await pending.shift()();
    assert.equal(grid.innerHTML, "retained playable card");
    assert.equal(card.classList.contains("is-complete"), true);
  } finally {
    deactivateCurrentViews();
    delete globalThis.__currentViewsApi;
    globalThis.window = previousWindow;
    globalThis.document = previousDocument;
  }
});
