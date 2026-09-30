import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source => "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(new URL("../../src/episode/ui/episodes-page.js", import.meta.url), "utf8");

const apiUrl = moduleUrl(`
  export const API = "/api/v1";
  export async function api(path) {
    globalThis.episodeApiCalls.push(path);
    if (path.startsWith("/episodes?")) {
      if (globalThis.episodeResponses?.length) {
        const response = globalThis.episodeResponses.shift();
        if (response instanceof Error) throw response;
        return response;
      }
      return globalThis.episodeList;
    }
    if (path.startsWith("/episodes/")) {
      if (globalThis.episodeDetailResponse instanceof Error) throw globalThis.episodeDetailResponse;
      return globalThis.episodeDetailResponse || {};
    }
    if (path === "/areas?include_disabled=true") return [{ id: "area-1", name: "Entrance" }];
    if (path.startsWith("/covers?")) {
      if (globalThis.coverResponses?.length) return globalThis.coverResponses.shift();
      return globalThis.coverPromise;
    }
    throw new Error("Unexpected API request: " + path);
  }
`);
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
const episodePlayerUrl = moduleUrl(`
  export function renderEpisodeRecordingPanel({ cover = "" } = {}) { return '<section data-episode-recordings>' + cover + '</section>'; }
  export function bindEpisodeHistoryPlayer() { return () => {}; }
`);
const sidebarUrl = moduleUrl(`export function updateRecentEpisodes() {}`);
const viewUrl = moduleUrl(`
  export function showLoading() {}
  export function showError(message) { globalThis.episodeError = message; }
  export function showContent(html) { globalThis.episodeHtml = html; globalThis.episodeRoot = globalThis.nextEpisodeRoot; }
`);

const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=4"', JSON.stringify(apiUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./episode-history-player.js?v=2"', JSON.stringify(episodePlayerUrl))
    .replace('"./sidebar.js?v=4"', JSON.stringify(sidebarUrl))
    .replace('"./view.js?v=1"', JSON.stringify(viewUrl)),
));

function coverRoot(connected = true) {
  const span = { textContent: "Loading preview…", removed: false, remove() { this.removed = true; } };
  const image = { src: "/logo.svg", alt: "", onerror: null };
  const player = { poster: "", setAttribute(name, value) { this[name] = value; } };
  const slot = {
    dataset: { coverEpisode: "episode-1", previewCover: "" },
    classList: { removed: false, remove() { this.removed = true; } },
    querySelector(selector) {
      if (selector === "img") return image;
      if (selector === "span") return span;
      return null;
    },
  };
  return {
    root: {
      isConnected: connected,
      dataset: { selectedEpisode: "episode-1", detailToken: "1" },
      querySelectorAll() { return [slot]; },
      querySelector(selector) { return selector === "[data-recording-player]" ? player : null; },
    },
    image,
    player,
    slot,
    span,
  };
}

function coverBatchRoot() {
  const makeSlot = id => {
    const span = { textContent: "Loading preview…", remove() { this.removed = true; } };
    const image = { src: "/logo.svg", onerror: null };
    return {
      dataset: { coverEpisode: id },
      span,
      image,
      classList: { remove() {} },
      querySelector(selector) { return selector === "span" ? span : selector === "img" ? image : null; },
    };
  };
  const slots = [makeSlot("missing"), makeSlot("pending")];
  return {
    slots,
    root: {
      isConnected: true,
      querySelectorAll() { return slots; },
      querySelector() { return null; },
    },
  };
}

function setEpisodeApi(list = [{
  id: "episode-1",
  primary_area_id: "area-1",
  start_time: "2026-09-29T12:00:00Z",
  end_time: "2026-09-29T12:01:00Z",
  event_count: 1,
  evidence_count: 1,
  state: "closed",
  trigger_type: "motion",
}]) {
  globalThis.episodeApiCalls = [];
  globalThis.episodeList = list;
  globalThis.episodeResponses = [];
  globalThis.coverResponses = [];
  globalThis.coverPromise = {};
}

function lazyRoot() {
  const status = { textContent: "", classList: { toggle() {} } };
  const retry = {
    hidden: true,
    listeners: new Map(),
    addEventListener(name, handler) { this.listeners.set(name, handler); },
    removeEventListener() {},
  };
  const groups = new Map();
  const makeGroup = date => {
    const dayItems = { inserted: "", insertAdjacentHTML(_position, html) { this.inserted += html; } };
    const group = { querySelector(selector) { return selector === ".episode-history-day-items" ? dayItems : null; } };
    groups.set(date, group);
    return group;
  };
  const items = {
    inserted: "",
    querySelector(selector) {
      const date = selector.match(/data-history-date="([^"]+)"/)?.[1];
      return date ? groups.get(date) || null : null;
    },
    insertAdjacentHTML(_position, html) {
      this.inserted += html;
      const date = html.match(/data-history-date="([^"]+)"/)?.[1];
      if (date) makeGroup(date).inserted = html;
    },
  };
  const rail = {
    scrollHeight: 1_000,
    clientHeight: 400,
    scrollTop: 0,
    listeners: new Map(),
    addEventListener(name, handler) { this.listeners.set(name, handler); },
    removeEventListener() {},
    querySelector() { return null; },
  };
  return {
    isConnected: true,
    dataset: { selectedEpisode: "existing" },
    querySelector(selector) {
      if (selector === "[data-history-items]") return items;
      if (selector === "[data-history-load-state]") return status;
      if (selector === "[data-history-load-retry]") return retry;
      if (selector === ".episode-history-rail") return rail;
      return null;
    },
    querySelectorAll() { return []; },
    groups,
    items,
    rail,
    status,
    retry,
  };
}

function lazyState(root, overrides = {}) {
  return {
    root,
    area: "area-1",
    startedBefore: "2026-09-30T00:00:00.000Z",
    covers: {},
    areaNames: new Map([["area-1", "Entrance"]]),
    list: [{ id: "existing", primary_area_id: "area-1", start_time: "2026-09-29T12:00:00Z" }],
    loadedIds: new Set(["existing"]),
    rawOffset: 48,
    hasMore: true,
    loading: false,
    loadError: false,
    disposed: false,
    generation: 1,
    ...overrides,
  };
}

function ongoingRoot(selectedEpisode = "history") {
  const marker = (id, ongoing = false) => {
    const time = { textContent: "12:00", setAttribute() {} };
    const trigger = { innerHTML: "Motion" };
    return {
      dataset: { selectEpisode: id },
      hidden: false,
      attributes: {},
      closest(selector) { return ongoing && selector === "[data-history-ongoing-items]" ? {} : null; },
      setAttribute(name, value) { this.attributes[name] = value; },
      querySelector(selector) {
        if (selector === ".episode-history-marker-time") return time;
        if (selector === ".episode-history-marker-trigger") return trigger;
        return null;
      },
      time,
      trigger,
    };
  };
  const historyMarker = marker("history");
  const ongoingMarker = marker("ongoing", true);
  const notice = { textContent: "", hidden: true, classList: { toggle() {} } };
  const ongoingItems = { innerHTML: "" };
  const coverImage = { src: "/logo.svg", onerror: null };
  const coverSlot = {
    dataset: { coverEpisode: "new" },
    classList: { remove() {} },
    querySelector(selector) { return selector === "img" ? coverImage : null; },
  };
  const region = { hidden: true, offsetHeight: 44 };
  const timeline = { style: { setProperty(_name, value) { timeline.ongoingHeight = value; } } };
  const player = { id: "player" };
  const preview = {
    title: { textContent: "Entrance" },
    time: { textContent: "Sep 30, 2026, 12:00 · Live" },
    querySelector(selector) {
      if (selector === ".episode-history-preview-heading h2") return this.title;
      if (selector === ".episode-history-preview-time") return this.time;
      return null;
    },
  };
  const root = {
    isConnected: true,
    dataset: { selectedEpisode },
    querySelector(selector) {
      if (selector === "[data-history-timeline]") return timeline;
      if (selector === "[data-history-ongoing]") return region;
      if (selector === "[data-history-ongoing-items]") return ongoingItems;
      if (selector === "[data-history-ongoing-state]") return notice;
      if (selector === "[data-episode-preview]") return preview;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-select-episode]") return [historyMarker, ongoingMarker];
      if (selector === "[data-cover-episode]") return [coverSlot];
      return [];
    },
    historyMarker,
    ongoingMarker,
    ongoingItems,
    notice,
    region,
    timeline,
    preview,
    player,
    coverImage,
    coverSlot,
  };
  return root;
}

function ongoingState(root, overrides = {}) {
  const history = { id: "history", primary_area_id: "area-1", start_time: "2026-09-30T12:00:00Z", state: "active", trigger_type: "motion" };
  return {
    root,
    area: "area-1",
    areaNames: new Map([["area-1", "Entrance"]]),
    list: [history],
    loadedIds: new Set(["history"]),
    knownEpisodes: new Map([[history.id, history]]),
    ongoingList: [],
    ongoingObservedIds: new Set(),
    ongoingLoading: false,
    ongoingNotice: "",
    ongoingNoticeError: false,
    ongoingTimer: null,
    ongoingController: null,
    detailControllers: new Map(),
    coverRequestedIds: new Set(["history"]),
    covers: {},
    generation: 1,
    disposed: false,
    ...overrides,
  };
}

test("Episodes render a usable rail before the delayed cover lookup completes", async () => {
  setEpisodeApi();
  const rendered = coverRoot();
  globalThis.nextEpisodeRoot = rendered.root;
  let resolveCovers;
  globalThis.coverPromise = new Promise(resolve => { resolveCovers = resolve; });
  globalThis.document = { querySelector() { return globalThis.episodeRoot; } };

  await module.episodes();

  assert.match(globalThis.episodeHtml, /data-episodes-page/);
  assert.match(globalThis.episodeHtml, /Loading preview/);
  assert.equal(rendered.image.src, "/logo.svg");
  assert.ok(globalThis.episodeApiCalls.some(path => path.includes("limit=49")));
  assert.ok(globalThis.episodeApiCalls.some(path => path.startsWith("/covers?ids=")));
  assert.doesNotMatch(globalThis.episodeApiCalls.join("\n"), /events|evidence\?episode_id/);

  resolveCovers({ "episode-1": "evidence-1" });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(rendered.image.src, "/api/v1/evidence/evidence-1/thumbnail");
  assert.equal(rendered.player.poster, "/api/v1/evidence/evidence-1/thumbnail");
  assert.equal(rendered.span.removed, true);
  assert.equal(rendered.slot.classList.removed, true);
});

test("cover lookup failure and missing snapshots remain explicit", async () => {
  const failure = coverRoot();
  await module.loadEpisodeCovers(failure.root, ["episode-1"], async () => { throw new Error("covers unavailable"); });
  assert.equal(failure.image.src, "/logo.svg");
  assert.equal(failure.span.textContent, "Preview unavailable");

  const empty = coverRoot();
  await module.loadEpisodeCovers(empty.root, ["episode-1"], async () => ({}));
  assert.equal(empty.span.textContent, "No snapshot");
  assert.equal(empty.image.src, "/logo.svg");
});

test("cover errors and confirmed missing results only update requested IDs", async () => {
  const missing = coverBatchRoot();
  const missingResults = await module.loadEpisodeCovers(missing.root, ["missing"], async () => ({}));
  assert.deepEqual(missingResults, { missing: null });
  assert.equal(missing.slots[0].span.textContent, "No snapshot");
  assert.equal(missing.slots[1].span.textContent, "Loading preview…");
  module.applyEpisodeCovers(missing.root, missingResults);
  assert.equal(missing.slots[0].span.textContent, "No snapshot");
  assert.equal(missing.slots[1].span.textContent, "Loading preview…");

  const failed = coverBatchRoot();
  await module.loadEpisodeCovers(failed.root, ["missing"], async () => { throw new Error("cover service down"); });
  assert.equal(failed.slots[0].span.textContent, "Preview unavailable");
  assert.equal(failed.slots[1].span.textContent, "Loading preview…");
});

test("the Episodes page keeps the chronological rail left of the viewer on desktop", () => {
  const html = module.renderEpisodesPage(
    [
      { id: "old", primary_area_id: "area-1", start_time: "2026-09-27T08:00:00Z", event_count: 1, trigger_type: "access" },
      { id: "new", primary_area_id: "area-1", start_time: "2026-09-29T12:00:00Z", event_count: 1, trigger_type: "motion" },
      { id: "middle", primary_area_id: "area-1", start_time: "2026-09-28T21:00:00Z", event_count: 1, trigger_type: "doorbell" },
    ],
    new Map([["area-1", "Entrance"]]),
    1,
    false,
    { areas: [{ id: "area-1", name: "Entrance" }] },
  );
  assert.ok(html.indexOf('data-select-episode="new"') < html.indexOf('data-select-episode="middle"'));
  assert.ok(html.indexOf('data-select-episode="middle"') < html.indexOf('data-select-episode="old"'));
  assert.equal((html.match(/data-history-date="/g) || []).length, 3);
  assert.ok(html.indexOf("episode-history-rail") < html.indexOf("episode-history-preview"));
  const emptyHtml = module.renderEpisodesPage([], new Map(), 1, false);
  assert.ok(emptyHtml.indexOf("episode-history-rail") < emptyHtml.indexOf("episode-history-empty"));
  assert.match(html, /episode-history-marker-time/);
  assert.match(html, /episode-history-marker-spine/);
  assert.match(html, /episode-history-cover/);
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /Open details/);
  assert.doesNotMatch(html, /Selected Episode|Recent activity|Recent evidence|Watch recording|episode-history-day-count/);
  assert.doesNotMatch(html, /event_count|evidence_count|Until|badge/);
});

test("active Episodes alone show Live/Ongoing and closed Episodes do not imply current activity", () => {
  const html = module.renderEpisodesPage([
    { id: "active", primary_area_id: "area-1", start_time: "2026-09-29T12:00:00Z", state: "active", trigger_type: "motion" },
    { id: "quiet", primary_area_id: "area-1", start_time: "2026-09-29T11:00:00Z", state: "quiescent", trigger_type: "motion" },
    { id: "closed", primary_area_id: "area-1", start_time: "2026-09-29T10:00:00Z", state: "closed", trigger_type: "motion" },
  ], new Map([["area-1", "Entrance"]]), 1, false);
  assert.match(html, /Live/);
  assert.match(html, /Ongoing/);
  const closedStart = html.indexOf('data-select-episode="closed"');
  const closedMarker = html.slice(closedStart, html.indexOf("</button>", closedStart));
  assert.doesNotMatch(closedMarker, /Live|Ongoing/);
});

test("ongoing polling ignores the history date, deduplicates states, and hides pinned history markers", async () => {
  const root = ongoingRoot();
  const state = ongoingState(root);
  globalThis.episodeApiCalls = [];
  globalThis.episodeResponses = [[
    { id: "history", primary_area_id: "area-1", start_time: "2026-09-30T12:00:00Z", state: "active", trigger_type: "motion" },
    { id: "new", primary_area_id: "area-1", start_time: "2026-09-30T13:00:00Z", state: "active", trigger_type: "access" },
  ], [
    { id: "history", primary_area_id: "area-1", start_time: "2026-09-30T12:00:00Z", state: "quiescent", trigger_type: "motion" },
    { id: "quiet", primary_area_id: "area-1", start_time: "2026-09-30T11:00:00Z", state: "quiescent", trigger_type: "doorbell" },
  ]];
  globalThis.coverResponses = [{}];

  assert.equal(await module.refreshOngoingEpisodes(root, state), true);
  const ongoingCalls = globalThis.episodeApiCalls.filter(path => path.startsWith("/episodes?"));
  assert.equal(ongoingCalls.length, 2);
  for (const path of ongoingCalls) {
    const query = new URLSearchParams(path.split("?")[1]);
    assert.equal(query.get("area_id"), "area-1");
    assert.equal(query.get("limit"), "101");
    assert.equal(query.get("offset"), "0");
    assert.equal(query.has("started_before"), false);
  }
  assert.deepEqual(state.ongoingList.map(item => item.id), ["new", "history", "quiet"]);
  assert.equal(root.historyMarker.hidden, true);
  assert.equal(root.region.hidden, false);
  assert.match(root.ongoingItems.innerHTML, /data-select-episode="new"/);
  assert.equal(root.timeline.ongoingHeight, "44px");
  assert.doesNotMatch(globalThis.episodeApiCalls.find(path => path.startsWith("/covers?")) || "", /history/);
});

test("ongoing refresh reapplies cached covers after rebuilding pinned markers", async () => {
  const root = ongoingRoot();
  const state = ongoingState(root, {
    covers: { new: "evidence-new" },
    coverRequestedIds: new Set(["new"]),
  });
  globalThis.episodeResponses = [[
    { id: "new", primary_area_id: "area-1", start_time: "2026-09-30T13:00:00Z", state: "active", trigger_type: "motion" },
  ], []];
  assert.equal(await module.refreshOngoingEpisodes(root, state), true);
  assert.equal(root.coverImage.src, "/api/v1/evidence/evidence-new/thumbnail");
});

test("ongoing display is capped at 100 total items with a concise overflow notice", async () => {
  const root = ongoingRoot();
  const state = ongoingState(root);
  globalThis.episodeResponses = [
    Array.from({ length: 101 }, (_, index) => ({
      id: `active-${index}`,
      primary_area_id: "area-1",
      start_time: `2026-09-30T${String(index % 24).padStart(2, "0")}:00:00Z`,
      state: "active",
    })),
    [],
  ];
  globalThis.coverResponses = [{}];
  assert.equal(await module.refreshOngoingEpisodes(root, state), true);
  assert.equal(state.ongoingList.length, 100);
  assert.match(root.notice.textContent, /100 most recent/);
});

test("ongoing closure unhides history, updates canonical state, and preserves the selected viewer", async () => {
  const root = ongoingRoot("history");
  const active = { id: "history", primary_area_id: "area-1", start_time: "2026-09-30T12:00:00Z", state: "active", trigger_type: "motion" };
  const state = ongoingState(root, {
    list: [active],
    knownEpisodes: new Map([[active.id, active]]),
    ongoingList: [active],
    ongoingObservedIds: new Set([active.id]),
  });
  globalThis.episodeApiCalls = [];
  globalThis.episodeResponses = [[] , []];
  globalThis.episodeDetailResponse = { ...active, state: "closed", end_time: "2026-09-30T12:01:00Z" };
  assert.equal(await module.refreshOngoingEpisodes(root, state), true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.ongoingList.length, 0);
  assert.equal(root.historyMarker.hidden, false);
  assert.equal(state.list[0].state, "closed");
  assert.doesNotMatch(root.historyMarker.trigger.innerHTML, /Live|Ongoing/);
  assert.match(root.preview.time.textContent, /Sep 30, 2026/);
  assert.equal(root.dataset.selectedEpisode, "history");
  assert.equal(root.player.id, "player");
  assert.match(globalThis.episodeApiCalls.join("\n"), /\/episodes\/history/);
});

test("ongoing errors preserve entries and stale polling cannot mutate a detached route", async () => {
  const failedRoot = ongoingRoot();
  const previous = { id: "history", primary_area_id: "area-1", start_time: "2026-09-30T12:00:00Z", state: "active" };
  const failedState = ongoingState(failedRoot, { ongoingList: [previous] });
  globalThis.episodeResponses = [new Error("status unavailable")];
  assert.equal(await module.refreshOngoingEpisodes(failedRoot, failedState), false);
  assert.deepEqual(failedState.ongoingList, [previous]);
  assert.equal(failedRoot.region.hidden, false);
  assert.equal(failedRoot.notice.textContent, "Live status unavailable — retrying");

  const malformedRoot = ongoingRoot();
  const malformedState = ongoingState(malformedRoot);
  globalThis.episodeResponses = [{}];
  assert.equal(await module.refreshOngoingEpisodes(malformedRoot, malformedState), false);
  assert.equal(malformedRoot.notice.textContent, "Live status unavailable — retrying");

  const abortedRoot = ongoingRoot();
  const abortedState = ongoingState(abortedRoot);
  const abortError = new Error("aborted");
  abortError.name = "AbortError";
  globalThis.episodeResponses = [abortError];
  assert.equal(await module.refreshOngoingEpisodes(abortedRoot, abortedState), false);
  assert.equal(abortedRoot.notice.textContent, "Live status unavailable — retrying");

  const staleRoot = ongoingRoot();
  const staleState = ongoingState(staleRoot);
  let resolve;
  globalThis.episodeResponses = [new Promise(result => { resolve = result; })];
  const pending = module.refreshOngoingEpisodes(staleRoot, staleState);
  staleState.disposed = true;
  staleRoot.isConnected = false;
  resolve([{ id: "late", state: "active", start_time: "2026-09-30T15:00:00Z" }]);
  assert.equal(await pending, false);
  assert.deepEqual(staleState.ongoingList, []);
  assert.equal(staleRoot.ongoingItems.innerHTML, "");
});

test("date and area toolbar bounds remain available without a pagination footer", () => {
  const html = module.renderEpisodesPage(
    [{ id: "episode-1", primary_area_id: "area-1", start_time: "2026-09-29T12:00:00Z" }],
    new Map([["area-1", "Entrance"]]),
    2,
    true,
    { date: "2026-09-29", area: "area-1", areas: [{ id: "area-1", name: "Entrance" }] },
  );
  assert.match(html, /On or before/);
  assert.match(html, /Latest/);
  assert.match(html, /href="#episodes\?area=area-1"/);
  assert.doesNotMatch(html, /Timeline navigation|PAGINATION|pagination-summary|page-wide/);
});

test("older batches append in order and merge the boundary day without replacing existing DOM", async () => {
  const root = lazyRoot();
  globalThis.episodeApiCalls = [];
  const existingDayItems = { inserted: "", insertAdjacentHTML(_position, html) { this.inserted += html; } };
  root.groups.set("2026-09-29", {
    querySelector() { return existingDayItems; },
  });
  const state = lazyState(root);
  globalThis.episodeResponses = [[
    { id: "same-day", primary_area_id: "area-1", start_time: "2026-09-29T11:00:00Z", trigger_type: "motion" },
    { id: "older-day", primary_area_id: "area-1", start_time: "2026-09-28T11:00:00Z", trigger_type: "access" },
    { id: "existing", primary_area_id: "area-1", start_time: "2026-09-29T10:00:00Z" },
  ]];
  globalThis.coverResponses = [{}];
  await module.loadOlderEpisodes(root, state);
  assert.deepEqual(state.list.map(item => item.id), ["existing", "same-day", "older-day"]);
  assert.match(existingDayItems.inserted, /data-select-episode="same-day"/);
  assert.ok(root.groups.has("2026-09-28"));
  assert.match(root.items.inserted, /data-history-date="2026-09-28"/);
  assert.equal(root.items.inserted.includes("existing"), false);
});

test("lazy-rendered markers are emitted inside the existing timeline without replacing the viewer", async () => {
  const html = module.renderEpisodesPage(
    [{ id: "existing", primary_area_id: "area-1", start_time: "2026-09-29T12:00:00Z" }],
    new Map([["area-1", "Entrance"]]),
    1,
    false,
  );
  assert.match(html, /data-history-items/);
  assert.match(html, /data-history-load-state/);
  assert.doesNotMatch(html, /Timeline navigation|page-wide/);
  assert.match(html, /data-select-episode="existing"/);
});

test("cover batches are limited to newly appended IDs and merge with existing cover state", async () => {
  const root = lazyRoot();
  globalThis.episodeApiCalls = [];
  const state = lazyState(root, { covers: { existing: "cover-existing" } });
  globalThis.episodeResponses = [[{ id: "new", primary_area_id: "area-1", start_time: "2026-09-28T11:00:00Z" }]];
  globalThis.coverResponses = [{ new: "cover-new" }];
  await module.loadOlderEpisodes(root, state);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(state.covers, { existing: "cover-existing", new: "cover-new" });
  const coverCall = globalThis.episodeApiCalls.find(path => path.startsWith("/covers?"));
  assert.match(coverCall, /ids=new/);
  assert.doesNotMatch(coverCall, /existing/);
});

test("older loading is single-flight and retry uses the unchanged raw offset", async () => {
  const root = lazyRoot();
  globalThis.episodeApiCalls = [];
  const state = lazyState(root);
  let resolve;
  globalThis.episodeResponses = [new Promise(result => { resolve = result; })];
  globalThis.coverResponses = [{}];
  const first = module.loadOlderEpisodes(root, state);
  const second = module.loadOlderEpisodes(root, state);
  assert.equal(await second, false);
  assert.equal(globalThis.episodeApiCalls.filter(path => path.startsWith("/episodes?")).length, 1);
  resolve([]);
  assert.equal(await first, true);
  assert.equal(state.rawOffset, 48);

  const failingRoot = lazyRoot();
  const failingState = lazyState(failingRoot);
  globalThis.episodeResponses = [new Error("temporary")];
  const originalApi = globalThis.episodeApiCalls.length;
  const failed = await module.loadOlderEpisodes(failingRoot, failingState);
  assert.equal(failed, false);
  assert.equal(failingState.rawOffset, 48);
  assert.match(failingRoot.status.textContent, /temporary/);
  assert.equal(failingRoot.retry.hidden, false);
  globalThis.episodeResponses = [[]];
  failingState.loadError = false;
  assert.equal(await module.loadOlderEpisodes(failingRoot, failingState), true);
  const olderCalls = globalThis.episodeApiCalls.slice(originalApi).filter(path => path.startsWith("/episodes?"));
  assert.equal(new URLSearchParams(olderCalls[0].split("?")[1]).get("offset"), "48");
  assert.equal(new URLSearchParams(olderCalls[1].split("?")[1]).get("offset"), "48");
});

test("lazy loading stops at end or the bounded cap and stale responses cannot append", async () => {
  const endedRoot = lazyRoot();
  const endedState = lazyState(endedRoot, { hasMore: false });
  globalThis.episodeResponses = [[{ id: "never", start_time: "2026-09-27T11:00:00Z" }]];
  assert.equal(await module.loadOlderEpisodes(endedRoot, endedState), false);
  assert.doesNotMatch(globalThis.episodeApiCalls.join("\n"), /never/);

  const capRoot = lazyRoot();
  const capItems = Array.from({ length: 480 }, (_, index) => ({ id: `id-${index}`, start_time: "2026-09-29T12:00:00Z" }));
  const capState = lazyState(capRoot, { list: capItems, loadedIds: new Set(capItems.map(item => item.id)), rawOffset: 480 });
  assert.equal(await module.loadOlderEpisodes(capRoot, capState), false);
  assert.equal(capRoot.status.textContent, "Choose an earlier date to continue");

  const staleRoot = lazyRoot();
  const staleState = lazyState(staleRoot);
  let resolve;
  globalThis.episodeResponses = [new Promise(result => { resolve = result; })];
  const pending = module.loadOlderEpisodes(staleRoot, staleState);
  staleState.disposed = true;
  staleRoot.isConnected = false;
  resolve([{ id: "late", start_time: "2026-09-27T11:00:00Z" }]);
  await pending;
  assert.equal(staleState.list.some(item => item.id === "late"), false);
});

test("history CSS keeps desktop rail and mobile window loading boundaries explicit", async () => {
  const css = await readFile(new URL("../../src/episode/ui/episode-history.css", import.meta.url), "utf8");
  assert.match(css, /\.episode-history-rail\s*\{[^}]*overflow-y: auto/);
  assert.match(css, /\.episode-history-timeline\s*\{[^}]*max-height: min\(76vh, 820px\)[^}]*position: sticky[^}]*top: 1rem/);
  assert.match(css, /\.episode-history-ongoing\s*\{[^}]*position: relative[^}]*z-index: 2/);
  assert.match(css, /\.episode-history-ongoing-items\s*\{[^}]*max-height: min\(30vh, 240px\)[^}]*overflow-y: auto/);
  assert.match(css, /\.episode-history-ongoing\[hidden\]/);
  assert.match(css, /grid-template-columns: minmax\(240px, 280px\) minmax\(0, 1fr\)/);
  assert.match(css, /\.episode-history-day-heading\s*\{[^}]*position: sticky[^}]*top: 0/);
  assert.match(css, /\.episode-history-preview \{ max-width: none; order: -1; position: static; \}/);
  assert.match(css, /\.episode-history-empty \{ order: -1; width: 100%; \}/);
  assert.match(css, /\.episode-history-ongoing \{ position: sticky; top: 0; \}/);
  assert.match(css, /\.episode-history-day-heading \{ top: var\(--episode-history-ongoing-height, 0px\); \}/);
  assert.match(css, /\.episode-history-rail \{ max-height: none; overflow: visible; width: 100%; \}/);
  assert.doesNotMatch(css, /episode-history-timeline-link/);
  assert.match(css, /\.episode-history-load-retry\[hidden\]/);
  assert.match(css, /max-width: 900px/);
  assert.match(await readFile(new URL("../../src/episode/ui/episodes-page.js", import.meta.url), "utf8"), /windowObject\?\.addEventListener\?\.\("scroll"/);
});

test("local day bounds use calendar-midnight boundaries for the upper bound", () => {
  const bounds = module.localDayBounds("2026-03-08");
  assert.equal(bounds.date, "2026-03-08");
  const start = new Date(bounds.startedFrom);
  const end = new Date(bounds.startedBefore);
  const expectedEnd = new Date(start);
  expectedEnd.setDate(expectedEnd.getDate() + 1);
  assert.equal(end.getTime(), expectedEnd.getTime());
  assert.ok([23, 24, 25].includes((end - start) / 3_600_000));
});

test("empty and stale cover states remain bounded and actionable", async () => {
  const stale = coverRoot(false);
  await module.loadEpisodeCovers(stale.root, ["episode-1"], async () => ({ "episode-1": "evidence-1" }));
  assert.equal(stale.image.src, "/logo.svg");

  setEpisodeApi([]);
  globalThis.nextEpisodeRoot = null;
  globalThis.document = { querySelector() { return null; } };
  await module.episodes();
  assert.match(globalThis.episodeHtml, /No Episodes found/);
  assert.doesNotMatch(globalThis.episodeApiCalls.join("\n"), /covers/);
});

test("initial marker scrolling uses the marker position inside the rail viewport", () => {
  const marker = { getBoundingClientRect() { return { top: 520 }; } };
  const rail = {
    clientHeight: 240,
    scrollHeight: 1200,
    scrollTop: 40,
    getBoundingClientRect() { return { top: 160 }; },
    querySelector() { return marker; },
  };
  module.scrollSelectedMarkerIntoView(rail);
  assert.equal(rail.scrollTop, 320);
});
