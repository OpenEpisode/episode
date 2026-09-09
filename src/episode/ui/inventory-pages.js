import { API, api, apiRequest } from "./api.js?v=3";
import {
  detailMetric,
  eventSourceBadges,
  pageHeader,
  sectionHeading,
} from "./components.js?v=4";
import { closeDialog, confirmDialog, notify } from "./dialogs.js?v=1";
import { escHtml } from "./dom.js";
import { fmtBytes, fmtShort, plural, titleCase } from "./format.js?v=4";
import { eventTitle } from "./timeline.js?v=6";
import {
  confirmAreaDelete,
  confirmDeviceDelete,
  openAreaEditor,
  openDeviceEditor,
} from "./inventory.js?v=6";
import { refreshRetentionPolicy } from "./retention-policy.js?v=1";
import { showContent, showError, showLoading } from "./view.js?v=1";

let inventoryAreas = [];
let inventoryDevices = [];

export function suggestedExternalUrl() {
  const origin = globalThis.window?.location?.origin || globalThis.location?.origin || "";
  if (!origin || origin === "null") return "";
  try {
    const parsed = new URL(origin);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.origin : "";
  } catch {
    return "";
  }
}

export function operationalIndicator(state) {
  if (state === "healthy") return "online";
  if (state === "degraded") return "warning";
  if (state === "disabled" || state === "unknown") return "idle";
  return "offline";
}

export function operationalBadge(state) {
  return `<span class="badge badge-${state}">${titleCase(state)}</span>`;
}

function capabilityBadges(capabilities) {
  return (capabilities || [])
    .filter(capability => capability !== "events")
    .map(capability => `<span class="badge badge-neutral">${escHtml(titleCase(capability))}</span>`)
    .join(" ");
}

function integrationBadges(integrations) {
  return (integrations || [])
    .map(integration => `<span class="badge badge-${integration.state}">${titleCase(integration.type)}</span>`)
    .join(" ");
}

function diagnosticLabel(key) {
  return titleCase(key)
    .replace(/\bId\b/g, "ID")
    .replace(/\bUri\b/g, "URI")
    .replace(/\bUrl\b/g, "URL")
    .replace(/\bOnvif\b/g, "ONVIF")
    .replace(/\bRtsp\b/g, "RTSP")
    .replace(/\bHls\b/g, "HLS");
}

function hasDiagnosticValue(value) {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function diagnosticScalar(key, value) {
  let display;
  if (typeof value === "boolean") {
    display = `<span class="diagnostic-boolean ${value ? "is-true" : "is-false"}">${value ? "Yes" : "No"}</span>`;
  } else if (key.endsWith("_at") && typeof value === "string") {
    display = `<time datetime="${escHtml(value)}" title="${escHtml(value)}">${escHtml(fmtShort(value))}</time>`;
  } else {
    display = escHtml(String(value));
  }
  return `<div class="diagnostic-fact"><dt>${escHtml(diagnosticLabel(key))}</dt><dd>${display}</dd></div>`;
}

function diagnosticPrimitiveList(key, values) {
  const visible = values.slice(0, 12);
  const remaining = values.slice(12);
  const tags = items => items.map(value =>
    `<span class="diagnostic-tag">${escHtml(String(value))}</span>`).join("");
  return `<section class="diagnostic-section">
    <header><h4>${escHtml(diagnosticLabel(key))}</h4><span>${values.length}</span></header>
    <div class="diagnostic-tags">${tags(visible)}</div>
    ${remaining.length ? `<details class="diagnostic-more">
      <summary>Show ${remaining.length} more</summary>
      <div class="diagnostic-tags">${tags(remaining)}</div>
    </details>` : ""}
  </section>`;
}

function diagnosticBody(value) {
  const entries = Object.entries(value || {}).filter(([, item]) => hasDiagnosticValue(item));
  const scalars = entries.filter(([, item]) => typeof item !== "object");
  const structured = entries.filter(([, item]) => typeof item === "object");
  return `${scalars.length ? `<dl class="diagnostic-facts">
      ${scalars.map(([key, item]) => diagnosticScalar(key, item)).join("")}
    </dl>` : ""}
    ${structured.map(([key, item]) => diagnosticStructuredValue(key, item)).join("")}`;
}

function diagnosticStructuredValue(key, value) {
  if (Array.isArray(value) && value.every(item => typeof item !== "object" || item === null)) {
    return diagnosticPrimitiveList(key, value);
  }
  if (Array.isArray(value)) {
    return `<section class="diagnostic-section">
      <header><h4>${escHtml(diagnosticLabel(key))}</h4><span>${value.length}</span></header>
      <div class="diagnostic-objects">${value.map((item, index) => {
        const heading = item?.name || item?.id || item?.token || `Item ${index + 1}`;
        return `<article class="diagnostic-object">
          <h5>${escHtml(String(heading))}</h5>
          ${diagnosticBody(item)}
        </article>`;
      }).join("")}</div>
    </section>`;
  }
  return `<section class="diagnostic-section">
    <header><h4>${escHtml(diagnosticLabel(key))}</h4></header>
    ${diagnosticBody(value)}
  </section>`;
}

export function renderDiagnosticDetails(details) {
  return `<div class="diagnostic-panel">
    ${diagnosticBody(details)}
    <details class="diagnostic-raw">
      <summary>View raw JSON</summary>
      <pre>${escHtml(JSON.stringify(details, null, 2))}</pre>
    </details>
  </div>`;
}

export function renderIntegrationRows(integrations, showDetails = false) {
  if (!integrations.length) return '<div class="empty">No integrations configured</div>';
  return `<div class="resource-list">
    ${integrations.map(integration => {
      const details = Object.entries(integration.details || {})
        .filter(([, value]) => value !== null && value !== ""
          && (!Array.isArray(value) || value.length));
      return `<div class="resource-row">
        <span class="status-indicator ${operationalIndicator(integration.state)}"></span>
        <div class="resource-main">
          <strong>${escHtml(integration.name)}</strong>
          <span>${escHtml(integration.summary || titleCase(integration.type))}</span>
          <div class="badge-cluster">${capabilityBadges(integration.capabilities)}</div>
        </div>
        ${operationalBadge(integration.state)}
        ${showDetails && details.length ? `<details class="diagnostic-details">
          <summary>Technical details</summary>
          ${renderDiagnosticDetails(integration.details)}
        </details>` : ""}
      </div>`;
    }).join("")}
  </div>`;
}

function recordingOperationalState(state) {
  if (state === "recording") return "healthy";
  if (state === "starting") return "unknown";
  if (state === "failed") return "unavailable";
  return "degraded";
}

function recordingIssueSummary(reason) {
  return {
    startup_recovery: "Preserved after an application restart",
    retry_limit_exceeded: "Camera stream could not be recovered",
    episode_closed: "Episode closed while the stream was reconnecting",
    application_shutdown: "Application stopped during recording",
    recording_task_cancelled: "Recording task was interrupted",
  }[reason] || (reason ? titleCase(reason) : "Capture did not complete");
}

export function renderRecordingOperations(recordings = [], metrics = {}, issues = []) {
  const counters = [
    ["Completed", metrics.completed_recordings || 0],
    ["Reconnects", metrics.reconnects || 0],
    ["Incomplete", metrics.incomplete_recordings || 0],
    ["Failures", metrics.failures || 0],
  ];
  return `<section class="section recording-operations">
    <div class="recording-operations-heading">
      <div><h3>Recording activity</h3><p>Current capture progress and recovery state. Counters cover this application run.</p></div>
      <span class="badge badge-neutral">${recordings.length} active</span>
    </div>
    ${recordings.length ? `<div class="resource-list">
      ${recordings.map(recording => {
        const state = recordingOperationalState(recording.state);
        const progress = recording.last_fragment_at
          ? `${plural(recording.fragment_count, "fragment")} · last ${fmtShort(recording.last_fragment_at)}`
          : "Waiting for the first media fragment";
        return `<div class="resource-row recording-operation-row">
          <span class="status-indicator ${operationalIndicator(state)}"></span>
          <div class="resource-main">
            <strong>${escHtml(recording.device_id)}</strong>
            <span>${escHtml(progress)}</span>
          </div>
          <span class="badge badge-${state}">${escHtml(titleCase(recording.state))}</span>
          <div class="recording-operation-links">
            <a href="#episode/${encodeURIComponent(recording.episode_id)}">Open Episode</a>
            ${recording.reconnect_count ? `<span>${plural(recording.reconnect_count, "reconnect")}</span>` : ""}
          </div>
          ${recording.last_error ? `<div class="recording-operation-error">${escHtml(recording.last_error)}</div>` : ""}
        </div>`;
      }).join("")}
    </div>` : '<div class="recording-idle"><span class="status-indicator online"></span><div><strong>Recorder ready</strong><span>No Episode is recording right now.</span></div></div>'}
    <dl class="recording-counters">
      ${counters.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("")}
    </dl>
    ${issues.length ? `<div class="recording-issues">
      <div class="recording-issues-heading"><h4>Interrupted recordings</h4><p>Available partial captures remain here until retention expires them. No action is required.</p></div>
      ${issues.map(issue => `<div class="recording-issue-row">
        <span class="status-indicator warning"></span>
        <span><strong>${escHtml(issue.device_id)}</strong><small>${escHtml(recordingIssueSummary(issue.reason))} · ${fmtShort(issue.timestamp)}</small></span>
        <span class="recording-issue-actions">
          <a href="#evidence/${encodeURIComponent(issue.evidence_id)}">Review capture</a>
          ${issue.episode_id ? `<a href="#episode/${encodeURIComponent(issue.episode_id)}">View Episode</a>` : ""}
        </span>
      </div>`).join("")}
    </div>` : ""}
  </section>`;
}

window.addArea = () => openAreaEditor(null, areas);
window.editArea = id => {
  const area = inventoryAreas.find(candidate => candidate.id === id);
  if (area) openAreaEditor(area, areas);
};
window.deleteArea = id => {
  const area = inventoryAreas.find(candidate => candidate.id === id);
  if (area) confirmAreaDelete(area, areas);
};
window.addDevice = () => {
  const activeAreas = inventoryAreas.filter(area => area.enabled);
  if (!activeAreas.length) {
    notify("Create an active Area before adding a Device", "warning");
    openAreaEditor(null, devices);
    return;
  }
  openDeviceEditor(null, inventoryAreas, devices);
};
window.editDevice = async id => {
  try {
    const device = await api("/devices/" + encodeURIComponent(id));
    inventoryDevices = [...inventoryDevices.filter(candidate => candidate.id !== id), device];
    openDeviceEditor(
      device,
      inventoryAreas,
      () => location.hash.startsWith("#device/") ? deviceView(id) : devices(),
    );
  } catch (error) {
    notify(`Could not load Device configuration: ${error.message}`, "warning");
  }
};
window.deleteDevice = id => {
  const device = inventoryDevices.find(candidate => candidate.id === id);
  if (device) {
    confirmDeviceDelete(device, async () => {
      location.hash = "devices";
      await devices();
    });
  }
};

window.saveRetention = form => {
  const data = new FormData(form);
  const retentionDays = Number(data.get("retention_days"));
  const enabled = data.get("retention_enabled") === "true";
  confirmDialog({
    title: enabled ? "Confirm visual Evidence retention?" : "Disable automatic deletion?",
    message: enabled
      ? `OpenEpisode will automatically and permanently delete managed visual Evidence older than ${retentionDays} days.`
      : "Managed visual Evidence will be retained indefinitely unless manually removed. A persistent warning will remain visible.",
    confirmLabel: enabled ? "Confirm retention" : "Disable retention",
    onConfirm: async () => {
      await apiRequest("/settings/retention", {
        method: "PUT",
        body: { enabled, retention_days: retentionDays },
      });
      closeDialog();
      notify(
        enabled ? "Visual Evidence retention updated" : "Automatic retention disabled",
        enabled ? "success" : "warning",
      );
      await refreshRetentionPolicy();
      await systemStatus("storage");
    },
  });
};

window.saveEpisodeLifecycle = async form => {
  const data = new FormData(form);
  const grace = Number(data.get("quiescent_grace_seconds"));
  try {
    await apiRequest("/settings/episode", {
      method: "PUT",
      body: { quiescent_grace_seconds: grace },
    });
    notify("Episode settling grace updated", "success");
    await systemStatus("recordings");
  } catch (error) {
    notify(`Could not update Episode settling grace: ${error.message}`, "warning");
  }
};

window.saveInstallationSettings = async form => {
  const data = new FormData(form);
  const externalUrl = String(data.get("external_url") || "").trim();
  try {
    await apiRequest("/settings/installation", {
      method: "PUT",
      body: { external_url: externalUrl },
    });
    notify(externalUrl ? "External Episode URL updated" : "External Episode URL removed", "success");
    await systemStatus("overview");
  } catch (error) {
    notify(`Could not update external Episode URL: ${error.message}`, "warning");
  }
};

export async function devices() {
  showLoading();
  try {
    const [list, areasList] = await Promise.all([
      api("/devices?include_disabled=true"),
      api("/areas?include_disabled=true"),
    ]);
    inventoryDevices = list;
    inventoryAreas = areasList;
    const areaNames = Object.fromEntries(areasList.map(area => [area.id, area.name]));
    showContent(`
      ${pageHeader({
        eyebrow: "Configure",
        title: "Devices",
        description: "Equipment, capture behavior, and source integrations.",
        actions: `<a href="#areas" class="button button-ghost">Manage Areas</a>
          <button class="button button-primary" onclick="addDevice()">Add Device</button>`,
      })}
      ${list.length === 0 ? `<div class="empty-state">
        <div class="empty-icon">◎</div><h3>Add your first Device</h3>
        <p>Connect a camera, doorbell, or another event source to an Area.</p>
        <button class="button button-primary" onclick="addDevice()">Add Device</button>
      </div>` : `<div class="resource-list inventory-list">
        ${list.map(device => {
          const identity = device.identity || {};
          return `<div class="resource-row inventory-row ${device.enabled ? "" : "resource-disabled"}">
            <span class="status-indicator ${operationalIndicator(device.state)}"></span>
            <a href="#device/${device.id}" class="resource-main resource-primary-link">
              <strong>${device.name || device.id}</strong>
              <span>${titleCase(device.device_type)} · ${[identity.manufacturer, identity.model].filter(Boolean).join(" ") || "Manufacturer not detected"}</span>
            </a>
            <div class="resource-context">${areaNames[device.area_id] || device.area_id || "No Area"}</div>
            <div class="resource-badges">${integrationBadges(device.integrations) || '<span class="meta">No integrations</span>'}</div>
            <div class="resource-actions">
              <button class="icon-button" onclick="editDevice('${device.id}')" aria-label="Edit ${device.name}">Edit</button>
              <button class="icon-button danger-text" onclick="deleteDevice('${device.id}')" aria-label="Delete ${device.name}">Delete</button>
            </div>
          </div>`;
        }).join("")}
      </div>`}`);
  } catch (error) {
    showError(error.message);
  }
}

export async function deviceView(id) {
  showLoading();
  try {
    const [item, areasList, activity, evidence] = await Promise.all([
      api("/devices/" + encodeURIComponent(id)),
      api("/areas?include_disabled=true"),
      api("/events?device_id=" + encodeURIComponent(id) + "&limit=12"),
      api("/evidence?device_id=" + encodeURIComponent(id) + "&limit=24"),
    ]);
    inventoryAreas = areasList;
    inventoryDevices = [item];
    const area = areasList.find(candidate => candidate.id === item.area_id);
    const identity = item.identity || {};
    const origins = [...new Set(evidence.map(entry => entry.metadata?.origin).filter(Boolean))];
    const onvif = item.integrations.find(integration => integration.type === "onvif");
    const profiles = onvif?.details?.profiles || [];
    const selectedProfile = onvif?.details?.selected_profile || "";
    const topics = onvif?.details?.event_topics || [];
    const deviceName = item.name || item.id;
    const areaName = area?.name || item.area_id || "Not assigned";
    const manufacturerModel = [identity.manufacturer, identity.model].filter(Boolean).join(" ")
      || "Manufacturer not detected";

    showContent(`
      <div class="breadcrumbs"><a href="#devices">Devices</a> <span class="sep">›</span> <span>${escHtml(deviceName)}</span></div>
      <header class="review-detail-hero device-review-hero">
        <div class="review-detail-identity">
          <div class="review-detail-icon"><svg><use href="icons.svg?v=2#devices"></use></svg></div>
          <div>
            <div class="eyebrow">Device</div>
            <h2>${escHtml(deviceName)}</h2>
            <code>${escHtml(item.id)}</code>
          </div>
        </div>
        <div class="review-detail-controls">
          <div class="review-detail-badges"><span class="badge badge-neutral">${escHtml(titleCase(item.device_type))}</span>${operationalBadge(item.state)}</div>
          <div class="page-actions">
            <button class="button button-ghost" onclick="editDevice('${escHtml(item.id)}')">Edit Device</button>
            <button class="button button-ghost danger-text" onclick="deleteDevice('${escHtml(item.id)}')">Delete</button>
          </div>
        </div>
        <div class="review-detail-metrics">
          ${detailMetric("areas", "Area", areaName)}
          ${detailMetric("devices", "Network", item.ip_address || "Not configured")}
          ${detailMetric("activity", "Recording", titleCase(item.capture_policy.recording))}
          ${detailMetric("system", "Integrations", plural(item.integrations.length, "connection"))}
        </div>
      </header>
      <div class="device-detail-overview section">
        <section class="review-panel">
          ${sectionHeading("devices", "Identity and capture", manufacturerModel)}
          <dl class="review-fact-grid">
            <div><dt>Manufacturer</dt><dd>${escHtml(identity.manufacturer || "Not detected")}</dd></div>
            <div><dt>Model</dt><dd>${escHtml(identity.model || "Not detected")}</dd></div>
            <div><dt>Firmware</dt><dd>${escHtml(identity.firmware_version || "Not reported")}</dd></div>
            <div><dt>Episode activity window</dt><dd>${item.capture_policy.activity_window_seconds} seconds</dd></div>
            <div><dt>Event-triggered snapshots</dt><dd>${item.capture_policy.automatic_snapshots
              ? "Enabled — requests and preserves an image for each new active Event"
              : "Disabled — active Events do not request preserved images"}</dd></div>
            <div><dt>ONVIF Events</dt><dd>${item.capture_policy.onvif_events === null ? "Unavailable" : item.capture_policy.onvif_events ? "Enabled" : "Disabled"}</dd></div>
          </dl>
        </section>
        <section class="review-panel device-contributions">
          ${sectionHeading("evidence", "Contributions", "Capabilities and sources observed at runtime")}
          <div class="device-contribution-group"><small>Capabilities</small><div class="badge-cluster">${capabilityBadges(item.capabilities) || '<span class="meta">None reported</span>'}</div></div>
          <div class="device-contribution-group"><small>Observed evidence</small><div class="badge-cluster">${origins.length ? origins.map(origin => `<span class="badge badge-neutral">${escHtml(titleCase(origin))}</span>`).join("") : '<span class="meta">None yet</span>'}</div></div>
        </section>
      </div>
      <section class="review-panel section">
        ${sectionHeading("system", "Integrations", "Configured connections and current runtime health", `<span class="review-section-count">${item.integrations.length}</span>`)}
        ${renderIntegrationRows(item.integrations)}
      </section>
      ${profiles.length ? `<section class="review-panel section">
        ${sectionHeading("devices", "ONVIF media", "Discovered media profiles · read-only", `<span class="review-section-count">${profiles.length}</span>`)}
        <div class="resource-list">${profiles.map(profile => `<div class="resource-row">
          <div class="resource-main"><strong>${escHtml(profile.name || profile.token)}</strong><span>${profile.width} × ${profile.height} · ${escHtml(profile.encoding || "Unknown codec")} · Snapshot ${profile.snapshot ? "available" : "unavailable"}</span></div>
          <span class="badge ${profile.token === selectedProfile ? "badge-active" : "badge-neutral"}">${profile.token === selectedProfile ? "Selected" : "Discovered"} · read-only</span>
        </div>`).join("")}</div>
      </section>` : ""}
      <section class="review-panel section">
        ${sectionHeading("activity", "Recent activity", "Latest normalized Events from this Device", `<a class="review-section-link" href="#activity?device_id=${encodeURIComponent(item.id)}">View all Activity</a>`)}
        ${activity.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Event</th><th>Source</th><th>Time</th><th>Episode</th></tr></thead>
          <tbody>${activity.map(event => `<tr class="clickable" onclick="location='#event/${event.id}'">
            <td><strong>${escHtml(eventTitle(event))}</strong></td>
            <td>${eventSourceBadges(event)}</td><td>${fmtShort(event.timestamp)}</td>
            <td>${event.episode_id ? `<a href="#episode/${escHtml(event.episode_id)}" onclick="event.stopPropagation()">Open Episode</a>` : '<span class="meta">Unassigned</span>'}</td>
          </tr>`).join("")}</tbody>
        </table></div>` : '<div class="empty">No activity recorded</div>'}
      </section>
      ${topics.length ? `<section class="section episode-secondary review-disclosure">
        <button type="button" class="collapse-header collapsed" onclick="toggleCollapse(this)">
          <span><svg><use href="icons.svg?v=2#file"></use></svg><span><strong>Technical details</strong><small>Identifiers and ONVIF event topics</small></span></span>
          <span class="review-disclosure-count">${topics.length}</span>
        </button>
        <div class="collapse-body collapsed"><dl class="review-fact-grid technical-facts">
          <div><dt>Device ID</dt><dd><code>${escHtml(item.id)}</code></dd></div>
          <div><dt>ONVIF event topics</dt><dd>${topics.map(escHtml).join("<br>")}</dd></div>
        </dl></div>
      </section>` : ""}`);
  } catch (error) {
    showError(error.message);
  }
}

export async function areas() {
  showLoading();
  try {
    const [list, devicesList] = await Promise.all([
      api("/areas?include_disabled=true"),
      api("/devices?include_disabled=true"),
    ]);
    inventoryAreas = list;
    inventoryDevices = devicesList;
    showContent(`
      ${pageHeader({
        eyebrow: "Configure",
        title: "Areas",
        description: "Correlation boundaries that keep related activity together.",
        actions: '<a href="#devices" class="button button-ghost">Back to Devices</a><button class="button button-primary" onclick="addArea()">Create Area</button>',
      })}
      ${list.length === 0 ? `<div class="empty-state">
        <div class="empty-icon">⌂</div><h3>Create your first Area</h3>
        <p>Start with a meaningful physical boundary, such as Front entrance or Garage.</p>
        <button class="button button-primary" onclick="addArea()">Create Area</button>
      </div>` : `<div class="resource-list inventory-list">${list.map(area => {
        const members = devicesList.filter(device => device.area_id === area.id);
        return `<div class="resource-row area-row ${area.enabled ? "" : "resource-disabled"}">
          <span class="status-indicator ${area.enabled ? "online" : "idle"}"></span>
          <div class="resource-main"><strong>${area.name}</strong><span>${area.location || "No location description"}</span></div>
          <div class="resource-context">${plural(members.length, "Device")}</div>
          <div class="resource-links">${members.slice(0, 3).map(device => `<a href="#device/${device.id}">${device.name || device.id}</a>`).join("")}${members.length > 3 ? `<span>+${members.length - 3}</span>` : ""}</div>
          <div class="resource-actions"><button class="icon-button" onclick="editArea('${area.id}')">Edit</button><button class="icon-button danger-text" onclick="deleteArea('${area.id}')">Delete</button></div>
        </div>`;
      }).join("")}</div>`}`);
  } catch (error) {
    showError(error.message);
  }
}

function systemNavigation(active) {
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

function retentionStateBadge(retention) {
  const state = retention.policy_state === "disabled"
    ? "unavailable"
    : retention.policy_state === "unconfirmed"
    ? "unknown"
    : "healthy";
  return `<span class="badge badge-${state}">${escHtml(titleCase(retention.policy_state))}</span>`;
}

function renderRetentionSettings(retention) {
  return `<section class="section system-retention">
    <div class="system-retention-heading">
      <div><h3>Visual Evidence retention</h3><p>One policy covers visual material managed by this OpenEpisode installation.</p></div>
      ${retentionStateBadge(retention)}
    </div>
    <form class="system-retention-form" onsubmit="saveRetention(this); return false">
      <label class="toggle-row system-retention-toggle">
        <input name="retention_enabled" type="checkbox" value="true" ${retention.enabled ? "checked" : ""}>
        <span><strong>Automatically delete managed visual Evidence</strong><small>Disable only if another process governs deletion or your use case requires indefinite retention.</small></span>
      </label>
      <label class="field system-retention-days">
        <span>Retention period (days)</span>
        <input name="retention_days" type="number" min="1" max="3650" required value="${retention.retention_days}">
      </label>
      <button type="submit" class="button button-primary system-retention-save">Save retention</button>
      ${retention.policy_state === "unconfirmed" ? '<div class="field-span notice notice-warning"><strong>Confirmation required</strong><span>The active 30-day default has not yet been reviewed by an administrator.</span></div>' : ""}
      ${retention.policy_state === "disabled" ? '<div class="field-span notice notice-danger"><strong>Automatic deletion is disabled</strong><span>Managed visual Evidence will remain until manually removed.</span></div>' : ""}
      <div class="field-span configuration-note">${escHtml(retention.notice)}</div>
    </form>
  </section>`;
}

function renderEpisodeLifecycleSettings(settings = {}) {
  const grace = Number.isInteger(settings.quiescent_grace_seconds)
    ? settings.quiescent_grace_seconds
    : 5;
  const minimum = Number.isInteger(settings.min_quiescent_grace_seconds)
    ? settings.min_quiescent_grace_seconds
    : 0;
  const maximum = Number.isInteger(settings.max_quiescent_grace_seconds)
    ? settings.max_quiescent_grace_seconds
    : 60;
  const notice = settings.notice ||
    "After a Device activity window expires, Episode keeps the Area Episode and its recordings active for this brief continuation window. A new active Event during the window continues the same Episode. This is separate from each Device's activity window.";
  return `<section class="section system-lifecycle">
    <div class="system-lifecycle-heading">
      <div><h3>Episode settling grace</h3><p>Reduce tiny Episode splits when related activity arrives around the activity deadline.</p></div>
    </div>
    <form class="system-lifecycle-form" onsubmit="saveEpisodeLifecycle(this); return false">
      <label class="field system-lifecycle-window">
        <span>Continuation window (seconds)</span>
        <input name="quiescent_grace_seconds" type="number" min="${minimum}" max="${maximum}" required value="${grace}">
        <small>Recordings stay active during this brief Area-level window after the Device activity window expires. A new active Event continues the same Episode.</small>
      </label>
      <button type="submit" class="button button-primary system-lifecycle-save">Save Episode setting</button>
      <div class="field-span configuration-note">${escHtml(notice)}</div>
    </form>
  </section>`;
}

function systemOverview(diagnostics, services, filesystemLabel, installationSettings) {
  const status = diagnostics.status;
  const savedExternalUrl = installationSettings?.external_url || "";
  const suggestedUrl = suggestedExternalUrl();
  const externalUrl = savedExternalUrl || suggestedUrl;
  return `<dl class="detail-facts section system-summary-facts">
      <div><dt>Version</dt><dd>v${status.version}</dd></div>
      <div><dt>Active recordings</dt><dd>${status.active_recordings}</dd></div>
      <div><dt>Integrations</dt><dd>${status.integrations.healthy}/${status.integrations.total} healthy</dd></div>
      <div><dt>Episode data</dt><dd>${fmtBytes(diagnostics.storage.data_bytes)}</dd></div>
      <div><dt>Filesystem available</dt><dd>${filesystemLabel}</dd></div>
    </dl>
    <section class="section system-installation-settings">
      <div class="system-section-heading"><div><h3>External Episode URL</h3><p>The address operators use to open this installation from notifications and integrations.</p></div></div>
      <form class="system-installation-form" onsubmit="saveInstallationSettings(this); return false">
        <label class="field">
          <span>Installation address</span>
          <input name="external_url" type="url" autocomplete="url" inputmode="url" value="${escHtml(externalUrl)}" placeholder="https://episode.example">
          <small>${savedExternalUrl ? "Saved globally. Delete the value to remove links from outbound notifications." : suggestedUrl ? "Suggested from this browser. Review and save it before Episode uses it." : "Optional. Enter an absolute HTTP(S) URL if outbound notifications should link back here."}</small>
        </label>
        <button type="submit" class="button button-primary">Save address</button>
      </form>
    </section>
    <section class="section system-core-services">
      <div class="system-section-heading"><div><h3>Core services</h3><p>The components required to receive Events and preserve Evidence.</p></div></div>
      ${renderIntegrationRows(services)}
    </section>`;
}

export async function systemStatus(requestedSection = "overview") {
  const validSections = new Set(["overview", "recordings", "integrations", "storage"]);
  const section = validSections.has(requestedSection) ? requestedSection : "overview";
  showLoading();
  try {
    const [diagnostics, retention, episodeSettings, installationSettings] = await Promise.all([
      api("/diagnostics"),
      api("/settings/retention"),
      api("/settings/episode"),
      api("/settings/installation"),
    ]);
    const status = diagnostics.status;
    const recorder = diagnostics.services.find(service => service.id === "recorder") || {};
    const filesystemTotal = diagnostics.storage.filesystem_total_bytes;
    const filesystemFree = diagnostics.storage.filesystem_free_bytes;
    const filesystemLabel = filesystemFree == null
      ? "Unavailable"
      : filesystemTotal
      ? `${fmtBytes(filesystemFree)} free of ${fmtBytes(filesystemTotal)} (${Math.round(filesystemFree / filesystemTotal * 100)}%)`
      : fmtBytes(filesystemFree);
    const services = diagnostics.services.map(service => ({
      ...service,
      type: "service",
      capabilities: [],
      details: service.metrics,
    }));
    const descriptions = {
      overview: "Runtime health and the areas that may need attention.",
      recordings: "Current capture progress, recovery, and the Episode activity lifecycle.",
      integrations: "Runtime health for connectors, plugins, and Device connections.",
      storage: "Filesystem capacity and the visual Evidence retention policy.",
    };
    let content;
    if (section === "recordings") {
      content = renderRecordingOperations(
        diagnostics.recordings,
        recorder.metrics,
        diagnostics.recording_issues,
      ) + renderEpisodeLifecycleSettings(episodeSettings || {});
    } else if (section === "integrations") {
      content = `<section class="section system-section">
        <div class="system-section-heading"><div><h3>Integrations</h3><p>Open technical details only when diagnosing a connection.</p></div><span class="badge badge-neutral">${diagnostics.integrations.length} configured</span></div>
        ${renderIntegrationRows(diagnostics.integrations, true)}
      </section>`;
    } else if (section === "storage") {
      content = `<dl class="detail-facts section system-storage-facts">
          <div><dt>Episode data</dt><dd>${fmtBytes(diagnostics.storage.data_bytes)}</dd></div>
          <div><dt>Filesystem available</dt><dd>${filesystemLabel}</dd></div>
        </dl>
        ${renderRetentionSettings(retention)}`;
    } else {
      content = systemOverview(diagnostics, services, filesystemLabel, installationSettings);
    }
    showContent(`
      ${pageHeader({
        eyebrow: "Operations",
        title: section === "overview" ? "System" : `System · ${titleCase(section)}`,
        description: descriptions[section],
        status: operationalBadge(status.state),
        actions: `<a class="button button-ghost" href="${API}/diagnostics/export" download>Download diagnostics</a>`,
      })}
      <div class="system-layout">
        ${systemNavigation(section)}
        <div class="system-content">${content}</div>
      </div>`);
  } catch (error) {
    showError(error.message);
  }
}
