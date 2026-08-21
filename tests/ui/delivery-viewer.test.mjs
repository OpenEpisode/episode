import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const uiFile = name => readFile(
  new URL("../../src/episode/ui/" + name, import.meta.url),
  "utf8",
);

const apiUrl = moduleUrl('export const API = "/api/v1";');
const domUrl = moduleUrl(await uiFile("dom.js"));
const formatUrl = moduleUrl(await uiFile("format.js"));
const viewerUrl = moduleUrl(
  (await uiFile("delivery-viewer.js"))
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js?v=3"', JSON.stringify(formatUrl)),
);
const { formatTextPayload, prettyXml } = await import(viewerUrl);

test("delivery viewer formats JSON without changing its values", () => {
  assert.deepEqual(formatTextPayload('{"active":true,"count":2}', "application/json"), {
    language: "json",
    text: '{\n  "active": true,\n  "count": 2\n}',
  });
});

test("delivery viewer makes XML readable and preserves transport prefixes", () => {
  const formatted = prettyXml(
    "--boundary\r\nContent-Type: application/xml\r\n\r\n"
      + "<?xml version=\"1.0\"?><EventNotificationAlert><eventType>VMD</eventType></EventNotificationAlert>",
  );

  assert.match(formatted, /^--boundary[\s\S]*<\?xml version="1\.0"\?>/);
  assert.match(formatted, /\n  <eventType>VMD<\/eventType>\n<\/EventNotificationAlert>$/);
});
