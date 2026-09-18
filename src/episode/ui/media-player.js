const sourceCleanups = new WeakMap();
const LIVE_STALL_RECOVERY_MS = 12_000;

function isHlsUrl(url) {
  return /\.m3u8(?:$|[?#])/.test(String(url || ""));
}

function listen(video, event, handler) {
  video.addEventListener(event, handler);
  return () => video.removeEventListener(event, handler);
}

function validRange(candidate) {
  if (!candidate || candidate.start == null || candidate.end == null) return null;
  const start = Number(candidate.start);
  const end = Number(candidate.end);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return { start, end };
}

function getSeekableRange(video) {
  return video?.seekable?.length
    ? validRange({
      start: Number(video.seekable.start(0)),
      end: Number(video.seekable.end(video.seekable.length - 1)),
    })
    : null;
}

function seekToLive(video) {
  const range = getSeekableRange(video);
  if (!range) return false;
  video.currentTime = Math.max(range.start, range.end - 0.25);
  return true;
}

function monitorTimeline(
  video,
  {
    live,
    recover,
    stallRecoveryMs = LIVE_STALL_RECOVERY_MS,
  },
) {
  const cleanups = [];
  let waiting = false;
  let stallSince = null;
  let lastRecovery = 0;
  let lastCurrentTime = null;
  let lastRangeEnd = null;

  const emit = () => {
    const range = getSeekableRange(video);
    if (!live || !range || video.paused || video.ended) {
      stallSince = null;
      return;
    }

    const now = Date.now();
    const currentTime = Number(video.currentTime);
    const currentAdvanced = lastCurrentTime === null
      || (Number.isFinite(currentTime) && currentTime > lastCurrentTime + 0.05);
    const playlistAdvanced = lastRangeEnd === null || range.end > lastRangeEnd + 0.05;
    lastCurrentTime = currentTime;
    lastRangeEnd = range.end;
    if (currentAdvanced || playlistAdvanced) stallSince = null;

    const readyState = Number(video.readyState);
    const needsData = waiting || !Number.isFinite(readyState) || readyState < 3;
    if (!needsData) {
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

function nativeSource(video, url, notify, { live, stallRecoveryMs }) {
  let initialPositionSet = !live;
  let playbackStarted = false;
  let userInteracted = false;
  const startAtLive = () => {
    if (live && !initialPositionSet && !playbackStarted && !userInteracted && seekToLive(video)) {
      initialPositionSet = true;
    }
  };
  const onPlaying = () => {
    if (!initialPositionSet) playbackStarted = true;
  };
  const onInteraction = () => {
    if (!initialPositionSet) userInteracted = true;
  };
  const cleanups = [
    listen(video, "loadstart", () => notify("loading", "Loading recording…")),
    listen(video, "loadedmetadata", startAtLive),
    listen(video, "durationchange", startAtLive),
    listen(video, "progress", startAtLive),
    listen(video, "canplay", startAtLive),
    listen(video, "playing", onPlaying),
    listen(video, "seeking", onInteraction),
    listen(video, "waiting", () => notify("buffering", "Buffering recording…")),
    listen(video, "playing", () => notify("ready", "Playback ready")),
    listen(video, "error", () => notify(
      "error",
      "This recording cannot be played. The codec may not be supported by this browser.",
    )),
  ];
  const monitor = monitorTimeline(video, {
    live,
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
  { live = false, onState = () => {}, stallRecoveryMs } = {},
) {
  detachMediaSource(video);
  const notify = (state, message) => onState({ state, message });
  const hlsUrl = isHlsUrl(url);
  const nativeHls = hlsUrl && video.canPlayType("application/vnd.apple.mpegurl");
  const hlsSupported = typeof window.Hls?.isSupported === "function"
    && window.Hls.isSupported();
  if (!hlsUrl || nativeHls) {
    const cleanup = nativeSource(video, url, notify, { live, stallRecoveryMs });
    sourceCleanups.set(video, cleanup);
    return () => detachMediaSource(video);
  }
  if (!hlsSupported) {
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
