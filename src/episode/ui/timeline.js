export const DETECTION_OBSERVATION_GAP_MS = 2500;

function timestamp(value) {
  const result = new Date(value).getTime();
  return Number.isFinite(result) ? result : 0;
}

function titleCase(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, letter => letter.toUpperCase());
}

export function eventTitle(event) {
  const type = String(event.event_type || "").toLowerCase();
  const phase = String(event.metadata?.phase || "").toLowerCase();
  if (type === "doorbell" && phase === "ringing") return "Doorbell rang";
  if (type === "doorbell" && phase === "dismissed") return "Doorbell call ended";
  if (type === "door_access" && event.metadata?.sdk_event_name === "unlock_record") {
    return "Door unlock record";
  }
  if (type.includes("door") && ["unlocked", "unlock", "opened", "open"].includes(phase)) {
    return "Door unlocked";
  }
  if (type.includes("door") && ["locked", "lock", "closed", "close"].includes(phase)) {
    return "Door locked";
  }
  if (type.includes("human") || type.includes("person")) return "Human detected";
  if (type.includes("vehicle")) return "Vehicle detected";
  if (type.includes("motion")) return "Motion detected";
  return titleCase(event.event_type || "Event");
}

export function recordingBounds(recording) {
  const start = timestamp(recording.metadata?.started_at || recording.timestamp);
  const explicitEnd = timestamp(recording.metadata?.ended_at);
  const duration = Number(recording.metadata?.duration_seconds || 0) * 1000;
  return {
    start,
    end: explicitEnd || start + duration || start,
  };
}

function eventEntries(events) {
  const entries = [];
  const open = new Map();
  for (const event of [...events].sort((a, b) => timestamp(a.timestamp) - timestamp(b.timestamp))) {
    const state = String(event.event_state || "").toLowerCase();
    const key = event.device_id + "::" + event.event_type;
    if (state === "inactive") {
      const pending = open.get(key);
      if (pending?.length) {
        const entry = pending.shift();
        entry.end = timestamp(event.timestamp);
        entry.endEvent = event;
      }
    }
    const entry = {
      id: "event-" + event.id,
      kind: "event",
      start: timestamp(event.timestamp),
      end: timestamp(event.timestamp),
      deviceId: event.device_id,
      event,
      endEvent: null,
      title: eventTitle(event),
    };
    entries.push(entry);
    if (state === "active") {
      if (!open.has(key)) open.set(key, []);
      open.get(key).push(entry);
    }
  }
  return entries;
}

function snapshotEntries(snapshots) {
  return [...snapshots]
    .sort((a, b) => timestamp(a.timestamp) - timestamp(b.timestamp))
    .map(snapshot => ({
      id: "snapshot-" + snapshot.id,
      kind: "snapshot",
      start: timestamp(snapshot.timestamp),
      end: timestamp(snapshot.timestamp),
      deviceId: snapshot.device_id,
      item: snapshot,
      relatedEvent: null,
    }));
}

function hasBoundingBox(entry) {
  const box = entry.event?.metadata?.bounding_box;
  return box && [box.x, box.y, box.width, box.height].every(Number.isFinite);
}

export function buildDetectionTracks(entries) {
  const tracks = [];
  const eventsById = new Map(
    entries.filter(entry => entry.kind === "event").map(entry => [entry.event.id, entry.event])
  );
  const currentByDevice = new Map();
  const closeTrack = (deviceId, explicitEnd = null) => {
    const current = currentByDevice.get(deviceId);
    if (!current) return;
    const inferredEnd = current.lastObservedAt + DETECTION_OBSERVATION_GAP_MS;
    current.end = explicitEnd === null ? inferredEnd : Math.min(explicitEnd, inferredEnd);
    tracks.push(current);
    currentByDevice.delete(deviceId);
  };

  for (const observation of [...entries].sort((a, b) => a.start - b.start)) {
    if (observation.kind === "event") {
      const state = String(observation.event.event_state || "").toLowerCase();
      if (state === "inactive") {
        const current = currentByDevice.get(observation.deviceId);
        if (current?.updates.at(-1)?.event.event_type === observation.event.event_type) {
          closeTrack(observation.deviceId, observation.start);
        }
      } else if (hasBoundingBox(observation)) {
        let current = currentByDevice.get(observation.deviceId);
        if (current
            && observation.start - current.lastObservedAt > DETECTION_OBSERVATION_GAP_MS) {
          closeTrack(observation.deviceId);
          current = null;
        }
        if (!current) {
          current = {
            deviceId: observation.deviceId,
            start: observation.start,
            end: observation.start,
            lastObservedAt: observation.start,
            updates: [],
          };
          currentByDevice.set(observation.deviceId, current);
        }
        current.updates.push(observation);
        current.lastObservedAt = observation.start;
      }
      continue;
    }

    const exact = eventsById.get(observation.item.event_id);
    if (exact) {
      observation.relatedEvent = exact;
    }
    const isTargetSnapshot = String(observation.item.metadata?.event_type || "")
      .toLowerCase().includes("target");
    const current = currentByDevice.get(observation.deviceId);
    if ((isTargetSnapshot || exact?.metadata?.bounding_box)
        && current
        && observation.start - current.lastObservedAt <= DETECTION_OBSERVATION_GAP_MS) {
      observation.relatedEvent ||= current.updates.at(-1).event;
      current.lastObservedAt = observation.start;
    } else if (current && observation.start - current.lastObservedAt > DETECTION_OBSERVATION_GAP_MS) {
      closeTrack(observation.deviceId);
    }
  }
  for (const deviceId of [...currentByDevice.keys()]) closeTrack(deviceId);
  return tracks;
}

export function detectionForMoment(tracks, deviceId, moment) {
  const track = tracks.find(candidate =>
    candidate.deviceId === deviceId && candidate.start <= moment && candidate.end >= moment
  );
  if (!track) return null;
  return [...track.updates].reverse().find(update => update.start <= moment) || null;
}

export function buildEpisodeTimeline(episode, events, evidence) {
  const recordings = evidence
    .filter(item => item.evidence_type === "recording")
    .map(item => ({ ...item, bounds: recordingBounds(item) }))
    .sort((a, b) => a.bounds.start - b.bounds.start);
  const snapshots = evidence.filter(item => item.evidence_type === "snapshot");
  const entries = eventEntries(events);
  entries.push(...snapshotEntries(snapshots));
  entries.sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
  const detectionTracks = buildDetectionTracks(entries);

  const startCandidates = [
    timestamp(episode.start_time),
    ...entries.map(entry => entry.start),
    ...recordings.map(recording => recording.bounds.start),
  ].filter(Boolean);
  const endCandidates = [
    timestamp(episode.end_time || episode.last_event_time),
    ...entries.map(entry => entry.end),
    ...recordings.map(recording => recording.bounds.end),
  ].filter(Boolean);
  const start = startCandidates.length ? Math.min(...startCandidates) : 0;
  const end = endCandidates.length ? Math.max(...endCandidates) : start;

  return { start, end, entries, recordings, snapshots, detectionTracks };
}
