import { API } from "./api.js?v=3";
import { eventSourceBadges } from "./components.js?v=3";
import { $, $$, escHtml } from "./dom.js";
import { fmtDuration, fmtTime, titleCase, trunc } from "./format.js?v=3";
import {
  buildEpisodeTimeline,
  detectionForMoment,
  eventTitle,
} from "./timeline.js?v=5";

function secondsLabel(milliseconds) {
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return seconds + "s";
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? minutes + "m " + remainder + "s" : minutes + "m";
}

function deviceLabel(deviceId, deviceNames) {
  return deviceNames.get(deviceId) || deviceId || "Unknown Device";
}

function recordingLabel(recording, counts, deviceNames) {
  const count = counts.get(recording.device_id) || 0;
  const label = deviceLabel(recording.device_id, deviceNames);
  if (count <= 1) return label;
  const segment = Number(recording.metadata?.segment_index || 0) + 1;
  return `${label} · segment ${segment}`;
}

function renderRecordingCoverage(model, deviceNames) {
  if (!model.recordings.length) {
    return '<div class="timeline-no-coverage">No recordings captured</div>';
  }
  const duration = Math.max(model.end - model.start, 1);
  const lanes = new Map();
  for (const recording of model.recordings) {
    if (!lanes.has(recording.device_id)) lanes.set(recording.device_id, []);
    lanes.get(recording.device_id).push(recording);
  }
  return `<div class="recording-coverage">
    ${[...lanes.entries()].map(([deviceId, recordings]) => `
      <div class="coverage-lane">
        <span title="${escHtml(deviceId)}">${escHtml(trunc(deviceLabel(deviceId, deviceNames), 24))}</span>
        <div class="coverage-track">
          ${recordings.map(recording => {
            const left = Math.max(0, (recording.bounds.start - model.start) / duration * 100);
            const width = Math.max(1.5, (recording.bounds.end - recording.bounds.start) / duration * 100);
            return `<button type="button" class="coverage-segment" data-media-id="${recording.id}"
              style="left:${left}%;width:${Math.min(width, 100 - left)}%"
              title="${fmtTime(recording.bounds.start)}–${fmtTime(recording.bounds.end)}"></button>`;
          }).join("")}
        </div>
      </div>
    `).join("")}
  </div>`;
}

function eventMarkerClass(entry) {
  const type = String(entry.event?.event_type || "").toLowerCase();
  if (type === "doorbell") return "doorbell";
  if (type.includes("human") || type.includes("person")) return "human";
  if (type.includes("vehicle")) return "vehicle";
  if (type.includes("door")) return "door";
  return "event";
}

function eventContext(event) {
  if (event.event_type !== "door_access") return "";
  const lockName = String(event.metadata?.lock_name || "").trim();
  const unlockMethod = event.metadata?.unlock_method
    ? titleCase(event.metadata.unlock_method)
    : "";
  return [lockName, unlockMethod].filter(Boolean).join(" · ");
}

function renderTimelineEvent(entry, deviceNames) {
  const duration = entry.end > entry.start
    ? ` · ${secondsLabel(entry.end - entry.start)}`
    : "";
  const context = eventContext(entry.event);
  const lockName = String(entry.event.metadata?.lock_name || "").trim();
  const unlockMethod = entry.event.metadata?.unlock_method
    ? titleCase(entry.event.metadata.unlock_method)
    : "";
  return `<div class="timeline-entry timeline-entry-${eventMarkerClass(entry)}"
      data-timeline-id="${entry.id}">
    <time datetime="${new Date(entry.start).toISOString()}">${fmtTime(entry.start)}</time>
    <span class="timeline-marker"></span>
    <div class="timeline-entry-content">
      <button type="button" class="timeline-moment" data-moment-id="${entry.id}">
        <strong>${entry.title}</strong>
        <span>${escHtml(trunc(deviceLabel(entry.deviceId, deviceNames), 28))}${duration}${context ? ` · ${escHtml(context)}` : ""}</span>
      </button>
      <details class="timeline-details">
        <summary>Details</summary>
        <div>${titleCase(entry.event.event_state)} · ${eventSourceBadges(entry.event)}</div>
        ${lockName ? `<div>Lock: ${escHtml(lockName)}</div>` : ""}
        ${unlockMethod ? `<div>Method: ${escHtml(unlockMethod)}</div>` : ""}
        ${entry.event.metadata?.unlock_outcome ? `<div>Outcome: ${titleCase(entry.event.metadata.unlock_outcome)}</div>` : ""}
        <a href="#event/${entry.event.id}">Open Event</a>
      </details>
    </div>
  </div>`;
}

function renderTimelineSnapshot(entry, deviceNames) {
  const relatedTitle = entry.relatedEvent ? eventTitle(entry.relatedEvent) : "";
  return `<div class="timeline-entry timeline-entry-snapshot" data-timeline-id="${entry.id}">
    <time datetime="${new Date(entry.start).toISOString()}">${fmtTime(entry.start)}</time>
    <span class="timeline-marker"></span>
    <div class="timeline-entry-content">
      <button type="button" class="timeline-moment timeline-snapshot-moment"
          data-moment-id="${entry.id}" data-snapshot-id="${entry.item.id}">
        <img src="${API}/evidence/${entry.item.id}/file" loading="lazy" alt="">
        <span class="timeline-snapshot-copy">
          <strong>Snapshot</strong>
          <span>${escHtml(trunc(deviceLabel(entry.deviceId, deviceNames), 28))}</span>
          ${relatedTitle ? `<small>Linked to ${relatedTitle}</small>` : `<small>Uncorrelated evidence</small>`}
        </span>
      </button>
    </div>
  </div>`;
}

function renderTimelineEntries(model, deviceNames) {
  if (!model.entries.length) return '<div class="timeline-empty">No Events or snapshots</div>';
  let previousEnd = model.start;
  const rows = [];
  for (const entry of model.entries) {
    const gap = entry.start - previousEnd;
    if (gap > 60000) {
      rows.push(`<div class="timeline-gap">
        <span></span><span class="timeline-gap-line"></span>
        <span>${secondsLabel(gap)} without activity</span>
      </div>`);
    }
    rows.push(entry.kind === "event"
      ? renderTimelineEvent(entry, deviceNames)
      : renderTimelineSnapshot(entry, deviceNames));
    previousEnd = Math.max(previousEnd, entry.end);
  }
  return rows.join("");
}

function renderMediaTabs(model, timelapseDevices, deviceNames) {
  const counts = new Map();
  for (const recording of model.recordings) {
    counts.set(recording.device_id, (counts.get(recording.device_id) || 0) + 1);
  }
  const recordingTabs = model.recordings.map(recording => `
    <button type="button" class="media-tab" data-media-id="${recording.id}">
      <span class="media-tab-dot"></span>${escHtml(recordingLabel(recording, counts, deviceNames))}
    </button>`).join("");
  const timelapseTabs = timelapseDevices.map(deviceId => `
    <button type="button" class="media-tab media-tab-secondary" data-timelapse-device="${deviceId}">
      Timelapse · ${escHtml(trunc(deviceLabel(deviceId, deviceNames), 22))}
    </button>`).join("");
  return recordingTabs || timelapseTabs
    ? `<div class="episode-media-tabs">${recordingTabs}${timelapseTabs}</div>`
    : "";
}

export function renderEpisodeWorkspace(
  episode,
  events,
  evidence,
  timelapseDevices = [],
  deviceNames = new Map(),
) {
  const model = buildEpisodeTimeline(episode, events, evidence);
  return {
    model,
    html: `<div class="episode-workspace">
      <section class="episode-media-panel">
        <div class="episode-media-header">
          <div>
            <span>Evidence player</span>
            <strong id="episode-media-title">Choose a timeline moment</strong>
          </div>
          <div class="episode-media-status">
            <label class="episode-overlay-control hidden" id="episode-overlay-control"
                title="Show detection regions while camera observations remain continuous">
              <input type="checkbox" id="episode-overlay-enabled" checked>
              Detection overlay
            </label>
            <time id="episode-playhead-time">—</time>
          </div>
        </div>
        <div class="episode-media-stage" id="episode-media-stage">
          <div class="episode-media-empty">No playable media selected</div>
        </div>
        ${renderMediaTabs(model, timelapseDevices, deviceNames)}
      </section>
      <section class="episode-timeline-panel">
        <div class="episode-timeline-heading">
          <div><span>Episode timeline</span><strong>${fmtDuration(episode.start_time, episode.end_time || episode.last_event_time)}</strong></div>
          <small>Click an Event to inspect that moment</small>
        </div>
        ${renderRecordingCoverage(model, deviceNames)}
        <div class="episode-timeline-rail">${renderTimelineEntries(model, deviceNames)}</div>
      </section>
    </div>`,
  };
}

function recordingForMoment(model, entry) {
  const covering = model.recordings.filter(recording =>
    recording.bounds.start <= entry.start && recording.bounds.end >= entry.start
  );
  return covering.find(recording => recording.device_id === entry.deviceId)
    || covering[0]
    || model.recordings.find(recording => recording.device_id === entry.deviceId)
    || model.recordings[0];
}

export function activateEpisodeWorkspace(model, episode, deviceNames = new Map()) {
  const stage = $("#episode-media-stage");
  const title = $("#episode-media-title");
  const playhead = $("#episode-playhead-time");
  const overlayControl = $("#episode-overlay-control");
  const overlayEnabled = $("#episode-overlay-enabled");
  if (!stage || !title || !playhead || !overlayControl || !overlayEnabled) return;

  let clearMedia = () => {};
  const resetMedia = () => {
    clearMedia();
    clearMedia = () => {};
    overlayControl.classList.add("hidden");
  };

  const setActiveMedia = id => {
    $$("[data-media-id]").forEach(button => {
      button.classList.toggle("active", button.dataset.mediaId === id);
    });
  };
  const setActiveMoment = id => {
    $$(".timeline-entry").forEach(row => {
      row.classList.toggle("active", row.dataset.timelineId === id);
    });
  };
  const updateMomentFromPlayback = moment => {
    const current = [...model.entries].reverse().find(entry => entry.start <= moment);
    if (current) setActiveMoment(current.id);
  };

  const showRecording = (recording, targetTime = null, play = false) => {
    if (!recording) return;
    resetMedia();
    const hasDetections = model.detectionTracks.some(track =>
      track.deviceId === recording.device_id
    );
    stage.innerHTML = `<div class="episode-video-stage">
      <video id="episode-recording-player"
        src="${API}/evidence/${recording.id}/file" controls preload="metadata"></video>
      <svg id="episode-video-overlay" viewBox="0 0 1 1" preserveAspectRatio="none"
          aria-label="Detection region">
        <rect></rect>
      </svg>
      <span id="episode-video-overlay-label"></span>
    </div>`;
    title.textContent = deviceLabel(recording.device_id, deviceNames);
    playhead.textContent = fmtTime(targetTime || recording.bounds.start);
    setActiveMedia(recording.id);
    const player = $("#episode-recording-player");
    const videoStage = stage.querySelector(".episode-video-stage");
    const overlay = $("#episode-video-overlay");
    const overlayBox = overlay.querySelector("rect");
    const overlayLabel = $("#episode-video-overlay-label");
    overlayControl.classList.toggle("hidden", !hasDetections);

    const fitOverlay = () => {
      if (!player.videoWidth || !player.videoHeight) return;
      const scale = Math.min(
        videoStage.clientWidth / player.videoWidth,
        videoStage.clientHeight / player.videoHeight,
      );
      const width = player.videoWidth * scale;
      const height = player.videoHeight * scale;
      Object.assign(overlay.style, {
        height: height + "px",
        left: (videoStage.clientWidth - width) / 2 + "px",
        top: (videoStage.clientHeight - height) / 2 + "px",
        width: width + "px",
      });
    };
    const updateOverlay = moment => {
      const match = detectionForMoment(model.detectionTracks, recording.device_id, moment);
      const box = match?.event?.metadata?.bounding_box;
      const visible = Boolean(overlayEnabled.checked && box);
      overlay.classList.toggle("visible", visible);
      overlayLabel.classList.toggle("visible", visible);
      if (!visible) return;
      overlayBox.setAttribute("x", box.x);
      overlayBox.setAttribute("y", box.y);
      overlayBox.setAttribute("width", box.width);
      overlayBox.setAttribute("height", box.height);
      overlayLabel.textContent = eventTitle(match.event);
    };
    const resizeObserver = new ResizeObserver(fitOverlay);
    resizeObserver.observe(videoStage);
    const onOverlayChange = () => {
      updateOverlay(recording.bounds.start + player.currentTime * 1000);
    };
    overlayEnabled.addEventListener("change", onOverlayChange);
    clearMedia = () => {
      resizeObserver.disconnect();
      overlayEnabled.removeEventListener("change", onOverlayChange);
    };
    const seek = () => {
      if (targetTime !== null) {
        const offset = Math.max(0, Math.min(
          (targetTime - recording.bounds.start) / 1000,
          Number.isFinite(player.duration) ? player.duration : Number.MAX_SAFE_INTEGER,
        ));
        player.currentTime = offset;
      }
      fitOverlay();
      updateOverlay(recording.bounds.start + player.currentTime * 1000);
      if (play) player.play().catch(() => {});
    };
    if (player.readyState >= 1) seek();
    else player.addEventListener("loadedmetadata", seek, { once: true });
    player.addEventListener("timeupdate", () => {
      const moment = recording.bounds.start + player.currentTime * 1000;
      playhead.textContent = fmtTime(moment);
      updateMomentFromPlayback(moment);
      updateOverlay(moment);
    });
  };

  const showSnapshot = (snapshot, event = null) => {
    if (!snapshot) return;
    resetMedia();
    const box = event?.metadata?.bounding_box;
    stage.innerHTML = `<div class="episode-snapshot-stage">
      <div class="episode-snapshot-frame">
        <img src="${API}/evidence/${snapshot.id}/file" alt="">
        ${box ? `<svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-label="Detection region">
          <rect x="${box.x}" y="${box.y}" width="${box.width}" height="${box.height}"></rect>
        </svg>` : ""}
      </div>
    </div>`;
    title.textContent = `${deviceLabel(snapshot.device_id, deviceNames)} · snapshot`;
    playhead.textContent = fmtTime(snapshot.timestamp);
    setActiveMedia("");
  };

  const showTimelapse = deviceId => {
    resetMedia();
    stage.innerHTML = `<video src="${API}/episodes/${episode.id}/timelapse?device_id=${encodeURIComponent(deviceId)}"
      controls preload="metadata"></video>`;
    title.textContent = `${deviceLabel(deviceId, deviceNames)} · timelapse`;
    playhead.textContent = "Overview";
    setActiveMedia("");
  };

  const selectMoment = entry => {
    if (!entry) return;
    setActiveMoment(entry.id);
    if (entry.kind === "snapshot") {
      showSnapshot(entry.item, entry.relatedEvent);
      return;
    }
    const recording = recordingForMoment(model, entry);
    if (recording) showRecording(recording, entry.start, true);
  };

  $$("[data-media-id]").forEach(button => {
    button.addEventListener("click", () => {
      const recording = model.recordings.find(item => item.id === button.dataset.mediaId);
      showRecording(recording);
    });
  });
  $$("[data-timelapse-device]").forEach(button => {
    button.addEventListener("click", () => showTimelapse(button.dataset.timelapseDevice));
  });
  $$("[data-moment-id]").forEach(button => {
    button.addEventListener("click", () => {
      selectMoment(model.entries.find(entry => entry.id === button.dataset.momentId));
    });
  });
  if (model.recordings.length) showRecording(model.recordings[0]);
  else if (model.snapshots.length) showSnapshot(model.snapshots[0]);
}
