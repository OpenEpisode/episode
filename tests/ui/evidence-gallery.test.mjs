import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const uiFile = name => readFile(
  new URL("../../src/episode/ui/" + name, import.meta.url),
  "utf8",
);

globalThis.window = {};
globalThis.document = { addEventListener() {} };

const domUrl = moduleUrl(await uiFile("dom.js"));
const formatUrl = moduleUrl(await uiFile("format.js"));
const apiUrl = moduleUrl(
  (await uiFile("api.js")).replace('"./dom.js"', JSON.stringify(domUrl)),
);
const mediaUrl = moduleUrl(
  "export function attachMediaSource() { return () => {}; } "
  + "export function evidenceMediaUrl() { return '/media'; } "
  + "export function isHlsEvidence(item) { return item?.metadata?.format === 'hls-fmp4'; } "
  + "export function updateMediaStatus() {}",
);
const galleryUrl = moduleUrl(
  (await uiFile("evidence-gallery.js"))
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js?v=3"', JSON.stringify(formatUrl))
    .replace('"./media-player.js?v=7"', JSON.stringify(mediaUrl)),
);
const { renderEvidenceGrid } = await import(galleryUrl);

test("Evidence collections use cached thumbnails while viewers retain originals", () => {
  const html = renderEvidenceGrid([
    {
      id: "snapshot-1",
      device_id: "camera",
      evidence_type: "snapshot",
      mime_type: "image/jpeg",
      metadata: {},
    },
    {
      id: "recording-1",
      device_id: "camera",
      evidence_type: "recording",
      mime_type: "video/mp4",
      metadata: {},
    },
    {
      id: "expired-snapshot",
      device_id: "camera",
      evidence_type: "snapshot",
      mime_type: "image/jpeg",
      availability: "expired",
      metadata: {},
    },
  ]);

  assert.match(html, /evidence\/snapshot-1\/thumbnail/);
  assert.match(html, /evidence\/recording-1\/thumbnail/);
  assert.match(html, /this\.src='\/api\/v1\/evidence\/snapshot-1\/file'/);
  assert.match(html, /this\.hidden=true/);
  assert.doesNotMatch(html, /<video/);
  assert.doesNotMatch(html, /evidence\/expired-snapshot\/thumbnail/);
  assert.match(html, /Visual Evidence expired under the retention policy/);
});
