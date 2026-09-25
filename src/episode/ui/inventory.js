import { apiRequest } from "./api.js?v=3";
import { closeDialog, confirmDialog, notify, openDialog } from "./dialogs.js?v=1";
import { escHtml } from "./dom.js";
import {
  deviceEventFilterSelect,
  EVENT_CLASS_LABELS,
  normalizeEventFilter,
  resolveEventFilter,
  wireEventFilterSummary,
} from "./event-filter.js?v=3";
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
  const isNew = !device;
  const config = device?.configuration || {};
  const policy = config.episode_policy || {};
  return {
    setupState: device?.setup_state || config.setup_state || "ready",
    manufacturer: device?.identity?.manufacturer || config.manufacturer || "",
    manufacturerOverride: config.manufacturer || "",
    episodePolicy: {
      activity_window_seconds: policy.activity_window_seconds ?? 30,
      // `null`/absent stays inherit; an array (including an empty one) is the
      // camera's own decision.
      event_filter: Array.isArray(policy.event_filter) ? policy.event_filter : null,
    },
    video: { enabled: !isNew, manual_endpoint: false, protocol: "rtsp", port: 554, path: "/Streaming/Channels/101", recording_mode: "on_event", ...(config.video || {}) },
    onvif: { enabled: !isNew, protocol: "http", port: 80, path: "/onvif/device_service", auth_mode: "digest_wsse", events_enabled: false, relaxed_xml: false, ...(config.onvif || {}) },
    // Nothing is preset: `ignore_events` stops interpretation before a canonical
    // Event exists, and class filtering now owns suppression after one exists. An
    // operator who still needs the plugin-level lever sets it deliberately.
    isapi: { enabled: false, protocol: "http", port: 80, path: "/ISAPI/Event/notification/alertStream", ignore_events: [], ...(config.isapi || {}) },
    sdk: { enabled: false, port: 8000, ...(config.hikvision_sdk || {}) },
    reolink: { enabled: false, host: "", port: 9000, media_enabled: false, events_enabled: false, native_video: false, preview_variant: "main", preview_timeout: null, ...(config.reolink || {}) },
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
  ["isapi", "Hikvision ISAPI"],
  ["hikvision_sdk", "Hikvision HCNetSDK"],
  ["reolink", "Reolink API"],
];
const validationStatuses = new Set([
  "supported",
  "unsupported",
  "authentication_failed",
  "unreachable",
  "unavailable",
  "not_validated",
]);

const integrationIds = {
  onvif: "onvif",
  isapi: "hikvision-isapi",
  hikvision_sdk: "hikvision-sdk",
  reolink: "reolink",
};

function integrationOptionAttributes(name) {
  const group = name === "reolink" ? "reolink" : name === "onvif" ? "generic" : "hikvision";
  return `data-integration-id="${safeValue(integrationIds[name] || name)}" data-vendor="${group}"`;
}

function normalizedManufacturer(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, " ");
}

const knownManufacturerAliases = {
  hikvision: new Set([
    "hikvision",
    "hikvision digital technology",
    "hikvision digital technology co ltd",
  ]),
  reolink: new Set(["reolink", "reolink innovation", "reolink innovation limited"]),
};

function canonicalManufacturer(value) {
  const normalized = normalizedManufacturer(value);
  for (const [canonical, aliases] of Object.entries(knownManufacturerAliases)) {
    if ([...aliases].some(alias => normalized === alias || normalized.startsWith(`${alias} `))) {
      return canonical;
    }
  }
  return normalized;
}

function manufacturerMatches(value, expected) {
  const actual = canonicalManufacturer(value);
  const target = canonicalManufacturer(expected);
  return Boolean(actual && target && actual === target);
}

function catalogEntryFor(catalog, type) {
  const id = integrationIds[type] || type;
  return (catalog || []).find(entry => entry.type === type || entry.id === id);
}

function catalogEntryMatches(entry, manufacturer, deviceType) {
  if (!entry || entry.available === false) return false;
  if (entry.device_types?.length && !entry.device_types.includes(deviceType)) return false;
  if (entry.manufacturer_scope_kind === "universal") return true;
  if (entry.manufacturer_scope_kind !== "targeted" || !manufacturer) return false;
  return (entry.manufacturer_scope || []).some(value => manufacturerMatches(manufacturer, value));
}

export function applicableValidationKeys({
  catalog = [],
  configured = [],
  deviceType = "camera",
  manufacturer = "",
  selected = [],
} = {}) {
  const configuredKeys = new Set(configured);
  const selectedKeys = new Set(selected);
  return new Set(
    validationIntegrations
      .filter(([key]) => key === "onvif"
        || configuredKeys.has(key)
        || selectedKeys.has(key)
        || catalogEntryMatches(catalogEntryFor(catalog, key), manufacturer, deviceType))
      .map(([key]) => key),
  );
}

function validationManufacturer(results = {}) {
  for (const result of Object.values(results)) {
    const manufacturer = result?.details?.manufacturer || result?.manufacturer;
    if (manufacturer) return String(manufacturer);
  }
  return "";
}

export function preferredManufacturer(onvif, selected, choiceTouched, savedOverride, savedIdentity) {
  const discovered = onvif?.status === "supported"
    ? validationManufacturer({ onvif })
    : "";
  if (discovered) return discovered;
  if (choiceTouched) return selected;
  return savedOverride || savedIdentity;
}

function validationDetails(result = {}) {
  return Object.entries(result.details || {})
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${titleCase(key.replaceAll("_", " "))}: ${value}`)
    .join(" · ");
}

function renderValidationResults(results = {}, visibleKeys = null) {
  const keys = visibleKeys
    ? validationIntegrations.filter(([key]) => visibleKeys.has(key))
    : validationIntegrations.filter(([key]) => key === "onvif" || results[key]);
  return keys.map(([key, label]) => {
    const result = results[key] || {
      status: "not_validated",
      summary: "Support has not been validated",
      capabilities: [],
    };
    const status = validationStatuses.has(result.status) ? result.status : "unavailable";
    const capabilities = (result.capabilities || [])
      .map(capability => `<span>${safeValue(titleCase(capability))}</span>`).join("");
    const details = validationDetails(result);
    return `<div class="validation-result validation-${status}">
      <span class="validation-dot"></span>
      <div><strong>${safeValue(label)}</strong><small>${safeValue(result.summary)}</small>
        ${details ? `<small>${safeValue(details)}</small>` : ""}
        ${capabilities ? `<div class="validation-capabilities">${capabilities}</div>` : ""}
      </div>
      <span class="validation-status">${safeValue(titleCase(status))}</span>
    </div>`;
  }).join("");
}

function devicePayload(data, editing, device) {
  const manualManufacturer = field(data, "manufacturer");
  const manufacturerExplicit = field(data, "manufacturer_explicit") === "true";
  return {
    id: editing ? device.id : field(data, "id") || null,
    name: field(data, "name"),
    device_type: field(data, "device_type"),
    area_id: field(data, "area_id"),
    enabled: editing ? isChecked(data, "enabled") : true,
    setup_state: isChecked(data, "save_for_later") ? "needs_setup" : "ready",
    ...(manufacturerExplicit ? { manufacturer: manualManufacturer || null } : {}),
    ip_address: field(data, "ip_address"),
    username: field(data, "username") || null,
    password: field(data, "password") || null,
    clear_credentials: isChecked(data, "clear_credentials"),
    episode_policy: {
      activity_window_seconds: Number(field(data, "activity_window_seconds")),
      // null means inherit; an array is the camera's own selector.
      event_filter: resolveEventFilter(data, "event_filter", "device"),
    },
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
      relaxed_xml: isChecked(data, "onvif_relaxed_xml"),
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
    reolink: {
      enabled: isChecked(data, "reolink_enabled"),
      host: field(data, "reolink_host"),
      port: numberOrNull(field(data, "reolink_port")),
      media_enabled: isChecked(data, "reolink_media_enabled"),
      events_enabled: isChecked(data, "reolink_events_enabled"),
      native_video: isChecked(data, "reolink_native_video"),
      preview_variant: field(data, "reolink_preview_variant") || "main",
      preview_timeout: numberOrNull(field(data, "reolink_preview_timeout")),
    },
  };
}

export function openDeviceEditor(device, areas, onSaved) {
  const editing = Boolean(device);
  const values = deviceDefaults(device);
  const configuredIntegrations = new Set(
    Object.entries({
      onvif: values.onvif.enabled,
      isapi: values.isapi.enabled,
      hikvision_sdk: values.sdk.enabled,
      reolink: values.reolink.enabled,
    }).filter(([, enabled]) => enabled).map(([name]) => name),
  );
  const physicalTypes = ["camera", "doorbell", "alarm_panel", "sensor", "other"];
  const deviceType = physicalTypes.includes(device?.device_type) ? device.device_type : "camera";
  const credentialHint = editing && device.configuration?.password_configured
    ? "Stored securely — leave blank to keep"
    : "Camera password";
  const areaOptions = areas.filter(area => area.enabled || area.id === device?.area_id)
    .map(area => `<option value="${safeValue(area.id)}"${selected(area.id === device?.area_id)}>${safeValue(area.name)}${area.enabled ? "" : " (disabled)"}</option>`).join("");
  const setupState = values.setupState;
  const discoveredManufacturer = device?.identity?.manufacturer || "";
  const manualManufacturerValue = values.manufacturerOverride;
  const initialValidationKeys = applicableValidationKeys({
    configured: configuredIntegrations,
    deviceType,
    manufacturer: discoveredManufacturer || manualManufacturerValue,
  });
  const setupDescription = setupState === "needs_setup"
    ? "This Device is saved for later and will not open Episodes or join new captures."
    : "Ready Devices can participate in new activity; configure an Event source or a recording contribution.";

  const overlay = openDialog({
    title: editing ? "Edit Device" : "Add a Device",
    subtitle: "Start with ONVIF. Vendor integrations can complement it when useful.",
    wide: true,
    content: `
      <div class="form-section"><h3>Device</h3><div class="form-grid">
        <label class="field"><span>Name</span><input name="name" required maxlength="80" value="${safeValue(device?.name || "")}" placeholder="Front door camera"></label>
        <label class="field"><span>Area</span><select name="area_id" required><option value="">Choose an Area</option>${areaOptions}</select></label>
        <label class="field"><span>Network address</span><input name="ip_address" inputmode="decimal" value="${safeValue(device?.ip_address || "")}" placeholder="Optional for externally driven sensors"><small>Required only when Episode connects directly to this Device.</small></label>
        <label class="field"><span>Device type</span><select name="device_type" data-device-type>
          ${physicalTypes.map(type => `<option value="${type}"${selected(deviceType === type)}>${titleCase(type)}</option>`).join("")}
        </select><small>Physical role only. Manufacturer and vendor integrations are kept separate.</small></label>
        ${editing ? `<label class="toggle-row field-span"><input type="checkbox" name="enabled"${checked(device.enabled)}><span><strong>Active</strong><small>Disable without losing historical relationships.</small></span></label>` : ""}
        <label class="toggle-row field-span"><input type="checkbox" name="save_for_later"${checked(setupState === "needs_setup")}><span><strong>Save for later</strong><small>${safeValue(setupDescription)} It will remain visible for configuration but will not participate in new activity.</small></span></label>
      </div></div>

      <div class="form-section"><h3>Credentials</h3><div class="form-grid">
        <label class="field"><span>Username</span><input name="username" autocomplete="off" placeholder="${editing && device.configuration?.username_configured ? "Stored — leave blank to keep" : "admin"}"></label>
        <label class="field"><span>Password</span><input type="password" name="password" autocomplete="new-password" placeholder="${credentialHint}"></label>
        ${editing && (device.configuration?.username_configured || device.configuration?.password_configured) ? `<label class="toggle-row field-span"><input type="checkbox" name="clear_credentials"><span><strong>Clear stored credentials</strong><small>Use only for devices that allow anonymous access.</small></span></label>` : ""}
      </div></div>

      <div class="form-section discovery-section">
        <div class="form-section-heading"><h3>Discover this Device</h3><p>Episode starts with one bounded ONVIF check. It reports identity and capabilities without activating vendor integrations.</p></div>
        <div class="discovery-summary" data-discovery-summary>
          ${discoveredManufacturer ? `<strong>Manufacturer: ${safeValue(discoveredManufacturer)}</strong>` : `<span>Run discovery to see which integrations can safely be offered.</span>`}
          <span data-discovery-detail>${editing && device?.identity?.model ? `Model: ${safeValue(device.identity.model)}` : ""}</span>
        </div>
        <div class="form-grid discovery-fallback hidden" data-manual-manufacturer>
          <label class="field"><span>Manufacturer (manual fallback)</span><select name="manufacturer">
            <option value=""${selected(!manualManufacturerValue)}>Unknown / choose later</option>
            <option value="Hikvision"${selected(manualManufacturerValue === "Hikvision")}>Hikvision</option>
            <option value="Reolink"${selected(manualManufacturerValue === "Reolink")}>Reolink</option>
            <option value="Other"${selected(manualManufacturerValue === "Other")}>Other</option>
          </select><input type="hidden" name="manufacturer_explicit" value="false"><small>Only use this when discovery cannot identify the Device. It filters recommendations; it does not claim protocol support.</small></label>
        </div>
      </div>

      <div class="form-section"><h3>Capture</h3>
        <div class="form-grid capture-policy-fields">
          <label class="field"><span>Episode activity window</span><input name="activity_window_seconds" type="number" min="1" max="3600" required value="${values.episodePolicy.activity_window_seconds}"><small>Seconds this Device keeps an Episode open after each Event. Other recording Devices follow the Episode.</small></label>
          ${deviceEventFilterSelect("event_filter", values.episodePolicy.event_filter)}
        </div>
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
        <div class="form-section-heading"><h3>Connections and capture</h3><p>ONVIF is the generic starting point. Vendor choices appear only after discovery or an explicit manufacturer selection. Nothing is enabled automatically just because it is installed.</p></div>
        <div class="integration-stack" data-integration-stack>
          ${integrationToggle("onvif", "ONVIF", "Standards-based discovery, media, and optional Events.", values.onvif.enabled, `
            <label class="toggle-row"><input type="checkbox" name="onvif_events_enabled"${checked(values.onvif.events_enabled)}><span><strong>Receive ONVIF Events</strong><small>Disabled by default to avoid noisy motion state changes.</small></span></label>
            <label class="toggle-row"><input type="checkbox" name="onvif_relaxed_xml"${checked(values.onvif.relaxed_xml)}><span><strong>Tolerate malformed SOAP XML</strong><small>Compatibility fallback for Devices that return malformed ONVIF responses. Leave off unless validation fails.</small></span></label>`, integrationOptionAttributes("onvif")) }
          <div class="integration-group-label" data-vendor-group="hikvision"><strong>Hikvision enhancements</strong><span>Optional vendor connections that complement ONVIF.</span></div>
          ${integrationToggle("isapi", "Hikvision ISAPI Event stream", "Rich motion and classification Events from Hikvision devices.", values.isapi.enabled, "", integrationOptionAttributes("isapi"))}
          ${integrationToggle("hikvision_sdk", "Hikvision HCNetSDK", "Doorbell rings and unlock records; anti-tamper mapping is not yet device-tested. Available for Doorbell Devices.", values.sdk.enabled, "", integrationOptionAttributes("hikvision_sdk")) }
          <div class="integration-group-label" data-vendor-group="reolink"><strong>Reolink</strong><span>Native binary protocol for Reolink cameras.</span></div>
          ${integrationToggle("reolink", "Reolink API", "Discovery, media, and Events over the Reolink binary protocol.", values.reolink.enabled, `
            <label class="toggle-row"><input type="checkbox" name="reolink_media_enabled"${checked(values.reolink.media_enabled)}><span><strong>Enable media (streams &amp; snapshots)</strong><small>Register the discovered RTSP stream and binary snapshots so recording and snapshot-on-event work without ONVIF.</small></span></label>
            <label class="toggle-row"><input type="checkbox" name="reolink_events_enabled"${checked(values.reolink.events_enabled)}><span><strong>Receive Reolink events</strong><small>Listen for motion and detection events pushed over the binary protocol. Disabled by default to avoid noisy state changes.</small></span></label>
            <label class="toggle-row"><input type="checkbox" name="reolink_native_video"${checked(values.reolink.native_video)}><span><strong>Native media acquisition</strong><small>Record from the on-demand native burst instead of RTSP. The camera leads with an I-Frame, so the first access unit reaches the recorder sooner than a fresh RTSP keyframe-wait.</small></span></label>
            <div class="form-grid">
              <label class="field"><span>Preview variant</span><select name="reolink_preview_variant">
                <option value="main"${selected(values.reolink.preview_variant === "main")}>main</option>
                <option value="sub"${selected(values.reolink.preview_variant === "sub")}>sub</option>
              </select><small>Native preview stream; also the variant used by native media acquisition.</small></label>
              <label class="field"><span>Preview timeout (s)</span><input name="reolink_preview_timeout" type="number" min="1" max="10" step="0.5" value="${values.reolink.preview_timeout ?? ""}" placeholder="3.0"><small>Seconds to wait for the first video packet of a native preview pass.</small></label>
            </div>`, integrationOptionAttributes("reolink")) }
        </div>
          <div class="validation-panel">
            <div class="validation-heading">
            <div><strong>Device validation</strong><span>Checks the selected connection without enabling it.</span></div>
            <button type="button" class="button button-ghost" data-validate-device>Discover with ONVIF</button>
          </div>
          <div class="validation-results" data-validation-results>
            ${renderValidationResults(device?.integration_support, initialValidationKeys)}
          </div>
          <div class="validation-empty hidden" data-validation-empty>Choose a matching integration, then validate it before enabling it.</div>
          <div class="catalog-status" data-catalog-status>Loading available integration choices…</div>
          <div class="catalog-external hidden" data-catalog-external></div>
        </div>
        <div class="notice notice-warning hidden" data-no-trigger-warning><div><strong>No Event source selected</strong><span data-no-trigger-message>This Device can still record when another Device opens an Episode. The Event API or a configured Alarm Server can supply Events; FTP alone supplies Evidence.</span></div></div>
      </div>

      <details class="form-advanced"><summary>Manual connection overrides</summary>
        <p class="configuration-note">These values are configured endpoints or fallbacks. Runtime-discovered manufacturer, model, firmware, media profiles, and selected profile are read-only on the Device page.</p>
        <div class="advanced-grid">
          <fieldset data-manual-video>
            <legend>RTSP fallback</legend>
            <label class="toggle-row"><input type="checkbox" name="manual_video_endpoint"${checked(values.video.manual_endpoint)}><span><strong>Use manual endpoint</strong><small>ONVIF-discovered media is preferred when available. A manual stream provides recording only; it does not provide Events or snapshots.</small></span></label>
            <div class="manual-endpoint-fields">
              <label class="field"><span>Protocol</span><input name="video_protocol" value="${safeValue(values.video.protocol)}"></label>
              <label class="field"><span>Port</span><input name="video_port" type="number" min="1" max="65535" value="${values.video.port || ""}"></label>
              <label class="field"><span>Path</span><input name="video_path" value="${safeValue(values.video.path)}"></label>
              <div class="manual-video-test"><button type="button" class="button button-ghost" data-test-video>Test video stream</button><small data-video-test-result>Tests the stream without saving a recording or changing the Device.</small></div>
            </div>
          </fieldset>
          <fieldset><legend>ONVIF service</legend><label class="field"><span>Protocol</span><input name="onvif_protocol" value="${safeValue(values.onvif.protocol)}"></label><label class="field"><span>Port</span><input name="onvif_port" type="number" min="1" max="65535" value="${values.onvif.port || ""}"></label><label class="field"><span>Path</span><input name="onvif_path" value="${safeValue(values.onvif.path)}"></label><label class="field"><span>Authentication</span><select name="onvif_auth_mode"><option value="digest_wsse"${selected(values.onvif.auth_mode === "digest_wsse")}>Digest + WS-Username Token</option><option value="digest"${selected(values.onvif.auth_mode === "digest")}>Digest only</option></select></label></fieldset>
          <fieldset><legend>ISAPI endpoint</legend><label class="field"><span>Protocol</span><input name="isapi_protocol" value="${safeValue(values.isapi.protocol)}"></label><label class="field"><span>Port</span><input name="isapi_port" type="number" min="1" max="65535" value="${values.isapi.port || ""}"></label><label class="field"><span>Path</span><input name="isapi_path" value="${safeValue(values.isapi.path)}"></label><label class="field"><span>Ignored Events</span><input name="isapi_ignore_events" value="${safeValue((values.isapi.ignore_events || []).join(", "))}"></label></fieldset>
          <fieldset><legend>HCNetSDK login</legend><label class="field"><span>SDK port</span><input name="sdk_port" type="number" min="1" max="65535" value="${values.sdk.port}"></label></fieldset>
          <fieldset><legend>Reolink API</legend><label class="field"><span>API host</span><input name="reolink_host" value="${safeValue(values.reolink.host)}" placeholder="Defaults to the Device address"><small>Optional. Overrides the Device network address.</small></label><label class="field"><span>API port</span><input name="reolink_port" type="number" min="1" max="65535" value="${values.reolink.port || ""}"></label></fieldset>
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
      notify(editing ? "Device and integrations updated" : "Device added and integrations activated");
      await onSaved();
    },
  });

  let validationResults = { ...(device?.integration_support || {}) };
  let discoveryAttempted = Boolean(
    editing && !discoveredManufacturer && validationResults.onvif?.status === "unsupported",
  );
  let integrationCatalog = null;
  const updateIntegration = option => {
    const toggle = option.querySelector(".integration-toggle input");
    if (!toggle) return;
    option.classList.toggle("integration-disabled", !toggle.checked);
  };
  overlay.querySelectorAll("[data-integration]").forEach(option => {
    const toggle = option.querySelector(".integration-toggle input");
    toggle.addEventListener("change", () => {
      updateIntegration(option);
      updateIntegrationVisibility();
    });
    updateIntegration(option);
  });

  const typeSelect = overlay.querySelector("[data-device-type]");
  const sdkOption = overlay.querySelector('[data-integration="hikvision_sdk"]');
  const sdkToggle = sdkOption.querySelector(".integration-toggle input");
  const sdkWasConfigured = values.sdk.enabled;
  const form = overlay.querySelector("form");
  const resultsElement = overlay.querySelector("[data-validation-results]");
  const discoverySummary = overlay.querySelector("[data-discovery-summary]");
  const discoveryDetail = overlay.querySelector("[data-discovery-detail]");
  const manualManufacturer = overlay.querySelector("[data-manual-manufacturer]");
  const validationEmpty = overlay.querySelector("[data-validation-empty]");
  const catalogStatus = overlay.querySelector("[data-catalog-status]");
  const catalogExternal = overlay.querySelector("[data-catalog-external]");
  const noTriggerWarning = overlay.querySelector("[data-no-trigger-warning]");
  const noTriggerMessage = overlay.querySelector("[data-no-trigger-message]");

  const selectedIntegrationNames = () => validationIntegrations
    .map(([key]) => {
      const toggle = overlay.querySelector(`[data-integration="${key}"] .integration-toggle input`);
      return toggle?.checked ? key : null;
    })
    .filter(Boolean);

  let manufacturerChoiceTouched = false;
  const currentManufacturer = () => {
    const onvif = validationResults.onvif;
    const selected = form.querySelector('[name="manufacturer"]')?.value || "";
    return preferredManufacturer(
      onvif, selected, manufacturerChoiceTouched, manualManufacturerValue, discoveredManufacturer,
    );
  };

  const optionMatchesDevice = option => {
    const key = option.dataset.integration;
    if (configuredIntegrations.has(key)) return true;
    if (!integrationCatalog) return key === "onvif";
    return catalogEntryMatches(
      catalogEntryFor(integrationCatalog, key),
      currentManufacturer(),
      typeSelect.value,
    );
  };

  const refreshValidationResults = () => {
    const visibleKeys = applicableValidationKeys({
      catalog: integrationCatalog || [],
      configured: configuredIntegrations,
      deviceType: typeSelect.value,
      manufacturer: currentManufacturer(),
      selected: selectedIntegrationNames(),
    });
    resultsElement.innerHTML = renderValidationResults(validationResults, visibleKeys);
  };

  const updateIntegrationVisibility = () => {
    const selectedIntegrations = new Set(selectedIntegrationNames());
    overlay.querySelectorAll("[data-integration]").forEach(option => {
      const key = option.dataset.integration;
      const shouldShow = key === "video" || optionMatchesDevice(option);
      const preserveSelection = configuredIntegrations.has(key) || selectedIntegrations.has(key);
      option.classList.toggle("hidden", !shouldShow && !preserveSelection);
      if (!shouldShow && !preserveSelection) {
        const toggle = option.querySelector(".integration-toggle input");
        if (toggle) toggle.checked = false;
      }
    });
    overlay.querySelectorAll("[data-vendor-group]").forEach(group => {
      const vendor = group.dataset.vendorGroup;
      const visible = [...overlay.querySelectorAll(`[data-integration][data-vendor="${vendor}"]`)]
        .some(option => !option.classList.contains("hidden"));
      group.classList.toggle("hidden", !visible);
    });
    const currentOnvifManufacturer = validationResults.onvif?.status === "supported"
      ? validationManufacturer({ onvif: validationResults.onvif })
      : "";
    if (manualManufacturer) manualManufacturer.classList.toggle("hidden", !discoveryAttempted || Boolean(currentOnvifManufacturer));
    if (validationEmpty) validationEmpty.classList.toggle("hidden", selectedIntegrationNames().length > 0);
    refreshValidationResults();
  };

  const updateNoTriggerWarning = () => {
    const eventSources = [
      ["onvif_events_enabled", "onvif"],
      ["isapi_enabled", "isapi"],
      ["hikvision_sdk_enabled", "hikvision_sdk"],
      ["reolink_events_enabled", "reolink"],
    ].some(([fieldName, integration]) => {
      const fieldValue = form.querySelector(`[name="${fieldName}"]`);
      const integrationToggle = form.querySelector(`[data-integration="${integration}"] .integration-toggle input`);
      return Boolean(fieldValue?.checked && integrationToggle?.checked);
    });
    const saveForLater = form.querySelector('[name="save_for_later"]')?.checked;
    const videoEnabled = form.querySelector('[name="video_enabled"]')?.checked;
    noTriggerWarning.classList.toggle("hidden", eventSources || saveForLater);
    noTriggerMessage.textContent = videoEnabled
      ? "This Device can still be saved for later or used as a recording-only target with a validated media stream. It will not open Episodes by itself. An Event API or configured Alarm Server can provide the trigger; FTP uploads provide Evidence but do not open Episodes."
      : "This Device has no direct Event or media integration selected. Keep it ready when Events will arrive through the shared Event API or a configured Alarm Server; FTP alone provides Evidence and does not open Episodes. Otherwise choose Save for later.";
  };

  const updateDeviceRole = () => {
    const available = optionMatchesDevice(sdkOption) || sdkWasConfigured;
    sdkToggle.disabled = !available;
    if (!available) sdkToggle.checked = false;
    sdkOption.classList.toggle("integration-role-unavailable", !available);
    updateIntegration(sdkOption);
    updateIntegrationVisibility();
    updateNoTriggerWarning();
  };

  const renderExternalCatalog = () => {
    if (!catalogExternal || !integrationCatalog) return;
    const builtInIds = new Set(Object.values(integrationIds));
    const external = integrationCatalog.filter(entry => !builtInIds.has(entry.id));
    catalogExternal.innerHTML = external.length
      ? `<strong>Installed plugins</strong><small>These plugins are visible for awareness only. Configure them through their documented integration path; they are not activated from this editor.</small>${external.map(entry => `<span><b>${safeValue(entry.name || entry.id)}</b> · ${safeValue(entry.capabilities?.join(", ") || "No published capabilities")}</span>`).join("")}`
      : "";
    catalogExternal.classList.toggle("hidden", !external.length);
  };

  const applyValidation = ({ performed = false } = {}) => {
    if (performed) discoveryAttempted = true;
    const manufacturer = currentManufacturer();
    const onvif = validationResults.onvif || {};
    const model = onvif.details?.model;
    discoverySummary.innerHTML = manufacturer
      ? `<strong>Manufacturer: ${safeValue(manufacturer)}</strong>`
      : `<span>Manufacturer was not identified. Choose one manually if you know it.</span>`;
    discoveryDetail.textContent = model ? `Model: ${model}` : "";
    for (const integration of ["onvif", "isapi", "reolink"]) {
      const option = overlay.querySelector(`[data-integration="${integration}"]`);
      const unsupported = validationResults[integration]?.status === "unsupported";
      option.classList.toggle("integration-support-unsupported", unsupported);
      updateIntegration(option);
    }
    updateIntegrationVisibility();
    updateDeviceRole();
  };
  typeSelect.addEventListener("change", updateDeviceRole);
  form.querySelector('[name="manufacturer"]')?.addEventListener("change", event => {
    manufacturerChoiceTouched = true;
    form.querySelector('[name="manufacturer_explicit"]').value = "true";
    updateIntegrationVisibility();
    updateNoTriggerWarning();
  });
  form.querySelector('[name="save_for_later"]')?.addEventListener("change", () => {
    updateNoTriggerWarning();
  });
  form.querySelectorAll('[name$="_enabled"], [name="onvif_events_enabled"], [name="reolink_events_enabled"]').forEach(input => {
    input.addEventListener("change", updateNoTriggerWarning);
  });
  applyValidation();

  const loadIntegrationCatalog = async () => {
    try {
      integrationCatalog = await apiRequest(
        `/devices/integrations/catalog?device_type=${encodeURIComponent(deviceType)}`,
      );
      catalogStatus.textContent = "Integration choices are filtered from the installed plugin catalogue.";
      catalogStatus.className = "catalog-status catalog-status-ready";
      renderExternalCatalog();
      updateIntegrationVisibility();
      updateDeviceRole();
    } catch (error) {
      catalogStatus.textContent = "Integration catalogue unavailable. Only already-configured connections and generic ONVIF are shown.";
      catalogStatus.className = "catalog-status catalog-status-error";
      updateIntegrationVisibility();
    }
  };
  void loadIntegrationCatalog();

  const validateButton = overlay.querySelector("[data-validate-device]");
  const validationButtonLabel = () => selectedIntegrationNames().some(key => key !== "onvif")
    ? "Validate selected"
    : "Discover with ONVIF";
  validateButton.addEventListener("click", async () => {
    if (!form.reportValidity()) return;
    const originalLabel = validateButton.textContent;
    validateButton.disabled = true;
    validateButton.textContent = selectedIntegrationNames().some(key => key !== "onvif")
      ? "Validating selected…"
      : "Discovering…";
    try {
      const payload = devicePayload(new FormData(form), editing, device);
      payload.integration_ids = selectedIntegrationNames();
      if (!payload.integration_ids.length) payload.integration_ids = ["onvif"];
      const response = await apiRequest("/devices/validate", {
        method: "POST",
        body: payload,
      });
      validationResults = { ...validationResults, ...(response.results || {}) };
      applyValidation({ performed: true });
      notify("Device discovery completed");
    } catch (error) {
      notify(`Validation failed: ${error.message}`, "warning");
    } finally {
      validateButton.disabled = false;
      validateButton.textContent = validationButtonLabel() || originalLabel;
    }
  });

  const testVideoButton = overlay.querySelector("[data-test-video]");
  const videoTestResult = overlay.querySelector("[data-video-test-result]");
  testVideoButton.addEventListener("click", async () => {
    if (!form.reportValidity()) return;
    testVideoButton.disabled = true;
    testVideoButton.textContent = "Testing…";
    videoTestResult.textContent = "Testing the stream without saving media…";
    try {
      const response = await apiRequest("/devices/validate-video", {
        method: "POST",
        body: devicePayload(new FormData(form), editing, device),
      });
      const status = validationStatuses.has(response.status) ? response.status : "unavailable";
      videoTestResult.textContent = response.summary || `Stream ${status}.`;
      videoTestResult.className = `video-test-${status}`;
      notify(status === "supported" ? "Video stream is ready" : "Video stream test completed", status === "supported" ? "success" : "warning");
    } catch (error) {
      videoTestResult.textContent = `Video stream test failed: ${error.message}`;
      videoTestResult.className = "video-test-error";
      notify(`Video stream test failed: ${error.message}`, "warning");
    } finally {
      testVideoButton.disabled = false;
      testVideoButton.textContent = "Test video stream";
    }
  });

  wireEventFilterSummary(overlay, "device");

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
      closeDialog(); notify("Device deleted and integrations updated"); await onDeleted();
    },
  });
}
