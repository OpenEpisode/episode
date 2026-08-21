import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../../src/episode/ui/format.js", import.meta.url),
  "utf8",
);
const moduleUrl = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const { fmtBytes } = await import(moduleUrl);

test("storage sizes use compact binary units", () => {
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(1536), "1.5 KiB");
  assert.equal(fmtBytes(12 * 1024 * 1024), "12 MiB");
  assert.equal(fmtBytes(null), "—");
});
