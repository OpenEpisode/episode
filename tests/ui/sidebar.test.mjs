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
const apiUrl = moduleUrl("export async function api() { return {}; }");
const domUrl = moduleUrl("export function $(selector) { return globalThis.sidebarElements[selector]; }");
const formatUrl = moduleUrl(`
  export function plural(value, label) { return value + " " + label + (value === 1 ? "" : "s"); }
  export function trunc(value) { return value; }
`);

globalThis.window = { setInterval() {} };
globalThis.sidebarElements = {};

const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=6"', JSON.stringify(emptyUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js?v=3"', JSON.stringify(formatUrl)),
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
