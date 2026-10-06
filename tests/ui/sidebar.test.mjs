import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/sidebar.js", import.meta.url),
  "utf8",
);

const emptyUrl = moduleUrl("export function episodeStateBadge() { return ''; }");
const apiUrl = moduleUrl(`
  export async function api(path) {
    if (path.startsWith("/alerts")) return globalThis.sidebarAlertResponse;
    return {};
  }
`);
const domUrl = moduleUrl("export function $(selector) { return globalThis.sidebarElements[selector]; }");
const formatUrl = moduleUrl(`
  export function plural(value, label) { return value + " " + label + (value === 1 ? "" : "s"); }
  export function trunc(value) { return value; }
`);

globalThis.window = { setInterval() {} };
globalThis.sidebarElements = {
  "#sidebar-alerts": { innerHTML: "" },
  "#mobile-alert-status": {
    className: "sidebar-status sidebar-alert-status mobile-alert-status hidden",
    innerHTML: "",
    title: "",
    setAttribute(name, value) { this[name] = value; },
  },
};
globalThis.sidebarAlertResponse = [];

const module = await import(moduleUrl(
  source
    .replace('"./api.js"', JSON.stringify(apiUrl))
    .replace('"./components.js"', JSON.stringify(emptyUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js"', JSON.stringify(formatUrl)),
));

test("degraded sidebar health links directly to integration diagnostics", () => {
  const html = module.sidebarStatusView({ state: "degraded", active_recordings: 0 });
  assert.match(html, /href="#system\/integrations"/);
  assert.match(html, /Attention needed/);
  assert.match(html, /Review ›/);
  assert.match(html, /title="Review integration health"/);
});

test("healthy sidebar health remains an actionable System summary", () => {
  const html = module.sidebarStatusView({ state: "healthy", active_recordings: 2 });
  assert.match(html, /href="#system"/);
  assert.match(html, /All systems operational/);
  assert.match(html, /2 recs/);
  assert.doesNotMatch(html, /#system\/integrations/);
});

test("sidebar alerts provide a distinct review link", () => {
  const html = module.sidebarAlertsView([{ id: "alert-1" }, { id: "alert-2" }]);
  assert.match(html, /href="#system\/alerts"/);
  assert.match(html, /Recording alerts need attention/);
  assert.doesNotMatch(html, />2</);
});

test("sidebar alert polling updates desktop and mobile affordances", async () => {
  globalThis.sidebarAlertResponse = [{ id: "alert-1" }];
  await module.updateSidebarAlerts();
  assert.match(globalThis.sidebarElements["#sidebar-alerts"].innerHTML, /#system\/alerts/);
  assert.doesNotMatch(globalThis.sidebarElements["#mobile-alert-status"].className, /hidden/);
  assert.match(globalThis.sidebarElements["#mobile-alert-status"].innerHTML, />Alerts</);
  assert.equal(globalThis.sidebarElements["#mobile-alert-status"]["aria-label"], "Recording alerts need attention. Review alerts.");

  globalThis.sidebarAlertResponse = null;
  await module.updateSidebarAlerts();
  assert.equal(globalThis.sidebarElements["#sidebar-alerts"].innerHTML, "");
  assert.match(globalThis.sidebarElements["#mobile-alert-status"].className, /hidden/);
});
