const sourceCleanups = new WeakMap();
const LIVE_EDGE_TOLERANCE_SECONDS = 2.5;
const LIVE_STALL_RECOVERY_MS = 12_000;

function isHlsUrl(url) {
  return /\.m3u8(?:$|[?#])/.test(String(url || ""));
}

function listen(video, event, handler) {
  video.addEventListener(event, handler);
  return () => video.removeEventListener(event, handler);
}

export function getSeekableRange(video) {
  if (!video?.seekable || !video.seekable.length) return null;
  const start = Number(video.seekable.start(0));
  const end = Number(video.seekable.end(video.seekable.length - 1));
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return { start, end };
}

export function formatLiveOffset(seconds) {
  if (!Number.isFinite(seconds) || seconds <= LIVE_EDGE_TOLERANCE_SECONDS) return "Live";
  const rounded = Math.max(1, Math.round(seconds));
  if (rounded < 60) return `${rounded} second${rounded === 1 ? "" : "s"} behind live`;
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  const minuteLabel = `${minutes} minute${minutes === 1 ? "" : "s"}`;
  return remainder
    ? `${minuteLabel} ${remainder} second${remainder === 1 ? "" : "s"} behind live`
    : `${minuteLabel} behind live`;
}

export function seekToBeginning(video) {
  const range = getSeekableRange(video);
  if (!range) return false;
  video.currentTime = range.start;
  return true;
}

export function seekToLive(video) {
  const range = getSeekableRange(video);
  if (!range) return false;
  video.currentTime = Math.max(range.start, range.end - 0.25);
  if (typeof video.play === "function") {
    const playback = video.play();
    playback?.catch(() => {});
  }
  return true;
}

function mediaTimeline(video) {
  const range = getSeekableRange(video);
  if (!range) return null;
  const currentTime = Number(video.currentTime);
  const position = Number.isFinite(currentTime) ? currentTime : range.start;
  const behindLive = Math.max(0, range.end - position);
  return {
    ...range,
    currentTime: position,
    behindLive,
    isLive: behindLive <= LIVE_EDGE_TOLERANCE_SECONDS,
  };
}

function monitorTimeline(
  video,
  { live, onTimeline, recover, stallRecoveryMs = LIVE_STALL_RECOVERY_MS },
) {
  const cleanups = [];
  let waiting = false;
  let stallSince = null;
  let lastRecovery = 0;
  let lastCurrentTime = null;
  let lastRangeEnd = null;

  const emit = () => {
    const timeline = mediaTimeline(video);
    if (timeline) onTimeline(timeline);
    if (!live || !timeline || video.paused || video.ended) {
      stallSince = null;
      return;
    }

    const now = Date.now();
    const currentAdvanced = lastCurrentTime === null
      || timeline.currentTime > lastCurrentTime + 0.05;
    const playlistAdvanced = lastRangeEnd === null || timeline.end > lastRangeEnd + 0.05;
    lastCurrentTime = timeline.currentTime;
    lastRangeEnd = timeline.end;
    if (currentAdvanced || playlistAdvanced) stallSince = null;

    const readyState = Number(video.readyState);
    const needsData = waiting || !Number.isFinite(readyState) || readyState < 3;
    if (!timeline.isLive || !needsData) {
      stallSince = null;
      return;
    }
    if (stallSince === null) stallSince = now;
    if (now - stallSince < stallRecoveryMs || now - lastRecovery < stallRecoveryMs) return;
    lastRecovery = now;
    stallSince = null;
    recover();
  };

  cleanups.push(listen(video, "waiting", () => {
    waiting = true;
    emit();
  }));
  cleanups.push(listen(video, "playing", () => {
    waiting = false;
    emit();
  }));
  ["timeupdate", "durationchange", "progress", "loadedmetadata", "canplay"].forEach(event => {
    cleanups.push(listen(video, event, emit));
  });
  emit();
  const timer = live && typeof globalThis.setInterval === "function"
    ? globalThis.setInterval(emit, 500)
    : null;
  return () => {
    cleanups.forEach(cleanup => cleanup());
    if (timer !== null) globalThis.clearInterval(timer);
  };
}

function nativeSource(video, url, notify, { live, onTimeline, stallRecoveryMs }) {
  let startedAtLive = false;
  const startAtLive = () => {
    if (live && !startedAtLive && seekToLive(video)) startedAtLive = true;
  };
  const cleanups = [
    listen(video, "loadstart", () => notify("loading", "Loading recording…")),
    listen(video, "loadedmetadata", startAtLive),
    listen(video, "waiting", () => notify("buffering", "Buffering recording…")),
    listen(video, "playing", () => notify("ready", "Playback ready")),
    listen(video, "error", () => notify(
      "error",
      "This recording cannot be played. The codec may not be supported by this browser.",
    )),
  ];
  const monitor = monitorTimeline(video, {
    live,
    onTimeline,
    stallRecoveryMs,
    recover: () => {
      notify("reconnecting", "Recording stream interrupted · retrying playback…");
      video.load();
      if (typeof video.play === "function") {
        const playback = video.play();
        playback?.catch(() => {});
      }
    },
  });
  video.src = url;
  return () => {
    cleanups.forEach(cleanup => cleanup());
    monitor();
    video.removeAttribute("src");
    video.load();
  };
}

export function isHlsEvidence(evidence) {
  return evidence?.metadata?.format === "hls-fmp4"
    || evidence?.mime_type === "application/vnd.apple.mpegurl";
}

export function evidenceMediaUrl(evidence) {
  return isHlsEvidence(evidence)
    ? `/api/v1/recordings/${encodeURIComponent(evidence.id)}/index.m3u8`
    : `/api/v1/evidence/${encodeURIComponent(evidence.id)}/file`;
}

export function updateMediaStatus(element, { state, message }) {
  if (!element) return;
  const visible = !["ready", "idle"].includes(state);
  element.className = `media-playback-status media-state-${state}${visible ? "" : " hidden"}`;
  element.textContent = message || "";
}

export function attachMediaSource(
  video,
  url,
  { live = false, onState = () => {}, onTimeline = () => {}, stallRecoveryMs } = {},
) {
  detachMediaSource(video);
  const notify = (state, message) => onState({ state, message });
  if (!isHlsUrl(url) || video.canPlayType("application/vnd.apple.mpegurl")) {
    const cleanup = nativeSource(video, url, notify, { live, onTimeline, stallRecoveryMs });
    sourceCleanups.set(video, cleanup);
    return () => detachMediaSource(video);
  }
  if (!window.Hls?.isSupported()) {
    notify(
      "unavailable",
      "HLS playback support could not be loaded. Check this browser's internet access.",
    );
    return () => {
      video.removeAttribute("src");
      video.load();
    };
  }
  const player = new window.Hls({
    enableWorker: true,
    lowLatencyMode: false,
    liveDurationInfinity: false,
  });
  const handleManifest = () => notify("ready", "Playback ready");
  const handleFragment = () => {
    notify("ready", live ? "Live recording" : "Playback ready");
  };
  const handleError = (_event, data = {}) => {
    if (!data.fatal) {
      if (data.type === window.Hls.ErrorTypes?.NETWORK_ERROR) {
        notify("buffering", live ? "Waiting for the next recording fragment…" : "Buffering recording…");
      }
      return;
    }
    if (data.type === window.Hls.ErrorTypes?.NETWORK_ERROR) {
      if (live) {
        notify("reconnecting", "Recording stream interrupted · retrying playback…");
        player.startLoad();
      } else {
        notify("error", "Recording media is incomplete or temporarily unavailable.");
      }
      return;
    }
    if (data.type === window.Hls.ErrorTypes?.MEDIA_ERROR) {
      notify("reconnecting", "Browser media decoder interrupted · recovering…");
      player.recoverMediaError();
      return;
    }
    notify(
      "error",
      "This recording cannot be played. The codec may not be supported by this browser.",
    );
  };
  player.on(window.Hls.Events.MANIFEST_PARSED, handleManifest);
  player.on(window.Hls.Events.FRAG_BUFFERED, handleFragment);
  player.on(window.Hls.Events.ERROR, handleError);
  const monitor = monitorTimeline(video, {
    live,
    onTimeline,
    stallRecoveryMs,
    recover: () => {
      notify("reconnecting", "Recording stream interrupted · retrying playback…");
      player.startLoad();
      if (typeof video.play === "function") {
        const playback = video.play();
        playback?.catch(() => {});
      }
    },
  });
  notify("loading", "Loading recording…");
  player.loadSource(url);
  player.attachMedia(video);
  sourceCleanups.set(video, () => {
    monitor();
    player.destroy();
  });
  return () => detachMediaSource(video);
}

export function detachMediaSource(video) {
  const cleanup = sourceCleanups.get(video);
  if (cleanup) {
    sourceCleanups.delete(video);
    cleanup();
  }
}
