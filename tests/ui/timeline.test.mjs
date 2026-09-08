import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../../src/episode/ui/timeline.js", import.meta.url),
  "utf8",
);
const timeline = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);

test("pairs Doorbell states while keeping the end and every snapshot visible", () => {
  const episode = {
    start_time: "2026-08-10T12:08:47Z",
    end_time: "2026-08-10T12:09:43Z",
  };
  const events = [
    {
      id: "ring",
      timestamp: "2026-08-10T12:08:47Z",
      device_id: "doorbell",
      event_type: "doorbell",
      event_state: "active",
      metadata: { phase: "ringing" },
    },
    {
      id: "dismissed",
      timestamp: "2026-08-10T12:09:03Z",
      device_id: "doorbell",
      event_type: "doorbell",
      event_state: "inactive",
      metadata: { phase: "dismissed" },
    },
    {
      id: "human",
      timestamp: "2026-08-10T12:09:11Z",
      device_id: "camera",
      event_type: "human_detection",
      event_state: "active",
      metadata: { bounding_box: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 } },
    },
  ];
  const evidence = [
    {
      id: "recording",
      timestamp: "2026-08-10T12:08:47Z",
      device_id: "camera",
      evidence_type: "recording",
      metadata: {
        started_at: "2026-08-10T12:08:47Z",
        ended_at: "2026-08-10T12:09:43Z",
      },
    },
    {
      id: "snapshot-1",
      timestamp: "2026-08-10T12:09:11.032Z",
      device_id: "camera",
      evidence_type: "snapshot",
      metadata: { origin: "ftp", event_type: "md_with_target" },
    },
    {
      id: "snapshot-2",
      timestamp: "2026-08-10T12:09:14.100Z",
      device_id: "camera",
      evidence_type: "snapshot",
      metadata: { origin: "ftp", event_type: "md_with_target" },
    },
  ];

  const result = timeline.buildEpisodeTimeline(episode, events, evidence);
  const ring = result.entries.find(entry => entry.id === "event-ring");
  const dismissed = result.entries.find(entry => entry.id === "event-dismissed");
  const human = result.entries.find(entry => entry.id === "event-human");
  const snapshots = result.entries.filter(entry => entry.kind === "snapshot");

  assert.equal(ring.title, "Doorbell rang");
  assert.equal((ring.end - ring.start) / 1000, 16);
  assert.equal(dismissed.title, "Doorbell call ended");
  assert.equal(human.title, "Human detected");
  assert.deepEqual(snapshots.map(entry => entry.item.id), ["snapshot-1", "snapshot-2"]);
  assert.deepEqual(snapshots.map(entry => entry.relatedEvent?.id), ["human", undefined]);
  assert.equal(result.recordings[0].bounds.end - result.recordings[0].bounds.start, 56000);
});

test("keeps unmatched snapshots and inactive events visible", () => {
  const result = timeline.buildEpisodeTimeline(
    { start_time: "2026-08-10T12:00:00Z", end_time: "2026-08-10T12:01:00Z" },
    [{
      id: "inactive",
      timestamp: "2026-08-10T12:00:10Z",
      device_id: "camera",
      event_type: "motion",
      event_state: "inactive",
      metadata: {},
    }],
    [{
      id: "orphan-snapshot",
      timestamp: "2026-08-10T12:00:30Z",
      device_id: "other-camera",
      evidence_type: "snapshot",
      metadata: { origin: "ftp" },
    }],
  );

  assert.deepEqual(result.entries.map(entry => entry.kind), ["event", "snapshot"]);
  assert.equal(result.entries[0].title, "Motion detection ended");
  assert.equal(result.entries[1].relatedEvent, null);
});

test("clusters same-device events in one displayed second without merging entries", () => {
  const entries = [
    {
      id: "event-human-a",
      kind: "event",
      start: 10000 + 100,
      end: 10000 + 100,
      deviceId: "camera",
      event: {
        id: "human-a",
        event_type: "human_detection",
        event_state: "active",
        metadata: { bounding_box: { x: 0.1, y: 0.2, width: 0.2, height: 0.2 } },
      },
    },
    {
      id: "event-motion",
      kind: "event",
      start: 10000 + 300,
      end: 10000 + 300,
      deviceId: "camera",
      event: {
        id: "motion",
        event_type: "motion_detection",
        event_state: "active",
        metadata: {},
      },
    },
    {
      id: "event-human-b",
      kind: "event",
      start: 10000 + 800,
      end: 10000 + 800,
      deviceId: "camera",
      event: {
        id: "human-b",
        event_type: "human_detection",
        event_state: "active",
        metadata: { bounding_box: { x: 0.7, y: 0.2, width: 0.2, height: 0.2 } },
      },
    },
    {
      id: "event-other-device",
      kind: "event",
      start: 10000 + 500,
      end: 10000 + 500,
      deviceId: "other-camera",
      event: {
        id: "other-device",
        event_type: "motion_detection",
        event_state: "active",
        metadata: {},
      },
    },
    {
      id: "event-next-second",
      kind: "event",
      start: 11000,
      end: 11000,
      deviceId: "camera",
      event: {
        id: "next-second",
        event_type: "human_detection",
        event_state: "active",
        metadata: {},
      },
    },
  ];

  const moments = timeline.buildTimelineMoments(entries);
  const cameraMoment = moments.find(moment => moment.deviceId === "camera" && moment.start < 11000);

  assert.equal(moments.length, 3);
  assert.deepEqual(
    cameraMoment.entries.map(entry => entry.id),
    ["event-human-a", "event-motion", "event-human-b"],
  );
  assert.deepEqual(
    cameraMoment.entries.map(entry => entry.event.id),
    ["human-a", "motion", "human-b"],
  );
  assert.notEqual(cameraMoment.entries[0].event.metadata.bounding_box.x,
    cameraMoment.entries[2].event.metadata.bounding_box.x);
  assert.equal(moments.find(moment => moment.deviceId === "other-camera").entries.length, 1);
  assert.equal(moments.find(moment => moment.start === 11000).entries[0].event.id, "next-second");
});

test("prefers an explicit evidence Event link over the time-window heuristic", () => {
  const result = timeline.buildEpisodeTimeline(
    { start_time: "2026-08-10T12:00:00Z", end_time: "2026-08-10T12:01:00Z" },
    [{
      id: "linked-event",
      timestamp: "2026-08-10T12:00:01Z",
      device_id: "camera",
      event_type: "human_detection",
      event_state: "active",
      metadata: {},
    }],
    [{
      id: "linked-snapshot",
      timestamp: "2026-08-10T12:00:30Z",
      device_id: "camera",
      evidence_type: "snapshot",
      event_id: "linked-event",
      metadata: { origin: "ftp" },
    }],
  );

  assert.equal(result.entries.find(entry => entry.kind === "snapshot").relatedEvent.id, "linked-event");
});

test("carries an annotation through a continuous target snapshot sequence", () => {
  const eventTime = new Date("2026-08-10T12:09:58Z").getTime();
  const snapshots = [137, 2200, 3257, 4316, 5372].map((offset, index) => ({
    id: "sequence-" + index,
    timestamp: new Date(eventTime + offset).toISOString(),
    device_id: "camera",
    evidence_type: "snapshot",
    metadata: { origin: "ftp", event_type: "md_with_target" },
  }));
  const result = timeline.buildEpisodeTimeline(
    { start_time: "2026-08-10T12:09:58Z", end_time: "2026-08-10T12:10:10Z" },
    [{
      id: "human",
      timestamp: "2026-08-10T12:09:58Z",
      device_id: "camera",
      event_type: "human_detection",
      event_state: "active",
      metadata: { bounding_box: { x: 0.8, y: 0.2, width: 0.1, height: 0.3 } },
    }],
    snapshots,
  );

  assert.deepEqual(
    result.entries.filter(entry => entry.kind === "snapshot").map(entry => entry.relatedEvent?.id),
    ["human", "human", "human", "human", "human"],
  );
});

test("does not claim that an SDK unlock record opened the door", () => {
  assert.equal(timeline.eventTitle({
    event_type: "door_access",
    metadata: { sdk_event_name: "unlock_record", unlock_outcome: "not_reported_by_device" },
  }), "Door unlock record");
});
