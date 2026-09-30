import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

process.env.TZ = "Europe/Lisbon";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/activity-page.js", import.meta.url),
  "utf8",
);
const timeRangeSource = await readFile(
  new URL("../../src/episode/ui/time-range.js", import.meta.url),
  "utf8",
);

const apiUrl = moduleUrl(`
  export async function api(path) {
    globalThis.activityApiCalls.push(path);
    if (globalThis.activityApiError && path.startsWith("/events?")) throw new Error(globalThis.activityApiError);
    return globalThis.activityApiResponses[path] ?? [];
  }
`);
const componentsUrl = moduleUrl(`
  export function eventSourceBadges(event) { return "<span class=source>" + (event.source || "source") + "</span>"; }
  export function pageControls(base, page, count, hasNext) { return "<nav data-page=\\"" + page + "\\" data-next=\\"" + hasNext + "\\" data-base=\\"" + base + "\\"></nav>"; }
  export function pageHeader() { return "<header>Activity</header>"; }
`);
const domUrl = moduleUrl(`
  export function escHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }
`);
const participationUrl = moduleUrl(`
  export function eventParticipationBadge(participation) {
    return participation?.allowed === false ? "<span class=participation>Filtered</span>" : "";
  }
`);
const filtersUrl = moduleUrl(`
  export function filterValues(items, field, defaults, selected = "") {
    return [...new Set([...defaults, ...items.map(item => item[field]).filter(Boolean), ...(selected ? [selected] : [])])].sort();
  }
  export function option(value, label, selected) {
    return "<option value=\\"" + value + "\\"" + (value === selected ? " selected" : "") + ">" + label + "</option>";
  }
  export function filteredHash(view, filters) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value);
    return "#" + view + (query.size ? "?" + query : "");
  }
`);
const formatUrl = moduleUrl(`
  export function fmtTime(value) { return "time:" + value; }
  export function plural(value, label) { return value + " " + label; }
  export function titleCase(value) { return String(value ?? "").replaceAll("_", " ").replace(/\\b\\w/g, letter => letter.toUpperCase()); }
`);
const reviewListsUrl = moduleUrl(`
  export function groupActivityByDay(events) { return events.length ? [{ label: "Today", events }] : []; }
`);
const timeRangeUrl = moduleUrl(timeRangeSource);
const viewUrl = moduleUrl(`
  export function showContent(html) { globalThis.activityHtml = html; }
  export function showError(message) { globalThis.activityError = message; }
  export function showLoading() { globalThis.activityLoading = (globalThis.activityLoading || 0) + 1; }
`);
const timelineUrl = moduleUrl(`
  export function eventTitle(event) { return event.title || event.event_type || "Event"; }
`);

const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=6"', JSON.stringify(componentsUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./review-participation.js?v=1"', JSON.stringify(participationUrl))
    .replace('"./review-filters.js?v=1"', JSON.stringify(filtersUrl))
    .replace('"./format.js?v=3"', JSON.stringify(formatUrl))
    .replace('"./review-lists.js?v=3"', JSON.stringify(reviewListsUrl))
    .replace('"./time-range.js?v=1"', JSON.stringify(timeRangeUrl))
    .replace('"./view.js?v=1"', JSON.stringify(viewUrl))
    .replace('"./timeline.js?v=6"', JSON.stringify(timelineUrl)),
));

function reset(responses = {}) {
  globalThis.activityApiCalls = [];
  globalThis.activityApiResponses = responses;
  globalThis.activityApiError = "";
  globalThis.activityHtml = "";
  globalThis.activityError = "";
}

const devices = [{ id: "front", name: "Front Door" }];
const areas = [{ id: "yard", name: "Yard" }];
const event = {
  id: "event-1",
  timestamp: "2026-09-28T20:00:00Z",
  event_type: "motion_detection",
  event_state: "active",
  device_id: "front",
  area_id: "yard",
  episode_id: "episode-1",
  source: "camera",
  title: "Motion detected",
  participation: { allowed: false },
};

test("Activity builds filtered local-time and association query parameters", async () => {
  reset({
    "/devices?include_disabled=true": devices,
    "/areas?include_disabled=true": areas,
    "/events?limit=101&offset=0&device_id=front&area_id=yard&event_type=motion_detection&event_state=active&observed_from=2026-09-11T23%3A00%3A00.000Z&observed_before=2026-09-14T23%3A00%3A00.000Z&has_episode=false": [],
  });
  await module.activity("", 1, new URLSearchParams({
    device_id: "front",
    area_id: "yard",
    event_type: "motion_detection",
    event_state: "active",
    association: "unassigned",
    time_range: "custom",
    custom_from: "2026-09-12",
    custom_to: "2026-09-14",
  }));

  const request = globalThis.activityApiCalls.find(path => path.startsWith("/events?"));
  assert.ok(request);
  const query = new URLSearchParams(request.split("?", 2)[1]);
  assert.equal(query.get("device_id"), "front");
  assert.equal(query.get("area_id"), "yard");
  assert.equal(query.get("event_type"), "motion_detection");
  assert.equal(query.get("event_state"), "active");
  assert.equal(query.get("has_episode"), "false");
  assert.match(query.get("observed_from"), /Z$/);
  assert.match(query.get("observed_before"), /Z$/);
  assert.match(globalThis.activityHtml, /name="custom_from" value="2026-09-12"/);
  assert.match(globalThis.activityHtml, /name="custom_to" value="2026-09-14"/);
  assert.doesNotMatch(globalThis.activityHtml, /Choose a valid start/);
});

test("Activity renders provenance, links, participation, and pagination", async () => {
  reset({
    "/devices?include_disabled=true": devices,
    "/areas?include_disabled=true": areas,
    "/events?limit=101&offset=100": Array.from({ length: 101 }, (_, index) => ({
      ...event,
      id: `event-${index}`,
    })),
  });
  await module.activity("", 2);

  assert.match(globalThis.activityHtml, /Motion detected/);
  assert.match(globalThis.activityHtml, /href="#event\/event-0"/);
  assert.match(globalThis.activityHtml, /href="#episode\/episode-1"/);
  assert.match(globalThis.activityHtml, /class=source/);
  assert.match(globalThis.activityHtml, /class=participation/);
  assert.match(globalThis.activityHtml, /data-page="2"/);
  assert.match(globalThis.activityHtml, /data-next="true"/);
});

test("Activity renders an explicit empty state", async () => {
  reset({
    "/devices?include_disabled=true": devices,
    "/areas?include_disabled=true": areas,
    "/events?limit=101&offset=0": [],
  });
  await module.activity("", 1);

  assert.match(globalThis.activityHtml, /No matching activity/);
  assert.match(globalThis.activityHtml, /Try changing the filters/);
});

test("Activity exposes API errors through the shared error view", async () => {
  reset({
    "/devices?include_disabled=true": devices,
    "/areas?include_disabled=true": areas,
  });
  globalThis.activityApiError = "Events unavailable";
  await module.activity("", 1);

  assert.equal(globalThis.activityError, "Events unavailable");
});
