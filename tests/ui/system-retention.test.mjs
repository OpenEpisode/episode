import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/inventory-pages.js", import.meta.url),
  "utf8",
);

const apiUrl = moduleUrl(`
  export const API = "/api/v1";
  export async function api(path) { return globalThis.systemResponses[path]; }
  export async function apiRequest(path, options) {
    globalThis.systemRequests.push({ path, options });
  }
`);
const componentsUrl = moduleUrl(`
  export function detailMetric() { return ""; }
  export function eventSourceBadges() { return ""; }
  export function pageHeader(value) { return "<header><h2>" + value.title + "</h2></header>"; }
  export function sectionHeading() { return ""; }
`);
const dialogsUrl = moduleUrl(`
  export function confirmDialog(options) { globalThis.systemConfirmation = options; }
  export function closeDialog() { globalThis.systemDialogCloses += 1; }
  export function notify(message) { globalThis.systemNotification = message; }
`);
const domUrl = moduleUrl(`
  export function escHtml(value) { return String(value ?? ""); }
`);
const formatUrl = moduleUrl(`
  export function fmtBytes(value) { return String(value ?? 0); }
  export function fmtShort(value) { return String(value ?? ""); }
  export function plural(value, label) { return value + " " + label; }
  export function titleCase(value) { return String(value ?? ""); }
`);
const timelineUrl = moduleUrl(`export function eventTitle() { return "Event"; }`);
const inventoryUrl = moduleUrl(`
  export function confirmAreaDelete() {}
  export function confirmDeviceDelete() {}
  export function openAreaEditor() {}
  export function openDeviceEditor() {}
`);
const retentionPolicyUrl = moduleUrl(`
  export async function refreshRetentionPolicy() { globalThis.policyRefreshes += 1; }
`);
const viewUrl = moduleUrl(`
  export function showContent(html) { globalThis.systemHtml = html; }
  export function showError(error) { throw new Error(error); }
  export function showLoading() {}
`);

globalThis.window = {};
globalThis.FormData = class {
  constructor(form) { this.form = form; }
  get(name) { return this.form[name]; }
};
globalThis.systemRequests = [];
globalThis.policyRefreshes = 0;
globalThis.systemDialogCloses = 0;
globalThis.systemResponses = {
  "/diagnostics": {
    status: {
      version: "0.1.0-beta.3",
      state: "healthy",
      active_recordings: 0,
      integrations: { healthy: 0, total: 0 },
    },
    services: [{
      id: "recorder",
      name: "Recorder",
      state: "degraded",
      summary: "1 active recording",
      metrics: {
        completed_recordings: 8,
        reconnects: 2,
        incomplete_recordings: 1,
        failures: 0,
      },
    }],
    integrations: [],
    recordings: [{
      evidence_id: "evidence-one",
      episode_id: "episode-one",
      device_id: "front-camera",
      started_at: "2026-08-28T12:00:00Z",
      state: "reconnecting",
      ready: true,
      fragment_count: 12,
      last_fragment_at: "2026-08-28T12:01:00Z",
      reconnect_count: 1,
      last_exit_code: 1,
      last_error: "FFmpeg exited with code 1; reconnecting",
    }],
    recording_issues: [{
      evidence_id: "incomplete-one",
      episode_id: "episode-old",
      device_id: "garage-camera",
      timestamp: "2026-08-27T12:00:00Z",
      reason: "retry_limit_exceeded",
    }],
    storage: { data_bytes: 0, filesystem_free_bytes: 1000 },
  },
  "/settings/retention": {
    enabled: true,
    retention_days: 30,
    policy_state: "unconfirmed",
    confirmed_at: null,
    notice: "Retention requirements vary by jurisdiction and use case.",
  },
};

const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=4"', JSON.stringify(componentsUrl))
    .replace('"./dialogs.js?v=1"', JSON.stringify(dialogsUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js?v=4"', JSON.stringify(formatUrl))
    .replace('"./timeline.js?v=6"', JSON.stringify(timelineUrl))
    .replace('"./inventory.js?v=6"', JSON.stringify(inventoryUrl))
    .replace('"./retention-policy.js?v=1"', JSON.stringify(retentionPolicyUrl))
    .replace('"./view.js?v=1"', JSON.stringify(viewUrl)),
));

test("System separates overview, recording, and storage concerns", async () => {
  await module.systemStatus();

  assert.match(globalThis.systemHtml, /System sections/);
  assert.match(globalThis.systemHtml, /href="#system\/recordings"/);
  assert.match(globalThis.systemHtml, /href="#system\/integrations"/);
  assert.match(globalThis.systemHtml, /href="#system\/storage"/);
  assert.doesNotMatch(globalThis.systemHtml, /name="retention_days"/);
  assert.doesNotMatch(globalThis.systemHtml, /Interrupted recordings/);

  await module.systemStatus("recordings");
  assert.match(globalThis.systemHtml, /Recording activity/);
  assert.match(globalThis.systemHtml, /front-camera/);
  assert.match(globalThis.systemHtml, /reconnecting/i);
  assert.match(globalThis.systemHtml, /12 fragment/);
  assert.match(globalThis.systemHtml, /Interrupted recordings/);
  assert.match(globalThis.systemHtml, /until retention expires them/);
  assert.match(globalThis.systemHtml, /garage-camera/);
  assert.match(globalThis.systemHtml, /Review capture/);
  assert.match(globalThis.systemHtml, /No action is required/);
  assert.doesNotMatch(globalThis.systemHtml, /name="retention_days"/);

  await module.systemStatus("storage");

  assert.match(globalThis.systemHtml, /name="retention_days"/);
  assert.match(globalThis.systemHtml, /value="30"/);
  assert.match(globalThis.systemHtml, /requirements vary by jurisdiction/);
  assert.match(globalThis.systemHtml, /class="section system-retention"/);
  assert.match(globalThis.systemHtml, /class="system-retention-form"/);
  assert.match(globalThis.systemHtml, /Confirmation required/);
  assert.match(globalThis.systemHtml, /name="retention_enabled"[^>]+checked/);
  assert.doesNotMatch(globalThis.systemHtml, /Interrupted recordings/);

  globalThis.window.saveRetention({ retention_days: "15", retention_enabled: "true" });
  assert.match(globalThis.systemConfirmation.message, /permanently delete.*15 days/);
  await globalThis.systemConfirmation.onConfirm();

  assert.deepEqual(globalThis.systemRequests, [{
    path: "/settings/retention",
    options: { method: "PUT", body: { enabled: true, retention_days: 15 } },
  }]);
  assert.equal(globalThis.systemNotification, "Visual Evidence retention updated");
  assert.equal(globalThis.systemDialogCloses, 1);
  assert.equal(globalThis.policyRefreshes, 1);
});

test("System requires explicit confirmation before disabling retention", async () => {
  globalThis.systemRequests = [];
  globalThis.systemDialogCloses = 0;
  globalThis.window.saveRetention({ retention_days: "30", retention_enabled: null });

  assert.match(globalThis.systemConfirmation.title, /Disable automatic deletion/);
  assert.match(globalThis.systemConfirmation.message, /retained indefinitely/);
  await globalThis.systemConfirmation.onConfirm();

  assert.deepEqual(globalThis.systemRequests, [{
    path: "/settings/retention",
    options: { method: "PUT", body: { enabled: false, retention_days: 30 } },
  }]);
  assert.equal(globalThis.systemNotification, "Automatic retention disabled");
  assert.equal(globalThis.systemDialogCloses, 1);
});
