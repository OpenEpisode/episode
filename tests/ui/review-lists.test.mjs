import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../../src/episode/ui/review-lists.js", import.meta.url),
  "utf8",
);
const moduleUrl = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const {
  groupActivityByDay,
  groupEvidenceBundlesByDay,
  groupEvidenceByEpisode,
} = await import(moduleUrl);

test("activity is grouped into chronological day sections", () => {
  const groups = groupActivityByDay([
    { id: "a", timestamp: "2026-08-21T10:00:00" },
    { id: "b", timestamp: "2026-08-21T09:00:00" },
    { id: "c", timestamp: "2026-08-20T20:00:00" },
  ], new Date("2026-08-21T12:00:00"));

  assert.deepEqual(groups.map(group => group.label), ["Today", "Yesterday"]);
  assert.deepEqual(groups.map(group => group.events.map(event => event.id)), [["a", "b"], ["c"]]);
});

test("unassigned evidence is surfaced before Episode groups", () => {
  const groups = groupEvidenceByEpisode([
    { id: "episode-a", episode_id: "episode-1" },
    { id: "orphan", episode_id: null },
    { id: "episode-b", episode_id: "episode-1" },
    { id: "episode-c", episode_id: "episode-2" },
  ]);

  assert.equal(groups[0].attention, true);
  assert.deepEqual(groups[0].evidence.map(item => item.id), ["orphan"]);
  assert.deepEqual(groups.slice(1).map(group => group.episodeId), ["episode-1", "episode-2"]);
  assert.deepEqual(groups[1].evidence.map(item => item.id), ["episode-a", "episode-b"]);
  assert.equal(groups[1].deviceCount, 0);
});

test("Evidence bundles summarize their Devices and capture range", () => {
  const [group] = groupEvidenceByEpisode([
    { episode_id: "episode", device_id: "camera-a", timestamp: "2026-08-20T23:08:42Z" },
    { episode_id: "episode", device_id: "camera-b", timestamp: "2026-08-20T23:09:12Z" },
    { episode_id: "episode", device_id: "camera-a", timestamp: "2026-08-20T23:08:52Z" },
  ]);

  assert.equal(group.deviceCount, 2);
  assert.equal(group.firstCaptureAt, "2026-08-20T23:08:42.000Z");
  assert.equal(group.lastCaptureAt, "2026-08-20T23:09:12.000Z");
});

test("Evidence bundles form newest-first timeline periods", () => {
  const periods = groupEvidenceBundlesByDay([
    { episodeId: "older", firstCaptureAt: "2026-08-20T20:00:00" },
    { episodeId: "newer", firstCaptureAt: "2026-08-21T10:00:00" },
    { episodeId: "same-day", firstCaptureAt: "2026-08-21T09:00:00" },
  ], new Date("2026-08-21T12:00:00"));

  assert.deepEqual(periods.map(period => period.label), ["Today", "Yesterday"]);
  assert.deepEqual(
    periods.map(period => period.bundles.map(bundle => bundle.episodeId)),
    [["newer", "same-day"], ["older"]],
  );
});
