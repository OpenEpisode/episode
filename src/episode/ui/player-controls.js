// NVR Playback & Detections — Player + Controls.
// Attaches HLS video via media-player.js and provides a 48 px control bar.
// Pure seek/segment math is exported for headless testing; mountPlayer wires
// the DOM, video events, and the control bar.
import { attachMediaSource, evidenceMediaUrl, updateMediaStatus } from "./media-player.js?v=8";

// ---------------------------------------------------------------------------
// Pure seek math (testable without DOM)
// ---------------------------------------------------------------------------

/** Seconds into the video for a given playhead time and segment start. */
export function seekOffset(playheadMs, segmentStartMs) {
  return Math.max(0, playheadMs - segmentStartMs) / 1000;
}

/**
 * Find the segment containing `timeMs`, or the nearest one by boundary
 * distance. Returns null when there are no segments.
 */
export function findSegment(segments, timeMs) {
  if (!segments?.length) return null;
  const contained = segments.find(seg => timeMs >= seg.start && timeMs < seg.end);
  if (contained) return contained;
  let best = null;
  let bestDist = Infinity;
  for (const seg of segments) {
    const dist = timeMs < seg.start ? seg.start - timeMs : timeMs - seg.end;
    if (dist < bestDist) {
      bestDist = dist;
      best = seg;
    }
  }
  return best;
}

export const SPEEDS = [0.5, 1, 2, 4, 8, 16];

/** Format seconds as H:MM:SS or M:SS. */
export function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * Render the 48 px control bar HTML.
 * @param {{isPlaying?:boolean, isMuted?:boolean, speed?:number, isFullscreen?:boolean, timeLabel?:string}} state
 */
export function renderControls({ isPlaying = false, isMuted = false, speed = 1, isFullscreen = false, timeLabel = "0:00" } = {}) {
  return `
    <div class="tl-controls" role="toolbar" aria-label="Playback controls">
      <button type="button" class="tl-ctl-btn" data-ctl="playpause" aria-label="${isPlaying ? "Pause" : "Play"}" title="${isPlaying ? "Pause" : "Play"}">
        <svg><use href="icons.svg#${isPlaying ? "pause" : "play"}"></use></svg>
      </button>
      <button type="button" class="tl-ctl-btn" data-ctl="back10" aria-label="Back 10 seconds" title="-10 s">
        <svg><use href="icons.svg#rewind"></use></svg>
      </button>
      <span class="tl-ctl-time" data-ctl-time>${timeLabel}</span>
      <button type="button" class="tl-ctl-btn" data-ctl="fwd10" aria-label="Forward 10 seconds" title="+10 s">
        <svg><use href="icons.svg#forward"></use></svg>
      </button>
      <select class="tl-ctl-speed" data-ctl="speed" aria-label="Playback speed">
        ${SPEEDS.map(s => `<option value="${s}"${s === speed ? " selected" : ""}>${s}×</option>`).join("")}
      </select>
      <button type="button" class="tl-ctl-btn${isMuted ? " active" : ""}" data-ctl="mute" aria-label="${isMuted ? "Unmute" : "Mute"}" title="${isMuted ? "Unmute" : "Mute"}">
        <svg><use href="icons.svg#${isMuted ? "unmute" : "mute"}"></use></svg>
      </button>
      <button type="button" class="tl-ctl-btn${isFullscreen ? " active" : ""}" data-ctl="fullscreen" aria-label="Fullscreen" title="Fullscreen">
        <svg><use href="icons.svg#fullscreen"></use></svg>
      </button>
      <button type="button" class="tl-ctl-btn" data-ctl="export" aria-label="Export clip" title="Export clip">
        <svg><use href="icons.svg#export"></use></svg>
      </button>
    </div>`;
}

// ---------------------------------------------------------------------------
// DOM mount
// ---------------------------------------------------------------------------

/**
 * Mount the player into `container`.
 * @param {HTMLElement} container
 * @param {{ onStateChange?: (s: {isPlaying, isMuted, speed}) => void, onExport?: (segment: object|null) => void }} options
 * @returns {{ seekTo, playSegment, togglePlay, setSpeed, toggleMute, toggleFullscreen, cleanup }}
 */
export function mountPlayer(container, { onStateChange = () => {}, onExport = () => {} } = {}) {
  container.innerHTML = `
    <div class="tl-player-wrap">
      <div class="tl-player-stage">
        <video class="tl-player-video" playsinline controls tabindex="0"></video>
        <div class="tl-media-status hidden"></div>
      <div class="tl-player-empty"><svg><use href="icons.svg#clock"></use></svg><span role="status" aria-live="polite">Click a recording on the timeline</span></div>
      </div>
      <div class="tl-controls-row">
        <div class="tl-controls-wrap"></div>
        <a class="tl-episode-link hidden" data-ctl-episode href="#" aria-label="Open the source episode"></a>
      </div>
    </div>`;

  const video = container.querySelector(".tl-player-video");
  const statusEl = container.querySelector(".tl-media-status");
  const controlsWrap = container.querySelector(".tl-controls-wrap");
  const emptyEl = container.querySelector(".tl-player-empty");
  const episodeLink = container.querySelector(".tl-episode-link");
  const emptyMessage = emptyEl?.querySelector("span");
  const defaultEmptyMessage = "Click a recording on the timeline";

  let currentSegment = null;
  let mediaCleanup = null;
  let isPlaying = false;
  let isMuted = false;
  let speed = 1;
  let isFullscreen = false;
  let timeTimer = null;
  let ready = false;
  let readyPromise = Promise.resolve();
  let resolveReady = null;
  let pendingSeek = null;
  let attachSequence = 0;
  let playRequest = 0;
  let metadataCleanup = null;

  function renderBar() {
    controlsWrap.innerHTML = renderControls({
      isPlaying, isMuted, speed, isFullscreen,
      timeLabel: formatTime(video.currentTime ?? 0),
    });
  }

  function emitState() {
    onStateChange({ isPlaying, isMuted, speed });
  }

  // Lower-right link to the source episode. Visible only while a recording
  // segment (which carries its episode id) is attached; hidden when empty.
  function updateEpisodeLink() {
    if (!episodeLink) return;
    const id = currentSegment?.episodeId;
    if (id) {
      episodeLink.href = `#episode/${encodeURIComponent(id)}`;
      episodeLink.textContent = "Open episode";
      episodeLink.classList.remove("hidden");
    } else {
      episodeLink.classList.add("hidden");
    }
  }

  function attachSegment(segment, seekMs) {
    const sequence = ++attachSequence;
    const stage = container.querySelector(".tl-player-stage");
    const prevHeight = stage?.offsetHeight;
    metadataCleanup?.();
    metadataCleanup = null;
    if (mediaCleanup) {
      mediaCleanup();
      mediaCleanup = null;
    }
    video.pause();
    isPlaying = false;
    if (!segment) {
      currentSegment = null;
      ready = false;
      pendingSeek = null;
      video.removeAttribute("src");
      video.load();
      updateMediaStatus(statusEl, { state: "idle", message: "" });
      updateEpisodeLink();
      if (emptyMessage) emptyMessage.textContent = defaultEmptyMessage;
      emptyEl?.classList.remove("hidden");
      stage?.style.removeProperty("min-height");
      return Promise.resolve();
    }
    // Keep the stage from collapsing while the new stream loads.
    if (prevHeight && stage) stage.style.minHeight = prevHeight + "px";
    currentSegment = segment;
    ready = false;
    pendingSeek = seekMs;
    readyPromise = new Promise(resolve => { resolveReady = resolve; });
    emptyEl?.classList.add("hidden");
    updateEpisodeLink();
    const onMeta = () => {
      if (sequence !== attachSequence) return;
      const target = pendingSeek ?? segment.start;
      const targetOffset = seekOffset(target, segment.start);
      if (!video.duration || !Number.isFinite(video.duration)) video.currentTime = targetOffset;
      else video.currentTime = Math.min(targetOffset, video.duration);
      pendingSeek = null;
      ready = true;
      video.removeEventListener("loadedmetadata", onMeta);
      stage?.style.removeProperty("min-height");
      resolveReady?.();
      resolveReady = null;
      metadataCleanup = null;
    };
    video.addEventListener("loadedmetadata", onMeta);
    metadataCleanup = () => video.removeEventListener("loadedmetadata", onMeta);
    const url = evidenceMediaUrl(segment);
    mediaCleanup = attachMediaSource(video, url, {
      onState: ({ state, message }) => {
        if (sequence !== attachSequence) return;
        updateMediaStatus(statusEl, { state, message });
      },
    });
    return readyPromise;
  }

  function seekTo(timeMs) {
    if (!currentSegment) return;
    pendingSeek = timeMs;
    if (!ready) return;
    const offset = seekOffset(timeMs, currentSegment.start);
    if (video.duration && Number.isFinite(video.duration)) {
      video.currentTime = Math.min(offset, video.duration);
    } else {
      video.currentTime = offset;
    }
  }

  function playSegment(segment, timeMs) {
    const sequence = attachSequence + 1;
    const pending = attachSegment(segment, timeMs);
    const request = ++playRequest;
    pending.then(() => {
      if (request !== playRequest || sequence !== attachSequence || !currentSegment) return;
      video.play().catch(() => {
        isPlaying = false;
        renderBar();
        emitState();
      });
    });
    renderBar();
    emitState();
  }

  function loadSegment(segment, timeMs) {
    const request = ++playRequest;
    const pending = attachSegment(segment, timeMs);
    pending.then(() => {
      if (request !== playRequest) return;
      if (segment && currentSegment === segment) {
        video.pause();
        isPlaying = false;
        renderBar();
        emitState();
      }
    });
  }

  function setEmptyMessage(message) {
    if (emptyMessage) emptyMessage.textContent = message || defaultEmptyMessage;
    emptyEl?.classList.remove("hidden");
  }

  /** Move keyboard focus to the video (space/arrows then act on the player). */
  function focus() {
    video.focus({ preventScroll: true });
  }

  const onVideoClick = () => focus();

  /** Absolute playhead time in ms (segment start + video offset), or null. */
  function now() {
    if (!currentSegment) return null;
    const t = video.currentTime;
    if (!Number.isFinite(t)) return null;
    return currentSegment.start + t * 1000;
  }

  function pause() {
    if (!currentSegment) return;
    ++playRequest;
    if (video.paused) {
      isPlaying = false;
      renderBar();
      emitState();
      return;
    }
    video.pause();
    isPlaying = false;
    renderBar();
    emitState();
  }

  function togglePlay() {
    if (!currentSegment) return;
    if (video.paused) {
      const request = ++playRequest;
      // Call play synchronously from the user-gesture handler. Waiting for
      // metadata here can consume Chrome's transient activation and make the
      // first Space press appear to do nothing; the media element will wait
      // for its source to become playable internally.
      try {
        const playback = video.play();
        playback?.catch(() => {
          if (request !== playRequest) return;
          isPlaying = false;
          renderBar();
          emitState();
        });
      } catch {
        if (request === playRequest) {
          isPlaying = false;
          renderBar();
          emitState();
        }
      }
    } else {
      ++playRequest;
      video.pause();
      isPlaying = false;
    }
    renderBar();
    emitState();
  }

  function skip(deltaSeconds) {
    if (!Number.isFinite(video.currentTime)) return;
    video.currentTime = Math.max(0, video.currentTime + deltaSeconds);
  }

  function setSpeed(newSpeed) {
    video.playbackRate = newSpeed;
    speed = newSpeed;
    renderBar();
    emitState();
  }

  function toggleMute() {
    video.muted = !video.muted;
    isMuted = video.muted;
    renderBar();
    emitState();
  }

  function toggleFullscreen() {
    const wrap = container.querySelector(".tl-player-wrap");
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      wrap?.requestFullscreen?.().catch(() => {});
    }
  }

  // Video events → state
  const onPlay = () => {
    isPlaying = true;
    renderBar();
    emitState();
  };
  const onPause = () => {
    isPlaying = false;
    renderBar();
    emitState();
  };
  const onFsChange = () => {
    isFullscreen = document.fullscreenElement !== null;
    renderBar();
  };
  video.addEventListener("play", onPlay);
  video.addEventListener("pause", onPause);
  video.addEventListener("click", onVideoClick);
  document.addEventListener("fullscreenchange", onFsChange);

  // Time display update
  timeTimer = setInterval(() => {
    const timeEl = controlsWrap.querySelector("[data-ctl-time]");
    if (timeEl) timeEl.textContent = formatTime(video.currentTime ?? 0);
  }, 500);

  // Control bar interaction
  controlsWrap.addEventListener("click", event => {
    const btn = event.target.closest("[data-ctl]");
    if (!btn) return;
    const ctl = btn.dataset.ctl;
    if (ctl === "playpause") togglePlay();
    else if (ctl === "back10") skip(-10);
    else if (ctl === "fwd10") skip(10);
    else if (ctl === "mute") toggleMute();
    else if (ctl === "fullscreen") toggleFullscreen();
    else if (ctl === "export") onExport(currentSegment);
  });

  controlsWrap.addEventListener("change", event => {
    const select = event.target.closest("[data-ctl='speed']");
    if (select) setSpeed(Number(select.value));
  });

  renderBar();

  function cleanup() {
    ++attachSequence;
    ++playRequest;
    metadataCleanup?.();
    metadataCleanup = null;
    video.removeEventListener("play", onPlay);
    video.removeEventListener("pause", onPause);
    video.removeEventListener("click", onVideoClick);
    document.removeEventListener("fullscreenchange", onFsChange);
    clearInterval(timeTimer);
    if (mediaCleanup) {
      mediaCleanup();
      mediaCleanup = null;
    }
    video.removeAttribute("src");
    video.load();
    container.innerHTML = "";
  }

  return {
    seekTo,
    playSegment,
    loadSegment,
    now,
    currentSegment: () => currentSegment,
    setEmptyMessage,
    focus,
    pause,
    togglePlay,
    setSpeed,
    toggleMute,
    toggleFullscreen,
    cleanup,
  };
}
