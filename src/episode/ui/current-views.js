import { api } from "./api.js?v=3";
import { escHtml } from "./dom.js";

let refreshTimer = null;
let refreshGeneration = 0;

function viewCard(view) {
  const available = view.mode === "snapshot" && view.image_url;
  return `<article class="current-view-card ${available ? "is-loading" : "is-unavailable"}" data-device-id="${escHtml(view.device_id)}">
    <div class="current-view-frame">
      ${available
        ? `<img alt="Current view from ${escHtml(view.device_name)}" data-preview-url="${escHtml(view.image_url)}">`
        : '<div class="current-view-unavailable"><img src="/logo.svg" alt=""><span>Preview unavailable</span></div>'}
      <span class="current-view-live"><i></i>${available ? "Current" : "Recording"}</span>
    </div>
    <div class="current-view-caption">
      <strong>${escHtml(view.device_name)}</strong>
      <span class="current-view-status">${escHtml(view.summary)}</span>
    </div>
  </article>`;
}

function viewsMarkup(views, ended = false) {
  if (!views.length) {
    return `<div class="current-view-waiting">${ended
      ? "This Episode has ended · current views are no longer requested."
      : "Waiting for recording Devices to join this Episode…"}</div>`;
  }
  return views.map(viewCard).join("");
}

export function renderCurrentViews(views) {
  return `<section class="current-views" aria-labelledby="current-views-title">
    <div class="current-views-heading">
      <div>
        <span class="eyebrow">Happening now</span>
        <h3 id="current-views-title">Current views</h3>
      </div>
      <span class="current-views-note">Refreshes automatically · no preview is stored</span>
    </div>
    <div id="current-view-grid" class="current-view-grid">${viewsMarkup(views)}</div>
  </section>`;
}

function signature(views) {
  return views.map(view => `${view.device_id}:${view.mode}`).join("|");
}

function loadPreview(image, generation) {
  const source = image.dataset.previewUrl;
  if (!source) return;
  const separator = source.includes("?") ? "&" : "?";
  const candidate = new Image();
  candidate.onload = () => {
    if (generation !== refreshGeneration || !image.isConnected) return;
    image.src = candidate.src;
    const card = image.closest(".current-view-card");
    card?.classList.remove("is-loading", "has-error");
    const status = card?.querySelector(".current-view-status");
    if (status) {
      status.textContent = `Updated ${new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      })}`;
    }
  };
  candidate.onerror = () => {
    if (generation !== refreshGeneration || !image.isConnected) return;
    image.closest(".current-view-card")?.classList.add("has-error");
    const status = image.closest(".current-view-card")?.querySelector(".current-view-status");
    if (status) status.textContent = "Preview temporarily unavailable · recording continues";
  };
  candidate.src = `${source}${separator}t=${Date.now()}`;
}

async function refresh(episodeId, generation, previousSignature, intervalSeconds) {
  if (generation !== refreshGeneration) return;
  let views;
  let ended = false;
  try {
    views = await api(`/episodes/${encodeURIComponent(episodeId)}/current-views`);
    if (!views.length && previousSignature) {
      const episode = await api(`/episodes/${encodeURIComponent(episodeId)}`);
      ended = ["closed", "archived"].includes(episode.state);
    }
  } catch {
    views = null;
  }
  if (generation !== refreshGeneration) return;

  const grid = document.getElementById("current-view-grid");
  let nextSignature = previousSignature;
  if (grid && views) {
    nextSignature = signature(views);
    if (nextSignature !== previousSignature) grid.innerHTML = viewsMarkup(views, ended);
  }
  document.querySelectorAll("#current-view-grid img[data-preview-url]")
    .forEach(image => loadPreview(image, generation));

  if (ended) return;

  refreshTimer = window.setTimeout(
    () => refresh(episodeId, generation, nextSignature, intervalSeconds),
    intervalSeconds * 1000,
  );
}

export function activateCurrentViews(episodeId, initialViews) {
  deactivateCurrentViews();
  const generation = refreshGeneration;
  const interval = Math.max(
    2,
    Math.min(...initialViews.map(view => view.refresh_interval_seconds || 3), 3),
  );
  document.querySelectorAll("#current-view-grid img[data-preview-url]")
    .forEach(image => loadPreview(image, generation));
  refreshTimer = window.setTimeout(
    () => refresh(episodeId, generation, signature(initialViews), interval),
    interval * 1000,
  );
}

export function deactivateCurrentViews() {
  refreshGeneration += 1;
  if (refreshTimer !== null) window.clearTimeout(refreshTimer);
  refreshTimer = null;
}
