import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/alerts.js", import.meta.url),
  "utf8",
);
const apiUrl = moduleUrl(`
  export async function api(path) {
    globalThis.alertApiPath = path;
    globalThis.alertApiPaths.push(path);
    if (globalThis.alertFailure) throw new Error(globalThis.alertFailure);
    if (path.includes("limit=1")) return globalThis.alertProbeResponse;
    return globalThis.alertResponse;
  }
`);
const componentsUrl = moduleUrl(`
  export function pageHeader(value) { return "<header><h2>" + value.title + "</h2></header>"; }
`);
const domUrl = moduleUrl(`
  export function escHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }
`);
const formatUrl = moduleUrl(`
  export function fmtShort(value) { return String(value ?? ""); }
  export function plural(value, label) { return value + " " + label + (value === 1 ? "" : "s"); }
  export function titleCase(value) { return String(value ?? "").replaceAll("_", " ").replace(/\\b\\w/g, letter => letter.toUpperCase()); }
  export function trunc(value, length) { return String(value ?? "").slice(0, length); }
`);
const viewUrl = moduleUrl(`
  export function showContent(html) { globalThis.alertHtml = html; }
  export function showError(error) { globalThis.alertError = error; }
  export function showLoading() { globalThis.alertLoading = true; }
`);
const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=4"', JSON.stringify(componentsUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js?v=4"', JSON.stringify(formatUrl))
    .replace('"./view.js?v=1"', JSON.stringify(viewUrl)),
));

test("alerts render newest first with evidence and episode links", () => {
  const html = module.renderAlertsPage([
    {
      id: "old",
      code: "hls_finalization",
      title: "Older alert",
      message: "Older message",
      created_at: "2026-10-01T09:00:00Z",
      device_id: "camera-old",
      evidence_id: "evidence-old",
      episode_id: "episode-old",
    },
    {
      id: "new",
      code: "hls_finalization",
      title: "Newer alert",
      message: "The playlist was empty.",
      created_at: "2026-10-01T10:00:00Z",
      device_id: "camera-new",
      evidence_id: "evidence-new",
      episode_id: null,
      ffmpeg_exit_code: 1,
      playlist_validation: { valid: false, error: "No media segments" },
    },
  ]);
  assert.ok(html.indexOf("Newer alert") < html.indexOf("Older alert"));
  assert.match(html, /#evidence\/evidence-new/);
  assert.match(html, /#episode\/episode-old/);
  assert.match(html, /Technical details/);
  assert.match(html, /No media segments/);
  assert.match(html, /href="#system\/alerts"/);
});

test("empty alerts page explains the normal state", () => {
  const html = module.renderAlertsPage([]);
  assert.match(html, /No alerts/);
  assert.match(html, /operating normally/);
  assert.doesNotMatch(html, /system-alert-row/);
});

test("alerts request is bounded and paginated", async () => {
  globalThis.alertResponse = [];
  globalThis.alertProbeResponse = [];
  globalThis.alertApiPaths = [];
  globalThis.alertFailure = null;
  await module.alerts(2);
  assert.equal(globalThis.alertApiPath, "/alerts?limit=50&offset=50");
  assert.match(globalThis.alertHtml, /Page 2/);
});

test("a full page probes for a real older page", async () => {
  globalThis.alertFailure = null;
  globalThis.alertResponse = Array.from({ length: 50 }, (_, index) => ({
    id: `alert-${index}`,
    title: `Alert ${index}`,
    created_at: `2026-10-01T${String(index % 24).padStart(2, "0")}:00:00Z`,
    evidence_id: `evidence-${index}`,
  }));
  globalThis.alertApiPaths = [];
  globalThis.alertProbeResponse = [{ id: "alert-older" }];
  await module.alerts(1);
  assert.deepEqual(globalThis.alertApiPaths, [
    "/alerts?limit=50&offset=0",
    "/alerts?limit=1&offset=50",
  ]);
  assert.match(globalThis.alertHtml, /Older →/);

  globalThis.alertApiPaths = [];
  globalThis.alertProbeResponse = [];
  await module.alerts(1);
  assert.deepEqual(globalThis.alertApiPaths, [
    "/alerts?limit=50&offset=0",
    "/alerts?limit=1&offset=50",
  ]);
  assert.doesNotMatch(globalThis.alertHtml, /Older →/);
});

test("alerts expose API failures through the shared error state", async () => {
  globalThis.alertFailure = "Alerts unavailable";
  await module.alerts(1);
  assert.equal(globalThis.alertError, "Alerts unavailable");
  globalThis.alertFailure = null;
});
