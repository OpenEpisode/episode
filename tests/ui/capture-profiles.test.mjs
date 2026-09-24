import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/capture-profiles.js", import.meta.url),
  "utf8",
);

const apiUrl = moduleUrl(`
  export async function api(path) {
    if (globalThis.captureApiError) throw globalThis.captureApiError;
    return globalThis.captureResponses[path];
  }
  export async function apiRequest(path, options) {
    globalThis.captureRequests.push({ path, options });
    if (globalThis.captureRequestError) throw globalThis.captureRequestError;
    if (path === "/capture-profiles/active" && options.method === "PUT") {
      const profile = globalThis.captureResponses["/capture-profiles"].find(item => item.id === options.body.profile_id);
      globalThis.captureResponses["/capture-profiles/active"].profile = { ...profile, active: true };
    }
  }
`);
const componentsUrl = moduleUrl(`
  export function pageHeader(value) { return "<header><h2>" + value.title + "</h2>" + value.actions + "</header>"; }
`);
const dialogsUrl = moduleUrl(`
  export function openDialog(config) { globalThis.captureDialog = config; }
  export function closeDialog() { globalThis.captureDialogClosed = true; }
  export function confirmDialog(config) { globalThis.captureConfirmation = config; }
  export function notify(message, tone) { globalThis.captureNotification = { message, tone }; }
`);
const domUrl = moduleUrl(`export function escHtml(value) { return String(value ?? ""); }`);
const formatUrl = moduleUrl(`
  export function fmtShort(value) { return String(value ?? ""); }
  export function plural(value, label) { return value + " " + label + (value === 1 ? "" : "s"); }
`);
const viewUrl = moduleUrl(`
  export function showLoading() {}
  export function showError(error) { globalThis.captureError = error; }
  export function showContent(html) { globalThis.captureHtml = html; }
`);
// Load the real shared vocabulary module so the editor cannot drift from the
// classes and presets the API accepts.
const eventFilterSource = await readFile(
  new URL("../../src/episode/ui/event-filter.js", import.meta.url),
  "utf8",
);
const eventFilterModUrl = moduleUrl(eventFilterSource.replace('"./dom.js"', JSON.stringify(domUrl)));

globalThis.window = {};
globalThis.captureActionButtons = [];
globalThis.document = {
  getElementById(id) {
    if (id === "capture-profile-status") return globalThis.captureStatus;
    if (id === "mobile-capture-profile-status") return globalThis.mobileCaptureStatus;
    return null;
  },
  querySelectorAll(selector) {
    return selector === "[data-capture-profile-action]" ? globalThis.captureActionButtons : [];
  },
};
function statusHost(id) {
  return {
    id,
    className: "capture-profile-status hidden",
    innerHTML: "",
    setAttribute(name, value) { this[name] = value; },
  };
}
globalThis.captureStatus = statusHost("capture-profile-status");
globalThis.mobileCaptureStatus = statusHost("mobile-capture-profile-status");
globalThis.captureResponses = {
  "/capture-profiles": [
    { id: "all", name: "All Devices", include_all_devices: true, device_ids: [], builtin: true, active: true, event_filter: [] },
    { id: "night", name: "Night", include_all_devices: false, device_ids: ["front"], builtin: false, active: false, event_filter: ["motion", "heartbeat"] },
  ],
  "/capture-profiles/active": {
    profile: { id: "all", name: "All Devices", include_all_devices: true, device_ids: [], builtin: true, active: true, event_filter: [] },
    recent_changes: [{ previous_profile_id: "night", previous_profile_name: "Night", new_profile_id: "all", new_profile_name: "All Devices", changed_at: "2026-09-09T10:00:00Z", source: "operator" }],
  },
  "/devices?include_disabled=true": [
    { id: "front", name: "Front camera", area_id: "entrance", device_type: "camera", enabled: true },
    { id: "garage", name: "Garage camera", area_id: "garage", device_type: "camera", enabled: true },
  ],
  "/areas?include_disabled=true": [
    { id: "entrance", name: "Entrance" },
    { id: "garage", name: "Garage" },
  ],
};
globalThis.captureRequests = [];
globalThis.captureRequestError = null;
globalThis.captureApiError = null;

const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=4"', JSON.stringify(componentsUrl))
    .replace('"./dialogs.js?v=1"', JSON.stringify(dialogsUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./event-filter.js?v=3"', JSON.stringify(eventFilterModUrl))
    .replace('"./format.js?v=3"', JSON.stringify(formatUrl))
    .replace('"./view.js?v=1"', JSON.stringify(viewUrl)),
));

test("Capture profiles render active state, immutable All Devices, grouped labels, and history", async () => {
  let createClick;
  globalThis.captureActionButtons = [{
    addEventListener(type, handler) {
      if (type === "click") createClick = handler;
    },
    getAttribute(name) {
      return name === "data-capture-profile-action" ? "create" : null;
    },
  }];
  await module.captureProfiles();

  assert.match(globalThis.captureHtml, /Capture profiles/);
  assert.match(globalThis.captureHtml, /All current and future enabled Devices participate/);
  assert.match(globalThis.captureHtml, /Built-in · cannot edit or delete/);
  assert.match(globalThis.captureHtml, /Night/);
  assert.match(globalThis.captureHtml, /href="#system\/capture-profiles"/);
  assert.match(globalThis.captureHtml, /All Devices/);
  assert.match(globalThis.captureHtml, /Night <span aria-hidden="true">→<\/span> All Devices/);
  assert.match(globalThis.captureHtml, /operator/);
  assert.doesNotMatch(globalThis.captureHtml, /onclick=.*(?:edit|delete).*all/);

  assert.equal(typeof createClick, "function");
  createClick();
  assert.match(globalThis.captureDialog.content, /name="name"/);
  assert.match(globalThis.captureDialog.content, /<legend>Entrance<\/legend>/);
  assert.match(globalThis.captureDialog.content, /for="capture-profile-device-front"/);
  assert.match(globalThis.captureDialog.content, /Leave every box clear for a Disarmed profile/);

  await globalThis.captureDialog.onSubmit(new Map([
    ["name", "Disarmed"],
  ]));
  assert.deepEqual(globalThis.captureRequests.at(-1), {
    path: "/capture-profiles",
    options: { method: "POST", body: { name: "Disarmed", device_ids: [] } },
  });

  globalThis.window.editCaptureProfile("night");
  await globalThis.captureDialog.onSubmit({
    get(name) { return name === "name" ? "Night watch" : null; },
    getAll(name) { return name === "device_id" ? ["front", "garage"] : []; },
  });
  assert.deepEqual(globalThis.captureRequests.at(-1), {
    path: "/capture-profiles/night",
    options: { method: "PUT", body: { name: "Night watch", device_ids: ["front", "garage"] } },
  });

  globalThis.window.deleteCaptureProfile("night");
  await globalThis.captureConfirmation.onConfirm();
  assert.deepEqual(globalThis.captureRequests.at(-1), {
    path: "/capture-profiles/night",
    options: { method: "DELETE" },
  });
});

test("event-class selector renders presets and submits the chosen classes", async () => {
  globalThis.captureRequests = [];
  globalThis.captureResponses["/capture-profiles"].push({
    id: "night-filtered",
    name: "Night filtered",
    include_all_devices: false,
    device_ids: ["front"],
    event_filter: ["heartbeat", "motion"],
    builtin: false,
    active: false,
  });
  await module.captureProfiles();

  // The row states the selector so an operator sees what a profile suppresses
  // before opening it.
  assert.match(globalThis.captureHtml, /Night filtered[\s\S]*· filtering motion, heartbeat/);

  globalThis.window.editCaptureProfile("night-filtered");
  const content = globalThis.captureDialog.content;
  assert.match(content, /name="event_filter"/);
  assert.match(content, /option value="motion-status" selected/);
  assert.match(content, /Filters: Motion, Status/);
  // A profile offers every class, and says what suppressing one does.
  assert.match(content, /value="security"/);
  assert.match(content, /never starts an Episode and never extends one/);

  await globalThis.captureDialog.onSubmit({
    get(name) {
      if (name === "name") return "Night filtered";
      if (name === "event_filter") return "motion-status-audio";
      return null;
    },
    getAll(name) {
      if (name === "device_id") return ["front"];
      return [];
    },
  });
  assert.deepEqual(globalThis.captureRequests.at(-1).options.body, {
    name: "Night filtered",
    device_ids: ["front"],
    // The payload is the canonical sorted form, not the preset's reading order.
    event_filter: ["condition", "heartbeat", "motion"],
  });

  // Custom… submits exactly the ticked classes.
  globalThis.window.editCaptureProfile("night-filtered");
  await globalThis.captureDialog.onSubmit({
    get(name) {
      if (name === "name") return "Night filtered";
      if (name === "event_filter") return "custom";
      return null;
    },
    getAll(name) {
      if (name === "device_id") return ["front"];
      if (name === "event_filter_class") return ["motion", "motion", "heartbeat"];
      return [];
    },
  });
  assert.deepEqual(globalThis.captureRequests.at(-1).options.body.event_filter, [
    "heartbeat",
    "motion",
  ]);

  // An unrecognised value submits nothing rather than a guessed selector.
  globalThis.window.editCaptureProfile("night");
  await globalThis.captureDialog.onSubmit({
    get(name) {
      if (name === "name") return "Night";
      if (name === "event_filter") return "not-a-preset";
      return null;
    },
    getAll(name) {
      return name === "device_id" ? ["front"] : [];
    },
  });
  assert.deepEqual(globalThis.captureRequests.at(-1).options.body, {
    name: "Night",
    device_ids: ["front"],
  });
});

test("Activating a restricted profile requires confirmation and preserves existing recordings", async () => {
  globalThis.captureRequests = [];
  globalThis.captureConfirmation = null;
  globalThis.window.activateCaptureProfile("night");

  assert.equal(globalThis.captureRequests.length, 0);
  assert.match(globalThis.captureConfirmation.title, /Activate Night/);
  assert.match(globalThis.captureConfirmation.message, /Existing recordings continue until their Episode closes/);

  await globalThis.captureConfirmation.onConfirm();
  assert.deepEqual(globalThis.captureRequests[0], {
    path: "/capture-profiles/active",
    options: { method: "PUT", body: { profile_id: "night" } },
  });
  assert.match(globalThis.captureStatus.className, /capture-profile-status-restricted/);
  assert.match(globalThis.captureStatus.innerHTML, /Night/);
  assert.match(globalThis.captureStatus.innerHTML, /Manage/);
  assert.match(globalThis.mobileCaptureStatus.innerHTML, /Night/);

  globalThis.captureConfirmation = null;
  globalThis.window.deleteCaptureProfile("night");
  assert.equal(globalThis.captureConfirmation, null);
  assert.match(globalThis.captureNotification.message, /active Capture profile cannot be deleted/);
});

test("inactive built-in All Devices can be activated but not edited or deleted", async () => {
  let activateClick;
  globalThis.captureActionButtons = [{
    addEventListener(type, handler) {
      if (type === "click") activateClick = handler;
    },
    getAttribute(name) {
      if (name === "data-capture-profile-action") return "activate";
      if (name === "data-profile-id") return "all";
      return null;
    },
  }];
  globalThis.captureRequests = [];
  await module.captureProfiles();

  assert.match(globalThis.captureHtml, /data-capture-profile-action="activate" data-profile-id="all"/);
  assert.doesNotMatch(globalThis.captureHtml, /data-capture-profile-action="(?:edit|delete)" data-profile-id="all"/);
  assert.equal(typeof activateClick, "function");

  activateClick();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(globalThis.captureRequests[0], {
    path: "/capture-profiles/active",
    options: { method: "PUT", body: { profile_id: "all" } },
  });
});

test("global status distinguishes restricted, All Devices, and unavailable states", async () => {
  globalThis.captureResponses["/capture-profiles/active"].profile = {
    id: "night",
    name: "Night",
    include_all_devices: false,
    device_ids: [],
  };
  await module.refreshCaptureProfileNotice();
  assert.match(globalThis.captureStatus.className, /capture-profile-status-disarmed/);
  assert.match(globalThis.captureStatus.innerHTML, /Night/);
  assert.match(globalThis.captureStatus["aria-label"], /No Devices may start capture/);

  globalThis.captureResponses["/capture-profiles/active"].profile = {
    id: "all",
    name: "All Devices",
    include_all_devices: true,
    device_ids: [],
  };
  await module.refreshCaptureProfileNotice();
  assert.match(globalThis.captureStatus.className, /capture-profile-status-all/);
  assert.doesNotMatch(globalThis.captureStatus.className, /hidden/);
  assert.match(globalThis.captureStatus.innerHTML, /All Devices/);

  globalThis.captureApiError = new Error("profiles unavailable");
  await module.refreshCaptureProfileNotice();
  assert.match(globalThis.captureStatus.className, /capture-profile-status-unavailable/);
  assert.match(globalThis.captureStatus.innerHTML, /Unavailable/);
  globalThis.captureApiError = null;
});

test("profile request errors surface without hiding the rendered UI", async () => {
  const profiles = globalThis.captureResponses["/capture-profiles"];
  const devices = globalThis.captureResponses["/devices?include_disabled=true"];
  const areas = globalThis.captureResponses["/areas?include_disabled=true"];
  globalThis.captureResponses["/capture-profiles"] = [];
  globalThis.captureResponses["/devices?include_disabled=true"] = [];
  globalThis.captureResponses["/areas?include_disabled=true"] = [];
  globalThis.captureResponses["/capture-profiles/active"].profile = {
    id: "all",
    name: "All Devices",
    include_all_devices: true,
    device_ids: [],
  };
  await module.captureProfiles();
  assert.match(globalThis.captureHtml, /No Capture profiles yet/);
  assert.match(globalThis.captureHtml, /built-in All Devices/);
  globalThis.captureResponses["/capture-profiles"] = profiles;
  globalThis.captureResponses["/devices?include_disabled=true"] = devices;
  globalThis.captureResponses["/areas?include_disabled=true"] = areas;

  globalThis.captureApiError = new Error("profiles unavailable");
  await module.captureProfiles();
  assert.match(String(globalThis.captureError), /profiles unavailable/);
  globalThis.captureApiError = null;
});
