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
