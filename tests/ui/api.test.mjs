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
const apiUrl = moduleUrl(
  (await uiFile("api.js")).replace('"./dom.js"', JSON.stringify(domUrl)),
);
const { apiAll, apiRequest } = await import(apiUrl);

test("apiAll follows limit and offset pages until the collection is exhausted", async t => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const requested = [];
  globalThis.fetch = async url => {
    requested.push(url);
    const offset = Number(new URL(url, "http://episode.test").searchParams.get("offset"));
    const items = offset === 0 ? [{ id: 1 }, { id: 2 }] : [{ id: 3 }];
    return new Response(JSON.stringify(items), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  assert.deepEqual(await apiAll("/episodes/example/events", 2), [
    { id: 1 },
    { id: 2 },
    { id: 3 },
  ]);
  assert.deepEqual(requested, [
    "/api/v1/episodes/example/events?limit=2&offset=0",
    "/api/v1/episodes/example/events?limit=2&offset=2",
  ]);
});

test("API requests surface the stable error message and validation details", async t => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: {
      code: "validation_error",
      message: "Request validation failed",
      details: [{ message: "Network address is required" }],
    },
  }), {
    status: 422,
    headers: { "Content-Type": "application/json" },
  });

  await assert.rejects(
    apiRequest("/devices", { method: "POST", body: {} }),
    /Request validation failed · Network address is required/,
  );
});
