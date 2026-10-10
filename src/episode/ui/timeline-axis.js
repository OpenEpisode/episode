// 24h timeline axis for the Timeline view.
// Renders a canvas: hour labels, 10min/hour ticks, recording-segment bars
// (the camera's videos), a NOW pill, and a draggable playhead. Zoom levels map to
// pixels-per-hour. All time<->pixel math is pure and unit-testable.
//
// Supports two orientations:
//   - vertical (default): time flows top→bottom, canvas height = range × zoom
//   - horizontal: time flows left→right, canvas width = range × zoom, fixed height
//
// The model is composed by the orchestrator (timeline-view.js) from the real
// API; this module only draws and handles pointer/keyboard interaction.

// pixels per hour, indexed by zoomLevel (0=24h ... 4=15min).
export const ZOOM_LEVELS = [39, 78, 156, 312, 1248];
export const ZOOM_LABELS = ["24h", "12h", "6h", "1h", "15min"];
export const LABEL_W = 34; // hour-label column width (px)
export const HORIZONTAL_AXIS_H = 48; // fixed canvas height in horizontal mode

const MS_PER_HOUR = 3600000;
const RECORDING_MARKER_RADIUS = 8;
const RECORDING_MARKER_HIT_X = 18;
const RECORDING_MARKER_HIT_Y = 11;
const RANGE_DRAG_THRESHOLD = 5;
const DEFAULT_DAY_END_OFFSET_MS = 24 * MS_PER_HOUR;

export function timeToY(time, dayStart, zoom, dayEnd = dayStart + DEFAULT_DAY_END_OFFSET_MS) {
  return ((dayEnd - time) / MS_PER_HOUR) * ZOOM_LEVELS[zoom];
}

export function yToTime(y, dayStart, zoom, dayEnd = dayStart + DEFAULT_DAY_END_OFFSET_MS) {
  return dayEnd - (y / ZOOM_LEVELS[zoom]) * MS_PER_HOUR;
}

export function canvasHeight(dayStart, dayEnd, zoom) {
  return ((dayEnd - dayStart) / MS_PER_HOUR) * ZOOM_LEVELS[zoom];
}

export function timeToX(time, dayStart, zoom) {
  return ((time - dayStart) / MS_PER_HOUR) * ZOOM_LEVELS[zoom];
}

export function xToTime(x, dayStart, zoom) {
  return dayStart + (x / ZOOM_LEVELS[zoom]) * MS_PER_HOUR;
}

export function canvasWidth(dayStart, dayEnd, zoom) {
  return ((dayEnd - dayStart) / MS_PER_HOUR) * ZOOM_LEVELS[zoom];
}

export function normalizeTimeRange(start, end) {
  return { start: Math.min(start, end), end: Math.max(start, end) };
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function nextLocalHour(time) {
  return time + MS_PER_HOUR;
}

function firstLocalHourAtOrAfter(time) {
  const date = new Date(time);
  const boundary = date.getTime();
  date.setMinutes(0, 0, 0);
  if (date.getTime() < boundary) date.setTime(date.getTime() + MS_PER_HOUR);
  return date.getTime();
}

// Draws the horizontal axis onto `ctx`. Time flows left→right.
// `width` is the full canvas width (time range × zoom). Fixed height.
export function renderAxisHorizontal(ctx, width, model, zoom, palette) {
  const { dayStart, dayEnd } = model;
  const H = HORIZONTAL_AXIS_H;
  const t2x = time => timeToX(time, dayStart, zoom);

  ctx.clearRect(0, 0, width, H);

  // Background.
  ctx.fillStyle = palette.bg || "transparent";
  ctx.fillRect(0, 0, width, H);

  // Selected range overlay.
  if (model.selectedRange) {
    const selectEnd = Math.max(dayStart, Math.min(dayEnd, model.selectEnd ?? Math.min(dayEnd, model.now ?? dayEnd)));
    const rangeStart = Math.max(dayStart, Math.min(selectEnd, model.selectedRange.start));
    const rangeEnd = Math.max(dayStart, Math.min(selectEnd, model.selectedRange.end));
    const x1 = t2x(rangeStart);
    const x2 = t2x(rangeEnd);
    ctx.fillStyle = palette.playhead;
    ctx.globalAlpha = 0.14;
    ctx.fillRect(Math.min(x1, x2), 0, Math.abs(x2 - x1), H);
    ctx.globalAlpha = 0.72;
    ctx.fillRect(Math.min(x1, x2), 0, 1, H);
    ctx.fillRect(Math.max(x1, x2), 0, 1, H);
    ctx.globalAlpha = 1;
  }

  // Hour labels + ticks along the bottom.
  const labelY = H - 12;
  const tickTop = 20;
  ctx.font = "9px system-ui, sans-serif";
  ctx.textBaseline = "top";
  for (let t = firstLocalHourAtOrAfter(dayStart); t < dayEnd; t = nextLocalHour(t)) {
    const x = t2x(t);
    ctx.strokeStyle = palette.border;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, tickTop);
    ctx.lineTo(x, H - 18);
    ctx.stroke();
    const labelDate = new Date(t);
    ctx.fillStyle = palette.muted;
    ctx.fillText(`${pad2(labelDate.getHours())}:00`, x + 2, labelY);
  }

  // Recording dots (centered vertically in the upper portion).
  const dotY = 10;
  ctx.fillStyle = palette.playhead;
  for (const segment of model.segments) {
    if (segment.start > dayEnd) continue;
    const x = t2x(Math.max(segment.start, dayStart));
    if (x < 0 || x > width) continue;
    ctx.beginPath();
    ctx.arc(x, dotY, RECORDING_MARKER_RADIUS, 0, Math.PI * 2);
    ctx.fill();
  }

  // NOW marker.
  if (model.now >= dayStart && model.now <= dayEnd) {
    const x = t2x(model.now);
    ctx.strokeStyle = palette.accent;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, H);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = palette.accent;
    const nowW = 30;
    ctx.fillRect(x - nowW / 2, 2, nowW, 12);
    ctx.fillStyle = "#fff";
    ctx.font = "8px system-ui, sans-serif";
    ctx.textBaseline = "top";
    ctx.fillText("NOW", x - 10, 4);
  }

  // Playhead (full-height vertical line).
  const selectEnd = Math.max(dayStart, Math.min(dayEnd, model.selectEnd ?? Math.min(dayEnd, model.now ?? dayEnd)));
  const px = Math.max(0, Math.min(width, t2x(Math.min(selectEnd, model.playhead))));
  ctx.strokeStyle = palette.playhead;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(px, 0);
  ctx.lineTo(px, H);
  ctx.stroke();
  ctx.fillStyle = palette.playhead;
  ctx.fillRect(px - 3, 0, 6, 8);
}

// Draws the axis onto `ctx`. `palette` holds concrete colors (resolved from CSS
// custom properties at mount; hex fallbacks make this testable headlessly).
export function renderAxis(ctx, width, model, zoom, palette) {
  const { dayStart, dayEnd } = model;
  const selectEnd = Math.max(dayStart, Math.min(dayEnd, model.selectEnd ?? Math.min(dayEnd, model.now ?? dayEnd)));
  const height = canvasHeight(dayStart, dayEnd, zoom);
  const t2y = time => timeToY(time, dayStart, zoom, dayEnd);

  ctx.clearRect(0, 0, width, height);

  // A selected interval is a translucent overlay; it never changes the
  // timeline's scale or hides recordings outside the interval.
  if (model.selectedRange) {
    const rangeStart = Math.max(dayStart, Math.min(selectEnd, model.selectedRange.start));
    const rangeEnd = Math.max(dayStart, Math.min(selectEnd, model.selectedRange.end));
    const rangeYStart = t2y(rangeStart);
    const rangeYEnd = t2y(rangeEnd);
    const top = Math.min(rangeYStart, rangeYEnd);
    const bottom = Math.max(rangeYStart, rangeYEnd);
    ctx.fillStyle = palette.playhead;
    ctx.globalAlpha = 0.14;
    ctx.fillRect(0, top, width, Math.max(2, bottom - top));
    ctx.globalAlpha = 0.72;
    ctx.fillRect(0, top, width, 1);
    ctx.fillRect(0, bottom, width, 1);
    ctx.globalAlpha = 1;
  }

  // Hour labels + ticks.
  ctx.font = "10px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  for (let t = firstLocalHourAtOrAfter(dayStart); t < dayEnd; t = nextLocalHour(t)) {
    const y = t2y(t);
    ctx.strokeStyle = palette.border;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(LABEL_W, y);
    ctx.lineTo(LABEL_W + 10, y);
    ctx.stroke();
    const labelDate = new Date(t);
    ctx.fillStyle = palette.muted;
    ctx.fillText(`${pad2(labelDate.getHours())}:00`, 2, y);
    // Minor 10-minute ticks.
    for (let minute = 10; minute < 60; minute += 10) {
      const minorTime = t + minute * 60000;
      if (minorTime >= dayEnd) break;
      const ym = t2y(minorTime);
      ctx.beginPath();
      ctx.moveTo(LABEL_W, ym);
      ctx.lineTo(LABEL_W + 6, ym);
      ctx.stroke();
    }
  }

  // Recording blobs (one per video segment, brand blue).
  const trackX = LABEL_W + 26;
  ctx.fillStyle = palette.playhead;
  for (const segment of model.segments) {
    if (segment.start > selectEnd) continue;
    const y = t2y(Math.max(segment.start, dayStart));
    ctx.beginPath();
    ctx.arc(trackX + 6, y, RECORDING_MARKER_RADIUS, 0, Math.PI * 2);
    ctx.fill();
  }

  // NOW marker: short dashed line just past the blob track, label on the
  // left (the right side of the canvas stays clear).
  if (model.now >= dayStart && model.now <= dayEnd) {
    const y = t2y(model.now);
    const lineEnd = trackX + 12; // short line: label column -> blob track
    ctx.strokeStyle = palette.accent;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(LABEL_W, y);
    ctx.lineTo(lineEnd, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = palette.accent;
    ctx.fillRect(lineEnd + 2, y - 8, 31, 16);
    ctx.fillStyle = "#fff";
    ctx.fillText("NOW", lineEnd + 7, y);
  }

  // Playhead (full-width line + left handle).
  const py = Math.max(0, Math.min(height, t2y(Math.min(selectEnd, model.playhead))));
  ctx.strokeStyle = palette.playhead;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(0, py);
  ctx.lineTo(width, py);
  ctx.stroke();
  ctx.fillStyle = palette.playhead;
  ctx.fillRect(0, py - 7, 6, 14);
}

// Resolve concrete colors from the container's CSS custom properties.
function resolvePalette(container) {
  const style = getComputedStyle(container);
  const var2 = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
  return {
    accent: var2("--accent", "#00C2C7"),
    muted: var2("--text-muted", "#8a8f98"),
    border: var2("--border-subtle", "#2a2e35"),
    playhead: var2("--brand-blue", "#3D8BFF"),
    detection: {
      human_detection: var2("--brand-blue", "#3D8BFF"),
      vehicle_detection: var2("--brand-purple", "#7B5CFF"),
      motion_detection: var2("--brand-cyan", "#00C2C7"),
      doorbell: var2("--warning", "#E0A020"),
      door_access: var2("--success", "#3CB371"),
      tamper_detection: var2("--danger", "#E05555"),
      manual_trigger: var2("--info", "#4A90D9"),
    },
  };
}

// Mounts the axis into `container`. Returns model, zoom, range-selection, and
// cleanup controls for the timeline view.
export function mountTimelineAxis(container, {
  model,
  zoom = 0,
  horizontal = false,
  onSeek,
  onScrub,
  onZoom,
  onRangeChange,
}) {
  const palette = resolvePalette(container);
  const canvas = document.createElement("canvas");
  canvas.className = "tl-axis-canvas";
  const wrap = document.createElement("div");
  wrap.className = "tl-axis" + (horizontal ? " tl-axis-horizontal" : "");
  wrap.setAttribute("role", "slider");
  wrap.setAttribute("tabindex", "0");
  wrap.setAttribute("aria-label", "Timeline. Click to seek; drag to select a time range.");
  wrap.title = "Click to seek. Drag to filter detections by time; click outside the timeline to clear the filter.";
  wrap.setAttribute("aria-orientation", horizontal ? "horizontal" : "vertical");
  wrap.appendChild(canvas);
  container.innerHTML = "";
  container.appendChild(wrap);

  let currentModel = model;
  let currentZoom = zoom;
  let rangePreview = null;
  let gesture = null;
  let raf = 0;

  // The wrap fills the region body's content box exactly; the container
  // would include the body's 10px padding on each side.
  function cssWidth() {
    return Math.max(120, wrap.clientWidth || canvas.clientWidth || 130);
  }

  function render() {
    const dpr = window.devicePixelRatio || 1;
    if (horizontal) {
      const width = Math.ceil(canvasWidth(currentModel.dayStart, currentModel.dayEnd, currentZoom));
      const height = HORIZONTAL_AXIS_H;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      renderAxisHorizontal(ctx, width, {
        ...currentModel,
        selectedRange: rangePreview || currentModel.selectedRange,
      }, currentZoom, palette);
    } else {
      const width = cssWidth();
      const height = Math.ceil(canvasHeight(currentModel.dayStart, currentModel.dayEnd, currentZoom));
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      renderAxis(ctx, width, {
        ...currentModel,
        selectedRange: rangePreview || currentModel.selectedRange,
      }, currentZoom, palette);
    }
    wrap.setAttribute("aria-valuemin", String(currentModel.dayStart));
    const end = currentModel.selectEnd ?? Math.min(currentModel.dayEnd, currentModel.now ?? currentModel.dayEnd);
    const playhead = Math.max(currentModel.dayStart, Math.min(end, currentModel.playhead));
    wrap.setAttribute("aria-valuemax", String(end));
    wrap.setAttribute("aria-valuenow", String(playhead));
    wrap.setAttribute("aria-valuetext", new Date(playhead).toLocaleTimeString());
  }

  function scheduleRender() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(render);
  }

  function pointerTime(event, { snapToRecording = true } = {}) {
    const rect = canvas.getBoundingClientRect();
    const localX = event.clientX - rect.left;
    const localY = event.clientY - rect.top;
    const raw = horizontal
      ? xToTime(localX, currentModel.dayStart, currentZoom)
      : yToTime(localY, currentModel.dayStart, currentZoom, currentModel.dayEnd);
    const end = currentModel.selectEnd ?? Math.min(currentModel.dayEnd, currentModel.now ?? currentModel.dayEnd);
    const clamped = Math.max(currentModel.dayStart, Math.min(end, raw));
    if (!snapToRecording) return clamped;

    // The visible recording dot is also a forgiving target: a near miss snaps
    // to its start instead of landing just outside the recording interval.
    for (const segment of currentModel.segments || []) {
      if (segment.start > end) continue;
      if (horizontal) {
        const markerX = timeToX(Math.max(segment.start, currentModel.dayStart), currentModel.dayStart, currentZoom);
        if (Math.abs(localX - markerX) <= RECORDING_MARKER_HIT_X
            && Math.abs(localY - 10) <= RECORDING_MARKER_HIT_Y) {
          return Math.max(currentModel.dayStart, Math.min(end, segment.start));
        }
      } else {
        const markerX = LABEL_W + 32;
        const markerY = timeToY(
          Math.max(segment.start, currentModel.dayStart),
          currentModel.dayStart,
          currentZoom,
          currentModel.dayEnd,
        );
        if (Math.abs(localX - markerX) <= RECORDING_MARKER_HIT_X
            && Math.abs(localY - markerY) <= RECORDING_MARKER_HIT_Y) {
          return Math.max(currentModel.dayStart, Math.min(end, segment.start));
        }
      }
    }
    return clamped;
  }

  function playheadY() {
    return timeToY(currentModel.playhead, currentModel.dayStart, currentZoom, currentModel.dayEnd);
  }

  // A click seeks; a vertical drag selects a shaded interval without resizing
  // the axis. Listeners live on `wrap` so captured pointer events remain local.
  function onPointerDown(event) {
    const start = pointerTime(event, { snapToRecording: false });
    gesture = {
      kind: "pending",
      start,
      startX: event.clientX,
      startY: event.clientY,
    };
    wrap.setPointerCapture?.(event.pointerId);
  }
  function onPointerMove(event) {
    if (!gesture) return;
    // Horizontal strips scroll natively on touch; a drag must not select a range.
    if (horizontal) return;
    if (gesture.kind === "pending"
        && Math.abs(event.clientY - gesture.startY) >= RANGE_DRAG_THRESHOLD) {
      gesture.kind = "range";
    }
    if (gesture.kind === "range") {
      const end = pointerTime(event, { snapToRecording: false });
      rangePreview = normalizeTimeRange(gesture.start, end);
      scheduleRender();
    }
  }
  function onPointerUp(event) {
    if (!gesture) return;
    const completedGesture = gesture;
    gesture = null;
    wrap.releasePointerCapture?.(event.pointerId);
    if (completedGesture.kind === "range") {
      const end = pointerTime(event, { snapToRecording: false });
      rangePreview = null;
      if (Math.abs(end - completedGesture.start) >= 1000) {
        onRangeChange?.(normalizeTimeRange(completedGesture.start, end));
      } else {
        onSeek?.(pointerTime(event));
      }
      scheduleRender();
      return;
    }

    const rawTime = pointerTime(event, { snapToRecording: false });
    const selectedRange = currentModel.selectedRange;
    if (selectedRange && (rawTime < selectedRange.start || rawTime > selectedRange.end)) {
      onRangeChange?.(null);
    }
    onSeek?.(pointerTime(event));
  }
  function onPointerCancel(event) {
    if (!gesture) return;
    gesture = null;
    rangePreview = null;
    wrap.releasePointerCapture?.(event.pointerId);
    scheduleRender();
  }
  function onWheel(event) {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const next = Math.max(0, Math.min(ZOOM_LEVELS.length - 1, currentZoom + (event.deltaY < 0 ? 1 : -1)));
    if (next !== currentZoom) {
      currentZoom = next;
      scheduleRender();
      onZoom?.(next);
    }
  }
  function onKey(event) {
    const keys = horizontal
      ? { ArrowLeft: -60000, ArrowRight: 60000, PageDown: -MS_PER_HOUR, PageUp: MS_PER_HOUR }
      : { ArrowDown: -60000, ArrowUp: 60000, PageDown: -MS_PER_HOUR, PageUp: MS_PER_HOUR };
    const step = keys[event.key];
    if (step === undefined) return;
    event.preventDefault();
    const end = currentModel.selectEnd ?? Math.min(currentModel.dayEnd, currentModel.now ?? currentModel.dayEnd);
    onScrub?.(Math.max(currentModel.dayStart, Math.min(end, currentModel.playhead + step)));
  }

  wrap.addEventListener("pointerdown", onPointerDown);
  wrap.addEventListener("pointermove", onPointerMove);
  wrap.addEventListener("pointerup", onPointerUp);
  wrap.addEventListener("pointercancel", onPointerCancel);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  wrap.addEventListener("keydown", onKey);
  render();

  return {
    setModel(next) {
      currentModel = next;
      scheduleRender();
    },
    setZoom(next) {
      currentZoom = next;
      scheduleRender();
    },
    getZoom() {
      return currentZoom;
    },
    render,
    cleanup() {
      cancelAnimationFrame(raf);
      wrap.removeEventListener("pointerdown", onPointerDown);
      wrap.removeEventListener("pointermove", onPointerMove);
      wrap.removeEventListener("pointerup", onPointerUp);
      wrap.removeEventListener("pointercancel", onPointerCancel);
      canvas.removeEventListener("wheel", onWheel);
      wrap.removeEventListener("keydown", onKey);
    },
  };
}
