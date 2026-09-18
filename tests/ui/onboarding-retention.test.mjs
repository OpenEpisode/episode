import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/onboarding.js", import.meta.url),
  "utf8",
);
const apiUrl = moduleUrl(`
  export async function api(path) { return globalThis.onboardingResponses[path]; }
  export async function apiRequest(path, options) {
    globalThis.onboardingRequests.push({ path, options });
    globalThis.onboardingResponses["/settings/retention"] = {
      enabled: options.body.enabled,
      retention_days: options.body.retention_days,
      policy_state: options.body.enabled ? "configured" : "disabled",
    };
  }
`);
const componentsUrl = moduleUrl(`
  export function pageHeader(value) { return "<header><h2>" + value.title + "</h2></header>"; }
`);
const dialogsUrl = moduleUrl(`
  export function confirmDialog(options) { globalThis.onboardingConfirmation = options; }
  export function closeDialog() { globalThis.onboardingDialogCloses += 1; }
  export function notify(message) { globalThis.onboardingNotification = message; }
`);
const inventoryUrl = moduleUrl(`
  export function openAreaEditor() {}
  export function openDeviceEditor() {}
`);
const retentionUrl = moduleUrl(`
  export async function refreshRetentionPolicy() { globalThis.retentionRefreshes += 1; }
`);
const viewUrl = moduleUrl(`
  export function showContent(html) { globalThis.onboardingHtml = html; }
  export function showError(error) { throw new Error(error); }
  export function showLoading() {}
`);

globalThis.window = {};
globalThis.retentionRefreshes = 0;
globalThis.onboardingRequests = [];
globalThis.onboardingDialogCloses = 0;
globalThis.onboardingResponses = {
  "/areas?include_disabled=true": [{ id: "entrance", enabled: true }],
  "/devices?include_disabled=true": [{ id: "camera" }],
  "/status": { integrations: { healthy: 1, total: 1 } },
  "/settings/retention": {
    enabled: true,
    retention_days: 30,
    policy_state: "unconfirmed",
  },
};

const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=3"', JSON.stringify(componentsUrl))
    .replace('"./dialogs.js?v=1"', JSON.stringify(dialogsUrl))
    .replace('"./inventory.js?v=7"', JSON.stringify(inventoryUrl))
    .replace('"./retention-policy.js?v=1"', JSON.stringify(retentionUrl))
    .replace('"./view.js?v=1"', JSON.stringify(viewUrl)),
));

test("unconfirmed retention keeps first-run setup open", async () => {
  assert.equal(await module.onboardingNeeded(), true);
  await module.welcome();
  assert.match(globalThis.onboardingHtml, /Confirm visual Evidence retention/);
  assert.match(globalThis.onboardingHtml, /Confirm 30 days/);
});

test("confirming the default records the policy and completes setup", async () => {
  globalThis.window.confirmDefaultRetention(30);
  assert.match(globalThis.onboardingConfirmation.message, /permanently delete/);
  await globalThis.onboardingConfirmation.onConfirm();

  assert.deepEqual(globalThis.onboardingRequests, [{
    path: "/settings/retention",
    options: { method: "PUT", body: { enabled: true, retention_days: 30 } },
  }]);
  assert.equal(globalThis.onboardingNotification, "Visual Evidence retention confirmed");
  assert.equal(globalThis.onboardingDialogCloses, 1);
  assert.equal(globalThis.retentionRefreshes, 1);
  assert.equal(await module.onboardingNeeded(), false);
  assert.match(globalThis.onboardingHtml, /evidence workspace is ready/i);
});
