import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/notifications.js", import.meta.url),
  "utf8",
);

const apiUrl = moduleUrl(`
  export const API = "/api/v1";
  export async function api(path) {
    if (globalThis.notificationGetError) throw globalThis.notificationGetError;
    return globalThis.notificationResponses[path];
  }
  export async function apiRequest(path, options) {
    globalThis.notificationRequests.push({ path, options });
    if (globalThis.notificationRequestError) throw globalThis.notificationRequestError;
    return globalThis.notificationResponses[path];
  }
`);
const componentsUrl = moduleUrl(`
  export function pageHeader(value) { return "<header><h2>" + value.title + "</h2>" + value.description + value.actions + "</header>"; }
`);
const dialogsUrl = moduleUrl(`
  export function notify(message, tone) { globalThis.notificationNotifications.push({ message, tone }); }
`);
const domUrl = moduleUrl(`
  export function escHtml(value) { return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
`);
const viewUrl = moduleUrl(`
  export function showLoading() { globalThis.notificationLoading += 1; }
  export function showError(error) { globalThis.notificationError = error; }
  export function showContent(html) { globalThis.notificationHtml = html; }
`);

globalThis.window = {};
globalThis.notificationResponses = {};
globalThis.notificationRequests = [];
globalThis.notificationNotifications = [];
globalThis.notificationGetError = null;
globalThis.notificationRequestError = null;
globalThis.notificationLoading = 0;
globalThis.notificationError = null;
globalThis.notificationHtml = "";
globalThis.notificationDom = {};
globalThis.document = {
  getElementById(id) {
    return globalThis.notificationDom[id] || null;
  },
  querySelector() {
    return null;
  },
};

const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=4"', JSON.stringify(componentsUrl))
    .replace('"./dialogs.js?v=1"', JSON.stringify(dialogsUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./view.js?v=1"', JSON.stringify(viewUrl)),
));

test("notification request preserves, replaces, or clears the write-only URL", () => {
  const body = module.notificationRequestBody(new Map([
    ["enabled", "true"],
    ["payload_format", "discord"],
    ["timeout_seconds", "2.5"],
    ["url", "••••••••••••••••"],
  ]), true);
  assert.deepEqual(body, {
    enabled: true,
    payload_format: "discord",
    timeout_seconds: 2.5,
  });
  assert.equal(Object.hasOwn(body, "url"), false);

  const replacement = module.notificationRequestBody(new Map([
    ["enabled", "true"],
    ["payload_format", "generic"],
    ["timeout_seconds", "5"],
    ["url", "https://replacement.example/hook"],
  ]), true);
  assert.equal(replacement.url, "https://replacement.example/hook");

  const cleared = module.notificationRequestBody(new Map([
    ["enabled", "true"],
    ["payload_format", "generic"],
    ["timeout_seconds", "5"],
    ["url", ""],
  ]), true);
  assert.deepEqual(cleared, {
    enabled: false,
    payload_format: "generic",
    timeout_seconds: 5,
    clear_url: true,
  });
});

test("notification screen masks the URL and exposes compact bounded settings", async () => {
  globalThis.notificationResponses["/settings/notifications/episode-started"] = {
    enabled: true,
    payload_format: "generic",
    timeout_seconds: 5,
    url_configured: true,
  };
  globalThis.notificationLoading = 0;
  globalThis.notificationError = null;
  await module.notifications();

  assert.equal(globalThis.notificationLoading, 1);
  assert.match(globalThis.notificationHtml, /Notifications/);
  assert.match(globalThis.notificationHtml, /Episode-start webhook/);
  assert.match(globalThis.notificationHtml, /href="#system\/notifications" class="active"/);
  assert.match(globalThis.notificationHtml, /name="url" type="password"/);
  assert.match(globalThis.notificationHtml, /value="••••••••••••••••"/);
  assert.match(globalThis.notificationHtml, /delete it to remove the destination/);
  assert.doesNotMatch(globalThis.notificationHtml, /Clear saved URL/);
  assert.doesNotMatch(globalThis.notificationHtml, /Episode address/);
  assert.match(globalThis.notificationHtml, /Generic JSON/);
  assert.match(globalThis.notificationHtml, />Discord</);
  assert.match(globalThis.notificationHtml, /no retries or delivery history/);

  globalThis.notificationResponses["/settings/notifications/episode-started"] = null;
  await module.notifications();
  assert.match(globalThis.notificationHtml, /Notification settings unavailable/);
  assert.match(globalThis.notificationHtml, /data-notification-retry/);
});

test("save and test actions use the write-only settings contract and show success/errors", async () => {
  globalThis.notificationResponses["/settings/notifications/episode-started"] = {
    enabled: false,
    payload_format: "generic",
    timeout_seconds: 5,
    url_configured: true,
  };
  await module.notifications();
  globalThis.notificationRequests = [];
  globalThis.notificationNotifications = [];
  const status = {
    className: "",
    hidden: true,
    textContent: "",
    setAttribute(name, value) { this[name] = value; },
  };
  globalThis.notificationDom["notification-action-status"] = status;
  const submit = { disabled: false };
  const form = {
    querySelector() { return submit; },
    enabled: "true",
    payload_format: "generic",
    timeout_seconds: "3",
    url: "••••••••••••••••",
  };
  globalThis.FormData = class {
    constructor(values) { this.values = values; }
    get(name) { return this.values[name] ?? null; }
  };
  await module.saveEpisodeStartedWebhook(form);

  assert.deepEqual(globalThis.notificationRequests[0], {
    path: "/settings/notifications/episode-started",
    options: {
      method: "PUT",
      body: { enabled: true, payload_format: "generic", timeout_seconds: 3 },
    },
  });
  assert.match(globalThis.notificationHtml, /Notification settings saved/);
  assert.doesNotMatch(globalThis.notificationHtml, /value="https:\/\//);

  const testButton = { disabled: false };
  globalThis.notificationResponses["/settings/notifications/episode-started/test"] = {
    success: true,
    message: "Test notification delivered",
  };
  await module.sendEpisodeStartedWebhookTest(testButton);
  assert.deepEqual(globalThis.notificationRequests[1], {
    path: "/settings/notifications/episode-started/test",
    options: { method: "POST" },
  });
  assert.equal(testButton.disabled, false);
  assert.match(status.textContent, /Test request sent/);

  globalThis.notificationResponses["/settings/notifications/episode-started/test"] = {
    success: false,
    message: "Webhook returned HTTP 500",
  };
  await module.sendEpisodeStartedWebhookTest(testButton);
  assert.match(status.textContent, /Test failed: Webhook returned HTTP 500/);

  globalThis.notificationRequestError = new Error("endpoint unavailable");
  await module.sendEpisodeStartedWebhookTest(testButton);
  assert.match(status.textContent, /Test failed: endpoint unavailable/);
  assert.equal(testButton.disabled, false);
  globalThis.notificationRequestError = null;
});

test("settings load errors stay in the shared error state", async () => {
  globalThis.notificationGetError = new Error("settings unavailable");
  globalThis.notificationError = null;
  await module.notifications();
  assert.equal(globalThis.notificationError, "settings unavailable");
  globalThis.notificationGetError = null;
});
