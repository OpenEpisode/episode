# Using Episode

This guide describes the operator-facing behavior behind Episode correlation,
recording, notifications, storage, and recovery. For deployment and network
configuration, see the [installation guide](INSTALLATION.md).

## Areas and recording behavior

An Area is the current correlation and action boundary. Related Events from
Devices in the same Area contribute to one evolving Episode.

Each video Device has a recording behavior:

- `on_event` records when that Device emits an active Event;
- `on_episode` records whenever an active Event opens or updates an Episode in
  its Area, including Events from doorbells and non-video sensors.

Each Device also has an **Episode activity window**. When it emits an active
Event, the Device guarantees that the Episode remains open for at least that
many seconds. Later Events can extend the deadline but never shorten it.
Devices joining through `on_episode` follow the Episode lifetime; their own
activity window applies only when they emit a contributing Event.

`episode_timeout` is the fallback for Devices without an explicit activity
window. Inactive observations remain preserved but do not extend or shorten the
deadline, and duplicate connector deliveries do not restart actions.

When the minimum deadline passes, an Episode enters a short **quiescent**
settling state. Recordings continue during this configurable grace period. A
new active Event returns the same Area Episode to active. If the grace expires,
the Episode enters **finalizing**: normal Events and Evidence can no longer
extend or join it, while recording output already in progress is finalized.
Episode writes one final atomic manifest and then becomes **closed**. A failure
or restart leaves it finalizing for a later retry. Configure the grace period
under **System → Recordings**.

Events or Evidence that arrive after the grace period remain preserved and
searchable, but unassigned. They never reopen or amend a finalizing or closed
Episode. This keeps a closed Episode a stable, sealed account of what was
observed within its lifecycle.

## Capture profiles

Capture profiles provide a small armed/disarmed-style control under **System →
Capture profiles**:

- **All Devices** is built in and dynamically includes current and future
  enabled Devices;
- custom profiles contain a fixed Device selection;
- an empty custom profile is a useful Disarmed state.

The active profile is always visible in the application shell. Profiles govern
new Episode participation, not connectivity or preservation. An excluded
Device remains connected, and Episode still preserves its Raw Artifacts,
Receipts, and canonical Events, but its active Events cannot open or extend an
Episode or start actions.

Changing profiles never interrupts an existing recording. Episode persists the
participation decision and selected action targets so a restart or later
configuration change cannot reinterpret already accepted activity.

## Episode-start notifications

Episode can send one optional HTTP POST when a new Episode is created. It
supports a versioned generic JSON payload and a Discord-compatible embed.
Configure the destination, format, timeout, and test action under **System →
Notifications**. Changes apply immediately.

The webhook URL is a write-only secret. The UI displays a fixed mask:

- leave the mask unchanged to preserve the destination;
- paste a new URL to replace it;
- delete the mask and save to clear and disable notifications.

The global **External Episode URL** under **System → Overview** supplies
clickable links for notifications and future outbound integrations. Episode
does not infer or trust the HTTP request host; review and explicitly save the
browser's separately labeled suggested address. Until it is saved, Overview
shows **Not configured** and outbound notifications omit the Episode link.

Generic payloads include the Episode, triggering Event, Area, Device,
timestamps, state, and relative UI path. An absolute `episode.url` is added only
when the global external URL is configured. Discord embeds show the Area,
Event, Device, time, and an optional link to the ongoing Episode.

Delivery is intentionally best effort: one bounded in-memory queue, a short
timeout, and no retries or history. A slow or unavailable destination cannot
stop capture, but a notification can be lost during failure, overload,
shutdown, or restart.

## Recordings and live review

Recordings are rolling HLS/fMP4 bundles. Each participating camera contributes
one logical recording Evidence item to an Episode, backed by a playlist,
initialization file, immutable media fragments, and a checksummed component
manifest. This allows playback while the Episode is active and keeps the whole
recording portable.

`actions.recording.fragment_seconds` controls the target fragment duration
(four seconds by default). Camera keyframe intervals can make fragments longer.
This controls playback latency and file granularity, not Episode duration.

For ongoing recordings, the UI uses the browser's native HLS support when
available. Browsers without native HLS use the pinned hls.js light build from
jsDelivr as a fallback; those browsers require Internet access to load it.
H.264 with AAC has the broadest compatibility; HEVC/H.265 is preserved without
transcoding and plays only when the browser and operating system provide a
decoder.

While an Episode is active, **Ongoing recordings** are treated as live
operational previews. They use the browser's standard muted video controls and
do not add Episode-specific seeking, review, or NOW controls. The preview may
trail the newest captured frame by a small buffering interval. When the Episode
closes, the preview is marked complete and the resulting Evidence remains
available through the normal Evidence player for review.

Current camera views are operational previews. They become Evidence only
through an explicit preservation action.

## Shutdown and recovery

During shutdown, Episode signals all active FFmpeg processes together and
leaves their HLS bundles recoverable. On startup, capture can continue in the
same logical recording with a discontinuity marker when its Episode remains
active. Finalizing Episodes are retried after recorder recovery; a failed
finalization remains visible as finalizing instead of being silently marked
closed. Closed Episodes are trusted sealed history and are not scanned or
rebuilt during normal startup.

Startup loads the retention policy synchronously, then runs the initial visual
cleanup in the background. Plugin activation remains synchronous so configured
capture paths are ready before connectors begin delivering. Phase timing logs
identify slow startup work without making old Episode directories part of the
critical path.

Use **System → Recordings** to inspect active, reconnecting, stalled, or
interrupted captures.

## Data and portability

Runtime data lives under `./data`. Each Episode directory contains its raw
payloads, Evidence, an atomic `manifest.json`, and an append-only
`journal.ndjson`. The directory remains understandable even if the SQLite
operational index is unavailable.

Original Evidence is immutable and checksummed. Detection regions, thumbnails,
timelapses, transcodes, and future AI results are derived material and never
replace the original bytes.

Each Episode records a safe historical snapshot of its Area and participating
Device identities at capture time, including recording targets selected by
`on_episode`. Names or addresses changed later in Devices do not rewrite that
history. Credentials, integration configuration, and stream URLs are excluded.

Back up or synchronize `./data` separately when its contents matter. Copies
outside Episode are not governed by Episode's retention policy.

## Retention

Episode automatically removes managed visual Evidence after 30 days by
default. First-run setup requires an administrator to confirm that policy or
choose another period under **System → Storage**.

Disabling automatic deletion requires confirmation and leaves a persistent UI
warning. Cleanup runs initially in the background after startup recovery and
then hourly. It removes original and derived visual files together and leaves
an integrity-bearing expiration tombstone. Retention is the deliberate
post-closure Evidence lifecycle: it may remove managed bytes and record their
expiry, but it does not add Events, Evidence, or associations to a sealed
Episode. Legal and operational requirements vary; exported Evidence and
external backups need their own lifecycle policy.
