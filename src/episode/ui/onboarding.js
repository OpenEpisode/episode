import { api, apiRequest } from "./api.js?v=3";
import { pageHeader } from "./components.js?v=3";
import { closeDialog, confirmDialog, notify } from "./dialogs.js?v=1";
import { openAreaEditor, openDeviceEditor } from "./inventory.js?v=7";
import { refreshRetentionPolicy } from "./retention-policy.js?v=1";
import { showContent, showError, showLoading } from "./view.js?v=1";

let areas = [];
let devices = [];

export async function onboardingNeeded() {
  const [inventory, retention] = await Promise.all([
    api("/devices?include_disabled=true"),
    api("/settings/retention"),
  ]);
  return !inventory.some(device => device.setup_state !== "needs_setup" && device.enabled !== false)
    || retention.policy_state === "unconfirmed";
}

function step(number, title, description, state, action = "") {
  return `<div class="onboarding-step onboarding-step-${state}">
    <div class="onboarding-step-number">${state === "complete" ? "✓" : number}</div>
    <div class="onboarding-step-copy"><h3>${title}</h3><p>${description}</p>${action}</div>
  </div>`;
}

export async function welcome() {
  showLoading();
  try {
    const [areaList, deviceList, status, retention] = await Promise.all([
      api("/areas?include_disabled=true"),
      api("/devices?include_disabled=true"),
      api("/status"),
      api("/settings/retention"),
    ]);
    areas = areaList;
    devices = deviceList;
    const activeAreas = areas.filter(area => area.enabled);
    const hasArea = activeAreas.length > 0;
    const readyDevices = devices.filter(device => device.setup_state !== "needs_setup" && device.enabled !== false);
    const draftDevices = devices.filter(device => device.setup_state === "needs_setup");
    const hasDevice = readyDevices.length > 0;
    const retentionConfirmed = retention.policy_state !== "unconfirmed";
    const ready = hasDevice && retentionConfirmed;

    showContent(`
      ${pageHeader({
        eyebrow: "Welcome to Episode",
        title: ready
          ? "Your evidence workspace is ready"
          : hasDevice
            ? "Confirm your evidence policy"
            : draftDevices.length
              ? "Finish setting up a Device"
              : "Connect your first Device",
        description: "Create one physical Area, add a Device, validate what it supports, and let Episode handle correlation and capture.",
        actions: ready ? '<a href="#episodes" class="button button-primary">Review Episodes</a>' : "",
      })}
      <div class="onboarding-layout">
        <section class="onboarding-intro">
          <div class="eyebrow">How Episode thinks</div>
          <h2>Events become Episodes. Evidence stays original.</h2>
          <p>An Area keeps related activity together. Devices contribute Events, recordings, and snapshots without changing the source material Episode received.</p>
          <div class="onboarding-principles">
            <span><strong>Area-scoped</strong> correlation and recording</span>
            <span><strong>ONVIF-first</strong> discovery and media</span>
            <span><strong>Raw-first</strong> immutable provenance</span>
          </div>
        </section>
        <section class="onboarding-steps" aria-label="Setup progress">
          ${step(
            1,
            "Create an Area",
            hasArea
              ? `${activeAreas.length} active ${activeAreas.length === 1 ? "Area defines" : "Areas define"} where activity is correlated.`
              : "Use a real physical boundary such as Front entrance, Garage, or Garden.",
            hasArea ? "complete" : "active",
            hasArea ? "" : '<button class="button button-primary" type="button" onclick="startOnboardingArea()">Create first Area</button>',
          )}
          ${step(
            2,
            "Add and validate a Device",
            hasDevice
              ? `${readyDevices.length} ${readyDevices.length === 1 ? "Device is" : "Devices are"} ready. Configured integrations activate automatically.`
              : draftDevices.length
                ? `${draftDevices.length} Device${draftDevices.length === 1 ? " is" : "s are"} saved for later. Finish one before expecting Episodes or capture.`
                : "Enter the Device address and credentials, then use Validate and discover before choosing its integrations.",
            hasDevice ? "complete" : hasArea ? "active" : "pending",
            !hasDevice && hasArea ? `<div class="onboarding-actions"><button class="button button-primary" type="button" onclick="startOnboardingDevice()">${draftDevices.length ? "Add another Device" : "Add first Device"}</button>${draftDevices.length ? '<a class="button button-ghost" href="#devices">Finish Device setup</a>' : ""}</div>` : "",
          )}
          ${step(
            3,
            "Confirm visual Evidence retention",
            retentionConfirmed
              ? retention.enabled
                ? `Automatic deletion is enabled after ${retention.retention_days} days.`
                : "Automatic deletion is disabled. A persistent warning will remain visible."
              : `OpenEpisode is applying its ${retention.retention_days}-day default. Confirm that it is appropriate for your use case and jurisdiction.`,
            retentionConfirmed ? "complete" : "active",
            retentionConfirmed
              ? ""
              : `<div class="onboarding-actions"><button class="button button-primary" type="button" onclick="confirmDefaultRetention(${retention.retention_days})">Confirm ${retention.retention_days} days</button><a href="#system/storage" class="button button-ghost">Review options</a></div>`,
          )}
          ${step(
            4,
            "Verify connections",
            ready
              ? `${status.integrations.healthy}/${status.integrations.total} integrations are healthy. Episode is ready for its first Event.`
              : hasDevice
                ? "Confirm the retention policy to complete setup."
                : draftDevices.length
                  ? "Finish Device setup before expecting its Events or recordings to participate."
                  : "Saving a Device also activates its selected integrations.",
            ready ? "complete" : hasDevice ? "active" : "pending",
            ready
              ? '<div class="onboarding-actions"><a href="#devices" class="button button-ghost">View Device health</a><a href="#episodes" class="button button-primary">Open Episode</a></div>'
              : "",
          )}
        </section>
      </div>`);
  } catch (error) {
    showError(error.message);
  }
}

window.startOnboardingArea = () => openAreaEditor(null, welcome);
window.startOnboardingDevice = () => openDeviceEditor(
  null,
  areas.filter(area => area.enabled),
  welcome,
);
window.confirmDefaultRetention = retentionDays => {
  confirmDialog({
    title: `Confirm ${retentionDays}-day retention?`,
    message: "OpenEpisode will automatically and permanently delete managed visual Evidence older than this period. Exported and externally stored copies are not covered.",
    confirmLabel: "Confirm retention",
    onConfirm: async () => {
      await apiRequest("/settings/retention", {
        method: "PUT",
        body: { enabled: true, retention_days: retentionDays },
      });
      closeDialog();
      notify("Visual Evidence retention confirmed");
      await refreshRetentionPolicy();
      await welcome();
    },
  });
};
window.refreshOnboarding = welcome;
