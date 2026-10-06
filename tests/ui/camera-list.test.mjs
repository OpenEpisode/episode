import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");

const source = await readFile(
  new URL("../../src/episode/ui/camera-list.js", import.meta.url),
  "utf8",
);

const domUrl = moduleUrl(`
  export function escHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }
`);
const apiUrl = moduleUrl(`
  export const API = "/api/v1";
  globalThis.cameraApiCalls = globalThis.cameraApiCalls || [];
  export async function api(path) {
    globalThis.cameraApiCalls.push(path);
    return [];
  }
`);
const formatUrl = moduleUrl(`
  export function trunc(value, length) {
    const text = String(value ?? "");
    return text.length > length ? text.slice(0, length - 1) + "…" : text;
  }
  export function titleCase(value) {
    return String(value ?? "").replace(/_/g, " ").replace(/\\b\\w/g, c => c.toUpperCase());
  }
`);
const inventoryUrl = moduleUrl(`
  export function operationalIndicator(state) {
    if (state === "healthy") return "online";
    if (state === "degraded") return "warning";
    if (state === "disabled" || state === "unknown") return "idle";
    return "offline";
  }
`);

const cameraListUrl = moduleUrl(
  source
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./api.js"', JSON.stringify(apiUrl))
    .replace('"./format.js"', JSON.stringify(formatUrl))
    .replace('"./inventory-pages.js"', JSON.stringify(inventoryUrl)),
);
const { cameraStatus, renderCameraList, mountCameraList } = await import(cameraListUrl);

test("cameraStatus maps OperationalState to a status dot", () => {
  assert.equal(cameraStatus("healthy"), "online");
  assert.equal(cameraStatus("degraded"), "warning");
  assert.equal(cameraStatus("unavailable"), "offline");
  assert.equal(cameraStatus("disabled"), "offline");
  assert.equal(cameraStatus("unknown"), "offline");
});

test("renderCameraList renders a listbox with one option per camera", () => {
  const html = renderCameraList(
    [
      { id: "cam-1", name: "Front", state: "healthy", identity: { model: "DS-2CD" } },
      { id: "cam-2", name: "Back", state: "disabled", identity: {} },
    ],
    "cam-1",
  );
  assert.match(html, /role="listbox"/);
  assert.match(html, /data-cam-id="cam-1"/);
  assert.match(html, /data-cam-id="cam-2"/);
  assert.match(html, /aria-selected="true"/);
  assert.match(html, /class="tl-cam selected"/); // cam-1: selected + healthy (online)
  assert.match(html, /class="tl-cam offline"/); // cam-2: disabled (offline)
  assert.match(html, /Front/);
  assert.match(html, /DS-2CD/);
});

test("renderCameraList shows an empty state when there are no cameras", () => {
  assert.match(renderCameraList([], null), /No cameras configured/);
});

function fakeMount(cameras, selectedId) {
  const onSelectCalls = [];
  const buttons = cameras.map(device => ({
    dataset: { camId: device.id },
    classList: { toggle(name, active) { this[name] = active; } },
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = value; },
    querySelector(sel) {
      return sel === ".tl-cam-img" ? { src: "", dataset: {} } : null;
    },
  }));
  const handlers = {};
  const container = {
    innerHTML: "",
    querySelector(sel) {
      if (sel !== ".tl-cam-list") return null;
      return {
        querySelectorAll(sel) {
          return sel === ".tl-cam" ? buttons : [];
        },
        addEventListener(type, fn) { handlers[type] = fn; },
        removeEventListener(type) { delete handlers[type]; },
      };
    },
  };
  const cleanup = mountCameraList(container, {
    cameras,
    selectedId,
    onSelect: id => onSelectCalls.push(id),
  });
  return { container, buttons, onSelectCalls, cleanup, handlers };
}

test("mountCameraList wires click selection and updates aria-selected", () => {
  const cameras = [
    { id: "cam-1", name: "Front", state: "healthy", identity: {} },
    { id: "cam-2", name: "Back", state: "healthy", identity: {} },
  ];
  const { buttons, onSelectCalls, handlers, cleanup } = fakeMount(cameras, "cam-1");

  // Simulate a click landing on the second camera.
  handlers.click({ target: { closest: () => buttons[1] } });

  assert.equal(buttons[1].classList.selected, true);
  assert.equal(buttons[1].attrs["aria-selected"], "true");
  assert.equal(buttons[0].classList.selected, false);
  assert.equal(buttons[0].attrs["aria-selected"], "false");
  assert.deepEqual(onSelectCalls, ["cam-2"]);

  cleanup();
});

test("mountCameraList requests the latest snapshot thumbnail per camera", async () => {
  globalThis.cameraApiCalls = [];
  const cameras = [
    { id: "cam-1", name: "Front", state: "healthy", identity: {} },
    { id: "cam-2", name: "Back", state: "healthy", identity: {} },
  ];
  const { cleanup } = fakeMount(cameras, "cam-1");
  await new Promise(resolve => setTimeout(resolve, 0));
  cleanup();

  assert.deepEqual(globalThis.cameraApiCalls, [
    "/evidence?device_id=cam-1&evidence_type=snapshot&limit=1",
    "/evidence?device_id=cam-2&evidence_type=snapshot&limit=1",
  ]);
});
