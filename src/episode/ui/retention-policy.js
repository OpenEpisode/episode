import { api } from "./api.js";

function renderPolicyNotice(policy) {
  const host = document.getElementById("policy-banner");
  if (!host) return;
  if (policy.policy_state === "configured") {
    host.className = "policy-banner hidden";
    host.innerHTML = "";
    return;
  }

  const disabled = policy.policy_state === "disabled";
  host.className = `policy-banner policy-banner-${disabled ? "danger" : "warning"}`;
  host.innerHTML = `<div>
      <strong>${disabled ? "Automatic visual Evidence deletion is disabled" : "Confirm the Evidence retention policy"}</strong>
      <span>${disabled
        ? "OpenEpisode-managed visual Evidence will be retained indefinitely unless it is manually removed. Verify your legal and storage requirements."
        : `OpenEpisode is automatically deleting managed visual Evidence after ${policy.retention_days} days using its unconfirmed default.`}</span>
    </div>
    <a href="#system/storage" class="button button-ghost">${disabled ? "Review policy" : "Review and confirm"}</a>`;
}

export async function refreshRetentionPolicy() {
  try {
    const policy = await api("/settings/retention");
    renderPolicyNotice(policy);
    return policy;
  } catch {
    const host = document.getElementById("policy-banner");
    if (host) host.className = "policy-banner hidden";
    return null;
  }
}

export function startRetentionPolicy() {
  return refreshRetentionPolicy();
}
