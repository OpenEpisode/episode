import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/inventory.js", import.meta.url),
  "utf8",
);

const apiUrl = moduleUrl(`
  export async function apiRequest(path, options) {
    globalThis.inventoryRequests.push({ path, options });
  }
`);
const dialogsUrl = moduleUrl(`
  export function openDialog(config) {
    globalThis.inventoryDialog = config;
    throw new Error("dialog captured");
  }
  export function closeDialog() {}
  export async function confirmDialog() { return true; }
  export function notify() {}
`);
const domUrl = moduleUrl(`
  export function escHtml(value) { return String(value ?? ""); }
`);
const formatUrl = moduleUrl(`
  export function titleCase(value) { return String(value ?? ""); }
`);
// Load the real shared vocabulary module so the Device editor cannot offer a
// class the API would reject.
const eventFilterSource = await readFile(
  new URL("../../src/episode/ui/event-filter.js", import.meta.url),
  "utf8",
);
const eventFilterModUrl = moduleUrl(eventFilterSource.replace('"./dom.js"', JSON.stringify(domUrl)));

globalThis.document = {
  createElement() {
    return {
      value: "",
      set innerHTML(value) { this.value = String(value); },
    };
  },
};
globalThis.inventoryRequests = [];

const inventoryUrl = moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./dialogs.js?v=1"', JSON.stringify(dialogsUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./event-filter.js?v=1"', JSON.stringify(eventFilterModUrl))
    .replace('"./format.js"', JSON.stringify(formatUrl)),
);
const { openDeviceEditor } = await import(inventoryUrl);

function captureEditor(device = null) {
  assert.throws(
    () => openDeviceEditor(device, [{ id: "entrance", name: "Entrance" }], async () => {}),
    /dialog captured/,
  );
  return globalThis.inventoryDialog;
}

test("ONVIF malformed XML recovery is explicit and included in onboarding validation", async () => {
  const dialog = captureEditor();

  assert.match(dialog.content, /name="onvif_relaxed_xml"/);
  assert.doesNotMatch(dialog.content, /name="onvif_relaxed_xml" checked/);
  assert.match(dialog.content, /Tolerate malformed SOAP XML/);

  const data = new Map([
    ["name", "Front camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["ip_address", "192.0.2.10"],
    ["username", "viewer"],
    ["password", "secret"],
    ["activity_window_seconds", "30"],
    ["video_enabled", "on"],
    ["video_protocol", "rtsp"],
    ["video_port", "554"],
    ["video_path", "/stream"],
    ["recording_mode", "on_event"],
    ["onvif_enabled", "on"],
    ["onvif_protocol", "http"],
    ["onvif_port", "80"],
    ["onvif_path", "/onvif/device_service"],
    ["onvif_auth_mode", "digest_wsse"],
    ["onvif_relaxed_xml", "on"],
  ]);
  await dialog.onSubmit(data);

  assert.equal(globalThis.inventoryRequests.length, 1);
  assert.equal(globalThis.inventoryRequests[0].options.body.onvif.relaxed_xml, true);
});

test("the Device event filter defaults to inherit and keeps the profile deciding", async () => {
  globalThis.inventoryRequests = [];
  const dialog = captureEditor();

  assert.match(dialog.content, /name="event_filter"/);
  assert.match(dialog.content, /option value="inherit" selected/);
  assert.match(dialog.content, /Follows the active Capture profile/);
  // The camera wins over a profile, which is what makes an empty selection useful.
  assert.match(dialog.content, /This camera’s own setting wins over the active Capture profile/);
  // The class list is the only control: no separate confirmation tick.
  assert.doesNotMatch(dialog.content, /_security_ack/);

  await dialog.onSubmit(new Map([
    ["name", "Front camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["activity_window_seconds", "30"],
    ["event_filter", "inherit"],
  ]));
  assert.equal(globalThis.inventoryRequests[0].options.body.episode_policy.event_filter, null);
});

test("an explicit Device selection is submitted and never inherits silently", async () => {
  globalThis.inventoryRequests = [];
  const dialog = captureEditor({
    id: "attic",
    configuration: { episode_policy: { event_filter: ["heartbeat", "motion"] } },
  });
  assert.match(dialog.content, /option value="motion-status" selected/);
  assert.match(dialog.content, /Filters: Motion, Status/);

  await dialog.onSubmit(new Map([
    ["name", "Attic camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["activity_window_seconds", "30"],
    // The operator opts this camera out of a filtering profile entirely.
    ["event_filter", ""],
  ]));
  assert.deepEqual(globalThis.inventoryRequests[0].options.body.episode_policy.event_filter, []);

  // A custom selection submits the ticked classes only.
  globalThis.inventoryRequests = [];
  await dialog.onSubmit(new Map([
    ["name", "Attic camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["activity_window_seconds", "30"],
    ["event_filter", "custom"],
    ["event_filter_class", "heartbeat"],
  ]));
  assert.deepEqual(globalThis.inventoryRequests[0].options.body.episode_policy.event_filter, [
    "heartbeat",
  ]);
});

test("a security class selection is submitted on its own, with no second tick", async () => {
  globalThis.inventoryRequests = [];
  const dialog = captureEditor();
  await dialog.onSubmit(
    new Map([
      ["name", "Attic camera"],
      ["device_type", "camera"],
      ["area_id", "entrance"],
      ["activity_window_seconds", "30"],
      ["event_filter", "custom"],
      ["event_filter_class", "security"],
    ]),
  );
  assert.deepEqual(globalThis.inventoryRequests[0].options.body.episode_policy.event_filter, [
    "security",
  ]);
});

test("Reolink settings are explicit and included in the Device payload", async () => {
  globalThis.inventoryRequests = [];
  const dialog = captureEditor();

  assert.match(dialog.content, /name="reolink_enabled"/);
  assert.match(dialog.content, /name="reolink_media_enabled"/);
  assert.match(dialog.content, /name="reolink_events_enabled"/);

  const data = new Map([
    ["name", "Driveway camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["ip_address", "192.0.2.20"],
    ["username", "viewer"],
    ["password", "secret"],
    ["activity_window_seconds", "30"],
    ["recording_mode", "on_event"],
    ["reolink_enabled", "on"],
    ["reolink_host", "192.0.2.21"],
    ["reolink_port", "9000"],
    ["reolink_media_enabled", "on"],
    ["reolink_events_enabled", "on"],
  ]);
  await dialog.onSubmit(data);

  assert.deepEqual(globalThis.inventoryRequests[0].options.body.reolink, {
    enabled: true,
    host: "192.0.2.21",
    port: 9000,
    media_enabled: true,
    events_enabled: true,
  });
});

test("Ignored Events starts empty because it stops interpretation, not capture", async () => {
  globalThis.inventoryRequests = [];
  const dialog = captureEditor();

  // A new camera interprets everything: the field exists but nothing is preset,
  // so a video-loss stream is never silently dropped before it becomes an Event.
  assert.match(dialog.content, /name="isapi_ignore_events"/);
  assert.doesNotMatch(dialog.content, /videoloss/);

  const base = new Map([
    ["name", "Gate camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["ip_address", "192.0.2.20"],
    ["username", "viewer"],
    ["password", "secret"],
  ]);
  await dialog.onSubmit(base);
  assert.deepEqual(globalThis.inventoryRequests[0].options.body.isapi.ignore_events, []);

  // The lever remains available to an operator who needs it.
  base.set("isapi_enabled", "on");
  base.set("isapi_ignore_events", "videoloss, illaccess ");
  await dialog.onSubmit(base);
  assert.deepEqual(globalThis.inventoryRequests[1].options.body.isapi.ignore_events, [
    "videoloss",
    "illaccess",
  ]);
});
