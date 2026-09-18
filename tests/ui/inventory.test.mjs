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
const {
  applicableValidationKeys,
  openDeviceEditor,
  preferredManufacturer,
} = await import(inventoryUrl);

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
  assert.equal(globalThis.inventoryRequests[0].options.body.setup_state, "ready");
});

test("Save for later marks a Device as setup-incomplete", async () => {
  globalThis.inventoryRequests = [];
  const dialog = captureEditor();
  const data = new Map([
    ["name", "Unidentified sensor"],
    ["device_type", "sensor"],
    ["area_id", "entrance"],
    ["activity_window_seconds", "30"],
    ["save_for_later", "on"],
  ]);
  await dialog.onSubmit(data);

  assert.equal(globalThis.inventoryRequests[0].options.body.setup_state, "needs_setup");
  assert.match(dialog.content, /Save for later/);
  assert.match(dialog.content, /will not participate in new activity/);
});

test("discovered identity is not persisted as a manual override unless selected", async () => {
  globalThis.inventoryRequests = [];
  const dialog = captureEditor({
    id: "camera",
    name: "Camera",
    device_type: "camera",
    area_id: "entrance",
    enabled: true,
    identity: { manufacturer: "Hikvision Digital Technology" },
    configuration: { setup_state: "ready", video: {}, onvif: {} },
  });
  const data = new Map([
    ["name", "Camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["activity_window_seconds", "30"],
    ["manufacturer", ""],
    ["manufacturer_explicit", "false"],
  ]);
  await dialog.onSubmit(data);
  assert.equal(Object.hasOwn(globalThis.inventoryRequests[0].options.body, "manufacturer"), false);

  globalThis.inventoryRequests = [];
  const cleared = captureEditor({
    id: "camera",
    name: "Camera",
    device_type: "camera",
    area_id: "entrance",
    enabled: true,
    identity: { manufacturer: "Hikvision" },
    configuration: { manufacturer: "Hikvision", setup_state: "ready", video: {}, onvif: {} },
  });
  await cleared.onSubmit(new Map([
    ["name", "Camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["activity_window_seconds", "30"],
    ["manufacturer", ""],
    ["manufacturer_explicit", "true"],
  ]));
  assert.equal(globalThis.inventoryRequests[0].options.body.manufacturer, null);
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

test("HCNetSDK is presented as catalogue-gated vendor integration", () => {
  const dialog = captureEditor();

  assert.match(dialog.content, /anti-tamper mapping is not yet device-tested/);
  assert.match(dialog.content, /Available for Doorbell Devices/);
  assert.doesNotMatch(dialog.content, /experimental camera callbacks/i);
  assert.doesNotMatch(dialog.content, /Camera mode is diagnostic-only/);
  assert.match(dialog.content, /name="hikvision_sdk_enabled"\>/);
  assert.match(source, /devices\/integrations\/catalog/);
  assert.match(source, /catalogEntryMatches/);
});

test("onboarding exposes scoped discovery and a credential-safe video test", () => {
  const dialog = captureEditor();
  assert.match(dialog.content, /Discover with ONVIF/);
  assert.doesNotMatch(dialog.content, /name="onvif_enabled" checked/);
  assert.doesNotMatch(dialog.content, /name="video_enabled" checked/);
  assert.match(dialog.content, /data-test-video/);
  assert.match(dialog.content, /manual stream provides recording only/);
  assert.match(dialog.content, /data-manual-manufacturer/);
  assert.match(dialog.content, /data-integration-id="hikvision-isapi"/);
  assert.match(dialog.content, /data-integration-id="reolink"/);
  assert.match(source, /devices\/integrations\/catalog/);
  assert.doesNotMatch(source, /const integrationDefinitions/);
  assert.match(source, /payload\.integration_ids/);
  assert.match(source, /validationStatuses\.has\(result\.status\)/);
  assert.match(source, /validationStatuses\.has\(response\.status\)/);
});

test("manual unknown choice clears stale manufacturer recommendations", () => {
  const failed = { status: "unreachable", details: { manufacturer: "Hikvision" } };
  assert.equal(preferredManufacturer(failed, "", true, "Hikvision", "Hikvision"), "");
  assert.equal(preferredManufacturer(failed, "Reolink", true, "Hikvision", "Hikvision"), "Reolink");
  const discovered = { status: "supported", details: { manufacturer: "Reolink" } };
  assert.equal(preferredManufacturer(discovered, "", true, "Hikvision", "Hikvision"), "Reolink");
});

const validationCatalog = [
  {
    id: "onvif",
    type: "onvif",
    available: true,
    manufacturer_scope_kind: "universal",
    device_types: ["camera", "doorbell", "sensor"],
  },
  {
    id: "hikvision-isapi",
    type: "isapi",
    available: true,
    manufacturer_scope_kind: "targeted",
    manufacturer_scope: ["hikvision"],
    device_types: ["camera", "doorbell"],
  },
  {
    id: "hikvision-sdk",
    type: "hikvision_sdk",
    available: true,
    manufacturer_scope_kind: "targeted",
    manufacturer_scope: ["hikvision"],
    device_types: ["doorbell"],
  },
  {
    id: "reolink",
    type: "reolink",
    available: true,
    manufacturer_scope_kind: "targeted",
    manufacturer_scope: ["reolink"],
    device_types: ["camera", "doorbell"],
  },
];

test("validation results follow current manufacturer and device type", () => {
  assert.deepEqual(
    [...applicableValidationKeys({
      catalog: validationCatalog,
      deviceType: "camera",
      manufacturer: "Hikvision Digital Technology",
    })],
    ["onvif", "isapi"],
  );
});

test("the Device editor hides stale validation from unrelated integrations", () => {
  const dialog = captureEditor({
    id: "garage",
    name: "Garage camera",
    device_type: "camera",
    area_id: "entrance",
    identity: { manufacturer: "Hikvision" },
    configuration: {
      onvif: { enabled: true },
      isapi: { enabled: true },
      hikvision_sdk: { enabled: false },
      reolink: { enabled: false },
    },
    integration_support: {
      onvif: { status: "supported", summary: "ONVIF works" },
      isapi: { status: "supported", summary: "ISAPI works" },
      hikvision_sdk: { status: "unavailable", summary: "SDK configured but unavailable" },
      reolink: { status: "authentication_failed", summary: "Authentication Failed" },
    },
  });
  const validation = dialog.content.split('class="validation-results" data-validation-results>')[1]
    .split('class="validation-empty')[0];

  assert.match(validation, /ONVIF works/);
  assert.match(validation, /ISAPI works/);
  assert.doesNotMatch(validation, /SDK configured but unavailable/);
  assert.doesNotMatch(validation, /Authentication Failed/);
});

test("configured mismatched integrations remain visible for diagnosis", () => {
  const keys = applicableValidationKeys({
    catalog: validationCatalog,
    configured: new Set(["reolink"]),
    deviceType: "camera",
    manufacturer: "Hikvision",
  });

  assert.deepEqual([...keys], ["onvif", "isapi", "reolink"]);
});

test("selected mismatched integrations remain visible while being validated", () => {
  const keys = applicableValidationKeys({
    catalog: validationCatalog,
    deviceType: "camera",
    manufacturer: "Hikvision",
    selected: new Set(["reolink"]),
  });

  assert.deepEqual([...keys], ["onvif", "isapi", "reolink"]);
});
