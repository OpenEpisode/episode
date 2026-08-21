import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../../src/episode/ui/episode-list.js", import.meta.url),
  "utf8",
);
const moduleUrl = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const { episodeRailTime, groupEpisodesByTime } = await import(moduleUrl);

test("episodes are grouped into useful non-overlapping chronological periods", () => {
  const now = new Date("2026-08-20T12:00:00Z");
  const episodes = [
    { id: "today", state: "closed", start_time: "2026-08-20T10:00:00Z" },
    { id: "active", state: "active", start_time: "2026-08-20T09:00:00Z" },
    { id: "week", state: "closed", start_time: "2026-08-18T10:00:00Z" },
    { id: "last-week", state: "closed", start_time: "2026-08-12T10:00:00Z" },
    { id: "month", state: "closed", start_time: "2026-08-02T10:00:00Z" },
    { id: "july", state: "closed", start_time: "2026-07-02T10:00:00Z" },
    { id: "old", state: "closed", start_time: "2025-07-02T10:00:00Z" },
  ];

  const groups = groupEpisodesByTime(episodes, now);

  assert.deepEqual(groups.map(group => group.label), [
    "Happening now",
    "Today",
    "Earlier this week",
    "Last week",
    "Earlier this month",
    "July",
    "2025",
  ]);
  assert.equal(groups[0].active, true);
  assert.deepEqual(groups.flatMap(group => group.episodes.map(item => item.id)), [
    "active",
    "today",
    "week",
    "last-week",
    "month",
    "july",
    "old",
  ]);
});

test("timeline time formatting handles invalid values", () => {
  assert.equal(episodeRailTime("not-a-date"), "–");
  const afternoon = new Date(2026, 7, 20, 15, 1);
  const formatted = episodeRailTime(afternoon, new Date(2026, 7, 20, 18));
  assert.match(formatted, /15:01/);
  assert.doesNotMatch(formatted, /AM|PM/i);
});
