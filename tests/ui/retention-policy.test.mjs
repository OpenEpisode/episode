import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/retention-policy.js", import.meta.url),
  "utf8",
);
const apiUrl = moduleUrl(`
  export async function api() { return globalThis.retentionPolicy; }
`);
const module = await import(moduleUrl(
  source.replace('"./api.js"', JSON.stringify(apiUrl)),
));

const banner = { className: "", innerHTML: "" };
globalThis.document = {
  getElementById(id) { return id === "policy-banner" ? banner : null; },
};

test("unconfirmed default is persistently visible", async () => {
  globalThis.retentionPolicy = {
    enabled: true,
    retention_days: 30,
    policy_state: "unconfirmed",
  };

  await module.refreshRetentionPolicy();

  assert.match(banner.className, /policy-banner-warning/);
  assert.match(banner.innerHTML, /automatically deleting.*30 days/);
  assert.match(banner.innerHTML, /Review and confirm/);
});

test("configured retention removes the global notice", async () => {
  globalThis.retentionPolicy = {
    enabled: true,
    retention_days: 15,
    policy_state: "configured",
  };

  await module.refreshRetentionPolicy();

  assert.match(banner.className, /hidden/);
  assert.equal(banner.innerHTML, "");
});

test("disabled retention displays a persistent danger notice", async () => {
  globalThis.retentionPolicy = {
    enabled: false,
    retention_days: 30,
    policy_state: "disabled",
  };

  await module.refreshRetentionPolicy();

  assert.match(banner.className, /policy-banner-danger/);
  assert.match(banner.innerHTML, /retained indefinitely/);
  assert.match(banner.innerHTML, /Review policy/);
});
