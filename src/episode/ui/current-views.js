import { api } from "./api.js?v=3";
import { escHtml } from "./dom.js";
import { attachMediaSource } from "./media-player.js?v=7";

let refreshTimer = null;
let refreshGeneration = 0;
let streamDetachers = [];

function viewCard(view) {
  const streamAvailable = view.mode === "hls" && view.stream_url;
  const snapshotAvailable = view.mode === "snapshot" && view.image_url;
  const available = streamAvailable || snapshotAvailable;
  return `<article class="current-view-card ${available ? "is-loading" : "is-unavailable"}" data-device-id="${escHtml(view.device_id)}">
    <div class="current-view-frame">
      ${streamAvailable
        ? `<video muted autoplay playsinline controls aria-label="Current recording from ${escHtml(view.device_name)}" data-stream-url="${escHtml(view.stream_url)}"></video>`
        : snapshotAvailable
        ? `<img alt="Current view from ${escHtml(view.device_name)}" data-preview-url="${escHtml(view.image_url)}">`
        : '<div class="current-view-unavailable"><img src="/logo.svg" alt=""><span>Preview unavailable</span></div>'}
      <span class="current-view-live" data-current-view-live aria-live="polite"><i></i>${streamAvailable ? "Live" : available ? "Current" : "Recording"}</span>
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
      ? "This Episode has ended · recordings remain available to review."
      : "Waiting for recording Devices to join this Episode…"}</div>`;
  }
  return views.map(viewCard).join("");
}

export function renderCurrentViews(views) {
  return `<section class="current-views" aria-labelledby="current-views-title">
    <div class="current-views-heading">
      <div>
        <span class="eyebrow">Happening now</span>
        <h3 id="current-views-title">Ongoing recordings</h3>
      </div>
      <span class="current-views-note" data-current-views-state>Live preview · recording continues</span>
    </div>
    <div id="current-view-grid" class="current-view-grid">${viewsMarkup(views)}</div>
  </section>`;
}

function signature(views) {
  return views.map(view => (
    `${view.device_id}:${view.mode}:${view.stream_url || ""}`
  )).join("|");
}

function attachStreams() {
  streamDetachers.forEach(detach => detach());
  streamDetachers = [];
  document.querySelectorAll("#current-view-grid video[data-stream-url]").forEach(video => {
    const card = video.closest(".current-view-card");
    const detachMedia = attachMediaSource(video, video.dataset.streamUrl, {
      live: true,
      onState: ({ state, message }) => {
        const complete = card?.classList.contains("is-complete");
        card?.classList.toggle("has-error", !complete && ["error", "unavailable"].includes(state));
        card?.classList.toggle("is-loading", !complete && ["loading", "buffering", "reconnecting"].includes(state));
        const status = card?.querySelector(".current-view-status");
        if (status && !card?.classList.contains("is-complete") && state !== "ready") {
          status.textContent = message;
        }
        if (status && !card?.classList.contains("is-complete") && state === "ready") {
          status.textContent = "Streaming the recording as it is captured";
        }
      },
    });
    streamDetachers.push(detachMedia);
  });
}

export function markEpisodeComplete() {
  document.querySelector("[data-current-views-state]")?.replaceChildren(
    "Episode closed · recordings remain available to review",
  );
  document.querySelectorAll("#current-view-grid .current-view-card").forEach(card => {
    card.classList.add("is-complete");
    card.classList.remove("is-loading");
    const badge = card.querySelector("[data-current-view-live]");
    const status = card.querySelector(".current-view-status");
    if (badge) badge.lastChild.textContent = "Complete";
    if (status) status.textContent = "Episode closed · recording available to review";
  });
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
    if (views?.length === 0) {
      const episode = await api(`/episodes/${encodeURIComponent(episodeId)}`);
      ended = ["closed", "archived"].includes(episode.state);
    }
  } catch {
    views = null;
  }
  if (generation !== refreshGeneration) return;

  const grid = document.getElementById("current-view-grid");
  let nextSignature = previousSignature;
  if (grid && views?.length && !ended) {
    nextSignature = signature(views);
    if (nextSignature !== previousSignature) {
      grid.innerHTML = viewsMarkup(views, ended);
      attachStreams();
    }
  }
  document.querySelectorAll("#current-view-grid img[data-preview-url]")
    .forEach(image => loadPreview(image, generation));

  if (ended) {
    if (grid && !grid.querySelector(".current-view-card")) {
      grid.innerHTML = viewsMarkup([], true);
    }
    markEpisodeComplete();
    return;
  }

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
  attachStreams();
  refreshTimer = window.setTimeout(
    () => refresh(episodeId, generation, signature(initialViews), interval),
    interval * 1000,
  );
}

export function deactivateCurrentViews() {
  refreshGeneration += 1;
  streamDetachers.forEach(detach => detach());
  streamDetachers = [];
  if (refreshTimer !== null) window.clearTimeout(refreshTimer);
  refreshTimer = null;
}
