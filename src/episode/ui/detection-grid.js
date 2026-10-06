// NVR Playback & Detections — Detection snapshot grid.
// Pure rendering functions + DOM wiring for the filterable, paginated
// snapshot grid in the bottom region of the timeline view.
import { escHtml } from "./dom.js";
import { fmtTime } from "./format.js";

export const PAGE_SIZE = 60;
export const VIRTUAL_THRESHOLD = 200;
const TILE_HEIGHT = 120; // approximate: 112px tile + 8px gap

export const DETECTION_TYPE_LABELS = {
  human_detection: "Person",
  vehicle_detection: "Vehicle",
  motion_detection: "Motion",
  doorbell: "Doorbell",
  door_access: "Door",
  tamper_detection: "Tamper",
  manual_trigger: "Manual",
};

/** Group same-device events rendered in the same displayed second. */
export function groupDetections(detections) {
  const groups = new Map();
  const seenIds = new Set();
  for (const detection of detections || []) {
    const sourceEvents = detection.events?.length ? detection.events : [detection.event || detection];
    const events = sourceEvents.filter(event => {
      if (!event?.id || seenIds.has(event.id)) return false;
      seenIds.add(event.id);
      return true;
    });
    if (!events.length) continue;
    const key = `${detection.deviceId ?? ""}:${Math.floor(detection.time / 1000)}`;
    let group = groups.get(key);
    if (!group) {
      group = { ...detection, events: [], eventIds: [], eventTypes: [], snapshot: detection.snapshot || null };
      groups.set(key, group);
    }
    for (const event of events) {
      if (!event?.id || group.eventIds.includes(event.id)) continue;
      const type = event.event_type || event.type || detection.type;
      group.events.push(event);
      group.eventIds.push(event.id);
      if (type && !group.eventTypes.includes(type)) group.eventTypes.push(type);
    }
    if (!group.snapshot && detection.snapshot) group.snapshot = detection.snapshot;
    group.time = Math.min(group.time, detection.time);
  }
  return [...groups.values()].sort((a, b) => b.time - a.time || String(a.id).localeCompare(String(b.id)));
}

/**
 * Filter detections by active type set.
 * @param {Array<{type: string}>} detections
 * @param {Set<string>} activeFilters
 * @returns {Array}
 */
export function filterDetections(detections, activeFilters, nowMs = Infinity, timeRange = null) {
  if (!detections?.length) return [];
  return groupDetections(detections).filter(detection =>
    detection.time <= nowMs
    && (!timeRange || (detection.time >= timeRange.start && detection.time <= timeRange.end))
    && (detection.eventTypes?.length ? detection.eventTypes : [detection.type]).some(type => activeFilters.has(type))
  );
}

/**
 * Format a millisecond timestamp as a relative time string.
 * @param {number} timeMs
 * @param {number} nowMs
 * @returns {string}
 */
export function relativeTime(timeMs, nowMs) {
  const diff = nowMs - timeMs;
  if (diff < 0) return "upcoming";
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/**
 * Render a single detection tile as HTML.
 * @param {{id: string, type: string, time: number, snapshot?: object, nowMs?: number}} detection
 * @returns {string}
 */
export function renderDetectionTile(detection) {
  const types = detection.eventTypes?.length ? detection.eventTypes : [detection.type];
  const labels = [...new Set(types)].map(type => DETECTION_TYPE_LABELS[type] || type);
  const label = labels.join(" · ");
  const eventIds = detection.eventIds?.length
    ? detection.eventIds
    : (detection.events || [detection]).map(event => event.id).filter(Boolean);
  const nowMs = detection.nowMs ?? Date.now();
  const relative = relativeTime(detection.time, nowMs);
  const hasSnapshot = !!detection.snapshot;
  const thumbSrc = hasSnapshot
    ? `/api/v1/evidence/${encodeURIComponent(detection.snapshot.id)}/thumbnail`
    : "";
  const clock = fmtTime(detection.time);
  return `<button type="button" class="tl-det-tile" data-det-id="${escHtml(detection.id)}" data-det-event-ids="${escHtml(eventIds.join(","))}" data-det-time="${detection.time}" data-det-type="${escHtml(types.join(","))}" aria-label="${escHtml(label)} at ${clock}">
    <div class="tl-det-thumb">
      ${hasSnapshot
        ? `<img src="${thumbSrc}" loading="lazy" decoding="async" alt="" onerror="this.onerror=null;this.hidden=true">`
        : `<svg><use href="icons.svg#activity"></use></svg>`
      }
    </div>
    <span class="tl-det-time" data-det-time="${detection.time}" title="${escHtml(relative)}">${clock}</span>
    <span class="tl-det-type" data-type="${escHtml(detection.type)}">${escHtml(label)}</span>
  </button>`;
}

/**
 * Render the filter toggle bar as HTML.
 * @param {string[]} types - all known types to show
 * @param {Set<string>} activeFilters
 * @returns {string}
 */
export function renderFilterBar(types, activeFilters) {
  const buttons = types
    .map(type => {
      const label = DETECTION_TYPE_LABELS[type] || type;
      const active = activeFilters.has(type);
      return `<button type="button" class="tl-det-filter${active ? " active" : ""}" data-filter="${escHtml(type)}" aria-pressed="${active}">${escHtml(label)}</button>`;
    })
    .join("");
  return `<div class="tl-det-filters" role="group" aria-label="Detection type filters">${buttons}</div>`;
}

/**
 * Render the full detection grid as HTML.
 * @param {Array} detections - all detections for the day
 * @param {Set<string>} activeFilters
 * @param {number} nowMs
 * @param {object} [options]
 * @param {number} [options.pageSize=60]
 * @param {number} [options.visibleCount] - how many tiles to show (infinite scroll)
 * @returns {string}
 */
export function renderDetectionGrid(detections, activeFilters, nowMs, options = {}) {
  const { pageSize = PAGE_SIZE, visibleCount, timeRange = null } = options;
  const filtered = filterDetections(detections, activeFilters, nowMs, timeRange);
  const count = visibleCount ?? pageSize;
  const visible = filtered.slice(0, count);
  const hasMore = filtered.length > count;

  if (!filtered.length) {
    const emptyMessage = timeRange ? "No detections in the selected period" : "No detections";
    return `<div class="tl-det-empty"><svg><use href="icons.svg#activity"></use></svg><span>${emptyMessage}</span></div>`;
  }

  const tiles = visible.map(det => renderDetectionTile({ ...det, nowMs })).join("");
  const sentinel = hasMore ? `<div class="tl-det-sentinel" data-det-sentinel></div>` : "";
  return `<div class="tl-det-grid" role="grid" aria-label="Detection snapshots">${tiles}${sentinel}</div>`;
}

/**
 * Mount the detection grid into a container element.
 * Handles: filter bar, grid rendering, infinite scroll, click-to-seek,
 * click highlighting, and new event animation. (The date selector lives in
 * the timeline region head, not here.)
 *
 * @param {HTMLElement} container - the region body element
 * @param {object} options
 * @param {Array} [options.detections]
 * @param {Set<string>} [options.activeFilters]
 * @param {{start: number, end: number}|null} [options.timeRange] - inclusive event-time filter
 * @param {number} [options.nowMs]
 * @param {number} [options.pageSize]
 * @param {(timeMs: number) => void} [options.onSelect]
 * @param {(types: Set<string>) => void} [options.onFilterChange]
 * @returns {{ update: (detections: Array) => void, setFilters: (filters: Set<string>) => void, setTimeRange: (range: {start: number, end: number}|null) => void, highlightNearest: (timeMs: number) => void, addNew: (detections: Array) => void, cleanup: () => void }}
 */
export function mountDetectionGrid(container, options = {}) {
  const {
    detections = [],
    activeFilters = new Set(Object.keys(DETECTION_TYPE_LABELS)),
    timeRange = null,
    nowMs = Date.now(),
    pageSize = PAGE_SIZE,
    onSelect = () => {},
    onFilterChange = () => {},
  } = options;

  let currentDetections = detections;
  let currentFilters = new Set(activeFilters);
  let currentTimeRange = timeRange;
  let visibleCount = pageSize;
  let observer = null;
  let virtualCleanup = null;
  let timeInterval = null;

  const allTypes = new Set(Object.keys(DETECTION_TYPE_LABELS));
  for (const d of currentDetections) {
    allTypes.add(d.type);
    for (const type of d.eventTypes || []) allTypes.add(type);
    for (const event of d.events || []) if (event.event_type) allTypes.add(event.event_type);
  }

  // Shared 30 s interval that updates relative-time labels in place.
  function startTimeUpdates() {
    stopTimeUpdates();
    timeInterval = setInterval(() => {
      const now = Date.now();
      container.querySelectorAll(".tl-det-time[data-det-time]").forEach(el => {
        const t = Number(el.dataset.detTime);
        if (Number.isFinite(t)) el.title = relativeTime(t, now);
      });
    }, 30000);
  }
  function stopTimeUpdates() {
    if (timeInterval) {
      clearInterval(timeInterval);
      timeInterval = null;
    }
  }

  function render() {
    const filtered = filterDetections(currentDetections, currentFilters, Date.now(), currentTimeRange);
    const filterHtml = renderFilterBar([...allTypes], currentFilters);

    if (filtered.length > VIRTUAL_THRESHOLD) {
      renderVirtual(filtered, filterHtml);
    } else {
      renderInfiniteScroll(filtered, filterHtml);
    }
    startTimeUpdates();
  }

  function renderInfiniteScroll(filtered, filterHtml) {
    if (virtualCleanup) {
      virtualCleanup();
      virtualCleanup = null;
    }
    const gridHtml = renderDetectionGrid(currentDetections, currentFilters, Date.now(), {
      pageSize,
      visibleCount,
      timeRange: currentTimeRange,
    });
    container.innerHTML = `
      <div class="tl-det-header">${filterHtml}</div>
      <div class="tl-det-body">${gridHtml}</div>`;
    setupObserver();
  }

  function renderVirtual(filtered, filterHtml) {
    if (observer) {
      observer.disconnect();
      observer = null;
    }
    const totalHeight = filtered.length * TILE_HEIGHT;
    container.innerHTML = `
      <div class="tl-det-header">${filterHtml}</div>
      <div class="tl-det-body" data-det-virtual>
        <div class="tl-det-virtual-spacer" style="height:${totalHeight}px;position:relative;">
          <div class="tl-det-grid" role="grid" aria-label="Detection snapshots" style="position:absolute;left:0;right:0;"></div>
        </div>
      </div>`;

    const body = container.querySelector("[data-det-virtual]");
    const grid = container.querySelector(".tl-det-grid");

    let lastStart = -1;
    let lastEnd = -1;

    function update() {
      const scrollTop = body.scrollTop;
      const viewportH = body.clientHeight || 260;
      const startRow = Math.floor(scrollTop / TILE_HEIGHT);
      const visibleRows = Math.ceil(viewportH / TILE_HEIGHT) + 1;
      const start = Math.max(0, startRow - 2);
      const end = Math.min(filtered.length, startRow + visibleRows + 2);
      if (start === lastStart && end === lastEnd) return;
      lastStart = start;
      lastEnd = end;
      const tiles = filtered.slice(start, end).map(det => renderDetectionTile({ ...det, nowMs: Date.now() }));
      grid.style.top = `${start * TILE_HEIGHT}px`;
      grid.innerHTML = tiles.join("");
    }

    let raf = 0;
    const onScroll = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(update);
    };
    body.addEventListener("scroll", onScroll);
    update();

    virtualCleanup = () => {
      cancelAnimationFrame(raf);
      body.removeEventListener("scroll", onScroll);
    };
  }

  function setupObserver() {
    if (observer) observer.disconnect();
    const body = container.querySelector(".tl-det-body");
    const sentinel = container.querySelector("[data-det-sentinel]");
    if (!sentinel || !body) return;
    observer = new IntersectionObserver(
      entries => {
        if (entries.some(e => e.isIntersecting)) {
          visibleCount += pageSize;
          render();
        }
      },
      { root: body, rootMargin: "200px" },
    );
    observer.observe(sentinel);
  }

  const handleFilterClick = event => {
    const btn = event.target.closest("[data-filter]");
    if (!btn) return;
    const type = btn.dataset.filter;
    if (currentFilters.has(type)) {
      currentFilters.delete(type);
    } else {
      currentFilters.add(type);
    }
    render();
    onFilterChange(new Set(currentFilters));
  };

  const handleTileClick = event => {
    const tile = event.target.closest("[data-det-id]");
    if (!tile) return;
    const time = Number(tile.dataset.detTime);
    if (Number.isFinite(time)) {
      onSelect(time);
    }
  };

  // Arrow-key navigation across grid tiles.
  const handleGridKeydown = event => {
    const tile = event.target.closest("[data-det-id]");
    if (!tile) return;
    const tiles = [...container.querySelectorAll("[data-det-id]")];
    const idx = tiles.indexOf(tile);
    if (idx < 0) return;
    let next = null;
    if (event.key === "ArrowRight") next = tiles[idx + 1];
    else if (event.key === "ArrowLeft") next = tiles[idx - 1];
    else if (event.key === "ArrowDown") next = tiles[idx + 1];
    else if (event.key === "ArrowUp") next = tiles[idx - 1];
    else if (event.key === "Enter") {
      tile.click();
      return;
    } else return;
    event.preventDefault();
    next?.focus();
  };

  container.addEventListener("click", handleFilterClick);
  container.addEventListener("click", handleTileClick);
  container.addEventListener("keydown", handleGridKeydown);

  render();

  return {
    update(newDetections) {
      currentDetections = newDetections;
      for (const d of newDetections) {
        allTypes.add(d.type);
        for (const type of d.eventTypes || []) allTypes.add(type);
        for (const event of d.events || []) if (event.event_type) allTypes.add(event.event_type);
      }
      visibleCount = pageSize;
      render();
    },
    setFilters(newFilters) {
      currentFilters = new Set(newFilters);
      visibleCount = pageSize;
      render();
    },
    setTimeRange(newRange) {
      currentTimeRange = newRange;
      visibleCount = pageSize;
      render();
    },
    /**
     * Highlight the rendered tile closest to `timeMs`. Called after a
     * timeline click or a grid tile click so the operator can see which
     * detection the playhead landed on.
     */
    highlightNearest(timeMs) {
      const tiles = [...container.querySelectorAll("[data-det-id]")];
      if (!tiles.length) return;
      let best = null;
      let bestDist = Infinity;
      for (const tile of tiles) {
        const t = Number(tile.dataset.detTime);
        if (!Number.isFinite(t)) continue;
        const dist = Math.abs(t - timeMs);
        if (dist < bestDist) {
          bestDist = dist;
          best = tile;
        }
      }
      for (const tile of tiles) tile.classList.remove("tl-det-selected");
      if (best) {
        best.classList.add("tl-det-selected");
        // Scroll the detection body (not the page) so focus stays on the video.
        const body = container.querySelector(".tl-det-body");
        if (body) {
          const bodyRect = body.getBoundingClientRect();
          const tileRect = best.getBoundingClientRect();
          if (tileRect.top < bodyRect.top || tileRect.bottom > bodyRect.bottom) {
            body.scrollTop += tileRect.top - bodyRect.top - (body.clientHeight - tile.clientHeight) / 2;
          }
        }
      }
    },
    addNew(newDetections) {
      const existingIds = new Set(currentDetections.map(d => d.id));
      const fresh = newDetections.filter(d => !existingIds.has(d.id));
      if (!fresh.length) return;
      for (const d of fresh) {
        allTypes.add(d.type);
        for (const type of d.eventTypes || []) allTypes.add(type);
      }
      currentDetections = [...fresh, ...currentDetections];
      visibleCount = Math.max(pageSize, visibleCount + fresh.length);
      render();
      const newIds = new Set(fresh.map(d => d.id));
      container.querySelectorAll("[data-det-id]").forEach(tile => {
        if (newIds.has(tile.dataset.detId)) {
          tile.classList.add("tl-det-new");
        }
      });
      const body = container.querySelector(".tl-det-body");
      if (body && body.scrollTop > 50) {
        let pill = body.querySelector(".tl-det-new-pill");
        if (!pill) {
          pill = document.createElement("button");
          pill.className = "tl-det-new-pill";
          pill.type = "button";
          pill.addEventListener("click", () => {
            body.scrollTop = 0;
            pill.remove();
          });
          body.prepend(pill);
        }
        pill.textContent = `${fresh.length} new`;
      }
    },
    cleanup() {
      if (observer) observer.disconnect();
      if (virtualCleanup) virtualCleanup();
      stopTimeUpdates();
      container.removeEventListener("click", handleFilterClick);
      container.removeEventListener("click", handleTileClick);
      container.removeEventListener("keydown", handleGridKeydown);
      container.innerHTML = "";
    },
  };
}
