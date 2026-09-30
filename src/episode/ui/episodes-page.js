import { API, api } from "./api.js?v=4";
import { escHtml } from "./dom.js";
import {
  bindEpisodeHistoryPlayer,
  renderEpisodeRecordingPanel,
} from "./episode-history-player.js?v=2";
import { updateRecentEpisodes } from "./sidebar.js?v=4";
import { showContent, showError, showLoading } from "./view.js?v=1";

const PAGE_SIZE = 48;
const ONGOING_LIMIT = 101;
const ONGOING_DISPLAY_LIMIT = 100;
const ONGOING_INTERVAL = 5_000;
const ONGOING_TIMEOUT = 10_000;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
let historyGeneration = 0;
let closeActiveHistory = () => {};

export function closeEpisodeHistory() {
  historyGeneration += 1;
  closeActiveHistory();
  closeActiveHistory = () => {};
}

function localDateString(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = part => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function localDayBounds(value) {
  const date = DATE_PATTERN.test(String(value || ""))
    ? String(value)
    : localDateString(value);
  if (!DATE_PATTERN.test(date)) return null;
  const [year, month, day] = date.split("-").map(Number);
  const start = new Date(year, month - 1, day);
  if (start.getFullYear() !== year || start.getMonth() !== month - 1 || start.getDate() !== day) {
    return null;
  }
  const end = new Date(year, month - 1, day + 1);
  return {
    date,
    startedFrom: start.toISOString(),
    startedBefore: end.toISOString(),
  };
}

function readableDay(value, now = new Date()) {
  const bounds = localDayBounds(value);
  if (!bounds) return "Latest";
  const today = localDateString(now);
  const yesterday = localDateString(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  if (bounds.date === today) return "Today";
  if (bounds.date === yesterday) return "Yesterday";
  const [year, month, day] = bounds.date.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric", year: "numeric",
  });
}

function timeLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "–";
  return date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
}

function dateTimeLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
}

function areaName(item, areaNames) {
  return areaNames.get(item.primary_area_id) || item.primary_area_id || "Unknown Area";
}

function sortEpisodes(list) {
  return [...list].sort((left, right) => {
    const difference = new Date(right.start_time) - new Date(left.start_time);
    return difference || String(right.id).localeCompare(String(left.id));
  });
}

function uniqueEpisodes(list) {
  const seen = new Set();
  return list.filter(item => {
    if (!item?.id || seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function humanType(value) {
  const text = String(value || "activity")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, character => character.toUpperCase());
  return text || "Activity";
}

function episodeStatus(item) {
  const state = String(item.state || "").toLowerCase();
  if (state === "active") return "Live";
  if (state === "quiescent") return "Ongoing";
  return "";
}

function episodeCover(item, large = false) {
  const tag = large ? "div" : "span";
  return `<${tag} class="episode-history-cover episode-cover-placeholder${large ? " episode-preview-cover" : ""}"${large ? " data-preview-cover" : ""} data-cover-episode="${escHtml(item.id)}">
    <img class="episode-cover-image" data-episode-id="${escHtml(item.id)}" src="/logo.svg" loading="lazy" decoding="async" alt="">
    <span>Loading preview…</span>
  </${tag}>`;
}

function episodeMarker(item, areaNames, selectedId) {
  const selected = item.id === selectedId;
  const status = episodeStatus(item);
  const area = areaName(item, areaNames);
  const trigger = humanType(item.trigger_type);
  const description = `${area} · ${trigger}${status ? ` · ${status}` : ""} · ${timeLabel(item.start_time)}`;
  return `<button type="button" class="episode-history-marker${selected ? " is-selected" : ""}" data-select-episode="${escHtml(item.id)}" aria-pressed="${selected}" aria-label="${escHtml(description)}">
    <time class="episode-history-marker-time" datetime="${escHtml(item.start_time)}">${escHtml(timeLabel(item.start_time))}</time>
    <span class="episode-history-marker-spine" aria-hidden="true"></span>
    <span class="episode-history-marker-main">
      ${episodeCover(item)}
      <span class="episode-history-marker-content"><strong>${escHtml(area)}</strong><span class="episode-history-marker-trigger">${escHtml(trigger)}${status ? ` · <span class="episode-history-marker-status">${escHtml(status)}</span>` : ""}</span></span>
    </span>
  </button>`;
}

function episodeGroups(list, areaNames, selectedId) {
  const groups = groupedEpisodeItems(list);
  return groups.map(group => `<section class="episode-history-day-group" data-history-date="${escHtml(group.date)}">
    <h3 class="episode-history-day-heading">${escHtml(readableDay(group.date))}</h3>
    <div class="episode-history-day-items" aria-label="Episodes on ${escHtml(readableDay(group.date))}">
      ${group.items.map(item => episodeMarker(item, areaNames, selectedId)).join("")}
    </div>
  </section>`).join("");
}

function renderOngoingRegion() {
  return `<section class="episode-history-ongoing" data-history-ongoing hidden aria-labelledby="episode-history-ongoing-title">
    <header class="episode-history-ongoing-heading"><h2 id="episode-history-ongoing-title">Ongoing</h2></header>
    <div class="episode-history-ongoing-items" data-history-ongoing-items></div>
    <p class="episode-history-ongoing-state" data-history-ongoing-state role="status" aria-live="polite" hidden></p>
  </section>`;
}

function renderAreaOptions(areas, selectedArea) {
  return [{ id: "", name: "All Areas" }, ...areas]
    .map(area => `<option value="${escHtml(area.id)}"${area.id === selectedArea ? " selected" : ""}>${escHtml(area.name)}</option>`)
    .join("");
}

function historyHash({ date = "", area = "" } = {}) {
  const query = new URLSearchParams();
  if (date) query.set("date", date);
  if (area) query.set("area", area);
  const serialized = query.toString();
  return `#episodes${serialized ? `?${serialized}` : ""}`;
}

function renderToolbar(date, area, areas) {
  return `<div class="episode-history-toolbar">
    <label class="episode-history-day-picker" for="episode-history-date"><span>On or before</span><input id="episode-history-date" type="date" value="${escHtml(date)}" data-history-date></label>
    <label class="episode-history-area-picker" for="episode-history-area"><span>Area</span><select id="episode-history-area" data-history-area>${renderAreaOptions(areas, area)}</select></label>
    ${date ? `<a class="button button-ghost episode-history-latest" href="${escHtml(historyHash({ area }))}">Latest</a>` : ""}
  </div>`;
}

function renderPreview(item, areaNames) {
  if (!item) return `<section class="episode-history-preview is-empty" aria-label="Episode viewer"><div class="empty">Select an Episode to view its media.</div></section>`;
  const status = episodeStatus(item);
  return `<section class="episode-history-preview" data-episode-preview aria-label="Episode viewer">
    <header class="episode-history-preview-heading">
      <div><h2>${escHtml(areaName(item, areaNames))}</h2><p class="episode-history-preview-time">${escHtml(dateTimeLabel(item.start_time))}${status ? ` · ${escHtml(status)}` : ""}</p></div>
      <a class="episode-history-preview-details-link" href="#episode/${escHtml(item.id)}">Open details</a>
    </header>
    ${renderEpisodeRecordingPanel({ cover: episodeCover(item, true) })}
  </section>`;
}

export function renderEpisodesPage(list, areaNames, page, hasNext, options = {}) {
  const date = options.date || "";
  const area = options.area || "";
  const areas = options.areas || [];
  const ordered = sortEpisodes(list);
  const selectedId = options.selectedId || ordered[0]?.id || "";
  const selected = ordered.find(item => item.id === selectedId) || ordered[0];
  const timelineRail = `<div class="episode-history-rail" aria-label="Episodes, newest first">
    <div class="episode-history-items" data-history-items>${episodeGroups(ordered, areaNames, selectedId)}</div>
    <div class="episode-history-load-state" data-history-load-state role="status" aria-live="polite"></div>
    <button type="button" class="button button-ghost episode-history-load-retry" data-history-load-retry hidden>Retry</button>
  </div>`;
  const timeline = `<div class="episode-history-timeline" data-history-timeline>${renderOngoingRegion()}${timelineRail}</div>`;
  return `<section class="episode-history" data-episodes-page data-selected-episode="${escHtml(selectedId)}">
    ${renderToolbar(date, area, areas)}
    ${ordered.length
      ? `<div class="episode-history-layout">${timeline}${renderPreview(selected, areaNames)}</div>`
      : `<div class="episode-history-layout">${timeline}<div class="episode-history-empty"><strong>No Episodes found</strong><span>${date ? "Try a later date or choose Latest." : "Episodes will appear here when preserved activity is recorded."}</span></div></div>`}
  </section>`;
}

function coverSlots(root) { return [...root.querySelectorAll("[data-cover-episode]")]; }

export function applyEpisodeCovers(root, covers, episodeIds = null) {
  if (!root?.isConnected) return;
  const scopedIds = episodeIds ? new Set(episodeIds) : null;
  for (const slot of coverSlots(root)) {
    if (scopedIds && !scopedIds.has(slot.dataset.coverEpisode)) continue;
    const coverId = slot.dataset.coverEpisode;
    const hasCoverResult = Object.prototype.hasOwnProperty.call(covers || {}, coverId);
    if (!hasCoverResult) continue;
    const evidenceId = covers[coverId];
    if (!evidenceId) {
      const label = slot.querySelector("span");
      if (label) label.textContent = "No snapshot";
      continue;
    }
    const image = slot.querySelector("img");
    if (!image) continue;
    const thumbnailUrl = `${API}/evidence/${encodeURIComponent(evidenceId)}/thumbnail`;
    const fileUrl = `${API}/evidence/${encodeURIComponent(evidenceId)}/file`;
    image.src = thumbnailUrl;
    if (slot.dataset.previewCover !== undefined) {
      root.querySelector("[data-recording-player]")?.setAttribute("poster", thumbnailUrl);
    }
    image.onerror = () => { image.onerror = null; image.src = fileUrl; };
    slot.classList.remove("episode-cover-placeholder");
    slot.querySelector("span")?.remove();
  }
}

export async function loadEpisodeCovers(
  root,
  episodeIds,
  fetchCovers = api,
  isCurrent = () => root?.isConnected !== false,
) {
  if (!root || !episodeIds.length) return {};
  try {
    const covers = await fetchCovers(`/covers?ids=${encodeURIComponent(episodeIds.join(","))}`);
    if (!isCurrent()) return {};
    const resolved = Object.fromEntries(episodeIds.map(id => [id, covers?.[id] || null]));
    applyEpisodeCovers(root, resolved, episodeIds);
    return resolved;
  } catch {
    if (isCurrent() && root.isConnected) {
      const requestedIds = new Set(episodeIds);
      for (const slot of coverSlots(root)) {
        if (!requestedIds.has(slot.dataset.coverEpisode)) continue;
        const label = slot.querySelector("span");
        if (label) label.textContent = "Preview unavailable";
      }
    }
    return {};
  }
}

function ongoingRegion(root) { return root?.querySelector?.("[data-history-ongoing]"); }

function refreshOngoingLayout(root) {
  const timeline = root?.querySelector?.("[data-history-timeline]");
  const ongoing = ongoingRegion(root);
  if (!timeline?.style?.setProperty) return;
  timeline.style.setProperty("--episode-history-ongoing-height", `${ongoing?.offsetHeight || 0}px`);
}

function setHistoricalMarkerHidden(root, episodeId, hidden) {
  for (const marker of root?.querySelectorAll?.("[data-select-episode]") || []) {
    if (marker.dataset.selectEpisode !== episodeId) continue;
    if (marker.closest?.("[data-history-ongoing-items]")) continue;
    marker.hidden = hidden;
  }
}

function refreshHistoryDayVisibility(root) {
  for (const group of root?.querySelectorAll?.(".episode-history-day-group") || []) {
    const markers = [...group.querySelectorAll?.(".episode-history-marker") || []];
    if (markers.length) group.hidden = markers.every(marker => marker.hidden);
  }
}

function updateHistoricalMarker(root, item, areaNames = root.__episodeAreaNames || new Map()) {
  const status = episodeStatus(item);
  const description = `${areaName(item, areaNames)} · ${humanType(item.trigger_type)}${status ? ` · ${status}` : ""} · ${timeLabel(item.start_time)}`;
  for (const marker of root?.querySelectorAll?.("[data-select-episode]") || []) {
    if (marker.dataset.selectEpisode !== item.id || marker.closest?.("[data-history-ongoing-items]")) continue;
    marker.setAttribute?.("aria-label", description);
    const time = marker.querySelector?.(".episode-history-marker-time");
    if (time) {
      time.textContent = timeLabel(item.start_time);
      time.setAttribute?.("datetime", item.start_time || "");
    }
    const trigger = marker.querySelector?.(".episode-history-marker-trigger");
    if (trigger) {
      trigger.innerHTML = `${escHtml(humanType(item.trigger_type))}${status ? ` · <span class="episode-history-marker-status">${escHtml(status)}</span>` : ""}`;
    }
  }
}

function updatePreviewMetadata(root, item, areaNames) {
  if (root?.dataset?.selectedEpisode !== item.id) return;
  const preview = root.querySelector?.("[data-episode-preview]");
  if (!preview) return;
  const title = preview.querySelector?.(".episode-history-preview-heading h2");
  const time = preview.querySelector?.(".episode-history-preview-time");
  if (title) title.textContent = areaName(item, areaNames);
  if (time) {
    const status = episodeStatus(item);
    time.textContent = `${dateTimeLabel(item.start_time)}${status ? ` · ${status}` : ""}`;
  }
}

function setOngoingNotice(root, state, message = "", error = false) {
  const region = ongoingRegion(root);
  const notice = root?.querySelector?.("[data-history-ongoing-state]");
  state.ongoingNotice = message;
  if (notice) {
    notice.textContent = message;
    notice.hidden = !message;
    notice.classList?.toggle("is-error", error);
  }
  if (region) region.hidden = !(state.ongoingList.length || message);
  refreshOngoingLayout(root);
}

function renderOngoingItems(root, state) {
  const items = root?.querySelector?.("[data-history-ongoing-items]");
  const focusedId = globalThis.document?.activeElement?.dataset?.selectEpisode;
  if (items) {
    items.innerHTML = state.ongoingList.map(item => episodeMarker(
      item,
      state.areaNames,
      root.dataset.selectedEpisode,
    )).join("");
    if (focusedId && items.querySelectorAll) {
      const replacement = [...items.querySelectorAll("[data-select-episode]")]
        .find(marker => marker.dataset.selectEpisode === focusedId);
      replacement?.focus?.({ preventScroll: true });
    }
  }
  applyEpisodeCovers(root, state.covers);
  const region = ongoingRegion(root);
  if (region) region.hidden = !(state.ongoingList.length || state.ongoingNotice);
  refreshOngoingLayout(root);
}

function mergeCanonicalEpisode(state, item) {
  const current = state.knownEpisodes.get(item.id) || {};
  const merged = { ...current, ...item };
  state.knownEpisodes.set(item.id, merged);
  const index = state.list.findIndex(candidate => candidate.id === item.id);
  if (index >= 0) state.list[index] = merged;
  return merged;
}

function requestWithTimeout(path, state, slot = "ongoing") {
  const controller = new AbortController();
  const timer = globalThis.setTimeout(() => controller.abort(), ONGOING_TIMEOUT);
  timer?.unref?.();
  if (slot === "ongoing") state.ongoingController = controller;
  else state.detailControllers.set(slot, controller);
  return api(path, { signal: controller.signal }).finally(() => {
    globalThis.clearTimeout(timer);
    if (slot === "ongoing" && state.ongoingController === controller) state.ongoingController = null;
    if (slot !== "ongoing" && state.detailControllers.get(slot) === controller) state.detailControllers.delete(slot);
  });
}

function ongoingQuery(stateName, area) {
  const query = new URLSearchParams({ state: stateName, limit: String(ONGOING_LIMIT), offset: "0" });
  if (area) query.set("area_id", area);
  return `/episodes?${query}`;
}

async function refreshRemovedEpisode(root, state, item) {
  const id = item.id;
  if (state.detailControllers.has(id)) return;
  try {
    const detail = await requestWithTimeout(`/episodes/${encodeURIComponent(id)}`, state, id);
    if (!historyStateCurrent(root, state) || state.ongoingObservedIds?.has(id) || !detail?.id) return;
    const merged = mergeCanonicalEpisode(state, detail);
    updateHistoricalMarker(root, merged);
    updatePreviewMetadata(root, merged, state.areaNames);
  } catch {
    // Keep the historical entry and current player intact when detail refresh fails.
  }
}

function applyOngoingEpisodes(root, state, activeResult, quiescentResult) {
  state.knownEpisodes ||= new Map(state.list.map(item => [item.id, item]));
  state.coverRequestedIds ||= new Set();
  const active = Array.isArray(activeResult) ? activeResult.filter(item => String(item?.state).toLowerCase() === "active") : [];
  const quiescent = Array.isArray(quiescentResult) ? quiescentResult.filter(item => String(item?.state).toLowerCase() === "quiescent") : [];
  const previousIds = state.ongoingObservedIds || new Set(state.ongoingList.map(item => item.id));
  const combined = sortEpisodes(uniqueEpisodes([...active, ...quiescent]));
  const overflow = active.length > ONGOING_DISPLAY_LIMIT
    || quiescent.length > ONGOING_DISPLAY_LIMIT
    || combined.length > ONGOING_DISPLAY_LIMIT;
  const observedIds = new Set(combined.map(item => item.id));
  state.ongoingList = combined.slice(0, ONGOING_DISPLAY_LIMIT);
  const currentIds = new Set(state.ongoingList.map(item => item.id));

  for (const item of combined) {
    const merged = mergeCanonicalEpisode(state, item);
    updateHistoricalMarker(root, merged);
    setHistoricalMarkerHidden(root, merged.id, currentIds.has(merged.id));
    updatePreviewMetadata(root, merged, state.areaNames);
  }
  state.ongoingObservedIds = observedIds;
  for (const id of previousIds) {
    if (observedIds.has(id)) continue;
    const known = state.knownEpisodes.get(id);
    setHistoricalMarkerHidden(root, id, false);
    if (known) {
      if (state.loadedIds.has(id) || root.dataset.selectedEpisode === id) {
        void refreshRemovedEpisode(root, state, known);
      }
    }
  }
  refreshHistoryDayVisibility(root);
  renderOngoingItems(root, state);
  setOngoingNotice(root, state, overflow ? "Showing the 100 most recent ongoing Episodes." : "");
  const newlyPinned = state.ongoingList
    .map(item => item.id)
    .filter(id => !state.coverRequestedIds.has(id));
  if (newlyPinned.length) {
    newlyPinned.forEach(id => state.coverRequestedIds.add(id));
    const coverGeneration = state.generation;
    void loadEpisodeCovers(
      root,
      newlyPinned,
      api,
      () => historyStateCurrent(root, state, coverGeneration),
    ).then(covers => {
      if (historyStateCurrent(root, state, coverGeneration)) state.covers = { ...state.covers, ...covers };
    });
  }
}

function historyDocumentHidden() { return globalThis.document?.visibilityState === "hidden"; }

function scheduleOngoingPoll(root, state) {
  if (!historyStateCurrent(root, state) || historyDocumentHidden()) return;
  globalThis.clearTimeout(state.ongoingTimer);
  state.ongoingTimer = globalThis.setTimeout(() => {
    state.ongoingTimer = null;
    void refreshOngoingEpisodes(root, state);
  }, ONGOING_INTERVAL);
  state.ongoingTimer?.unref?.();
}

export async function refreshOngoingEpisodes(root, state) {
  if (!historyStateCurrent(root, state) || state.ongoingLoading || historyDocumentHidden()) return false;
  state.ongoingLoading = true;
  const requestGeneration = state.generation;
  try {
    const active = await requestWithTimeout(ongoingQuery("active", state.area), state);
    if (!historyStateCurrent(root, state, requestGeneration)) return false;
    if (!Array.isArray(active)) throw new Error("Live status unavailable");
    const quiescent = await requestWithTimeout(ongoingQuery("quiescent", state.area), state);
    if (!historyStateCurrent(root, state, requestGeneration)) return false;
    if (!Array.isArray(quiescent)) throw new Error("Live status unavailable");
    applyOngoingEpisodes(root, state, active, quiescent);
    return true;
  } catch (error) {
    if (historyStateCurrent(root, state, requestGeneration) && !historyDocumentHidden()) {
      setOngoingNotice(root, state, "Live status unavailable — retrying", true);
    }
    return false;
  } finally {
    state.ongoingLoading = false;
    if (historyStateCurrent(root, state, requestGeneration)) scheduleOngoingPoll(root, state);
  }
}

function bindOngoingPolling(root, state) {
  const onResize = () => refreshOngoingLayout(root);
  const onVisibility = () => {
    if (historyDocumentHidden()) {
      globalThis.clearTimeout(state.ongoingTimer);
      state.ongoingTimer = null;
      state.ongoingController?.abort();
      return;
    }
    void refreshOngoingEpisodes(root, state);
  };
  globalThis.window?.addEventListener?.("resize", onResize);
  globalThis.document?.addEventListener?.("visibilitychange", onVisibility);
  refreshOngoingLayout(root);
  void refreshOngoingEpisodes(root, state);
  return () => {
    globalThis.clearTimeout(state.ongoingTimer);
    state.ongoingTimer = null;
    state.ongoingController?.abort();
    for (const controller of state.detailControllers.values()) controller.abort();
    globalThis.window?.removeEventListener?.("resize", onResize);
    globalThis.document?.removeEventListener?.("visibilitychange", onVisibility);
  };
}

function historyStateCurrent(root, state, generation = state.generation) {
  return !state.disposed
    && generation === state.generation
    && root?.isConnected !== false;
}

function groupedEpisodeItems(items) {
  const groups = [];
  const byDate = new Map();
  for (const item of sortEpisodes(items)) {
    const date = localDateString(item.start_time);
    if (!date) continue;
    let group = byDate.get(date);
    if (!group) {
      group = { date, items: [] };
      byDate.set(date, group);
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups;
}

function appendTimelineItems(root, items, areaNames, selectedId) {
  if (!items.length) return;
  const itemsRoot = root.querySelector("[data-history-items]");
  if (!itemsRoot) return;
  for (const group of groupedEpisodeItems(items)) {
    const selector = `[data-history-date="${group.date}"]`;
    const existing = itemsRoot.querySelector(selector);
    if (existing) {
      existing.querySelector(".episode-history-day-items")?.insertAdjacentHTML(
        "beforeend",
        group.items.map(item => episodeMarker(item, areaNames, selectedId)).join(""),
      );
      continue;
    }
    itemsRoot.insertAdjacentHTML(
      "beforeend",
      `<section class="episode-history-day-group" data-history-date="${escHtml(group.date)}">
        <h3 class="episode-history-day-heading">${escHtml(readableDay(group.date))}</h3>
        <div class="episode-history-day-items" aria-label="Episodes on ${escHtml(readableDay(group.date))}">
          ${group.items.map(item => episodeMarker(item, areaNames, selectedId)).join("")}
        </div>
      </section>`,
    );
  }
}

function setHistoryLoadState(root, message = "", error = false, retry = false) {
  const status = root.querySelector("[data-history-load-state]");
  if (status) {
    status.textContent = message;
    status.classList.toggle("is-error", error);
  }
  const retryButton = root.querySelector("[data-history-load-retry]");
  if (retryButton) retryButton.hidden = !retry;
}

function isHistoryMobile() {
  return globalThis.window?.matchMedia?.("(max-width: 900px)")?.matches === true;
}

function historyNearBottom(rail) {
  if (isHistoryMobile()) {
    const documentElement = globalThis.document?.documentElement;
    const windowHeight = Number(globalThis.window?.innerHeight) || 0;
    const scrollTop = Number(globalThis.window?.scrollY) || 0;
    const pageHeight = Number(documentElement?.scrollHeight) || 0;
    return pageHeight - (scrollTop + windowHeight) <= 240;
  }
  return rail.scrollHeight - (rail.scrollTop + rail.clientHeight) <= 240;
}

function historyAtCapacity(state) {
  return state.list.length >= 480 || state.rawOffset >= 480;
}

export async function loadOlderEpisodes(root, state) {
  if (!historyStateCurrent(root, state) || state.loading || state.loadError || !state.hasMore) return false;
  if (historyAtCapacity(state)) {
    state.hasMore = false;
    setHistoryLoadState(root, "Choose an earlier date to continue");
    return false;
  }
  state.loading = true;
  const requestGeneration = state.generation;
  const requestOffset = state.rawOffset;
  setHistoryLoadState(root, "Loading older Episodes…");
  try {
    const result = await api(episodesQuery({
      area: state.area,
      before: state.startedBefore,
      offset: requestOffset,
    }));
    if (!historyStateCurrent(root, state, requestGeneration)) return false;
    state.loadError = false;
    const raw = Array.isArray(result) ? result : [];
    const consumed = Math.min(raw.length, PAGE_SIZE);
    state.rawOffset += consumed;
    state.hasMore = raw.length > PAGE_SIZE;
    const additions = [];
    for (const item of uniqueEpisodes(raw.slice(0, PAGE_SIZE))) {
      if (state.loadedIds.has(item.id)) continue;
      state.loadedIds.add(item.id);
      additions.push(item);
    }
    if (additions.length) {
      state.knownEpisodes ||= new Map(state.list.map(item => [item.id, item]));
      state.coverRequestedIds ||= new Set();
      state.list.push(...additions);
      additions.forEach(item => state.knownEpisodes.set(item.id, item));
      state.list = sortEpisodes(state.list);
      appendTimelineItems(root, additions, state.areaNames, root.dataset.selectedEpisode);
      additions.forEach(item => {
        if ((state.ongoingList || []).some(ongoing => ongoing.id === item.id)) setHistoricalMarkerHidden(root, item.id, true);
      });
      refreshHistoryDayVisibility(root);
      additions.forEach(item => state.coverRequestedIds.add(item.id));
      const coverGeneration = state.generation;
      void loadEpisodeCovers(
        root,
        additions.map(item => item.id),
        api,
        () => historyStateCurrent(root, state, coverGeneration),
      ).then(covers => {
        if (!historyStateCurrent(root, state, coverGeneration)) return;
        state.covers = { ...state.covers, ...covers };
      });
    }
    if (historyAtCapacity(state) && state.hasMore) {
      state.hasMore = false;
      setHistoryLoadState(root, "Choose an earlier date to continue");
    } else if (!state.hasMore) {
      setHistoryLoadState(root, "End of history");
    } else {
      setHistoryLoadState(root);
    }
    return true;
  } catch (error) {
    if (historyStateCurrent(root, state, requestGeneration)) {
      state.loadError = true;
      setHistoryLoadState(root, error?.message || "Older Episodes are temporarily unavailable.", true, true);
    }
    return false;
  } finally {
    if (requestGeneration === state.generation) state.loading = false;
  }
}

function bindHistoryLoading(root, state) {
  const rail = root.querySelector(".episode-history-rail");
  if (!rail) return () => {};
  const maybeLoad = () => {
    if (state.loadError || !historyNearBottom(rail)) return;
    void loadOlderEpisodes(root, state).then(loaded => {
      if (loaded && state.hasMore && !state.loadError && historyStateCurrent(root, state) && historyNearBottom(rail)) {
        maybeLoad();
      }
    });
  };
  const retryButton = root.querySelector("[data-history-load-retry]");
  const onRetry = () => {
    state.loadError = false;
    void loadOlderEpisodes(root, state);
  };
  const windowObject = globalThis.window;
  const onScroll = () => maybeLoad();
  const onResize = () => maybeLoad();
  rail.addEventListener?.("scroll", onScroll);
  windowObject?.addEventListener?.("scroll", onScroll, { passive: true });
  windowObject?.addEventListener?.("resize", onResize);
  retryButton?.addEventListener?.("click", onRetry);
  if (!state.hasMore) setHistoryLoadState(root, "End of history");
  maybeLoad();
  return () => {
    rail.removeEventListener?.("scroll", onScroll);
    windowObject?.removeEventListener?.("scroll", onScroll);
    windowObject?.removeEventListener?.("resize", onResize);
    retryButton?.removeEventListener?.("click", onRetry);
  };
}

function bindEpisodeHistory(root, list, areaNames, state) {
  if (!root) return;
  state.list = list;
  root.__episodeAreaNames = areaNames;
  const onClick = event => {
    const button = event.target?.closest?.("[data-select-episode]");
    if (!button) return;
    const selected = state.list.find(item => item.id === button.dataset.selectEpisode)
      || state.ongoingList.find(item => item.id === button.dataset.selectEpisode);
    if (!selected) return;
    root.dataset.selectedEpisode = selected.id;
    state.detailToken += 1;
    root.dataset.detailToken = String(state.detailToken);
    state.cleanupPlayer?.();
    root.querySelectorAll("[data-select-episode]").forEach(marker => {
      const active = marker === button;
      marker.classList.toggle("is-selected", active);
      marker.setAttribute("aria-pressed", String(active));
    });
    const preview = root.querySelector("[data-episode-preview]");
    if (preview) {
      preview.outerHTML = renderPreview(selected, areaNames);
      applyEpisodeCovers(root, state.covers);
      state.cleanupPlayer = bindEpisodeHistoryPlayer(root, selected, state.getDevices, state.detailToken);
    } else {
      const empty = root.querySelector(".episode-history-empty");
      if (empty) {
        empty.outerHTML = renderPreview(selected, areaNames);
        applyEpisodeCovers(root, state.covers);
        state.cleanupPlayer = bindEpisodeHistoryPlayer(root, selected, state.getDevices, state.detailToken);
      }
    }
    if (globalThis.window?.matchMedia?.("(max-width: 900px)")?.matches) {
      root.querySelector("[data-episode-preview]")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };
  root.addEventListener?.("click", onClick);
  root.querySelector("[data-history-date]")?.addEventListener("change", event => {
    if (localDayBounds(event.target.value)) window.location.hash = historyHash({ date: event.target.value, area: state.area });
  });
  root.querySelector("[data-history-area]")?.addEventListener("change", event => {
    const date = root.querySelector("[data-history-date]")?.value || "";
    window.location.hash = historyHash({ date, area: event.target.value });
  });
  scrollSelectedMarkerIntoView(root.querySelector(".episode-history-rail"));
  const selected = list.find(item => item.id === root.dataset.selectedEpisode) || list[0];
  if (selected) {
    state.cleanupPlayer = bindEpisodeHistoryPlayer(root, selected, state.getDevices, state.detailToken);
  }
  state.cleanupLoading = bindHistoryLoading(root, state);
  state.cleanupOngoing = bindOngoingPolling(root, state);
  state.cleanupInteractions = () => root.removeEventListener?.("click", onClick);
}

export function scrollSelectedMarkerIntoView(rail) {
  const selectedMarker = rail?.querySelector?.(".episode-history-marker.is-selected");
  if (!rail || !selectedMarker || rail.scrollHeight <= rail.clientHeight) return;
  const railRect = rail.getBoundingClientRect?.();
  const markerRect = selectedMarker.getBoundingClientRect?.();
  let markerTop;
  if (railRect && markerRect) {
    markerTop = markerRect.top - railRect.top + rail.scrollTop;
  } else {
    markerTop = 0;
    let node = selectedMarker;
    while (node && node !== rail) {
      markerTop += Number(node.offsetTop) || 0;
      node = node.offsetParent;
    }
  }
  rail.scrollTop = Math.max(0, markerTop - rail.clientHeight / 3);
}

function episodesQuery({ area = "", before = "", offset = 0, limit = PAGE_SIZE + 1 } = {}) {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (area) query.set("area_id", area);
  if (before) query.set("started_before", before);
  return `/episodes?${query}`;
}

export async function episodes(page = 1, parameters = new URLSearchParams()) {
  const generation = ++historyGeneration;
  closeActiveHistory();
  closeActiveHistory = () => {};
  showLoading();
  try {
    const area = parameters.get("area") || "";
    const requestedDate = parameters.get("date") || "";
    const bounds = localDayBounds(requestedDate);
    const startedBefore = bounds?.startedBefore || new Date().toISOString();
    const [result, areas] = await Promise.all([
      api(episodesQuery({ area, before: startedBefore, offset: 0 })),
      api("/areas?include_disabled=true"),
    ]);
    if (generation !== historyGeneration) return;
    const hasNext = result.length > PAGE_SIZE;
    const list = sortEpisodes(uniqueEpisodes(result.slice(0, PAGE_SIZE)));
    const areaNames = new Map(areas.map(item => [item.id, item.name]));
    showContent(renderEpisodesPage(list, areaNames, page, hasNext, {
      areas,
      area,
      date: bounds?.date || "",
      selectedId: list[0]?.id || "",
    }));
    updateRecentEpisodes();
    const root = document.querySelector("#view-content [data-episodes-page]");
    const state = {
      area,
      startedBefore,
      covers: {},
      coverRequestedIds: new Set(),
      detailToken: 1,
      list,
      ongoingList: [],
      ongoingObservedIds: new Set(),
      ongoingLoading: false,
      ongoingNotice: "",
      ongoingTimer: null,
      ongoingController: null,
      detailControllers: new Map(),
      knownEpisodes: new Map(list.map(item => [item.id, item])),
      areaNames,
      loadedIds: new Set(list.map(item => item.id)),
      rawOffset: Math.min(result.length, PAGE_SIZE),
      hasMore: hasNext,
      loading: false,
      loadError: false,
      disposed: false,
      generation: 1,
      devicesPromise: null,
      getDevices: async () => {
        if (state.devicesPromise) return state.devicesPromise;
        state.devicesPromise = api("/devices?include_disabled=true")
          .then(devices => devices || [])
          .catch(() => []);
        return state.devicesPromise;
      },
      cleanupPlayer: () => {},
      cleanupLoading: () => {},
      cleanupOngoing: () => {},
      cleanupInteractions: () => {},
    };
    if (root) root.dataset.detailToken = String(state.detailToken);
    bindEpisodeHistory(root, list, areaNames, state);
    closeActiveHistory = () => {
      state.disposed = true;
      state.generation += 1;
      state.cleanupPlayer?.();
      state.cleanupLoading?.();
      state.cleanupOngoing?.();
      state.cleanupInteractions?.();
    };
    list.forEach(item => state.coverRequestedIds.add(item.id));
    void loadEpisodeCovers(root, list.map(item => item.id), api, () => historyStateCurrent(root, state)).then(covers => {
      if (!historyStateCurrent(root, state)) return;
      state.covers = { ...state.covers, ...covers };
    });
  } catch (error) {
    if (generation === historyGeneration) showError(error.message);
  }
}
