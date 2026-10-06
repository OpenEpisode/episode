// Camera list panel for the Timeline view.
// Renders camera thumbnails + selection + keyboard nav, and loads the latest
// snapshot thumbnail per camera via GET /evidence?device_id=X&evidence_type=snapshot.
import { escHtml } from "./dom.js";
import { api, API } from "./api.js";
import { trunc, titleCase } from "./format.js";
import { operationalIndicator } from "./inventory-pages.js";

// OperationalState → status dot. healthy=online, degraded=warning,
// everything else (unavailable, disabled, unknown)=offline.
export function cameraStatus(state) {
  if (state === "healthy") return "online";
  if (state === "degraded") return "warning";
  return "offline";
}

function modelLabel(device) {
  const identity = device.identity || {};
  const parts = [identity.manufacturer, identity.model].filter(Boolean);
  return parts.length ? trunc(parts.join(" "), 24) : "";
}

function selectedSet(selectedIds, cameras) {
  if (selectedIds instanceof Set) return selectedIds;
  if (Array.isArray(selectedIds)) return new Set(selectedIds);
  if (selectedIds) return new Set([selectedIds]);
  return new Set(cameras.map(camera => camera.id));
}

function cameraItem(device, selectedIds) {
  const id = escHtml(device.id);
  const selected = selectedIds.has(device.id);
  const status = cameraStatus(device.state);
  const offline = status === "offline";
  const classes = ["tl-cam"];
  if (selected) classes.push("selected");
  if (offline) classes.push("offline");
  return `
    <label
      class="${classes.join(" ")}"
      data-cam-id="${id}"
      title="${escHtml(device.name)}${modelLabel(device) ? " · " + escHtml(modelLabel(device)) : ""}">
      <input class="tl-cam-toggle" type="checkbox" data-cam-toggle="${id}"${selected ? " checked" : ""} aria-label="Include ${escHtml(device.name)} in timeline">
      <span class="tl-cam-thumb">
        <img class="tl-cam-img" alt="" loading="lazy" decoding="async">
        <span class="status-indicator ${operationalIndicator(device.state)}" aria-label="${escHtml(titleCase(device.state))}"></span>
      </span>
      <span class="tl-cam-meta">
        <span class="tl-cam-name">${escHtml(device.name)}</span>
        ${modelLabel(device) ? `<span class="tl-cam-model">${escHtml(modelLabel(device))}</span>` : ""}
      </span>
    </label>`;
}

export function renderCameraList(cameras, selectedIds) {
  if (!cameras.length) {
    return `<div class="tl-placeholder"><svg><use href="icons.svg#devices"></use></svg><span>No cameras configured</span></div>`;
  }
  const selected = selectedSet(selectedIds, cameras);
  return `<div class="tl-cam-list" role="group" aria-label="Cameras">
    <div class="tl-cam-actions" role="group" aria-label="Camera selection">
      <button type="button" class="tl-cam-action" data-cam-select="all">Select all</button>
      <button type="button" class="tl-cam-action" data-cam-select="none">Select none</button>
    </div>
    ${cameras.map(device => cameraItem(device, selected)).join("")}
  </div>`;
}

async function loadThumbnail(img, deviceId) {
  if (!img) return;
  try {
    const items = await api(`/evidence?device_id=${encodeURIComponent(deviceId)}&evidence_type=snapshot&limit=1`);
    if (Array.isArray(items) && items[0]?.id && !img.dataset.failed) {
      img.src = `${API}/evidence/${items[0].id}/thumbnail`;
    }
  } catch {
    img.dataset.failed = "1";
  }
}

// Mounts the camera list into `container`. Returns a cleanup function that
// detaches listeners; thumbnail responses only retain a detached image node.
export function mountCameraList(container, { cameras, selectedId, selectedIds, onSelect, onSelectionChange }) {
  let selected = selectedSet(selectedIds ?? selectedId, cameras);
  container.innerHTML = renderCameraList(cameras, selected);

  const listbox = container.querySelector(".tl-cam-list");
  if (!listbox) return () => {};

  // Load the latest snapshot thumbnail for each camera.
  for (const button of listbox.querySelectorAll(".tl-cam")) {
    const img = button.querySelector(".tl-cam-img");
    const deviceId = button.dataset.camId;
    loadThumbnail(img, deviceId).catch(() => {});
  }

  function emitSelection() {
    const next = new Set(selected);
    onSelectionChange?.(next);
    // Preserve the original single-camera callback for callers that have not
    // opted into the checkbox API yet.
    if (!onSelectionChange && onSelect) onSelect(next.values().next().value || null);
  }

  function updateVisualState() {
    for (const button of listbox.querySelectorAll(".tl-cam")) {
      const active = selected.has(button.dataset.camId);
      button.classList.toggle("selected", active);
      const toggle = button.querySelector?.("[data-cam-toggle]");
      if (toggle) toggle.checked = active;
    }
  }

  function select(deviceId, checked) {
    if (!deviceId) return;
    if (checked) selected.add(deviceId);
    else selected.delete(deviceId);
    updateVisualState();
    emitSelection();
  }

  function selectAll(checked) {
    selected = checked ? new Set(cameras.map(camera => camera.id)) : new Set();
    updateVisualState();
    emitSelection();
  }

  function onKey(event) {
    const toggles = [...listbox.querySelectorAll("[data-cam-toggle]")];
    const index = toggles.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = event.key === "ArrowDown"
        ? Math.min(toggles.length - 1, index + 1)
        : Math.max(0, index - 1 || toggles.length - 1);
      toggles[next]?.focus();
    } else if (event.key === "Home") {
      event.preventDefault();
      toggles[0]?.focus();
    } else if (event.key === "End") {
      event.preventDefault();
      toggles.at(-1)?.focus();
    }
  }

  const onChange = event => {
    const toggle = event.target.closest?.("[data-cam-toggle]");
    if (toggle) select(toggle.dataset.camToggle, toggle.checked);
  };
  const onAction = event => {
    const button = event.target.closest?.("[data-cam-select]");
    if (button) selectAll(button.dataset.camSelect === "all");
  };
  listbox.addEventListener("change", onChange);
  listbox.addEventListener("click", onAction);
  listbox.addEventListener("keydown", onKey);

  return () => {
    listbox.removeEventListener("change", onChange);
    listbox.removeEventListener("click", onAction);
    listbox.removeEventListener("keydown", onKey);
  };
}
