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

function cameraItem(device, selectedId) {
  const id = escHtml(device.id);
  const selected = device.id === selectedId;
  const status = cameraStatus(device.state);
  const offline = status === "offline";
  const classes = ["tl-cam"];
  if (selected) classes.push("selected");
  if (offline) classes.push("offline");
  return `
    <button type="button"
      class="${classes.join(" ")}"
      data-cam-id="${id}"
      role="option"
      aria-selected="${selected}"
      title="${escHtml(device.name)}${modelLabel(device) ? " · " + escHtml(modelLabel(device)) : ""}">
      <span class="tl-cam-thumb">
        <img class="tl-cam-img" alt="" loading="lazy" decoding="async">
        <span class="status-indicator ${operationalIndicator(device.state)}" aria-label="${escHtml(titleCase(device.state))}"></span>
      </span>
      <span class="tl-cam-meta">
        <span class="tl-cam-name">${escHtml(device.name)}</span>
        ${modelLabel(device) ? `<span class="tl-cam-model">${escHtml(modelLabel(device))}</span>` : ""}
      </span>
    </button>`;
}

export function renderCameraList(cameras, selectedId) {
  if (!cameras.length) {
    return `<div class="tl-placeholder"><svg><use href="icons.svg#devices"></use></svg><span>No cameras configured</span></div>`;
  }
  return `<div class="tl-cam-list" role="listbox" aria-label="Cameras">
    ${cameras.map(device => cameraItem(device, selectedId)).join("")}
  </div>`;
}

async function loadThumbnail(img, deviceId) {
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
// detaches listeners and aborts in-flight thumbnail loads.
export function mountCameraList(container, { cameras, selectedId, onSelect }) {
  container.innerHTML = renderCameraList(cameras, selectedId);

  const listbox = container.querySelector(".tl-cam-list");
  if (!listbox) return () => {};

  // Load the latest snapshot thumbnail for each camera.
  const controllers = [];
  for (const button of listbox.querySelectorAll(".tl-cam")) {
    const img = button.querySelector(".tl-cam-img");
    const deviceId = button.dataset.camId;
    const controller = new AbortController();
    controllers.push(controller);
    loadThumbnail(img, deviceId);
  }

  function select(deviceId) {
    if (!deviceId) return;
    for (const button of listbox.querySelectorAll(".tl-cam")) {
      const active = button.dataset.camId === deviceId;
      button.classList.toggle("selected", active);
      button.setAttribute("aria-selected", String(active));
    }
    onSelect?.(deviceId);
  }

  function onKey(event) {
    const buttons = [...listbox.querySelectorAll(".tl-cam")];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = event.key === "ArrowDown"
        ? Math.min(buttons.length - 1, index + 1)
        : Math.max(0, index - 1 || buttons.length - 1);
      buttons[next]?.focus();
    } else if (event.key === "Home") {
      event.preventDefault();
      buttons[0]?.focus();
    } else if (event.key === "End") {
      event.preventDefault();
      buttons.at(-1)?.focus();
    }
  }

  listbox.addEventListener("click", event => {
    const button = event.target.closest(".tl-cam");
    if (button) select(button.dataset.camId);
  });
  listbox.addEventListener("keydown", onKey);

  return () => {
    listbox.removeEventListener("click", onKey);
    listbox.removeEventListener("keydown", onKey);
    for (const controller of controllers) controller.abort();
  };
}
