import { escHtml } from "./dom.js";
import { fmtShort, titleCase } from "./format.js";

function participationProfileName(participation) {
  return participation?.profile_name || participation?.profile_id || "active profile";
}

// Which level decided, phrased for an operator. ``device`` means this camera's
// own selector suppressed the Event, so naming the profile would mislead. An
// older decision recorded no source, and none is invented for it.
function participationFilterSource(participation) {
  if (participation?.filter_source === "device") return "this camera";
  if (participation?.filter_source === "profile") return participationProfileName(participation);
  return "";
}

// One clause naming the level that suppressed the Event. Without a recorded
// source (a ``beta.7`` row) it falls back to the active profile, which is the
// factual snapshot recorded with the decision.
function participationFilterBy(participation) {
  const source = participationFilterSource(participation);
  return source ? ` is filtered by ${source}` : " is filtered by the policy in force";
}

function participationFilterLabel(participation) {
  const source = participationFilterSource(participation);
  return [
    "Filtered",
    participation?.filtered_event_class ? titleCase(participation.filtered_event_class) : null,
    source ? `by ${source}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export function eventParticipationBadge(participation) {
  if (participation?.allowed !== false) return "";
  const filtered = participation?.reason === "generic_event_filtered";
  const label = filtered
    ? participationFilterLabel(participation)
    : `Capture excluded · ${escHtml(participationProfileName(participation))}`;
  return `<span class="badge ${filtered ? "badge-capture-filtered" : "badge-capture-excluded"}">${escHtml(label)}</span>`;
}

// What happened instead of driving capture. An attached Event is part of the
// Episode's story without having changed when it ends, and that distinction is
// the whole point of the filter, so the detail says which one occurred.
export function participationAttachmentNote(participation) {
  if (participation?.attachment === "attached") {
    return "It is attributed to the Episode that was already open for this Area, so the timeline stays complete. It did not extend the Episode, restart it, or start a recording.";
  }
  if (participation?.attachment === "no_open_episode") {
    return "No Episode was open for this Area at that moment, so the Event stays unassigned rather than opening one.";
  }
  return "It did not open or extend an Episode and it did not join a new recording.";
}

export function eventParticipationNotice(participation) {
  if (participation?.allowed !== false) return "";
  const filtered = participation?.reason === "generic_event_filtered";
  const activeProfile = filtered && participation?.filter_source !== "profile"
    ? ` · active profile ${escHtml(participationProfileName(participation))}`
    : "";
  const reason = participation.reason
    ? `<small>Reason: ${escHtml(titleCase(participation.reason))}${participation.filtered_event_type ? ` · ${escHtml(participation.filtered_event_type)}` : ""}${participation.filtered_event_class ? ` (${escHtml(titleCase(participation.filtered_event_class))})` : ""}${activeProfile}${participation.evaluated_at ? ` · evaluated ${escHtml(fmtShort(participation.evaluated_at))}` : ""}</small>`
    : participation.evaluated_at
    ? `<small>Evaluated ${escHtml(fmtShort(participation.evaluated_at))}</small>`
    : "";
  const message = filtered
    ? `<span>This observation was preserved. Its ${escHtml(titleCase(participation.filtered_event_class || "event"))} class (${escHtml(participation.filtered_event_type || "unknown event type")})${escHtml(participationFilterBy(participation))}, so it is not treated as activity on its own.${escHtml(participationAttachmentNote(participation))} Only the classes selected on this camera or its Capture profile are filtered, so this is the effect of your own selection.</span>${reason}`
    : `<span>This observation was preserved, but it did not affect an Episode. Its active Event did not open or extend an Episode and it did not join a new recording because the active Capture profile excludes this Device.</span>${reason}`;
  return `<section class="notice notice-info event-participation-notice" role="status">
    <div><strong>${
      filtered
        ? escHtml(participationFilterLabel(participation))
        : `Capture excluded · ${escHtml(participationProfileName(participation))}`
    }</strong>${message}</div>
  </section>`;
}
