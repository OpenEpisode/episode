import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/event-filter.js", import.meta.url),
  "utf8",
);
const domUrl = moduleUrl(`export function escHtml(value) { return String(value ?? ""); }`);

const mod = await import(
  moduleUrl(source.replace('"./dom.js"', JSON.stringify(domUrl)))
);

function form(entries) {
  const map = new Map(entries);
  return {
    get: name => (map.has(name) ? map.get(name) : null),
    getAll: name => (map.has(name) ? [map.get(name)] : []),
  };
}

function multiForm(name, values, extra = []) {
  const map = new Map(extra);
  map.set(name, values[0]);
  return {
    get: key => (map.has(key) ? map.get(key) : null),
    getAll: key => (key === name ? values : map.has(key) ? [map.get(key)] : []),
  };
}

test("both levels are offered every class", () => {
  // Consistency rule: a filter means the same thing whatever it names and
  // whoever it is applied to, so neither level hides a class from the operator.
  assert.deepEqual(mod.PROFILE_EVENT_CLASSES, mod.EVENT_CLASSES);
  assert.deepEqual(mod.DEVICE_EVENT_CLASSES, mod.EVENT_CLASSES);
  for (const level of ["profile", "device"]) {
    const markup = mod.eventFilterSelect("event_filter", ["motion"], level);
    for (const className of mod.EVENT_CLASSES) {
      assert.match(markup, new RegExp(`value="${className}"`), `${level}: ${className}`);
    }
  }
  // The widest preset states its effect rather than editorialising about it.
  const everything = mod.eventFilterPresets("profile").at(-1);
  assert.deepEqual(mod.normalizeEventFilter(everything.classes), [...mod.EVENT_CLASSES].sort());
  assert.match(everything.label, /nothing triggers an Episode/);
});

test("the control says what filtering does before it is chosen", () => {
  for (const markup of [
    mod.eventFilterSelect("event_filter", ["motion"], "profile"),
    mod.deviceEventFilterSelect("event_filter", ["motion"]),
  ]) {
    assert.match(
      markup,
      /<div class="capture-filter-control">\s*<label class="field capture-filter-field">[\s\S]*<fieldset class="capture-filter-classes" data-filter-custom hidden>[\s\S]*<\/fieldset>\s*<\/div>/,
    );
    assert.match(markup, /never starts an Episode and never extends one/);
    assert.match(markup, /payload, receipt, and Event are still stored/);
    assert.match(markup, /appears as context on an Episode that is already running/);
  }
});

test("inherit and the explicit negative render and resolve apart", () => {
  const inherits = mod.deviceEventFilterSelect("event_filter", null);
  assert.match(inherits, /option value="inherit" selected/);
  assert.match(inherits, /Follows the active Capture profile/);
  assert.doesNotMatch(inherits, /option value="" selected/);

  const negative = mod.deviceEventFilterSelect("event_filter", []);
  assert.match(negative, /option value="" selected/);
  // The empty selector reads exactly as the shared summary spells it, and not
  // as the inherit state above it.
  assert.match(negative, /class="capture-filter-summary">No filtering<\/small>/);
  assert.doesNotMatch(negative, /option value="inherit" selected/);

  assert.equal(mod.resolveEventFilter(form([["event_filter", "inherit"]]), "event_filter", "device"), null);
  assert.deepEqual(mod.resolveEventFilter(form([["event_filter", ""]]), "event_filter", "device"), []);
});

test("a saved selection opens on its preset and anything else on Custom", () => {
  assert.equal(mod.presetForEventFilter(["heartbeat", "motion"], mod.PROFILE_EVENT_FILTER_PRESETS), "motion-status");
  assert.equal(mod.presetForEventFilter(["condition"], mod.PROFILE_EVENT_FILTER_PRESETS), null);
  const custom = mod.deviceEventFilterSelect("event_filter", ["condition"]);
  assert.match(custom, /option value="custom" selected/);
  assert.match(custom, /<fieldset class="capture-filter-classes" data-filter-custom>/);
  // The ticked boxes still describe the stored selection.
  assert.match(custom, /value="condition" checked/);
  assert.match(custom, /value="motion"(?! checked)/);
  assert.match(
    mod.deviceEventFilterSelect("event_filter", ["motion"]),
    /<fieldset class="capture-filter-classes" data-filter-custom hidden>/,
  );
});

test("resolution de-duplicates and never guesses", () => {
  const custom = multiForm("event_filter_class", ["motion", "detection", "unknown", "motion"], [
    ["event_filter", "custom"],
  ]);
  // Every class is selectable now, so only the duplicate is dropped.
  assert.deepEqual(mod.resolveEventFilter(custom, "event_filter", "profile"), [
    "detection",
    "motion",
    "unknown",
  ]);

  // An unknown preset id leaves the stored selector untouched (undefined) rather
  // than substituting one.
  assert.deepEqual(mod.resolveEventFilter(form([["event_filter", "everything"]]), "event_filter", "profile"), [...mod.EVENT_CLASSES].sort());
  // An unknown preset id still leaves the stored selector untouched (undefined).
  assert.equal(mod.resolveEventFilter(form([["event_filter", "not-a-preset"]]), "event_filter", "profile"), undefined);
  assert.equal(mod.resolveEventFilter(form([["event_filter", "not-a-preset"]]), "event_filter", "device"), undefined);
  // Absent control means the same thing.
  assert.equal(mod.resolveEventFilter(form([]), "event_filter", "profile"), undefined);
});

test("summaries read in the intentional order and never overstate", () => {
  assert.equal(mod.eventFilterSummary([]), "No filtering");
  assert.equal(
    mod.eventFilterSummary(["security", "motion"]),
    "Filters: Motion, Tamper / video loss / low battery",
  );
  assert.equal(mod.eventFilterNote(["heartbeat", "motion"]), " · filtering motion, heartbeat");
  assert.equal(mod.eventFilterNote([]), "");
  // `security` holds the battery report that matters, and its label says so:
  // choosing it must not read as "just tamper".
  assert.equal(mod.eventFilterSummary(["condition"]), "Filters: Audio");
  // The wired alarm input lives in `detection`, and its label has to say so: an
  // operator dropping object detections must be able to see that a wired
  // trigger goes with them.
  assert.match(mod.EVENT_CLASS_LABELS.detection, /wired/);
  assert.equal(mod.eventFilterSummary(["access", "unknown"]), "Filters: Access records, Unrecognized");
});

test("Device detail distinguishes inherited policy from an explicit negative", () => {
  assert.equal(mod.describeDeviceEventFilter(null, "Night"), "Inherited from Night");
  assert.equal(mod.describeDeviceEventFilter(null), "Inherited from Capture profile");
  assert.equal(mod.describeDeviceEventFilter([]), "No filtering (explicit)");
  assert.equal(
    mod.describeDeviceEventFilter(["security", "motion"]),
    "Motion, Tamper / video loss / low battery",
  );
});

test("only a camera that filters security is marked in the list", () => {
  assert.equal(mod.deviceEventFilterBadge(null), "");
  assert.equal(mod.deviceEventFilterBadge([]), "");
  assert.equal(mod.deviceEventFilterBadge(["motion", "heartbeat"]), "");
  const marked = mod.deviceEventFilterBadge(["motion", "security"]);
  assert.match(marked, /badge-filter-security/);
  assert.match(marked, /not captured/);
});

test("there is only one control per class: no separate confirmation checkbox", () => {
  // A second, isolated tick for tamper implied the class selection was not the
  // operator's decision. The class list is now the single source of truth.
  assert.doesNotMatch(mod.deviceEventFilterSelect("event_filter", ["security"]), /_security_ack/);
  assert.doesNotMatch(mod.deviceEventFilterSelect("event_filter", null), /security-ack/);
});

test("the visible summary follows the control an operator changes", () => {
  const listeners = [];
  const summary = { textContent: "" };
  const customFieldset = { hidden: false };
  const select = {
    value: "motion",
    addEventListener: (type, handler) => listeners.push({ type, handler }),
  };
  const checkbox = {
    checked: false,
    value: "heartbeat",
    addEventListener: (type, handler) => listeners.push({ type, handler }),
  };
  const root = {
    querySelector: selector =>
      selector === "[data-filter-preset]"
        ? select
        : selector === ".capture-filter-summary"
          ? summary
          : selector === "[data-filter-custom]"
            ? customFieldset
          : null,
    querySelectorAll: selector => (selector === "[data-filter-custom] input" ? [checkbox] : []),
  };
  const read = mod.wireEventFilterSummary(root, "device", () => {});
  assert.equal(summary.textContent, "Filters: Motion");
  assert.equal(customFieldset.hidden, true);
  assert.equal(read().join(), "motion");

  select.value = "";
  listeners.find(listener => listener.type === "change").handler();
  // The explicit negative reads as the shared summary spells it, not as inherit.
  assert.equal(summary.textContent, "No filtering");

  select.value = "inherit";
  listeners.find(listener => listener.type === "change").handler();
  assert.equal(summary.textContent, "Follows the active Capture profile.");
  assert.deepEqual(read(), []);

  select.value = "custom";
  checkbox.checked = true;
  listeners.find(listener => listener.type === "change").handler();
  assert.equal(summary.textContent, "Filters: Status");
  assert.equal(customFieldset.hidden, false);
  assert.deepEqual(read(), ["heartbeat"]);

  select.value = "motion";
  listeners.find(listener => listener.type === "change").handler();
  assert.equal(customFieldset.hidden, true);
});
