import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const uiFile = name => readFile(
  new URL("../../src/episode/ui/" + name, import.meta.url),
  "utf8",
);

const domUrl = moduleUrl(await uiFile("dom.js"));
const formatUrl = moduleUrl(await uiFile("format.js"));
const timelineUrl = moduleUrl(await uiFile("timeline.js"));
const timeline = await import(timelineUrl);
const apiUrl = moduleUrl(
  (await uiFile("api.js")).replace('"./dom.js"', JSON.stringify(domUrl)),
);
const episodeViewUrl = moduleUrl(
  (await uiFile("episode-view.js"))
    .replace('"./api.js?v=2"', JSON.stringify(apiUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js?v=3"', JSON.stringify(formatUrl))
    .replace('"./timeline.js?v=4"', JSON.stringify(timelineUrl)),
);
const { renderEpisodeWorkspace } = await import(episodeViewUrl);

test("renders a media-first timeline with all Doorbell states and snapshots", () => {
  const episode = {
    id: "episode-1",
    start_time: "2026-08-10T12:08:47Z",
    last_event_time: "2026-08-10T12:09:11Z",
    end_time: "2026-08-10T12:09:43Z",
  };
  const events = [
    {
      id: "ring",
      timestamp: "2026-08-10T12:08:47Z",
      device_id: "doorbell",
      event_type: "doorbell",
      event_state: "active",
      sources: ["hikvision:sdk"],
      metadata: { phase: "ringing" },
    },
    {
      id: "dismissed",
      timestamp: "2026-08-10T12:09:03Z",
      device_id: "doorbell",
      event_type: "doorbell",
      event_state: "inactive",
      sources: ["hikvision:sdk"],
      metadata: { phase: "dismissed" },
    },
    {
      id: "unlock",
      timestamp: "2026-08-10T12:08:58Z",
      device_id: "doorbell",
      event_type: "door_access",
      event_state: "active",
      sources: ["hikvision:sdk"],
      metadata: {
        sdk_event_name: "unlock_record",
        lock_name: "Door1",
        unlock_method: "householder",
        unlock_outcome: "not_reported_by_device",
      },
    },
    {
      id: "human",
      timestamp: "2026-08-10T12:09:11Z",
      device_id: "camera",
      event_type: "human_detection",
      event_state: "active",
      sources: ["hikvision:isapi"],
      metadata: { bounding_box: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 } },
    },
  ];
  const evidence = [
    {
      id: "doorbell-recording",
      timestamp: "2026-08-10T12:08:47Z",
      device_id: "doorbell",
      evidence_type: "recording",
      metadata: { duration_seconds: 56 },
    },
    {
      id: "camera-recording",
      timestamp: "2026-08-10T12:08:47Z",
      device_id: "camera",
      evidence_type: "recording",
      metadata: { duration_seconds: 56 },
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
      timestamp: "2026-08-10T12:09:14Z",
      device_id: "camera",
      evidence_type: "snapshot",
      metadata: { origin: "ftp", event_type: "md_with_target" },
    },
  ];

  const { html, model } = renderEpisodeWorkspace(episode, events, evidence, []);

  assert.match(html, /episode-media-stage/);
  assert.match(html, /episode-timeline-rail/);
  assert.match(html, /Doorbell rang/);
  assert.match(html, /doorbell · 16s/);
  assert.match(html, /Human detected/);
  assert.match(html, /Doorbell call ended/);
  assert.match(html, /Door unlock record/);
  assert.match(html, /Door1 · Householder/);
  assert.match(html, /Lock: Door1/);
  assert.match(html, /Method: Householder/);
  assert.equal((html.match(/<strong>Snapshot<\/strong>/g) || []).length, 2);
  assert.match(html, /Linked to Human detected/);
  assert.match(html, /Uncorrelated evidence/);
  assert.match(html, /Detection overlay/);
  assert.equal(model.recordings.length, 2);
});

test("matches a video detection only near its timestamp and on the same Device", () => {
  const entries = [
    {
      id: "camera-detection",
      kind: "event",
      start: 10000,
      deviceId: "camera",
      event: { metadata: { bounding_box: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 } } },
    },
    {
      id: "other-detection",
      kind: "event",
      start: 11000,
      deviceId: "other-camera",
      event: { metadata: { bounding_box: { x: 0.2, y: 0.2, width: 0.2, height: 0.2 } } },
    },
  ];

  const tracks = timeline.buildDetectionTracks(entries);
  assert.equal(timeline.detectionForMoment(tracks, "camera", 11500)?.id, "camera-detection");
  assert.equal(timeline.detectionForMoment(tracks, "camera", 13000), null);
  assert.equal(timeline.detectionForMoment(tracks, "missing-camera", 11000), null);
});

test("keeps a video detection alive through related snapshot observations", () => {
  const event = {
    id: "camera-detection",
    kind: "event",
    start: 10000,
    deviceId: "camera",
    event: {
      id: "event",
      event_type: "human_detection",
      event_state: "active",
      metadata: { bounding_box: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 } },
    },
  };
  const entries = [event, 12100, 14200].map((item, index) => index === 0 ? item : ({
    id: "snapshot-" + index,
    kind: "snapshot",
    start: item,
    deviceId: "camera",
    item: { event_id: null, metadata: { event_type: "md_with_target" } },
    relatedEvent: event.event,
  }));

  const tracks = timeline.buildDetectionTracks(entries);
  assert.equal(timeline.detectionForMoment(tracks, "camera", 15000)?.id, "camera-detection");
  assert.equal(timeline.detectionForMoment(tracks, "camera", 17000), null);
});
