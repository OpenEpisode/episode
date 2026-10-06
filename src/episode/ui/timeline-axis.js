// Vertical 24h timeline axis for the Timeline view.
// Renders a canvas: hour labels, 10min/hour ticks, recording-segment bars
// (the camera's videos), a NOW pill, and a draggable playhead. Zoom levels map to
// pixels-per-hour. All time<->pixel math is pure and unit-testable.
//
// The model is composed by the orchestrator (timeline-view.js) from the real
// API; this module only draws and handles pointer/keyboard interaction.

// pixels per hour, indexed by zoomLevel (0=24h ... 4=15min).
export const ZOOM_LEVELS = [39, 78, 156, 312, 1248];
export const ZOOM_LABELS = ["24h", "12h", "6h", "1h", "15min"];
export const LABEL_W = 34; // hour-label column width (px)

const MS_PER_HOUR = 3600000;

export function timeToY(time, dayStart, zoom) {
  return ((time - dayStart) / MS_PER_HOUR) * ZOOM_LEVELS[zoom];
}

export function yToTime(y, dayStart, zoom) {
  return dayStart + (y / ZOOM_LEVELS[zoom]) * MS_PER_HOUR;
}

export function canvasHeight(dayStart, dayEnd, zoom) {
  return ((dayEnd - dayStart) / MS_PER_HOUR) * ZOOM_LEVELS[zoom];
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

// Draws the axis onto `ctx`. `palette` holds concrete colors (resolved from CSS
// custom properties at mount; hex fallbacks make this testable headlessly).
export function renderAxis(ctx, width, model, zoom, palette) {
  const { dayStart, dayEnd } = model;
  const height = canvasHeight(dayStart, dayEnd, zoom);
  const t2y = time => timeToY(time, dayStart, zoom);

  ctx.clearRect(0, 0, width, height);

  // Hour labels + ticks.
  ctx.font = "10px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  for (let hour = 0; hour <= 24; hour += 1) {
    const t = dayStart + hour * MS_PER_HOUR;
    const y = t2y(t);
    ctx.strokeStyle = palette.border;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(LABEL_W, y);
    ctx.lineTo(LABEL_W + 10, y);
    ctx.stroke();
    if (hour < 24) {
      ctx.fillStyle = palette.muted;
      ctx.fillText(`${pad2(hour)}:00`, 2, y);
    }
    // Minor 10-minute ticks.
    for (let minute = 10; minute < 60; minute += 10) {
      const ym = t2y(t + minute * 60000);
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
    const y = t2y(Math.max(segment.start, dayStart));
    ctx.beginPath();
    ctx.arc(trackX + 6, y, 6, 0, Math.PI * 2);
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
  const py = Math.max(0, Math.min(height, t2y(model.playhead)));
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

// Mounts the axis into `container`. Returns { setModel, setZoom, getZoom, render, cleanup }.
export function mountTimelineAxis(container, { model, zoom = 0, onSeek, onScrub, onZoom }) {
  const palette = resolvePalette(container);
  const canvas = document.createElement("canvas");
  canvas.className = "tl-axis-canvas";
  const wrap = document.createElement("div");
  wrap.className = "tl-axis";
  wrap.setAttribute("role", "slider");
  wrap.setAttribute("tabindex", "0");
  wrap.setAttribute("aria-label", "Timeline");
  wrap.setAttribute("aria-orientation", "vertical");
  wrap.appendChild(canvas);
  container.innerHTML = "";
  container.appendChild(wrap);

  let currentModel = model;
  let currentZoom = zoom;
  let raf = 0;

  // The wrap fills the region body's content box exactly; the container
  // would include the body's 10px padding on each side.
  function cssWidth() {
    return Math.max(120, wrap.clientWidth || canvas.clientWidth || 130);
  }

  function render() {
    const width = cssWidth();
    const height = Math.ceil(canvasHeight(currentModel.dayStart, currentModel.dayEnd, currentZoom));
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    renderAxis(ctx, width, currentModel, currentZoom, palette);
    wrap.setAttribute("aria-valuemin", String(currentModel.dayStart));
    wrap.setAttribute("aria-valuemax", String(currentModel.dayEnd));
    wrap.setAttribute("aria-valuenow", String(currentModel.playhead));
    wrap.setAttribute("aria-valuetext", new Date(currentModel.playhead).toLocaleTimeString());
  }

  function scheduleRender() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(render);
  }

  function pointerTime(event) {
    const rect = canvas.getBoundingClientRect();
    return yToTime(event.clientY - rect.top, currentModel.dayStart, currentZoom);
  }

  function playheadY() {
    return timeToY(currentModel.playhead, currentModel.dayStart, currentZoom);
  }

  // A click (pointerup) seeks the video; hover and drag only scrub the
  // playhead within the current segment. Listeners live on `wrap` because
  // pointer capture is set there, so all captured events arrive at the wrap.
  let dragging = false;
  function onPointerDown(event) {
    const rect = canvas.getBoundingClientRect();
    dragging = true;
    wrap.setPointerCapture?.(event.pointerId);
    onScrub?.(yToTime(event.clientY - rect.top, currentModel.dayStart, currentZoom));
  }
  function onPointerMove(event) {
    if (!dragging) return;
    const rect = canvas.getBoundingClientRect();
    onScrub?.(yToTime(event.clientY - rect.top, currentModel.dayStart, currentZoom));
  }
  function onPointerUp(event) {
    if (!dragging) return;
    dragging = false;
    wrap.releasePointerCapture?.(event.pointerId);
    const rect = canvas.getBoundingClientRect();
    onSeek?.(yToTime(event.clientY - rect.top, currentModel.dayStart, currentZoom));
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
    const step = { ArrowDown: 60000, ArrowUp: -60000, PageDown: MS_PER_HOUR, PageUp: -MS_PER_HOUR }[event.key];
    if (step === undefined) return;
    event.preventDefault();
    onScrub?.(Math.max(currentModel.dayStart, Math.min(currentModel.dayEnd, currentModel.playhead + step)));
  }

  wrap.addEventListener("pointerdown", onPointerDown);
  wrap.addEventListener("pointermove", onPointerMove);
  wrap.addEventListener("pointerup", onPointerUp);
  wrap.addEventListener("pointercancel", onPointerUp);
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
      wrap.removeEventListener("pointercancel", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      wrap.removeEventListener("keydown", onKey);
    },
  };
}
