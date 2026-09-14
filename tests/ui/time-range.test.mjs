import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

process.env.TZ = "Europe/Lisbon";

const source = await readFile(
  new URL("../../src/episode/ui/time-range.js", import.meta.url),
  "utf8",
);
const moduleUrl = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const { calendarTimeBounds } = await import(moduleUrl);

test("calendar bounds use local whole-day boundaries", () => {
  const now = new Date(2026, 8, 14, 12, 30);
  const bounds = calendarTimeBounds({ time_range: "today" }, now);

  assert.deepEqual(bounds, {
    from: new Date(2026, 8, 14).toISOString(),
    before: new Date(2026, 8, 15).toISOString(),
    valid: true,
  });

  const custom = calendarTimeBounds({
    time_range: "custom",
    custom_from: "2026-09-12",
    custom_to: "2026-09-14",
  }, now);
  assert.equal(custom.from, new Date(2026, 8, 12).toISOString());
  assert.equal(custom.before, new Date(2026, 8, 15).toISOString());
  assert.equal(custom.valid, true);
});

test("calendar bounds reject partial and reversed custom ranges", () => {
  assert.equal(calendarTimeBounds({ time_range: "custom", custom_from: "2026-09-14" }).valid, false);
  assert.equal(calendarTimeBounds({
    time_range: "custom",
    custom_from: "2026-09-15",
    custom_to: "2026-09-14",
  }).valid, false);
});

test("calendar bounds follow daylight-saving transitions", () => {
  const bounds = calendarTimeBounds(
    { time_range: "today" },
    new Date(2026, 2, 29, 12, 0),
  );
  const durationHours = (new Date(bounds.before) - new Date(bounds.from)) / 3_600_000;

  assert.equal(durationHours, 23);
});
