import { api } from "./api.js?v=3";
import { escHtml } from "./dom.js";
import {
  attachMediaSource,
  evidenceMediaUrl,
  updateMediaStatus,
} from "./media-player.js?v=7";

export const MAX_HISTORY_RECORDINGS = 50;

function deviceLabel(deviceId, deviceNames) {
  return deviceNames.get(deviceId) || deviceId || "Unknown Device";
}

function timeLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
}

function durationLabel(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "";
  const total = Math.round(value);
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return minutes ? `${minutes}m ${String(remainder).padStart(2, "0")}s` : `${remainder}s`;
}

function playableRecording(recording) {
  if (!recording || recording.availability === "expired") return false;
  const mime = String(recording.mime_type || "").toLowerCase();
  const format = String(recording.metadata?.format || "").toLowerCase();
  return mime.startsWith("video/")
    || mime === "application/vnd.apple.mpegurl"
    || format === "hls-fmp4";
}

function recordingSource(recording, deviceNames) {
  if (!playableRecording(recording)) return null;
  const device = deviceLabel(recording.device_id, deviceNames);
  const duration = durationLabel(recording.metadata?.duration_seconds);
  const detail = [timeLabel(recording.timestamp), duration].filter(Boolean).join(" · ");
  return {
    id: `recording:${recording.id}`,
    label: `${device} · ${detail}`,
    cameraLabel: device,
    url: evidenceMediaUrl(recording),
    live: false,
    deviceId: recording.device_id,
    evidenceId: recording.id,
  };
}

function currentViewSource(view, deviceNames) {
  if (
    !view
    || view.mode !== "hls"
    || !view.stream_url
    || view.recording_state === "failed"
  ) return null;
  const device = view.device_name || deviceLabel(view.device_id, deviceNames);
  return {
    id: `live:${view.device_id}`,
    label: `${device} · Live recording`,
    cameraLabel: device,
    url: view.stream_url,
    live: true,
    deviceId: view.device_id,
    evidenceId: view.recording_evidence_id || null,
  };
}

/**
 * Return bounded, playable sources for the selected Episode. Expired and
 * non-video Evidence stays out of the player while remaining available on the
 * full Episode page.
 */
export function recordingSourceOptions(recordings = [], views = [], deviceNames = new Map()) {
  const sources = [];
  const seen = new Set();
  for (const view of views) {
    const source = currentViewSource(view, deviceNames);
    if (source && !seen.has(source.id)) {
      seen.add(source.id);
      sources.push(source);
    }
  }
  for (const recording of recordings) {
    const source = recordingSource(recording, deviceNames);
    if (source && !seen.has(source.id)) {
      seen.add(source.id);
      sources.push(source);
    }
  }
  return {
    options: sources.slice(0, MAX_HISTORY_RECORDINGS),
    capped: recordings.length > MAX_HISTORY_RECORDINGS || sources.length > MAX_HISTORY_RECORDINGS,
    expiredCount: recordings.filter(item => item.availability === "expired").length,
    unready: views.some(view => !currentViewSource(view, deviceNames)),
  };
}

/**
 * The viewer owns one clean media viewport. The history page supplies the
 * delayed snapshot cover so the player can be useful before covers arrive.
 */
export function renderEpisodeRecordingPanel({ cover = "" } = {}) {
  return `<section class="episode-history-media" data-episode-recordings>
    <div class="episode-history-media-viewport" data-recording-media>
      ${cover}
      <div class="episode-history-player-stage" data-recording-player-stage hidden>
        <video controls muted playsinline preload="metadata" aria-label="Episode recording playback" data-recording-player></video>
        <div class="media-playback-status hidden" data-recording-player-status role="status"></div>
      </div>
      <div class="episode-history-recording-state" data-recording-state role="status" aria-live="polite"></div>
    </div>
    <footer class="episode-history-player-footer" data-recording-footer>
      <span class="episode-history-recording-camera" data-recording-camera></span>
      <label class="episode-history-recording-picker" data-recording-picker hidden>
        <select aria-label="Camera / recording" data-recording-select disabled></select>
      </label>
      <button type="button" class="button button-ghost episode-history-recording-retry" data-recording-retry hidden>Retry</button>
    </footer>
  </section>`;
}

function setState(element, message, error = false) {
  if (!element) return;
  element.textContent = message;
  element.classList.toggle("is-error", error);
}

function optionsMarkup(options) {
  const cameraCounts = new Map();
  for (const option of options) {
    cameraCounts.set(option.cameraLabel, (cameraCounts.get(option.cameraLabel) || 0) + 1);
  }
  return options.map((option, index) =>
    `<option value="${escHtml(option.id)}"${index === 0 ? " selected" : ""}>${escHtml(cameraCounts.get(option.cameraLabel) === 1 ? option.cameraLabel : option.label)}</option>`,
  ).join("");
}

function isOpenEpisode(episode) {
  return ["active", "quiescent"].includes(String(episode?.state || "").toLowerCase());
}

/**
 * Bind the automatically loaded player for one selected Episode. Cleanup is
 * deliberately explicit: a route change must prevent a late response from
 * reattaching media after this instance has gone away.
 */
export function bindEpisodeHistoryPlayer(
  root,
  episode,
  getDevices = async () => [],
  token = 0,
  fetchApi = api,
) {
  const panel = root?.querySelector?.("[data-episode-recordings]");
  if (!panel || !episode?.id) return () => {};
  const retryButton = panel.querySelector("[data-recording-retry]");
  const picker = panel.querySelector("[data-recording-picker]");
  const select = panel.querySelector("[data-recording-select]");
  const stage = panel.querySelector("[data-recording-player-stage]");
  const video = panel.querySelector("[data-recording-player]");
  const status = panel.querySelector("[data-recording-state]");
  const playbackStatus = panel.querySelector("[data-recording-player-status]");
  const camera = panel.querySelector("[data-recording-camera]");
  const cover = root.querySelector?.("[data-preview-cover]");
  let generation = 0;
  let disposed = false;
  let detach = () => {};
  let detachMediaEvents = () => {};
  let sources = [];
  let loading = false;

  const current = () => !disposed
    && root.isConnected !== false
    && root.dataset.selectedEpisode === episode.id
    && root.dataset.detailToken === String(token);

  const showRetry = visible => {
    if (retryButton) retryButton.hidden = !visible;
  };

  const clearMedia = (restoreCover = true) => {
    video?.pause?.();
    detachMediaEvents();
    detachMediaEvents = () => {};
    detach();
    detach = () => {};
    if (stage) stage.hidden = true;
    if (cover && restoreCover) cover.hidden = false;
    if (playbackStatus) updateMediaStatus(playbackStatus, { state: "idle", message: "" });
  };

  const attach = source => {
    if (!source || !video || !current()) return;
    clearMedia(false);
    showRetry(false);
    if (stage) stage.hidden = true;
    video.muted = true;
    video.autoplay = false;
    video.controls = true;
    const requestGeneration = generation;
    let failedDuringAttach = false;
    const reveal = () => {
      if (!current() || requestGeneration !== generation) return;
      if (stage) stage.hidden = false;
      if (cover) cover.hidden = true;
      updateMediaStatus(playbackStatus, { state: "idle", message: "" });
    };
    const fail = message => {
      if (!current() || requestGeneration !== generation) return;
      failedDuringAttach = true;
      clearMedia();
      setState(status, message, true);
      showRetry(true);
    };
    const stateChange = state => {
      if (!current() || requestGeneration !== generation) return;
      if (state.state === "ready") reveal();
      if (["error", "unavailable"].includes(state.state)) {
        fail(state.message || "This recording cannot be played.");
        return;
      }
      // Ready is intentionally quiet; the native controls communicate it.
      updateMediaStatus(playbackStatus, state.state === "ready"
        ? { state: "idle", message: "" }
        : state);
    };
    const onMetadata = () => reveal();
    const onCanPlay = () => reveal();
    video.addEventListener?.("loadedmetadata", onMetadata);
    video.addEventListener?.("canplay", onCanPlay);
    detachMediaEvents = () => {
      video.removeEventListener?.("loadedmetadata", onMetadata);
      video.removeEventListener?.("canplay", onCanPlay);
    };
    const mediaCleanup = attachMediaSource(video, source.url, {
      live: source.live,
      onState: stateChange,
    });
    detach = mediaCleanup;
    if (failedDuringAttach) {
      mediaCleanup?.();
      detach = () => {};
    }
    if (camera) camera.textContent = sources.length < 2 ? source.cameraLabel || "" : "";
    return !failedDuringAttach;
  };

  const load = async () => {
    if (loading || !current()) return;
    loading = true;
    generation += 1;
    const requestGeneration = generation;
    clearMedia();
    sources = [];
    if (picker) picker.hidden = true;
    if (select) {
      select.innerHTML = "";
      select.disabled = true;
    }
    if (camera) camera.textContent = "";
    showRetry(false);
    setState(status, "Loading recording…");
    try {
      const requests = [
        fetchApi(`/evidence?episode_id=${encodeURIComponent(episode.id)}&evidence_type=recording&limit=${MAX_HISTORY_RECORDINGS + 1}&offset=0`),
        getDevices(),
      ];
      if (isOpenEpisode(episode)) {
        requests.push(fetchApi(`/episodes/${encodeURIComponent(episode.id)}/current-views`));
      }
      const [recordingsResult, devicesResult, viewsResult] = await Promise.allSettled(requests);
      if (!current() || requestGeneration !== generation) return;
      const recordings = recordingsResult.status === "fulfilled" && Array.isArray(recordingsResult.value)
        ? recordingsResult.value
        : [];
      const deviceNames = devicesResult.status === "fulfilled" && Array.isArray(devicesResult.value)
        ? new Map(devicesResult.value.map(device => [device.id, device.name]))
        : new Map();
      const views = viewsResult?.status === "fulfilled" && Array.isArray(viewsResult.value)
        ? viewsResult.value
        : [];
      const result = recordingSourceOptions(recordings, views, deviceNames);
      sources = result.options;
      if (result.options.length) {
        const multiple = result.options.length > 1;
        if (picker) picker.hidden = !multiple;
        if (select) {
          select.innerHTML = optionsMarkup(result.options);
          select.disabled = !multiple;
        }
        const attached = attach(result.options[0]);
        if (attached && result.capped) {
          setState(status, `Showing the first ${MAX_HISTORY_RECORDINGS} recordings. Open the full Episode to see the rest.`);
        } else if (attached) {
          setState(status, "");
        }
        return;
      }
      if (recordingsResult.status === "rejected" || viewsResult?.status === "rejected") {
        setState(status, "Recordings are temporarily unavailable. Try again shortly.", true);
        showRetry(true);
      } else if (result.unready) {
        setState(status, "Recording is starting or the live stream is unavailable. Try again shortly.");
        showRetry(true);
      } else if (result.expiredCount) {
        setState(status, "The recordings for this Episode have expired under the retention policy.", true);
      } else {
        setState(status, "No playable recordings are available for this Episode.");
      }
    } catch {
      if (current() && requestGeneration === generation) {
        setState(status, "Recordings are temporarily unavailable. Try again shortly.", true);
        showRetry(true);
      }
    } finally {
      loading = false;
    }
  };

  const onRetry = () => { void load(); };
  const onSelect = () => {
    const source = sources.find(item => item.id === select?.value);
    if (!source) return;
    generation += 1;
    clearMedia();
    attach(source);
  };

  retryButton?.addEventListener("click", onRetry);
  select?.addEventListener("change", onSelect);
  void load();

  return () => {
    disposed = true;
    generation += 1;
    clearMedia();
    retryButton?.removeEventListener("click", onRetry);
    select?.removeEventListener("change", onSelect);
  };
}
