import { api, apiRequest } from "./api.js?v=3";
import { pageHeader } from "./components.js?v=4";
import { closeDialog, confirmDialog, notify, openDialog } from "./dialogs.js?v=1";
import { escHtml } from "./dom.js";
import { fmtShort, plural } from "./format.js?v=3";
import { showContent, showError, showLoading } from "./view.js?v=1";

let profileState = {
  profiles: [],
  active: null,
  recentChanges: [],
  devices: [],
  areas: [],
};

const MAX_HISTORY_ITEMS = 10;

function isChecked(formData, name) {
  return formData.get?.(name) === "on";
}

function checked(value) {
  return value ? " checked" : "";
}

function profileName(profile, fallback = "Unknown profile") {
  if (profile && typeof profile === "object") {
    return profile.name || profile.id || fallback;
  }
  return profile || fallback;
}

function profileIsRestricted(profile) {
  return Boolean(profile && profile.include_all_devices === false);
}

function activeProfileFromResponse(response) {
  const active = response?.profile;
  if (!active) return null;
  return {
    ...active,
    active: true,
  };
}

function historyTimestamp(change) {
  return change?.changed_at || "";
}

function historySource(change) {
  return change?.source || "Unknown source";
}

function historyRows(changes = []) {
  if (!Array.isArray(changes) || !changes.length) {
    return '<div class="empty capture-profile-history-empty">No profile activations recorded yet.</div>';
  }
  return `<div class="resource-list capture-profile-history-list">
    ${changes.slice(0, MAX_HISTORY_ITEMS).map(change => {
      const timestamp = historyTimestamp(change);
      const previous = profileName(change?.previous_profile_name);
      const next = profileName(change?.new_profile_name);
      return `<div class="resource-row capture-profile-history-row">
        <div class="resource-main"><strong>${escHtml(previous)} <span aria-hidden="true">→</span> ${escHtml(next)}</strong>
          <span>${escHtml(historySource(change))}</span></div>
        ${timestamp ? `<time datetime="${escHtml(timestamp)}">${escHtml(fmtShort(timestamp))}</time>` : '<span class="meta">Time unavailable</span>'}
      </div>`;
    }).join("")}
  </div>`;
}

function systemNavigation(active = "capture-profiles") {
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

function captureProfileStatus(profile, unavailable = false) {
  if (typeof document === "undefined") return;
  const hosts = [
    document.getElementById("capture-profile-status"),
    document.getElementById("mobile-capture-profile-status"),
  ].filter(Boolean);
  const restricted = profileIsRestricted(profile);
  const count = Array.isArray(profile?.device_ids) ? profile.device_ids.length : 0;
  const mode = unavailable ? "unavailable" : restricted && count === 0 ? "disarmed" : restricted ? "restricted" : "all";
  const name = unavailable ? "Unavailable" : profileName(profile, "Unknown");
  const detail = unavailable
    ? "Capture profile state could not be loaded"
    : profile?.include_all_devices
    ? "All enabled Devices may participate"
    : count
    ? `${plural(count, "Device")} may participate`
    : "No Devices may start capture";
  const indicator = unavailable || mode === "disarmed" ? "offline" : mode === "restricted" ? "idle" : "online";
  for (const host of hosts) {
    const mobile = host.id === "mobile-capture-profile-status";
    host.className = `capture-profile-status${mobile ? " capture-profile-status-mobile" : ""} capture-profile-status-${mode}`;
    host.title = `${name} — ${detail}. Open Capture profiles.`;
    host.setAttribute("aria-label", `Active Capture profile: ${name}. ${detail}. Open Capture profiles.`);
    host.innerHTML = `<span class="status-indicator ${indicator}" aria-hidden="true"></span>
      <span class="capture-profile-status-copy"><small>Capture${mobile ? "" : " profile"}</small><strong>${escHtml(name)}</strong></span>
      ${mobile ? "" : '<span class="capture-profile-status-action" aria-hidden="true">Manage ›</span>'}`;
  }
}

export async function refreshCaptureProfileNotice() {
  try {
    const response = await api("/capture-profiles/active");
    captureProfileStatus(response?.profile || null);
    return response;
  } catch {
    captureProfileStatus(null, true);
    return null;
  }
}

function groupedDevices(devices, areas) {
  const areaById = new Map(areas.map(area => [area.id, area]));
  const groups = areas.map(area => ({ area, devices: [] }));
  const groupsByArea = new Map(groups.map(group => [group.area.id, group]));
  const unassigned = { area: { id: "__unassigned__", name: "Unassigned Area" }, devices: [] };
  for (const device of devices) {
    const group = groupsByArea.get(device.area_id) || unassigned;
    group.devices.push(device);
  }
  if (unassigned.devices.length) groups.push(unassigned);
  // Keep configured Areas visible, but do not let a disabled Area disappear when it
  // still contains a Device that can be selected for a profile.
  return groups.filter(group => group.devices.length || areaById.has(group.area.id));
}

function deviceCheckbox(device, selectedIds) {
  const inputId = `capture-profile-device-${encodeURIComponent(device.id)}`;
  const checked = selectedIds.has(device.id) ? " checked" : "";
  const disabled = device.enabled === false ? " (disabled)" : "";
  return `<label class="capture-profile-device-option" for="${escHtml(inputId)}">
    <input id="${escHtml(inputId)}" type="checkbox" name="device_id" value="${escHtml(device.id)}"${checked}>
    <span><strong>${escHtml(device.name || device.id)}</strong><small>${escHtml(device.device_type || "Device")}${disabled}</small></span>
  </label>`;
}

function profileEditorContent(profile, devices, areas) {
  const selectedIds = new Set(profile?.device_ids || []);
  const groups = groupedDevices(devices, areas);
  return `<label class="field capture-profile-name-field"><span>Profile name</span>
    <input name="name" required maxlength="100" value="${escHtml(profile?.name || "")}" placeholder="Night">
  </label>
  <label class="toggle-row capture-profile-filter-toggle"><input type="checkbox" name="filter_generic_events"${checked(Boolean(profile?.filter_generic_events))}><span><strong>Filter generic events</strong><small>Generic observations (System, motion, video loss, tamper, audio) will not open or extend Episodes or start recordings. Higher-level detections (person, vehicle, pet, …) still participate. A per-camera override wins.</small></span></label>
  <fieldset class="capture-profile-device-selection">
    <legend>Devices that may participate</legend>
    <p class="configuration-note">Select the Devices whose active Events may open or extend Episodes and whose cameras may join new recordings. Leave every box clear for a Disarmed profile.</p>
    ${groups.length ? groups.map(group => `<fieldset class="capture-profile-area-group">
      <legend>${escHtml(group.area.name || group.area.id)}</legend>
      <div class="capture-profile-device-list">${group.devices.map(device => deviceCheckbox(device, selectedIds)).join("")}</div>
    </fieldset>`).join("") : '<div class="empty capture-profile-device-empty">No Devices are configured yet. You can save an empty profile for Disarmed.</div>'}
  </fieldset>
  <div class="notice notice-warning capture-profile-editor-notice"><div><strong>Exclusion is capture policy, not connection control</strong><span>Excluded Devices stay connected and their observations remain preserved. Their active Events do not open or extend Episodes, and they do not join new recordings. Existing recordings continue until their Episode closes.</span></div></div>`;
}

function deviceIdsFromForm(data) {
  if (typeof data.getAll === "function") return data.getAll("device_id").map(String);
  const value = data.get?.("device_id");
  return value === null || value === undefined || value === "" ? [] : [String(value)];
}

export function openCaptureProfileEditor(profile = null, devices = profileState.devices, areas = profileState.areas) {
  if (profile?.builtin) return;
  const editing = Boolean(profile);
  openDialog({
    title: editing ? `Edit ${profileName(profile)}` : "Create Capture profile",
    subtitle: "Choose which Devices may contribute to future Episodes and recordings.",
    wide: true,
    submitLabel: editing ? "Save profile" : "Create profile",
    content: profileEditorContent(profile, devices, areas),
    onSubmit: async data => {
      const name = String(data.get?.("name") || "").trim();
      const device_ids = deviceIdsFromForm(data);
      const filter_generic_events = isChecked(data, "filter_generic_events");
      await apiRequest(editing ? `/capture-profiles/${encodeURIComponent(profile.id)}` : "/capture-profiles", {
        method: editing ? "PUT" : "POST",
        body: { name, device_ids, filter_generic_events },
      });
      closeDialog();
      notify(editing ? "Capture profile updated" : "Capture profile created");
      await captureProfiles();
    },
  });
}

function profileRow(profile) {
  const active = Boolean(profile.active || profileState.active?.id === profile.id);
  const builtin = Boolean(profile.builtin);
  const restricted = profileIsRestricted(profile);
  const selection = builtin
    ? "All current and future enabled Devices participate"
    : `${plural((profile.device_ids || []).length, "Device")} selected`;
  const filterNote = profile.filter_generic_events
    ? " · generic events filtered"
    : "";
  const activationControl = active
    ? '<span class="badge badge-active">Active</span>'
    : `<button type="button" class="button button-ghost capture-profile-activate" data-capture-profile-action="activate" data-profile-id="${escHtml(profile.id)}">Activate</button>`;
  const controls = builtin
    ? `<div class="resource-actions capture-profile-actions">
        <span class="meta capture-profile-immutable">Built-in · cannot edit or delete</span>
        ${activationControl}
      </div>`
    : `<div class="resource-actions capture-profile-actions">
        ${active
          ? '<span class="meta capture-profile-active-lock">Active profile cannot be deleted</span>'
          : `<button type="button" class="icon-button" data-capture-profile-action="delete" data-profile-id="${escHtml(profile.id)}">Delete</button>`}
        <button type="button" class="icon-button" data-capture-profile-action="edit" data-profile-id="${escHtml(profile.id)}">Edit</button>
        ${activationControl}
      </div>`;
  return `<div class="resource-row capture-profile-row ${active ? "capture-profile-row-active" : ""}">
    <span class="status-indicator ${active ? "online" : "idle"}" aria-hidden="true"></span>
    <div class="resource-main"><strong>${escHtml(profileName(profile))}</strong>
      <span>${escHtml(selection)}${restricted ? " · restricted" : ""}${escHtml(filterNote)}</span>
    </div>
    ${controls}
  </div>`;
}

function attachProfileHandlers() {
  if (typeof document === "undefined") return;
  document.querySelectorAll("[data-capture-profile-action]").forEach(button => {
    button.addEventListener("click", () => {
      const id = button.getAttribute("data-profile-id");
      const action = button.getAttribute("data-capture-profile-action");
      if (action === "create") createCaptureProfile();
      if (action === "activate") activateCaptureProfile(id);
      if (action === "edit") editCaptureProfile(id);
      if (action === "delete") deleteCaptureProfile(id);
    });
  });
}

function renderCaptureProfiles() {
  const { profiles, active, recentChanges } = profileState;
  const list = profiles.length
    ? `<div class="resource-list capture-profile-list">${profiles.map(profileRow).join("")}</div>`
    : `<div class="empty-state capture-profile-empty"><h3>No Capture profiles yet</h3><p>The built-in All Devices profile will preserve the current default behavior. Create a custom profile when you want to limit participation.</p><button type="button" class="button button-primary" data-capture-profile-action="create">Create profile</button></div>`;
  const activeName = profileName(active, "No active profile reported");
  const activeDescription = active?.include_all_devices
    ? "All current and future enabled Devices may participate."
    : `${plural((active?.device_ids || []).length, "Device")} selected; excluded Devices remain connected and preserved but do not affect new capture.`;
  showContent(`
    ${pageHeader({
      eyebrow: "Operations · System",
      title: "Capture profiles",
      description: "Choose which connected Devices may contribute to future Episodes and recordings.",
      actions: '<button type="button" class="button button-primary" data-capture-profile-action="create">Create profile</button>',
    })}
    <div class="system-layout capture-profile-layout">
      ${systemNavigation("capture-profiles")}
      <div class="system-content">
        <section class="section capture-profile-active-summary">
          <div class="system-section-heading"><div><h3>Active profile</h3><p>Changes apply to new Events. Existing recordings continue until their Episode closes.</p></div><span class="badge badge-active">Active</span></div>
          <div class="capture-profile-active-card"><strong>${escHtml(activeName)}</strong><span>${escHtml(activeDescription)}</span></div>
        </section>
        <section class="section capture-profile-management">
          <div class="system-section-heading"><div><h3>Available profiles</h3><p>All Devices is immutable. Custom profiles may include zero Devices for a Disarmed state.</p></div><span class="badge badge-neutral">${plural(profiles.length, "profile")}</span></div>
          ${list}
        </section>
        <section class="section capture-profile-explanation notice notice-info">
          <div><strong>What a restricted profile changes</strong><span>Excluded Devices stay connected and observations remain preserved. Their active Events do not open or extend Episodes and they do not join new recordings. A profile change never interrupts a recording already in progress.</span></div>
        </section>
        <section class="section capture-profile-history">
          <div class="system-section-heading"><div><h3>Recent activations</h3><p>Bounded history of active-profile changes.</p></div></div>
          ${historyRows(recentChanges)}
        </section>
      </div>
    </div>`);
  attachProfileHandlers();
}

async function loadCaptureProfiles() {
  const [profiles, activeResponse, devices, areas] = await Promise.all([
    api("/capture-profiles"),
    api("/capture-profiles/active"),
    api("/devices?include_disabled=true"),
    api("/areas?include_disabled=true"),
  ]);
  const profileList = Array.isArray(profiles) ? profiles : [];
  const active = activeProfileFromResponse(activeResponse);
  profileState = {
    profiles: profileList.map(profile => ({ ...profile, active: Boolean(active && active.id === profile.id) })),
    active,
    recentChanges: Array.isArray(activeResponse?.recent_changes) ? activeResponse.recent_changes : [],
    devices: Array.isArray(devices) ? devices : [],
    areas: Array.isArray(areas) ? areas : [],
  };
  captureProfileStatus(active);
}

export async function captureProfiles() {
  showLoading();
  try {
    await loadCaptureProfiles();
    renderCaptureProfiles();
  } catch (error) {
    showError(error.message);
  }
}

async function performActivation(profile) {
  try {
    await apiRequest("/capture-profiles/active", {
      method: "PUT",
      body: { profile_id: profile.id },
    });
    closeDialog();
    notify(`Capture profile “${profileName(profile)}” activated`);
    await captureProfiles();
  } catch (error) {
    notify(`Could not activate Capture profile: ${error.message}`, "warning");
    throw error;
  }
}

export function activateCaptureProfile(id) {
  const profile = profileState.profiles.find(candidate => candidate.id === id);
  if (!profile) return;
  if (profile.active || profileState.active?.id === id) {
    notify(`Capture profile “${profileName(profile)}” is already active`, "warning");
    return;
  }
  const restricted = profileIsRestricted(profile);
  if (!restricted) {
    performActivation(profile).catch(() => {});
    return;
  }
  confirmDialog({
    title: `Activate ${profileName(profile)}?`,
    message: `This restrictive profile selects ${plural((profile.device_ids || []).length, "Device")}. Excluded Devices stay connected and observations remain preserved, but their active Events will not open or extend Episodes or join new recordings. Existing recordings continue until their Episode closes.`,
    confirmLabel: "Activate profile",
    onConfirm: () => performActivation(profile),
  });
}

export function editCaptureProfile(id) {
  const profile = profileState.profiles.find(candidate => candidate.id === id);
  if (profile && !profile.builtin) openCaptureProfileEditor(profile);
}

export function deleteCaptureProfile(id) {
  const profile = profileState.profiles.find(candidate => candidate.id === id);
  if (!profile || profile.builtin || profile.active || profileState.active?.id === id) {
    if (profile?.active) notify("The active Capture profile cannot be deleted", "warning");
    return;
  }
  confirmDialog({
    title: `Delete ${profileName(profile)}?`,
    message: "This removes the profile configuration. It does not remove Devices, Events, or Evidence.",
    confirmLabel: "Delete profile",
    onConfirm: async () => {
      try {
        await apiRequest(`/capture-profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE" });
        closeDialog();
        notify("Capture profile deleted");
        await captureProfiles();
      } catch (error) {
        notify(`Could not delete Capture profile: ${error.message}`, "warning");
        throw error;
      }
    },
  });
}

export function createCaptureProfile() {
  openCaptureProfileEditor();
}

window.createCaptureProfile = createCaptureProfile;
window.addCaptureProfile = createCaptureProfile;
window.activateCaptureProfile = activateCaptureProfile;
window.editCaptureProfile = editCaptureProfile;
window.deleteCaptureProfile = deleteCaptureProfile;
