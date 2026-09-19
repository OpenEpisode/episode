import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/review-pages.js", import.meta.url),
  "utf8",
);

const apiUrl = moduleUrl(`
  export const API = "/api/v1";
  export async function api() { return []; }
  export async function apiAll() { return []; }
  export async function apiBlob() { return new Blob(); }
`);
const componentsUrl = moduleUrl(`
  export function detailMetric() { return ""; }
  export function episodeStateBadge() { return ""; }
  export function episodeTriggerBadge() { return ""; }
  export function eventBadge() { return ""; }
  export function eventSourceBadges() { return ""; }
  export function pageControls() { return ""; }
  export function pageHeader() { return ""; }
  export function sectionHeading() { return ""; }
  export function stateBadge() { return ""; }
`);
const emptyUrl = moduleUrl(`
  export function closeDeliveryViewer() {}
  export function openDeliveryViewer() {}
  export function activateCurrentViews() {}
  export function deactivateCurrentViews() {}
  export function renderCurrentViews() { return ""; }
  export function episodeDisplayEnd() { return ""; }
  export function episodeRailTime() { return ""; }
  export function groupEpisodesByTime() { return []; }
  export function attachMediaSource() {}
  export function evidenceMediaUrl() { return ""; }
  export function isHlsEvidence() { return false; }
  export function updateMediaStatus() {}
  export function originBadge() { return ""; }
  export function renderEvidenceArchive() { return ""; }
  export function renderEpisodeEvidence() { return ""; }
  export function renderEvidenceGrid() { return ""; }
  export function showCarousel() {}
  export function activateEpisodeWorkspace() {}
  export function renderEpisodeWorkspace() { return { html: "", model: {} }; }
  export function groupActivityByDay() { return []; }
  export function groupEvidenceBundlesByDay() { return []; }
  export function groupEvidenceByEpisode() { return []; }
  export function updateRecentEpisodes() {}
  export function showContent() {}
  export function showError() {}
  export function showLoading() {}
  export function eventTitle() { return "Event"; }
`);
const domUrl = moduleUrl(`export function escHtml(value) { return String(value ?? ""); }`);
const formatUrl = moduleUrl(`
  export function fmt() { return "time"; }
  export function fmtBytes(value) { return String(value ?? 0); }
  export function fmtDuration() { return ""; }
  export function fmtShort(value) { return String(value ?? ""); }
  export function fmtTime() { return "time"; }
  export function plural(value, label) { return value + " " + label; }
  export function titleCase(value) {
    return String(value ?? "")
      .replaceAll("_", " ")
      .replace(/\\b\\w/g, letter => letter.toUpperCase());
  }
  export function trunc(value) { return value; }
`);

globalThis.window = {};
const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=6"', JSON.stringify(componentsUrl))
    .replace('"./delivery-viewer.js?v=1"', JSON.stringify(emptyUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./current-views.js?v=3"', JSON.stringify(emptyUrl))
    .replace('"./episode-list.js?v=3"', JSON.stringify(emptyUrl))
    .replace('"./media-player.js?v=2"', JSON.stringify(emptyUrl))
    .replace('"./evidence-gallery.js?v=7"', JSON.stringify(emptyUrl))
    .replace('"./episode-view.js?v=13"', JSON.stringify(emptyUrl))
    .replace('"./format.js?v=3"', JSON.stringify(formatUrl))
    .replace('"./review-lists.js?v=3"', JSON.stringify(emptyUrl))
    .replace('"./sidebar.js?v=4"', JSON.stringify(emptyUrl))
    .replace('"./view.js?v=1"', JSON.stringify(emptyUrl))
    .replace('"./timeline.js?v=6"', JSON.stringify(emptyUrl)),
));

test("capture participation stays absent for allowed and legacy Events", () => {
  assert.equal(module.eventParticipationBadge(null), "");
  assert.equal(module.eventParticipationBadge({ allowed: true }), "");
  assert.equal(module.eventParticipationNotice({ allowed: true }), "");
});

test("excluded participation is explicit in badges and Event detail guidance", () => {
  const participation = {
    allowed: false,
    profile_id: "night",
    profile_name: "Night",
    reason: "device_not_in_profile",
    evaluated_at: "2026-09-09T10:00:00Z",
  };
  const badge = module.eventParticipationBadge(participation);
  const notice = module.eventParticipationNotice(participation);

  assert.match(badge, /Capture excluded · Night/);
  assert.match(badge, /badge-capture-excluded/);
  assert.match(notice, /This observation was preserved/);
  assert.match(notice, /did not open or extend an Episode/);
  assert.match(notice, /did not join a new recording/);
  assert.match(notice, /device not in profile/i);
  assert.match(notice, /2026-09-09T10:00:00Z/);
});

test("filtered participation names the class and the level that decided", () => {
  const byProfile = {
    allowed: false,
    profile_id: "night",
    profile_name: "Night",
    reason: "generic_event_filtered",
    filtered_event_type: "motion_detection",
    filtered_event_class: "motion",
    filter_source: "profile",
    attachment: "attached",
    evaluated_at: "2026-09-09T10:00:00Z",
  };
  const badge = module.eventParticipationBadge(byProfile);
  const notice = module.eventParticipationNotice(byProfile);

  assert.match(badge, /Filtered · Motion · by Night/);
  assert.match(badge, /badge-capture-filtered/);
  assert.match(notice, /filtered by Night/);
  assert.match(notice, /\(Motion\)/);
  // Attachment is the v3 outcome: attributed without extending anything.
  assert.match(notice, /attributed to the Episode that was already open/);
  assert.match(notice, /did not extend the Episode, restart it, or start a recording/);
  assert.match(notice, /motion_detection/);
});

test("a Device-level selection is not credited to the profile", () => {
  const byDevice = {
    allowed: false,
    profile_id: "night",
    profile_name: "Night",
    reason: "generic_event_filtered",
    filtered_event_type: "system",
    filtered_event_class: "heartbeat",
    filter_source: "device",
    attachment: "no_open_episode",
  };
  assert.match(module.eventParticipationBadge(byDevice), /Filtered · Heartbeat · by this camera/);
  const notice = module.eventParticipationNotice(byDevice);
  assert.match(notice, /filtered by this camera/);
  assert.doesNotMatch(notice, /filtered by Night/);
  // Nothing was open, so the Event is not claimed as part of an Episode.
  assert.match(notice, /stays unassigned/);
  assert.doesNotMatch(notice, /already open for this Area/);
});

test("a filtered Event predating the attachment field still reads correctly", () => {
  const legacy = {
    allowed: false,
    profile_id: "night",
    reason: "generic_event_filtered",
    filtered_event_type: "motion_detection",
    evaluated_at: "2026-09-12T10:00:00Z",
  };
  const badge = module.eventParticipationBadge(legacy);
  const notice = module.eventParticipationNotice(legacy);
  // No class or source was recorded, so nothing is invented for it.
  assert.match(badge, /^<span class="badge badge-capture-filtered">Filtered<\/span>$/);
  assert.match(notice, /did not open or extend an Episode/);
  assert.doesNotMatch(notice, /undefined|night · night/);
});
