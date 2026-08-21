import { api } from "./api.js?v=3";
import { pageHeader } from "./components.js?v=3";
import { openAreaEditor, openDeviceEditor } from "./inventory.js?v=5";
import { showContent, showError, showLoading } from "./view.js?v=1";

let areas = [];
let devices = [];

export async function onboardingNeeded() {
  const inventory = await api("/devices?include_disabled=true");
  return inventory.length === 0;
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
    const [areaList, deviceList, status] = await Promise.all([
      api("/areas?include_disabled=true"),
      api("/devices?include_disabled=true"),
      api("/status"),
    ]);
    areas = areaList;
    devices = deviceList;
    const activeAreas = areas.filter(area => area.enabled);
    const hasArea = activeAreas.length > 0;
    const hasDevice = devices.length > 0;
    const ready = hasDevice;

    showContent(`
      ${pageHeader({
        eyebrow: "Welcome to Episode",
        title: ready ? "Your evidence workspace is ready" : "Connect your first Device",
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
              ? `${devices.length} ${devices.length === 1 ? "Device is" : "Devices are"} saved. Configured integrations activate automatically.`
              : "Enter the Device address and credentials, then use Validate and discover before choosing its integrations.",
            hasDevice ? "complete" : hasArea ? "active" : "pending",
            !hasDevice && hasArea ? '<button class="button button-primary" type="button" onclick="startOnboardingDevice()">Add first Device</button>' : "",
          )}
          ${step(
            3,
            "Verify connections",
            ready
              ? `${status.integrations.healthy}/${status.integrations.total} integrations are healthy. Episode is ready for its first Event.`
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
window.refreshOnboarding = welcome;
