import { API, api } from "./api.js?v=3";
import {
  detailMetric,
  eventSourceBadges,
  pageHeader,
  sectionHeading,
} from "./components.js?v=4";
import { notify } from "./dialogs.js?v=1";
import { escHtml } from "./dom.js";
import { fmtBytes, fmtShort, plural, titleCase } from "./format.js?v=4";
import { eventTitle } from "./timeline.js?v=5";
import {
  confirmAreaDelete,
  confirmDeviceDelete,
  openAreaEditor,
  openDeviceEditor,
} from "./inventory.js?v=6";
import { showContent, showError, showLoading } from "./view.js?v=1";

let inventoryAreas = [];
let inventoryDevices = [];

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
    .map(capability => `<span class="badge badge-neutral">${titleCase(capability)}</span>`)
    .join(" ");
}

function integrationBadges(integrations) {
  return (integrations || [])
    .map(integration => `<span class="badge badge-${integration.state}">${titleCase(integration.type)}</span>`)
    .join(" ");
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
          <strong>${integration.name}</strong>
          <span>${integration.summary || titleCase(integration.type)}</span>
          <div class="badge-cluster">${capabilityBadges(integration.capabilities)}</div>
        </div>
        ${operationalBadge(integration.state)}
        ${showDetails && details.length ? `<details class="diagnostic-details">
          <summary>Technical details</summary>
          <pre>${escHtml(JSON.stringify(integration.details, null, 2))}</pre>
        </details>` : ""}
      </div>`;
    }).join("")}
  </div>`;
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
            <div><dt>Automatic snapshots</dt><dd>${item.capture_policy.automatic_snapshots ? "Enabled" : "Disabled"}</dd></div>
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

export async function systemStatus() {
  showLoading();
  try {
    const diagnostics = await api("/diagnostics");
    const status = diagnostics.status;
    const services = diagnostics.services.map(service => ({
      ...service,
      type: "service",
      capabilities: [],
      details: service.metrics,
    }));
    showContent(`
      ${pageHeader({
        eyebrow: "Operations",
        title: "System",
        description: "Runtime health, core services, and integration diagnostics.",
        status: operationalBadge(status.state),
        actions: `<a class="button button-ghost" href="${API}/diagnostics/export" download>Download diagnostics</a>`,
      })}
      <dl class="detail-facts section">
        <div><dt>Version</dt><dd>v${status.version}</dd></div>
        <div><dt>Active recordings</dt><dd>${status.active_recordings}</dd></div>
        <div><dt>Integrations</dt><dd>${status.integrations.healthy}/${status.integrations.total} healthy</dd></div>
        <div><dt>Episode data</dt><dd>${fmtBytes(diagnostics.storage.data_bytes)}</dd></div>
        <div><dt>Filesystem available</dt><dd>${fmtBytes(diagnostics.storage.filesystem_free_bytes)}</dd></div>
      </dl>
      <div class="section"><h3>Core services</h3>${renderIntegrationRows(services, true)}</div>
      <div class="section"><h3>Integrations (${diagnostics.integrations.length})</h3>${renderIntegrationRows(diagnostics.integrations, true)}</div>`);
  } catch (error) {
    showError(error.message);
  }
}
