import { api } from "./api.js";
import {
  eventSourceBadges,
  pageControls,
  pageHeader,
} from "./components.js";
import { escHtml } from "./dom.js";
import { eventParticipationBadge } from "./review-participation.js";
import { filterValues, filteredHash, option } from "./review-filters.js";
import { fmtTime, plural, titleCase } from "./format.js";
import { groupActivityByDay } from "./review-lists.js";
import { calendarTimeBounds, TIME_RANGE_OPTIONS } from "./time-range.js";
import { showContent, showError, showLoading } from "./view.js";
import { eventTitle } from "./timeline.js";

const PAGE_SIZE = 100;
const COMMON_EVENT_TYPES = [
  "human_detection",
  "vehicle_detection",
  "motion_detection",
  "doorbell",
  "door_access",
  "manual_trigger",
  "tamper_detection",
];

export async function activity(deviceId, page = 1, parameters = new URLSearchParams()) {
  showLoading();
  try {
    const selected = {
      device_id: parameters.get("device_id") || deviceId || "",
      area_id: parameters.get("area_id") || "",
      event_type: parameters.get("event_type") || "",
      event_state: parameters.get("event_state") || "",
      association: parameters.get("association") || "",
      time_range: parameters.get("time_range") || "",
      custom_from: parameters.get("custom_from") || "",
      custom_to: parameters.get("custom_to") || "",
    };
    const timeBounds = calendarTimeBounds(selected);
    const offset = (page - 1) * PAGE_SIZE;
    const query = new URLSearchParams({ limit: PAGE_SIZE + 1, offset });
    for (const key of ["device_id", "area_id", "event_type", "event_state"]) {
      if (selected[key]) query.set(key, selected[key]);
    }
    if (timeBounds.valid) {
      if (timeBounds.from) query.set("observed_from", timeBounds.from);
      if (timeBounds.before) query.set("observed_before", timeBounds.before);
    }
    if (selected.association === "episode") query.set("has_episode", "true");
    if (selected.association === "unassigned") query.set("has_episode", "false");
    const [devices, areas, result] = await Promise.all([
      api("/devices?include_disabled=true"),
      api("/areas?include_disabled=true"),
      api(`/events?${query}`),
    ]);
    const hasNext = result.length > PAGE_SIZE;
    const list = result.slice(0, PAGE_SIZE);
    const deviceNames = new Map(devices.map(device => [device.id, device.name || device.id]));
    const areaNames = new Map(areas.map(area => [area.id, area.name || area.id]));
    const eventTypes = filterValues(list, "event_type", COMMON_EVENT_TYPES, selected.event_type);
    const groups = groupActivityByDay(list);
    const base = filteredHash("activity", selected);
    showContent(`
      ${pageHeader({
        eyebrow: "Review",
        title: "Activity",
        description: "Investigate the normalized Events that caused—or did not cause—an Episode.",
      })}
      <form class="review-filter-bar" onchange="applyReviewFilters(this, 'activity')">
        <label><span>Device</span><select name="device_id">
          ${option("", "All Devices", selected.device_id)}
          ${devices.map(device => option(device.id, device.name || device.id, selected.device_id)).join("")}
        </select></label>
        <label><span>Area</span><select name="area_id">
          ${option("", "All Areas", selected.area_id)}
          ${areas.map(area => option(area.id, area.name || area.id, selected.area_id)).join("")}
        </select></label>
        <label><span>Event</span><select name="event_type">
          ${option("", "All Event types", selected.event_type)}
          ${eventTypes.map(type => option(type, titleCase(type), selected.event_type)).join("")}
        </select></label>
        <label><span>Condition</span><select name="event_state">
          ${option("", "Any reported condition", selected.event_state)}
          ${option("active", "Reported active", selected.event_state)}
          ${option("inactive", "Reported ended", selected.event_state)}
        </select></label>
        <label><span>Episode</span><select name="association">
          ${option("", "Any association", selected.association)}
          ${option("episode", "Linked to an Episode", selected.association)}
          ${option("unassigned", "Not linked to an Episode", selected.association)}
        </select></label>
        <label><span>Time</span><select name="time_range">
          ${TIME_RANGE_OPTIONS.map(([value, label]) => option(value, label, selected.time_range || "all")).join("")}
        </select></label>
        ${selected.time_range === "custom" ? `
          <label class="review-custom-date"><span>From</span><input type="date" name="custom_from" value="${escHtml(selected.custom_from)}"></label>
          <label class="review-custom-date"><span>Through</span><input type="date" name="custom_to" value="${escHtml(selected.custom_to)}"></label>
          ${timeBounds.valid ? "" : '<small class="review-filter-status">Choose a valid start and end date to apply this range.</small>'}
        ` : ""}
        <a class="filter-reset" href="#activity">Reset</a>
      </form>
      ${list.length === 0 ? '<div class="empty-state"><h3>No matching activity</h3><p>Try changing the filters or wait for a new Event.</p></div>' : `
      <div class="activity-feed">
        ${groups.map(group => `<section class="activity-day">
          <header><strong>${escHtml(group.label)}</strong><span>${plural(group.events.length, "Event")}</span></header>
          <div class="activity-day-list">${group.events.map(event => {
            const deviceName = deviceNames.get(event.device_id) || event.device_id;
            const areaName = areaNames.get(event.area_id) || event.area_id;
            return `<article class="activity-entry ${event.episode_id ? "" : "needs-attention"}">
              <time datetime="${escHtml(event.timestamp)}">${fmtTime(event.timestamp)}</time>
              <div class="activity-marker"><span></span></div>
              <div class="activity-entry-body">
                <div class="activity-entry-heading">
                  <div><h3><a href="#event/${event.id}">${escHtml(eventTitle(event))}</a></h3>
                    ${eventParticipationBadge(event.participation)}
                    <div class="activity-context">
                      <span title="Device"><svg><use href="icons.svg#devices"></use></svg><span><small>Device</small><strong>${escHtml(deviceName)}</strong></span></span>
                      <span title="Area"><svg><use href="icons.svg#areas"></use></svg><span><small>Area</small><strong>${escHtml(areaName)}</strong></span></span>
                    </div>
                  </div>
                </div>
                <div class="activity-entry-footer">
                  <div>${eventSourceBadges(event)}</div>
                  <div class="activity-entry-actions">
                    <a href="#event/${event.id}">Inspect Event</a>
                    ${event.episode_id
                      ? `<a href="#episode/${event.episode_id}">Open Episode</a>`
                      : '<span class="association-warning">Not linked to an Episode</span>'}
                  </div>
                </div>
              </div>
            </article>`;
          }).join("")}</div>
        </section>`).join("")}
      </div>`}
      ${pageControls(base, page, list.length, hasNext)}`);
  } catch (error) {
    showError(error.message);
  }
}
