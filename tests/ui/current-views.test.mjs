import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const domUrl = moduleUrl('export const escHtml = value => String(value).replaceAll("<", "&lt;").replaceAll(">", "&gt;");');
const apiUrl = moduleUrl("export async function api() { return []; }");
const source = await readFile(
  new URL("../../src/episode/ui/current-views.js", import.meta.url),
  "utf8",
);
const currentViewsUrl = moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl)),
);
const { renderCurrentViews } = await import(currentViewsUrl);

test("current view panel distinguishes refreshing and unavailable recordings", () => {
  const html = renderCurrentViews([
    {
      device_id: "camera-a",
      device_name: "Entry camera",
      mode: "snapshot",
      image_url: "/api/v1/preview",
      summary: "Refreshing",
    },
    {
      device_id: "doorbell",
      device_name: "Doorbell",
      mode: "unavailable",
      image_url: null,
      summary: "Recording continues",
    },
  ]);

  assert.match(html, /Current views/);
  assert.match(html, /data-preview-url="\/api\/v1\/preview"/);
  assert.match(html, /Preview unavailable/);
  assert.match(html, /no preview is stored/);
});

test("current view labels are escaped", () => {
  const html = renderCurrentViews([
    {
      device_id: "camera-a",
      device_name: "<script>alert(1)</script>",
      mode: "unavailable",
      image_url: null,
      summary: "Unavailable",
    },
  ]);

  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});
