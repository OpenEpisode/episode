import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const uiFile = name => readFile(
  new URL("../../src/episode/ui/" + name, import.meta.url),
  "utf8",
);

const domUrl = moduleUrl(await uiFile("dom.js"));
const formatUrl = moduleUrl(await uiFile("format.js"));
const componentsUrl = moduleUrl(
  (await uiFile("components.js"))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js?v=3"', JSON.stringify(formatUrl)),
);
const {
  detailMetric,
  eventBadge,
  pageControls,
  pageHeader,
  sectionHeading,
  sourceBadges,
  stateBadge,
} = await import(componentsUrl);

test("shared badges normalize classes and escape external labels", () => {
  assert.equal(
    eventBadge('Motion Detection<script>'),
    '<span class="badge badge-motion-detection-script-">Motion Detection&lt;script&gt;</span>',
  );
  assert.equal(
    sourceBadges(['camera<a>', "onvif"]),
    '<span class="label">camera&lt;a&gt;</span> <span class="label">onvif</span>',
  );
  assert.match(
    sourceBadges([{
      kind: "plugin",
      id: "hikvision-sdk",
      name: "Hikvision HCNetSDK",
      source: "hikvision:sdk",
    }]),
    /<span class="source-chip-kind">plugin<\/span>\s*<span>Hikvision HCNetSDK<\/span>/,
  );
  assert.equal(
    stateBadge("closed"),
    '<span class="badge badge-closed">closed</span>',
  );
});

test("source badges have a deterministic presentation order", () => {
  const alarmServer = {
    kind: "plugin",
    id: "hikvision-alarm-server",
    name: "Hikvision Alarm Server",
    source: "hikvision:alarm_server",
  };
  const isapi = {
    kind: "plugin",
    id: "hikvision-isapi",
    name: "Hikvision ISAPI",
    source: "hikvision:isapi",
  };

  const forward = sourceBadges([alarmServer, isapi]);
  const reverse = sourceBadges([isapi, alarmServer]);
  assert.equal(reverse, forward);
  assert.ok(forward.indexOf("Hikvision Alarm Server") < forward.indexOf("Hikvision ISAPI"));
});

test("page header provides one consistent product heading contract", () => {
  const header = pageHeader({
    eyebrow: "Review",
    title: "Episodes",
    description: "Preserved evidence",
    actions: '<button type="button">Add</button>',
  });

  assert.match(header, /class="page-heading"/);
  assert.match(header, /<h2>Episodes<\/h2>/);
  assert.match(header, /class="page-actions"/);
});

test("detail components keep labels consistent and escape external values", () => {
  const metric = detailMetric("devices", "Device", '<camera id="1">', "#device/camera-1");
  assert.match(metric, /class="review-detail-metric"/);
  assert.match(metric, /Device/);
  assert.match(metric, /&lt;camera id=&quot;1&quot;&gt;/);
  assert.doesNotMatch(metric, /<camera/);

  const heading = sectionHeading(
    "evidence",
    "File and integrity",
    "Technical <facts>",
    '<span class="review-section-count">2</span>',
  );
  assert.match(heading, /Technical &lt;facts&gt;/);
  assert.match(heading, /review-section-count/);
});

test("pagination exposes bounded older and newer navigation", () => {
  assert.equal(pageControls("#episodes", 1, 12, false), "");
  const controls = pageControls("#episodes", 2, 48, true);
  assert.match(controls, /href="#episodes\?page=1"/);
  assert.match(controls, /href="#episodes\?page=3"/);
});
