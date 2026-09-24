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

A Device saved as **Needs setup** is an inventory draft, not an active capture
source or recording target. It cannot create canonical Events, Evidence, or
Episodes until marked ready. Incoming deliveries remain preserved as Raw
Artifacts and unmatched Receipts for diagnosis. This is different from
deliberately disabling a ready Device, and neither change erases historical
activity or interrupts a recording already in progress.

Media and triggering are separate: a camera may record **Any Episode in this
Area** using a validated manual RTSP stream without receiving Events itself.
ONVIF media discovery does not imply ONVIF Events are enabled. Conversely, an
Event API or SDK-only Device can trigger an Episode without a video stream of
its own.

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

### Filtering noisy event classes

A profile can also suppress observation classes so they stop opening Episodes on
their own. Under **System → Capture profiles**, choose a preset (`No filtering`,
`Motion only`, `Motion + status`, `Motion + status + audio`, `Everything —
nothing triggers an Episode`) or `Custom…`; the live summary states exactly what
the profile filters.

**Every class can be filtered, and a Capture profile and a camera are offered
exactly the same classes.** That symmetry is deliberate: one filter has to mean
one thing, whatever it names and whoever it is applied to. The classes are
`motion` (passive scene change), `heartbeat` (routine device bookkeeping such as
`system` and periodic battery reports), `condition` (`audio_detection`),
`security` (`tamper_detection`, `tampering_detection`, `video_loss`,
`battery_low`), `detection` (`human_detection`, `vehicle_detection`,
`line_crossing_detection`, `doorbell`, `digital_input`, …), `access` records, and
`unknown` (any event type the core does not recognize).

A wired alarm input is a `detection`, not a `security` event, because it asserts
nothing about the camera — it reports that a signal **you** decided was worth
raising has been raised, and which physical terminal means what is a deployment
choice written into the camera, not a property of the signal. Both spellings
reach the one canonical type: Hikvision's `alarm` and an ONVIF digital-input
channel or `DIInput`/`DIInputStatus` topic. Selecting `detection` therefore
covers them on both cameras, and selecting it silences a wired trigger too, so
it is not a safe "just drop the object detections" choice.

Filtering is never implicit and never a deletion: nothing is suppressed unless
you name its class. Because `unknown` has to be selected explicitly, an
unfamiliar camera stays fully audible by default.

The class of an Event, and therefore the effect of your selection, does not
depend on who made the camera. Every built-in integration resolves its own
recognisable signals onto the same vendor-neutral type, and an integration that
cannot tell what a message means reports it as unrecognized rather than falling
back to a name it happens to own, so an unidentified signal cannot be muted by a
`heartbeat` selection on one camera and kept on another. A camera whose topic
list includes things Episode does not interpret keeps those deliveries preserved
and counts them under that plugin's Device status, which is where to look when a
camera appears silent.

Suppressing `security` is the choice most likely to cost you evidence: it means
the camera does not record itself being tampered with, losing its picture, or
running down. It is available at both levels, but a camera with its own `security`
selection is marked `Tamper / video loss not captured` in the Device list so the
state is visible without opening the dialog.

A filtered Event is still preserved and queryable. When an Episode is already
open it is attributed to that Episode for the record — visible in the timeline —
but it does not extend the Episode, restart a quiet one, or start any capture.
The Activity view labels it `Filtered · <class> · by <profile>`, or names the
camera when the camera decided.

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

By default each recording is pulled over the camera's RTSP stream. A Reolink
device can instead record from an on-demand native `cmdId=3` burst
(`configs.reolink.settings.native_video: true`): the camera opens the burst
with an I-Frame, so the first access unit reaches the recorder ~157 ms after the
command rather than after a fresh RTSP keyframe-wait (~1.4–3.8 s). The bundle,
manifest, Evidence, and retention are unchanged — only the bytes' origin differs.
Native recording is off by default; enable it after confirming the camera
streams the chosen `preview_variant` natively.

For ongoing recordings, the UI uses the browser's native HLS support when
available. Browsers without native HLS use the pinned hls.js light build from
jsDelivr as a fallback; those browsers require Internet access to load it.
H.264 with AAC has the broadest compatibility; HEVC/H.265 is preserved without
transcoding and plays only when the browser and operating system provide a
decoder. HEVC recording is written with the `hvc1` codec tag (Safari/WebKit, the
main HEVC-capable browser, requires it and rejects the in-band `hev1` tag); the
parameter sets live in `init.mp4`, so every fragment is self-contained for
decoder initialization.

While an Episode is active, **Ongoing recordings** are treated as live
operational previews. They use the browser's standard muted video controls and
do not add Episode-specific seeking, review, or NOW controls. The preview may
trail the newest captured frame by a small buffering interval. When the Episode
closes, the preview is marked complete and the resulting Evidence remains
available through the normal Evidence player for review.

Current camera views are operational previews. They become Evidence only
through an explicit preservation action.

A camera URL capture is bounded: FFmpeg gives up on a camera that accepts the
connection and then stops answering after 15 seconds of silent socket I/O, and
that attempt reconnects like any other ended capture. Failed and reconnecting
recordings report FFmpeg's own last message as part of their reason, in
`System → Recordings` and the logs. That message is truncated and stripped of
the camera username and password, because FFmpeg prints the full stream URL,
credentials included, whenever it cannot open it. Host and port are kept so the
failing camera is still identifiable.

## Shutdown and recovery

During shutdown, Episode signals all active FFmpeg processes together and
leaves their HLS bundles recoverable. On startup, capture can continue in the
same logical recording with a discontinuity marker when its Episode remains
active. Finalizing Episodes are retried after recorder recovery; a failed
finalization remains visible as finalizing instead of being silently marked
closed. If FFmpeg exits while an Episode is still active, the recorder keeps its
existing HLS workspace and retries with capped backoff; it does not create a
second recording Evidence item for the same camera and Episode. A Device outage
therefore appears as reconnecting until capture resumes or the Episode ends.
The Episode engine also resumes Event-to-Episode correlation interrupted after
an Event was persisted, without changing that Event's original capture decision.
Closed Episodes are trusted sealed history and are not scanned or rebuilt during
normal startup.

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
