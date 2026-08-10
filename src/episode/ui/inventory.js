import { apiRequest } from "./api.js?v=2";
import { closeDialog, confirmDialog, notify, openDialog } from "./dialogs.js?v=1";
import { escHtml } from "./dom.js";
import { titleCase } from "./format.js";

const selected = value => value ? " selected" : "";
const checked = value => value ? " checked" : "";
const numberOrNull = value => value ? Number(value) : null;

function decodeApiText(value) {
  const element = document.createElement("textarea");
  element.innerHTML = String(value ?? "");
  return element.value;
}

function safeValue(value) {
  return escHtml(decodeApiText(value));
}

function field(formData, name) {
  return String(formData.get(name) || "").trim();
}

function isChecked(formData, name) {
  return formData.get(name) === "on";
}

export function openAreaEditor(area, onSaved) {
  const editing = Boolean(area);
  openDialog({
    title: editing ? "Edit Area" : "Create an Area",
    subtitle: "Areas group related activity and define the current correlation boundary.",
    content: `
      <div class="form-grid">
        <label class="field field-span"><span>Name</span><input name="name" required maxlength="80" value="${safeValue(area?.name || "")}" placeholder="Front entrance"></label>
        <label class="field field-span"><span>Location <small>optional</small></span><input name="location" maxlength="200" value="${safeValue(area?.location || "")}" placeholder="Main gate and approach"></label>
        ${editing ? `<label class="toggle-row field-span"><input type="checkbox" name="enabled"${checked(area.enabled)}><span><strong>Active</strong><small>Disabled Areas remain available to historical Episodes.</small></span></label>` : `
        <details class="form-advanced field-span"><summary>Advanced</summary>
          <label class="field"><span>Area ID <small>generated if empty</small></span><input name="id" maxlength="64" pattern="[a-z0-9][a-z0-9_-]*" placeholder="front-entrance"></label>
        </details>`}
      </div>`,
    submitLabel: editing ? "Save Area" : "Create Area",
    onSubmit: async data => {
      const body = {
        name: field(data, "name"),
        location: field(data, "location"),
        ...(editing ? { enabled: isChecked(data, "enabled") } : { id: field(data, "id") || null }),
      };
      await apiRequest(editing ? `/areas/${encodeURIComponent(area.id)}` : "/areas", {
        method: editing ? "PUT" : "POST",
        body,
      });
      closeDialog();
      notify(editing ? "Area updated" : "Area created");
      await onSaved();
    },
  });
}

function deviceDefaults(device) {
  const config = device?.configuration || {};
  return {
    video: { enabled: true, manual_endpoint: false, protocol: "rtsp", port: 554, path: "/Streaming/Channels/101", recording_mode: "on_event", ...(config.video || {}) },
    onvif: { enabled: true, protocol: "http", port: 80, path: "/onvif/device_service", auth_mode: "digest_wsse", events_enabled: false, ...(config.onvif || {}) },
    isapi: { enabled: false, protocol: "http", port: 80, path: "/ISAPI/Event/notification/alertStream", ignore_events: ["videoloss", "illaccess"], ...(config.isapi || {}) },
    sdk: { enabled: false, port: 8000, ...(config.hikvision_sdk || {}) },
  };
}

function integrationToggle(name, title, description, enabled, body, attributes = "") {
  return `<fieldset class="integration-option" data-integration="${name}" ${attributes}>
    <label class="toggle-row integration-toggle">
      <input type="checkbox" name="${name}_enabled"${checked(enabled)}>
      <span><strong>${title}</strong><small>${description}</small></span>
    </label>
    <div class="integration-fields">${body}</div>
  </fieldset>`;
}

const validationIntegrations = [
  ["onvif", "ONVIF"],
  ["isapi", "ISAPI"],
  ["hikvision_sdk", "HCNetSDK"],
];

function renderValidationResults(results = {}) {
  return validationIntegrations.map(([key, label]) => {
    const result = results[key] || {
      status: "not_validated",
      summary: "Support has not been validated",
      capabilities: [],
    };
    const capabilities = (result.capabilities || [])
      .map(capability => `<span>${titleCase(capability)}</span>`).join("");
    return `<div class="validation-result validation-${result.status}">
      <span class="validation-dot"></span>
      <div><strong>${label}</strong><small>${result.summary}</small>
        ${capabilities ? `<div class="validation-capabilities">${capabilities}</div>` : ""}
      </div>
      <span class="validation-status">${titleCase(result.status)}</span>
    </div>`;
  }).join("");
}

function devicePayload(data, editing, device) {
  return {
    id: editing ? device.id : field(data, "id") || null,
    name: field(data, "name"),
    device_type: field(data, "device_type"),
    area_id: field(data, "area_id"),
    enabled: editing ? isChecked(data, "enabled") : true,
    ip_address: field(data, "ip_address"),
    username: field(data, "username") || null,
    password: field(data, "password") || null,
    clear_credentials: isChecked(data, "clear_credentials"),
    doorbell: field(data, "device_type") === "doorbell",
    video: {
      enabled: isChecked(data, "video_enabled"),
      manual_endpoint: isChecked(data, "manual_video_endpoint"),
      protocol: field(data, "video_protocol"),
      port: numberOrNull(field(data, "video_port")),
      path: field(data, "video_path"),
      recording_mode: field(data, "recording_mode"),
    },
    onvif: {
      enabled: isChecked(data, "onvif_enabled"),
      protocol: field(data, "onvif_protocol"),
      port: numberOrNull(field(data, "onvif_port")),
      path: field(data, "onvif_path"),
      auth_mode: field(data, "onvif_auth_mode"),
      events_enabled: isChecked(data, "onvif_events_enabled"),
    },
    isapi: {
      enabled: isChecked(data, "isapi_enabled"),
      protocol: field(data, "isapi_protocol"),
      port: numberOrNull(field(data, "isapi_port")),
      path: field(data, "isapi_path"),
      ignore_events: field(data, "isapi_ignore_events")
        .split(",").map(value => value.trim()).filter(Boolean),
    },
    hikvision_sdk: {
      enabled: isChecked(data, "hikvision_sdk_enabled"),
      port: Number(field(data, "sdk_port")),
    },
  };
}

export function openDeviceEditor(device, areas, onSaved) {
  const editing = Boolean(device);
  const values = deviceDefaults(device);
  const physicalTypes = ["camera", "doorbell", "alarm_panel", "sensor", "other"];
  const deviceType = device?.configuration?.doorbell
    ? "doorbell"
    : physicalTypes.includes(device?.device_type) ? device.device_type : "camera";
  const credentialHint = editing && device.configuration?.password_configured
    ? "Stored securely — leave blank to keep"
    : "Camera password";
  const areaOptions = areas.filter(area => area.enabled || area.id === device?.area_id)
    .map(area => `<option value="${area.id}"${selected(area.id === device?.area_id)}>${area.name}${area.enabled ? "" : " (disabled)"}</option>`).join("");

  const overlay = openDialog({
    title: editing ? "Edit Device" : "Add a Device",
    subtitle: "Start with ONVIF. Vendor integrations can complement it when useful.",
    wide: true,
    content: `
      <div class="form-section"><h3>Device</h3><div class="form-grid">
        <label class="field"><span>Name</span><input name="name" required maxlength="80" value="${safeValue(device?.name || "")}" placeholder="Front door camera"></label>
        <label class="field"><span>Area</span><select name="area_id" required><option value="">Choose an Area</option>${areaOptions}</select></label>
        <label class="field"><span>Network address</span><input name="ip_address" required inputmode="decimal" value="${safeValue(device?.ip_address || "")}" placeholder="192.168.1.100"></label>
        <label class="field"><span>Device type</span><select name="device_type" data-device-type>
          ${physicalTypes.map(type => `<option value="${type}"${selected(deviceType === type)}>${titleCase(type)}</option>`).join("")}
        </select><small>Physical role only. Manufacturer and vendor integrations are kept separate.</small></label>
        ${editing ? `<label class="toggle-row field-span"><input type="checkbox" name="enabled"${checked(device.enabled)}><span><strong>Active</strong><small>Disable without losing historical relationships.</small></span></label>` : ""}
      </div></div>

      <div class="form-section"><h3>Credentials</h3><div class="form-grid">
        <label class="field"><span>Username</span><input name="username" autocomplete="off" placeholder="${editing && device.configuration?.username_configured ? "Stored — leave blank to keep" : "admin"}"></label>
        <label class="field"><span>Password</span><input type="password" name="password" autocomplete="new-password" placeholder="${credentialHint}"></label>
        ${editing && (device.configuration?.username_configured || device.configuration?.password_configured) ? `<label class="toggle-row field-span"><input type="checkbox" name="clear_credentials"><span><strong>Clear stored credentials</strong><small>Use only for devices that allow anonymous access.</small></span></label>` : ""}
      </div></div>

      <div class="form-section"><h3>Capture</h3>
        ${integrationToggle("video", "Video recording", "Capture this Device when its Area is active.", values.video.enabled, `
          <div class="form-grid">
            <label class="field"><span>Recording behavior</span><select name="recording_mode">
              <option value="disabled"${selected(values.video.recording_mode === "disabled")}>Do not record</option>
              <option value="on_event"${selected(values.video.recording_mode === "on_event")}>Own Events only</option>
              <option value="on_episode"${selected(values.video.recording_mode === "on_episode")}>Any Episode in this Area</option>
            </select></label>
          </div>`) }
      </div>

      <div class="form-section">
        <div class="form-section-heading"><h3>Integrations</h3><p>Configured connections are shown as active in the Device overview after restart.</p></div>
        <div class="integration-stack">
          ${integrationToggle("onvif", "ONVIF", "Standards-based discovery, media, and optional Events.", values.onvif.enabled, `
            <label class="toggle-row"><input type="checkbox" name="onvif_events_enabled"${checked(values.onvif.events_enabled)}><span><strong>Receive ONVIF Events</strong><small>Disabled by default to avoid noisy motion state changes.</small></span></label>`) }
          <div class="integration-group-label"><strong>Hikvision enhancements</strong><span>Optional vendor connections that complement ONVIF.</span></div>
          ${integrationToggle("isapi", "ISAPI Event stream", "Rich motion and classification Events. It is currently active when this switch is on.", values.isapi.enabled, "")}
          ${integrationToggle("hikvision_sdk", "HCNetSDK", "Native doorbell callbacks. General camera use is not validated in this alpha.", values.sdk.enabled, `
            <div class="role-guidance">Available for Doorbell Devices. Camera support remains a later validation task.</div>`) }
        </div>
        <div class="validation-panel">
          <div class="validation-heading">
            <div><strong>Device validation</strong><span>Checks support without enabling integrations.</span></div>
            <button type="button" class="button button-ghost" data-validate-device>Validate and discover</button>
          </div>
          <div class="validation-results" data-validation-results>
            ${renderValidationResults(device?.integration_support)}
          </div>
        </div>
      </div>

      <details class="form-advanced"><summary>Manual connection overrides</summary>
        <p class="configuration-note">These values are configured endpoints or fallbacks. Runtime-discovered manufacturer, model, firmware, media profiles, and selected profile are read-only on the Device page.</p>
        <div class="advanced-grid">
          <fieldset data-manual-video>
            <legend>RTSP fallback</legend>
            <label class="toggle-row"><input type="checkbox" name="manual_video_endpoint"${checked(values.video.manual_endpoint)}><span><strong>Use manual endpoint</strong><small>ONVIF-discovered media is preferred when available.</small></span></label>
            <div class="manual-endpoint-fields">
              <label class="field"><span>Protocol</span><input name="video_protocol" value="${safeValue(values.video.protocol)}"></label>
              <label class="field"><span>Port</span><input name="video_port" type="number" min="1" max="65535" value="${values.video.port || ""}"></label>
              <label class="field"><span>Path</span><input name="video_path" value="${safeValue(values.video.path)}"></label>
            </div>
          </fieldset>
          <fieldset><legend>ONVIF service</legend><label class="field"><span>Protocol</span><input name="onvif_protocol" value="${safeValue(values.onvif.protocol)}"></label><label class="field"><span>Port</span><input name="onvif_port" type="number" min="1" max="65535" value="${values.onvif.port || ""}"></label><label class="field"><span>Path</span><input name="onvif_path" value="${safeValue(values.onvif.path)}"></label><label class="field"><span>Authentication</span><select name="onvif_auth_mode"><option value="digest_wsse"${selected(values.onvif.auth_mode === "digest_wsse")}>Digest + WS-Username Token</option><option value="digest"${selected(values.onvif.auth_mode === "digest")}>Digest only</option></select></label></fieldset>
          <fieldset><legend>ISAPI endpoint</legend><label class="field"><span>Protocol</span><input name="isapi_protocol" value="${safeValue(values.isapi.protocol)}"></label><label class="field"><span>Port</span><input name="isapi_port" type="number" min="1" max="65535" value="${values.isapi.port || ""}"></label><label class="field"><span>Path</span><input name="isapi_path" value="${safeValue(values.isapi.path)}"></label><label class="field"><span>Ignored Events</span><input name="isapi_ignore_events" value="${safeValue((values.isapi.ignore_events || []).join(", "))}"></label></fieldset>
          <fieldset><legend>HCNetSDK login</legend><label class="field"><span>SDK port</span><input name="sdk_port" type="number" min="1" max="65535" value="${values.sdk.port}"></label></fieldset>
        </div>
        ${editing ? "" : `<label class="field"><span>Device ID <small>generated if empty</small></span><input name="id" maxlength="64" pattern="[a-z0-9][a-z0-9_-]*" placeholder="front-door-camera"></label>`}
      </details>`,
    submitLabel: editing ? "Save Device" : "Add Device",
    onSubmit: async data => {
      const payload = devicePayload(data, editing, device);
      await apiRequest(editing ? `/devices/${encodeURIComponent(device.id)}` : "/devices", {
        method: editing ? "PUT" : "POST", body: payload,
      });
      closeDialog();
      notify(editing ? "Device updated · restart Episode to apply integrations" : "Device added · restart Episode to activate integrations", "warning");
      await onSaved();
    },
  });

  let validationResults = { ...(device?.integration_support || {}) };
  const updateIntegration = option => {
    const toggle = option.querySelector(".integration-toggle input");
    option.classList.toggle("integration-disabled", !toggle.checked);
  };
  overlay.querySelectorAll("[data-integration]").forEach(option => {
    const toggle = option.querySelector(".integration-toggle input");
    toggle.addEventListener("change", () => updateIntegration(option));
    updateIntegration(option);
  });

  const typeSelect = overlay.querySelector("[data-device-type]");
  const sdkOption = overlay.querySelector('[data-integration="hikvision_sdk"]');
  const sdkToggle = sdkOption.querySelector(".integration-toggle input");
  const sdkWasConfigured = values.sdk.enabled;
  const updateDeviceRole = () => {
    const roleAvailable = typeSelect.value === "doorbell" || sdkWasConfigured;
    const supported = validationResults.hikvision_sdk?.status !== "unsupported";
    const available = roleAvailable && supported;
    sdkToggle.disabled = !available;
    if (!available) sdkToggle.checked = false;
    sdkOption.classList.toggle("integration-role-unavailable", !available);
    updateIntegration(sdkOption);
  };

  const resultsElement = overlay.querySelector("[data-validation-results]");
  const applyValidation = () => {
    resultsElement.innerHTML = renderValidationResults(validationResults);
    for (const integration of ["onvif", "isapi"]) {
      const option = overlay.querySelector(`[data-integration="${integration}"]`);
      const toggle = option.querySelector(".integration-toggle input");
      const unsupported = validationResults[integration]?.status === "unsupported";
      toggle.disabled = unsupported;
      if (unsupported) toggle.checked = false;
      option.classList.toggle("integration-support-unsupported", unsupported);
      updateIntegration(option);
    }
    updateDeviceRole();
  };
  typeSelect.addEventListener("change", updateDeviceRole);
  applyValidation();

  const form = overlay.querySelector("form");
  const validateButton = overlay.querySelector("[data-validate-device]");
  validateButton.addEventListener("click", async () => {
    if (!form.reportValidity()) return;
    const originalLabel = validateButton.textContent;
    validateButton.disabled = true;
    validateButton.textContent = "Validating…";
    try {
      const response = await apiRequest("/devices/validate", {
        method: "POST",
        body: devicePayload(new FormData(form), editing, device),
      });
      validationResults = response.results;
      applyValidation();
      notify("Device validation completed");
    } catch (error) {
      notify(`Validation failed: ${error.message}`, "warning");
    } finally {
      validateButton.disabled = false;
      validateButton.textContent = originalLabel;
    }
  });

  const manualVideo = overlay.querySelector("[data-manual-video]");
  const manualVideoToggle = manualVideo.querySelector('[name="manual_video_endpoint"]');
  const updateManualVideo = () => manualVideo.classList.toggle("manual-endpoint-disabled", !manualVideoToggle.checked);
  manualVideoToggle.addEventListener("change", updateManualVideo);
  updateManualVideo();
}

export function confirmAreaDelete(area, onDeleted) {
  confirmDialog({
    title: `Delete ${decodeApiText(area.name)}?`,
    message: "Only unused Areas can be deleted. Areas with Devices or incident history must be disabled.",
    onConfirm: async () => {
      await apiRequest(`/areas/${encodeURIComponent(area.id)}`, { method: "DELETE" });
      closeDialog(); notify("Area deleted"); await onDeleted();
    },
  });
}

export function confirmDeviceDelete(device, onDeleted) {
  confirmDialog({
    title: `Delete ${decodeApiText(device.name)}?`,
    message: "Only Devices without incident history can be deleted. Otherwise disable the Device to preserve its relationships.",
    onConfirm: async () => {
      await apiRequest(`/devices/${encodeURIComponent(device.id)}`, { method: "DELETE" });
      closeDialog(); notify("Device deleted · restart Episode to stop integrations", "warning"); await onDeleted();
    },
  });
}
