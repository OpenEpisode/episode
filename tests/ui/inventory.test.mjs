import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/inventory.js", import.meta.url),
  "utf8",
);
const inventoryPagesSource = await readFile(
  new URL("../../src/episode/ui/inventory-pages.js", import.meta.url),
  "utf8",
);
const inventoryCss = await readFile(
  new URL("../../src/episode/ui/inventory.css", import.meta.url),
  "utf8",
);
const episodeWorkspaceCss = await readFile(
  new URL("../../src/episode/ui/episode-workspace.css", import.meta.url),
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
    .replace('"./api.js"', JSON.stringify(apiUrl))
    .replace('"./dialogs.js"', JSON.stringify(dialogsUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./event-filter.js"', JSON.stringify(eventFilterModUrl))
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
  assert.match(dialog.content, /name="reolink_native_video"/);
  assert.doesNotMatch(dialog.content, /name="reolink_media_priming"/);
  assert.match(dialog.content, /name="reolink_preview_variant"/);
  assert.match(dialog.content, /name="reolink_preview_timeout"/);

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
    ["reolink_native_video", "on"],
    ["reolink_preview_variant", "sub"],
    ["reolink_preview_timeout", "4"],
  ]);
  await dialog.onSubmit(data);

  assert.deepEqual(globalThis.inventoryRequests[0].options.body.reolink, {
    enabled: true,
    host: "192.0.2.21",
    port: 9000,
    media_enabled: true,
    events_enabled: true,
    native_video: true,
    preview_variant: "sub",
    preview_timeout: 4,
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

test("HCNetSDK offers video for cameras and Events for Doorbells", () => {
  const camera = captureEditor({ device_type: "camera" });
  const doorbell = captureEditor({ device_type: "doorbell" });

  assert.match(camera.content, /Selectable HCNetSDK Main\/Sub video sources/);
  assert.match(camera.content, /Camera alarm events are not subscribed/);
  assert.match(doorbell.content, /Doorbell rings and unlock records/);
  assert.match(doorbell.content, /anti-tamper mapping is not yet device-tested/);
  assert.match(camera.content, /name="hikvision_sdk_enabled"\>/);
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
    device_types: ["camera", "doorbell"],
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
    ["onvif", "isapi", "hikvision_sdk"],
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

test("mobile inventory and episode layouts keep dense metadata readable", () => {
  assert.match(inventoryPagesSource, /resource-row recording-source-row/);
  assert.match(inventoryCss, /\.recording-source-row\s*\{[\s\S]*?grid-template-columns: minmax\(0, 1fr\);/);
  assert.match(inventoryCss, /\.validation-result \.validation-status\s*\{[\s\S]*?grid-column: 2;/);
  assert.match(inventoryCss, /\.dialog-footer \.button \{ min-height: 2\.75rem; width: 100%; \}/);
  assert.match(episodeWorkspaceCss, /\.episode-media-header \{ flex-direction: column; \}/);
});

test("configured mismatched integrations remain visible for diagnosis", () => {
  const keys = applicableValidationKeys({
    catalog: validationCatalog,
    configured: new Set(["reolink"]),
    deviceType: "camera",
    manufacturer: "Hikvision",
  });

  assert.deepEqual([...keys], ["onvif", "isapi", "hikvision_sdk", "reolink"]);
});

test("selected mismatched integrations remain visible while being validated", () => {
  const keys = applicableValidationKeys({
    catalog: validationCatalog,
    deviceType: "camera",
    manufacturer: "Hikvision",
    selected: new Set(["reolink"]),
  });

  assert.deepEqual([...keys], ["onvif", "isapi", "hikvision_sdk", "reolink"]);
});

test("Device editor lists discovered video sources and submits the pinned choice", async () => {
  globalThis.inventoryRequests = [];
  const dialog = captureEditor({
    id: "garage-camera",
    name: "Garage camera",
    device_type: "camera",
    area_id: "entrance",
    video_sources: [
      {
        id: "onvif:main",
        name: "Main profile",
        provider: "ONVIF",
        protocol: "rtsp",
        metadata_kind: "configured",
        width: 1920,
        height: 1080,
        frame_rate: 25,
        codec: "H264",
        modes: [],
        default: true,
      },
      {
        id: "reolink:native:sub",
        name: "Reolink native · Sub stream",
        provider: "Reolink",
        protocol: "Baichuan",
        metadata_kind: "capabilities",
        modes: [{ width: 640, height: 360, frame_rates: [15, 10], codec: "" }],
        default: false,
      },
      {
        id: "hikvision-sdk:main",
        name: "HCNetSDK · Main stream",
        provider: "Hikvision HCNetSDK",
        protocol: "HCNetSDK",
        metadata_kind: "capabilities",
        width: 2560,
        height: 1440,
        frame_rate: 25,
        codec: "h264",
        modes: [],
        default: false,
      },
    ],
    configuration: {
      video: { enabled: true, recording_source_id: "reolink:native:sub" },
    },
  });

  assert.match(dialog.content, /name="recording_source_id"/);
  assert.match(dialog.content, /Advertised capabilities; active camera settings may differ/);
  assert.match(dialog.content, /reolink:native:sub/);
  assert.match(dialog.content, /hikvision-sdk:main/);

  await dialog.onSubmit(new Map([
    ["name", "Garage camera"],
    ["device_type", "camera"],
    ["area_id", "entrance"],
    ["activity_window_seconds", "30"],
    ["video_enabled", "on"],
    ["recording_mode", "on_event"],
    ["recording_source_id", "reolink:native:sub"],
  ]));

  assert.equal(
    globalThis.inventoryRequests[0].options.body.video.recording_source_id,
    "reolink:native:sub",
  );
});
