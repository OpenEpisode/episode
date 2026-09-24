import { escHtml } from "./dom.js";

// Mirrors src/episode/domain/event_filter.py. Kept deliberately small: the core
// owns classification and validation, this module only renders the same
// vocabulary so an operator cannot be offered a class the API would reject.
// Every class is offered, at both levels, with a label that says what it holds.
export const EVENT_CLASS_LABELS = {
  motion: "Motion",
  heartbeat: "Status",
  condition: "Audio",
  security: "Tamper / video loss / low battery",
  detection: "Detections, rules and wired alarm inputs",
  access: "Access records",
  unknown: "Unrecognized",
};

export const EVENT_CLASSES = [
  "motion",
  "heartbeat",
  "condition",
  "security",
  "detection",
  "access",
  "unknown",
];

// A Capture Profile and a Device offer exactly the same classes. The two levels
// differ only in whose cameras they speak for, never in what may be suppressed.
export const PROFILE_EVENT_CLASSES = EVENT_CLASSES;
export const DEVICE_EVENT_CLASSES = EVENT_CLASSES;

export const EVENT_FILTER_INHERIT = "inherit";
export const EVENT_FILTER_CUSTOM = "custom";
export const EVENT_FILTER_NONE = "none";

// Display order. Storage and API payloads stay alphabetically sorted; only
// operator-facing text follows the intentional order (everyday noise first,
// then the classes whose suppression carries visible risk, then the two that
// should only ever be suppressed deliberately), so a summary reads the same
// wherever it appears.
export const EVENT_CLASS_ORDER = EVENT_CLASSES;

function displayOrder(classes) {
  const known = EVENT_CLASS_ORDER.filter(className => classes.includes(className));
  return [...known, ...classes.filter(className => !EVENT_CLASS_ORDER.includes(className))];
}

// Presets keep the common intents one click apart. Both levels get the same
// presets, and the widest one says what it means: no observation drives capture.
export const PROFILE_EVENT_FILTER_PRESETS = [
  { id: "", classes: [], label: "No filtering", hint: "Every observation may drive capture." },
  { id: "motion", classes: ["motion"], label: "Motion only", hint: "Plain scene change is noise." },
  {
    id: "motion-status",
    classes: ["motion", "heartbeat"],
    label: "Motion + status",
    hint: "Also drops routine device bookkeeping such as status and battery reports.",
  },
  {
    id: "motion-status-audio",
    classes: ["motion", "heartbeat", "condition"],
    label: "Motion + status + audio",
    hint: "Also drops audio reports.",
  },
  {
    id: "everything",
    classes: [...EVENT_CLASSES],
    label: "Everything — nothing triggers an Episode",
    hint: "No observation opens or extends an Episode. Everything is still recorded and shown as context.",
  },
];

// Both levels offer identical presets; only the Device adds inherit/none states.
export const DEVICE_EVENT_FILTER_PRESETS = PROFILE_EVENT_FILTER_PRESETS;

export function eventFilterPresets(level) {
  return level === "device" ? DEVICE_EVENT_FILTER_PRESETS : PROFILE_EVENT_FILTER_PRESETS;
}

export function eventClassesForLevel(level) {
  return level === "device" ? DEVICE_EVENT_CLASSES : PROFILE_EVENT_CLASSES;
}

export function normalizeEventFilter(value) {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(item => String(item).trim()).filter(Boolean))].sort();
}

// The one thing an operator has to understand before suppressing anything:
// filtering decides whether an observation may *drive* capture. It is never a
// deletion, and it never hides the observation from the record.
export const FILTER_EFFECT_NOTE =
  "A filtered Event never starts an Episode and never extends one. Its payload, receipt, and Event are still stored, and it still appears as context on an Episode that is already running.";
export const FILTER_FIELDSET_NOTE =
  "Tick the event classes you do not want to start or lengthen a recording.";

export function eventFilterSummary(classes) {
  const selector = normalizeEventFilter(classes);
  if (!selector.length) return "No filtering";
  const labels = displayOrder(selector).map(className => EVENT_CLASS_LABELS[className] || className);
  return `Filters: ${labels.join(", ")}`;
}

// Compact suffix for profile and device rows.
export function eventFilterNote(classes) {
  const selector = normalizeEventFilter(classes);
  if (!selector.length) return "";
  return ` · filtering ${displayOrder(selector).join(", ")}`;
}

// The preset is derived from the selection so a saved profile opens on the
// matching option, and any other combination is preserved exactly as Custom.
export function presetForEventFilter(classes, presets) {
  const selector = normalizeEventFilter(classes);
  const match = presets.find(preset => normalizeEventFilter(preset.classes).join() === selector.join());
  return match ? match.id : null;
}

export function eventClassCheckboxes(name, classes, level) {
  const selected = new Set(normalizeEventFilter(classes));
  return eventClassesForLevel(level)
    .map(
      className =>
        `<label class="toggle-row capture-filter-class"><input type="checkbox" name="${escHtml(name)}_class" data-event-class="${escHtml(className)}" value="${escHtml(className)}"${selected.has(className) ? " checked" : ""}><span><strong>${escHtml(EVENT_CLASS_LABELS[className] || className)}</strong><small>${escHtml(className)}</small></span></label>`,
    )
    .join("");
}

function presetOptions(presets, presetId) {
  return presets
    .map(
      preset =>
        `<option value="${escHtml(preset.id)}"${preset.id === presetId ? " selected" : ""}>${escHtml(preset.label)}</option>`,
    )
    .join("");
}

function filterFieldset(name, classes, level, note, custom) {
  return `<fieldset class="capture-filter-classes" data-filter-custom${custom ? "" : " hidden"}>
    <legend>Custom selection (used only when Custom… is chosen)</legend>
    ${eventClassCheckboxes(name, classes, level)}
    <p class="configuration-note">${escHtml(note)}</p>
  </fieldset>`;
}

export function eventFilterSelect(name, classes, level) {
  const presets = eventFilterPresets(level);
  const current = normalizeEventFilter(classes);
  const presetId = presetForEventFilter(current, presets);
  const custom = presetId === null;
  return `<div class="capture-filter-control">
    <label class="field capture-filter-field"><span>Filtered event classes</span>
      <select name="${escHtml(name)}" data-filter-preset>${presetOptions(presets, presetId)}<option value="${EVENT_FILTER_CUSTOM}"${custom ? " selected" : ""}>Custom…</option></select>
      <small class="capture-filter-summary">${escHtml(eventFilterSummary(current))}</small>
      <small>${escHtml(FILTER_EFFECT_NOTE)}</small>
    </label>
    ${filterFieldset(name, current, level, FILTER_FIELDSET_NOTE, custom)}
  </div>`;
}

// Device level adds one state the profile level cannot have: no override at all,
// so the active Capture profile keeps deciding for this camera. The stored value
// is an array (possibly empty) or `null` for inherit, and the two render apart.
export function deviceEventFilterSelect(name, value, options = {}) {
  const presets = eventFilterPresets("device");
  const inherits = value === null || value === undefined;
  const current = normalizeEventFilter(value || []);
  const presetId = inherits ? EVENT_FILTER_INHERIT : presetForEventFilter(current, presets);
  const custom = !inherits && presetId === null;
  const summary = inherits
    ? `Follows the active Capture profile${options.profileName ? ` (${options.profileName})` : ""}.`
    : eventFilterSummary(current);
  return `<div class="capture-filter-control">
    <label class="field capture-filter-field"><span>Filtered event classes</span>
      <select name="${escHtml(name)}" data-filter-preset>
        <option value="${EVENT_FILTER_INHERIT}"${presetId === EVENT_FILTER_INHERIT ? " selected" : ""}>Inherit from Capture profile</option>
        ${presetOptions(presets, presetId)}
        <option value="${EVENT_FILTER_CUSTOM}"${custom ? " selected" : ""}>Custom…</option>
      </select>
      <small class="capture-filter-summary">${escHtml(summary)}</small>
      <small>${escHtml(FILTER_EFFECT_NOTE)} This camera’s own setting wins over the active Capture profile, so “No filtering” keeps every observation here even while the profile filters.</small>
    </label>
    ${filterFieldset(name, current, "device", FILTER_FIELDSET_NOTE, custom)}
  </div>`;
}

// Reads one submitted control group. A preset expands to its classes, `custom`
// submits the checkbox group, and Device `inherit` means no override at all.
// Returns `undefined` when the control was not submitted, or when a value is not
// recognised, so a caller leaves the stored selector untouched instead of
// silently widening or narrowing filtering.
export function resolveEventFilter(data, name, level) {
  const submitted = data.get?.(name);
  if (submitted === undefined || submitted === null) return undefined;
  const value = String(submitted);
  if (level === "device" && value === EVENT_FILTER_INHERIT) return null;
  if (value === EVENT_FILTER_CUSTOM) return selectedEventClasses(data, name, level);
  const preset = eventFilterPresets(level).find(item => item.id === value);
  // An unknown preset id can only come from a mismatched UI, so keep the stored
  // selector rather than guessing one. Presets list classes in reading order;
  // what is stored and sent is always the canonical sorted form.
  return preset ? normalizeEventFilter(preset.classes) : undefined;
}

export function selectedEventClasses(data, name, level) {
  const values =
    typeof data.getAll === "function" ? data.getAll(`${name}_class`) : [data.get?.(`${name}_class`)];
  const allowed = eventClassesForLevel(level);
  const picked = values.filter(Boolean).map(item => String(item).trim());
  return normalizeEventFilter(picked.filter(className => allowed.includes(className)));
}

// Keeps the visible summary honest when an operator switches preset, and reports
// the effective selection back so a caller can gate on it (see `security`).
export function wireEventFilterSummary(root, level, onChange = null) {
  const select = root.querySelector?.("[data-filter-preset]");
  if (!select) return null;
  // A summary is nice to have; the control group is still worth wiring without
  // one, so callers can gate on the effective selection.
  const summary = root.querySelector?.(".capture-filter-summary");
  const customFieldset = root.querySelector?.("[data-filter-custom]");
  const customInputs = [...(root.querySelectorAll?.("[data-filter-custom] input") || [])];
  const classesNow = () => {
    const value = String(select.value ?? "");
    if (level === "device" && value === EVENT_FILTER_INHERIT) return [];
    if (value === EVENT_FILTER_CUSTOM) {
      return normalizeEventFilter(customInputs.filter(input => input.checked).map(input => input.value));
    }
    const preset = eventFilterPresets(level).find(item => item.id === value);
    return preset ? normalizeEventFilter(preset.classes) : [];
  };
  const update = () => {
    if (customFieldset) customFieldset.hidden = String(select.value ?? "") !== EVENT_FILTER_CUSTOM;
    const classes = classesNow();
    if (summary) {
      summary.textContent =
        level === "device" && String(select.value ?? "") === EVENT_FILTER_INHERIT
          ? "Follows the active Capture profile."
          : eventFilterSummary(classes);
    }
    onChange?.(classes);
  };
  select.addEventListener("change", update);
  customInputs.forEach(input => input.addEventListener("change", update));
  update();
  return classesNow;
}

// One-line description for Device detail views, where `null` and `[]` must not
// read the same: inherited policy and an explicit negative are different
// operator decisions.
export function describeDeviceEventFilter(value, profileName = "") {
  if (value === null || value === undefined) {
    return profileName ? `Inherited from ${profileName}` : "Inherited from Capture profile";
  }
  const selector = normalizeEventFilter(value);
  if (!selector.length) return "No filtering (explicit)";
  return displayOrder(selector).map(className => EVENT_CLASS_LABELS[className] || className).join(", ");
}

export function deviceFiltersSecurity(value) {
  return normalizeEventFilter(value).includes("security");
}

// Marker for the Device list. Suppressing the security class is deliberate but
// costly, so it stays visible instead of hiding inside the edit dialog.
// Inherited or narrow selectors get no marker: the risk belongs to this camera,
// not to a profile it happens to follow.
export function deviceEventFilterBadge(value) {
  if (!deviceFiltersSecurity(value)) return "";
  return `<span class="badge badge-filter-security">Tamper / video loss not captured</span>`;
}
