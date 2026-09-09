import { API, api, apiRequest } from "./api.js?v=3";
import { pageHeader } from "./components.js?v=4";
import { notify } from "./dialogs.js?v=1";
import { escHtml } from "./dom.js";
import { showContent, showError, showLoading } from "./view.js?v=1";

const SETTINGS_PATH = "/settings/notifications/episode-started";
const TEST_PATH = `${SETTINGS_PATH}/test`;
const DEFAULT_TIMEOUT_SECONDS = 5;
const WEBHOOK_URL_MASK = "••••••••••••••••";

let notificationSettings = null;

function systemNavigation(active = "notifications") {
  const sections = [
    ["overview", "Overview"],
    ["recordings", "Recordings"],
    ["integrations", "Integrations"],
    ["notifications", "Notifications"],
    ["capture-profiles", "Capture profiles"],
    ["storage", "Storage"],
  ];
  return `<nav class="system-navigation" aria-label="System sections">
    ${sections.map(([id, label]) => `<a href="#system${id === "overview" ? "" : `/${id}`}" class="${active === id ? "active" : ""}">${label}</a>`).join("")}
  </nav>`;
}

function normalizedTimeout(value) {
  const timeout = Number(value);
  return Number.isFinite(timeout) ? timeout : DEFAULT_TIMEOUT_SECONDS;
}

function normalizedSettings(settings) {
  if (!settings || typeof settings !== "object" || Array.isArray(settings)) return null;
  return {
    enabled: settings.enabled === true,
    payload_format: settings.payload_format === "discord" ? "discord" : "generic",
    timeout_seconds: normalizedTimeout(settings.timeout_seconds),
    url_configured: settings.url_configured === true,
  };
}

function dataValue(data, name) {
  if (data && typeof data.get === "function") return data.get(name);
  return data?.[name];
}

function checkedValue(value) {
  return value === true || value === "true";
}

/**
 * Build the write-only settings request without ever copying a saved URL into
 * UI state. The fixed mask preserves a saved URL; deleting it explicitly
 * clears the destination.
 */
export function notificationRequestBody(data, urlConfigured = notificationSettings?.url_configured === true) {
  const rawTimeout = dataValue(data, "timeout_seconds");
  const timeoutSeconds = Number(rawTimeout);
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 0.5 || timeoutSeconds > 30) {
    throw new Error("Timeout must be between 0.5 and 30 seconds.");
  }

  const url = String(dataValue(data, "url") ?? "").trim();

  const body = {
    enabled: checkedValue(dataValue(data, "enabled")),
    payload_format: dataValue(data, "payload_format") === "discord" ? "discord" : "generic",
    timeout_seconds: timeoutSeconds,
  };
  if (urlConfigured && url === "") {
    body.enabled = false;
    body.clear_url = true;
  } else if (url && url !== WEBHOOK_URL_MASK) {
    body.url = url;
  }
  return body;
}

function statusBadge(config) {
  if (config.enabled) return '<span class="badge badge-active">Enabled</span>';
  return config.url_configured
    ? '<span class="badge badge-neutral">Disabled</span>'
    : '<span class="badge badge-neutral">Not configured</span>';
}

function actionStatusMarkup(actionStatus) {
  if (!actionStatus?.message) {
    return '<p id="notification-action-status" class="notification-action-status" role="status" aria-live="polite" hidden></p>';
  }
  const tone = ["success", "error", "pending"].includes(actionStatus.tone)
    ? actionStatus.tone
    : "success";
  return `<p id="notification-action-status" class="notification-action-status notification-action-status-${tone}" role="${tone === "error" ? "alert" : "status"}" aria-live="polite">${escHtml(actionStatus.message)}</p>`;
}

function notificationEmptyState() {
  return `<section class="section empty-state notification-empty-state">
    <h3>Notification settings unavailable</h3>
    <p>The Episode-start webhook settings did not return a usable configuration.</p>
    <button type="button" class="button button-ghost" data-notification-retry>Try again</button>
  </section>`;
}

function notificationForm(config, actionStatus) {
  const timeout = normalizedTimeout(config.timeout_seconds);
  const configured = config.url_configured;
  return `<section class="section notification-settings-panel">
    <div class="system-section-heading notification-heading">
      <div><h3>Episode-start webhook</h3><p>Send one operational notification when a new Episode starts.</p></div>
      ${statusBadge(config)}
    </div>
    <form id="episode-start-webhook-form" class="notification-form" novalidate>
      <label class="toggle-row notification-toggle">
        <input name="enabled" type="checkbox" value="true" ${config.enabled ? "checked" : ""}>
        <span><strong>Enable Episode-start notifications</strong><small>Only new Episodes trigger a request; changing this does not affect existing Episodes.</small></span>
      </label>
      <div class="notification-form-grid">
        <label class="field notification-url-field">
          <span>Webhook URL</span>
          <input name="url" type="password" autocomplete="off" spellcheck="false" value="${configured ? WEBHOOK_URL_MASK : ""}" placeholder="https://example.invalid/episode-hook" aria-describedby="notification-url-help">
          <small id="notification-url-help">${configured ? "Leave the masked value unchanged, paste a replacement, or delete it to remove the destination." : "Paste the destination URL. Episode never returns it after saving."}</small>
        </label>
        <label class="field notification-format-field">
          <span>Format</span>
          <select name="payload_format" aria-describedby="notification-format-help">
            <option value="generic" ${config.payload_format === "generic" ? "selected" : ""}>Generic JSON</option>
            <option value="discord" ${config.payload_format === "discord" ? "selected" : ""}>Discord</option>
          </select>
          <small id="notification-format-help">Choose the receiving endpoint.</small>
        </label>
        <label class="field notification-timeout-field">
          <span>Timeout (seconds)</span>
          <input name="timeout_seconds" type="number" min="0.5" max="30" step="0.5" inputmode="decimal" required value="${escHtml(timeout)}" aria-describedby="notification-timeout-help">
          <small id="notification-timeout-help">0.5–30 seconds</small>
        </label>
      </div>
      <div class="notification-form-actions">
        <button type="submit" class="button button-primary">Save</button>
        <button type="button" class="button button-ghost" data-notification-test ${configured ? "" : "disabled title=\"Save a webhook URL before sending a test\""}>Send test</button>
      </div>
      ${actionStatusMarkup(actionStatus)}
    </form>
    <p class="notification-delivery-note"><strong>Best effort:</strong> one bounded request, with no retries or delivery history. Use a trusted endpoint and prefer HTTPS.</p>
  </section>`;
}

export function notificationPageHtml(settings, actionStatus = null) {
  const config = normalizedSettings(settings);
  const content = config ? notificationForm(config, actionStatus) : notificationEmptyState();
  return `${pageHeader({
    eyebrow: "Operations · System",
    title: "Notifications",
    description: "Configure the optional best-effort notification sent when an Episode starts.",
    actions: `<a class="button button-ghost" href="${API}/diagnostics/export" download>Download diagnostics</a>`,
  })}
  <div class="system-layout notification-layout">
    ${systemNavigation("notifications")}
    <div class="system-content">${content}</div>
  </div>`;
}

function actionStatusElement() {
  return typeof document === "undefined" ? null : document.getElementById("notification-action-status");
}

function setActionStatus(message, tone = "success") {
  const element = actionStatusElement();
  if (!element) return;
  element.className = `notification-action-status notification-action-status-${tone}`;
  element.hidden = !message;
  element.textContent = message;
  element.setAttribute("role", tone === "error" ? "alert" : "status");
}

function attachNotificationHandlers() {
  if (typeof document === "undefined") return;
  const form = document.getElementById("episode-start-webhook-form");
  if (form) {
    form.addEventListener("submit", event => {
      event.preventDefault();
      saveEpisodeStartedWebhook(form);
    });
  }
  const testButton = document.querySelector("[data-notification-test]");
  if (testButton) testButton.addEventListener("click", () => sendEpisodeStartedWebhookTest(testButton));
  const retryButton = document.querySelector("[data-notification-retry]");
  if (retryButton) retryButton.addEventListener("click", () => loadNotifications());
}

export function renderNotificationSettings(settings, actionStatus = null) {
  const config = normalizedSettings(settings);
  notificationSettings = config;
  showContent(notificationPageHtml(config, actionStatus));
  attachNotificationHandlers();
  return config;
}

function stateAfterSave(body, response) {
  const returned = normalizedSettings(response);
  if (returned) return returned;
  return normalizedSettings({
    ...notificationSettings,
    enabled: body.enabled,
    payload_format: body.payload_format,
    timeout_seconds: body.timeout_seconds,
    url_configured: body.clear_url ? false : body.url ? true : notificationSettings?.url_configured,
  });
}

export async function saveEpisodeStartedWebhook(form) {
  const submit = form?.querySelector?.('[type="submit"]');
  if (submit) submit.disabled = true;
  setActionStatus("Saving…", "pending");
  try {
    const body = notificationRequestBody(
      new FormData(form),
      notificationSettings?.url_configured === true,
    );
    const response = await apiRequest(SETTINGS_PATH, { method: "PUT", body });
    const next = stateAfterSave(body, response);
    renderNotificationSettings(next, { message: "Notification settings saved.", tone: "success" });
    notify("Notification settings saved");
  } catch (error) {
    setActionStatus(error.message, "error");
    notify(`Could not save notification settings: ${error.message}`, "warning");
    if (submit) submit.disabled = false;
  }
}

export async function sendEpisodeStartedWebhookTest(button = null) {
  const target = button || (typeof document !== "undefined" ? document.querySelector("[data-notification-test]") : null);
  if (target) target.disabled = true;
  setActionStatus("Sending test…", "pending");
  try {
    const result = await apiRequest(TEST_PATH, { method: "POST" });
    if (!result?.success) {
      throw new Error(result?.message || "The webhook rejected the test request.");
    }
    setActionStatus("Test request sent. Delivery remains best effort.", "success");
    notify("Test notification sent");
  } catch (error) {
    setActionStatus(`Test failed: ${error.message}`, "error");
    notify(`Could not send test notification: ${error.message}`, "warning");
  } finally {
    if (target) target.disabled = false;
  }
}

async function loadNotifications() {
  showLoading();
  try {
    const settings = await api(SETTINGS_PATH);
    renderNotificationSettings(settings);
  } catch (error) {
    showError(error.message);
  }
}

export async function notifications() {
  await loadNotifications();
}

if (typeof window !== "undefined") {
  window.saveEpisodeStartedWebhook = saveEpisodeStartedWebhook;
  window.sendEpisodeStartedWebhookTest = sendEpisodeStartedWebhookTest;
}
