# Reolink camera setup

Episode can connect to compatible Reolink cameras through the proprietary
Baichuan binary protocol. The connection is initiated by Episode to TCP port
9000 and is independent of ONVIF.

The initial integration supports:

- Device model and firmware discovery.
- Main and sub-stream RTSP registration.
- Snapshot requests over Baichuan.
- Subscribed motion and object-detection notifications.

Support varies by model and firmware. Validation reports only capabilities
that the configured camera answers successfully.

## Requirements

- The Episode host can reach the camera on TCP ports 9000 and 554.
- The camera has a local username and password.
- RTSP is enabled on models that expose it as a configurable service.
- The camera and Episode host have reasonably synchronized clocks.
- The camera is on a trusted network or isolated VLAN. Baichuan runs over a
  direct TCP connection; this integration does not add TLS to the protocol.

## Add a camera

1. Open Episode and go to **Devices**.
2. Create or select an Area and choose **Add Device**.
3. Enter the camera address and local credentials.
4. Select **Discover with ONVIF** first. Episode uses the discovered
   manufacturer to offer the matching Reolink integration; it does not send
   the configured credentials to every installed vendor plugin. If ONVIF is
   unavailable, select Reolink manually after the failed discovery check.
5. Select **Reolink API**, validate the selected connection, and enable Reolink
   media and/or Events only when validation reports those capabilities.
6. Select the desired recording mode and save the Device. Use **Save for later**
   when the Device is not ready; it remains visible but does not participate in
   new Episodes or captures.

The default Baichuan port is `9000`. The optional API host overrides the
Device address, which can be useful when connecting through an NVR or routed
network.

Credentials remain write-only in Episode's API and UI. Leaving them blank
while editing retains the stored values.

## Runtime behavior

When enabled, Episode authenticates to the camera, discovers its media
capabilities and registers an RTSP source. If Events are enabled, Episode sends
a Baichuan subscription request and listens for notifications on the same TCP
connection. It periodically verifies the connection and reconnects,
reauthenticates and resubscribes after a failure.

Every received event frame is stored and checksummed before the Reolink plugin
interprets it. Recognized notifications become derived JSON artifacts and
canonical Episode Events. Repeated states may be suppressed during
interpretation, but their original frames remain preserved. Unknown frames
remain available as raw deliveries for future interpretation.

Battery status frames are currently preserved as device telemetry and do not
create Episodes.

## Media

The integration registers the conventional Reolink RTSP paths for the selected
channel:

- Main stream: `/Preview_01_main`
- Sub stream: `/Preview_01_sub`

Channel numbers are one-based and zero-padded. These paths are a **client
convention**: the camera does not advertise them, and the Baichuan ability query
(`cmdId=151`) is flaky — measured rejecting all three test cameras in one run and
accepting all three in another — so a failure there is never treated as proof that
a capability is missing. Episode requests snapshots over the Baichuan connection
rather than modifying the received image.

If a model uses different RTSP paths, configure its video endpoint manually
and use Baichuan only for discovery, snapshots or Events.

Recording and snapshots therefore arrive over RTSP, while Events and pre-armed
snapshots arrive over the Baichuan connection. The two differ sharply in how long
they take to produce a usable picture. On the same three cameras in the same
window, native `cmdId=3` delivery reached a first independently decodable frame
in 80–270 ms at 12–15 fps, and the three captures kept as test fixtures recorded
157 ms, 223 ms and 473 ms; RTSP took 1.36–3.76 s to reach its first packet *and*
first IDR. Episode does not record natively — Video Evidence stays the core-owned
HLS/fMP4 bundle produced by the recorder — so that difference is currently
information rather than a recording source: it is what `media_priming` measures,
and what any future decision about which source to record from would be made of.

A snapshot requested after an Event is normally taken at that moment, so the
camera's JPEG encode time (measured at 0.3–1.1 s on current firmware) adds to the
perceived delay. Pre-arming starts that fetch as soon as the camera's Event frame
arrives and serves the result to the snapshot request if it is still fresh. This
overlaps the encode with Event processing and never changes what is preserved: the
Event frame is stored before anything interprets it, and a pre-armed snapshot is
discarded rather than turned into Evidence.

## Settings

Optional keys under `configs.reolink.settings`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `host` | device address | Baichuan host override. |
| `events_enabled` | `false` | Subscribe to pushed Event frames. |
| `media_enabled` | `false` | Register RTSP stream and native snapshot endpoints for this device. |
| `dedup_window` | `1.0` | Seconds in which a repeated identical event state is suppressed as `repeated_state`. Original frames stay preserved. |
| `event_retry_delay` | `5.0` | Seconds between event subscription retries. |
| `timeout` | `10.0` | Seconds for one command exchange. Must be greater than zero. |
| `snapshot_timeout` | `10.0` | Seconds for one snapshot capture (1.0–60.0). Applies per attempt, so a slow model fails in its own budget rather than in the shared command default. |
| `snapshot_prearm` | `true` | Start the snapshot fetch when the Event frame arrives instead of after the Event is canonical. |
| `snapshot_prearm_ttl` | `2.0` | Seconds a pre-armed snapshot stays servible (0.5–10.0); older snapshots are counted as `expired` and fetched live. |
| `snapshot_prearm_min_interval` | `1.0` | Minimum seconds between pre-armed snapshot captures (0–60), so a burst of frames cannot burn captures. |
| `media_priming` | `false` | On an Event frame, open one short native preview (`cmdId=3`) so the camera's encoder is warm and its codec, resolution, and time-to-first-keyframe are known before anything records. |
| `native_video` | `false` | Record from the on-demand native `cmdId=3` burst instead of RTSP. The camera opens the burst with an I-Frame, so the first access unit reaches the recorder's pipe ~157 ms after the command — instead of after a fresh RTSP keyframe-wait (~1.4–3.8 s). Video Evidence stays the core-owned HLS/fMP4 bundle; the plugin only hands Annex-B bytes to the recorder's pipe. |

These preview settings are also editable from the Device editor (Configure → Devices →
Edit Device → Reolink API): `native_video` ("Native media acquisition"), `preview_variant`,
and `preview_timeout`. `media_priming` is a diagnosis setting and is **not** exposed in the
UI; it is configured only under `configs.reolink.settings`.
| `preview_variant` | `main` | Native preview stream: `main` or `sub`. Also the variant used by the `native_video` recording source. |
| `preview_timeout` | `3.0` | Seconds to wait for the first video packet of a native preview pass (1.0–10.0). Also bounds an on-demand `native_video` recording burst. |

Native preview priming is off by default and stays off unless it is measured to help. It
costs the camera a real encode on the same TCP connection that delivered the Event, and it
only ever informs a decision; it never becomes Evidence. One pass is scheduled per Event
(never a second one while a first is still running), it exits at the first independently
decodable picture rather than at the end of a stream, it buffers at most 4 MiB, and its
`cmdId=6` stop is sent whatever happened to the pass — a timeout, a rejection, a stream that
stopped mid-picture, or a cancellation. Alarm pushes keep flowing throughout: preview frames
are matched by command id and a slow preview consumer pays in counted drops, never in a
stalled event reader.

Out-of-range or wrongly typed values fall back to the default and are logged as a
warning; they never prevent a device from connecting. Instance status reports
`snapshot_prearm` and a `snapshot_slot` counter set (`armed`, `hits`, `misses`,
`expired`, `failed`) so an operator can see whether pre-arming is helping, plus
`snapshot_timeout` and the last `snapshot_probe` (`declared_bytes`).

For native preview, status reports the opt-in (`preview_priming`, `preview_variant`,
`preview_timeout`), the last pass (`preview_first_packet_ms`, `preview_first_iframe_ms`,
`preview_codec`, `preview_keyframe`) and the totals that decide whether priming is worth
keeping: `preview_attempted`, `preview_observed`, `preview_timeout_total` and
`preview_failed_total`. `preview_timeout_total` counts passes that asked for a keyframe and
received no video packet at all, which is a different thing from `preview_failed_total`.

With `native_video` enabled, the recording source is the on-demand `cmdId=3` burst, not
RTSP. The plugin does **not** keep a continuous preview session open: it issues the command
when a recording starts, the camera opens with an I-Frame, and the burst is stopped with
`cmdId=6` whether the recording ended, failed, or was cancelled. The recorder keeps owning
the HLS/fMP4 bundle, manifest, Evidence, and retention — only the bytes' origin changes
(piped elementary stream instead of an RTSP URL). Because the camera leads with a keyframe,
the acquisition delay (recording start → first access unit) collapses from the ~3.5 s an
RTSP keyframe-wait costs toward the measured ~0.15 s of the first native picture. This is
the F1 change; enable it only after confirming the camera streams the chosen `preview_variant`
natively, and compare
A native HEVC source is recorded with the `hvc1` codec tag, which WebKit (Safari)
requires for MP4/fMP4 playback; the parameter sets stay in `init.mp4`, so each
fragment is self-contained for decoder initialization.
`preview_first_iframe_ms` against the snapshot and recording delay you actually see before
`preview_first_iframe_ms` against the snapshot and recording delay you actually see before
deciding to leave it on.

A snapshot is accepted when either completion signal arrives: the camera's own
`<pictureSize>` declaration in the request acknowledgment, or the JPEG end-of-image
marker. Measured across three firmwares, the declared size is exact to the byte, so
a capture that stops short of it is rejected rather than padded into a
plausible-looking file, and a declaration above the core's 25 MiB ceiling stops the
transfer before it buffers. Device validation and startup ask the same request a
capability-only question: they read the acknowledgment, confirm support, and reset
the socket instead of receiving a picture they would discard.

## Troubleshooting

- **Connection refused:** confirm that TCP port 9000 is reachable from the
  Episode container and that the camera supports Baichuan access.
- **Authentication failed:** verify the camera's local credentials. Cloud-only
  account credentials do not apply.
- **No recording:** test the discovered RTSP stream from the Episode host and
  confirm that port 554 and RTSP are enabled. RTSP paths are a convention (see
  Media), so a `404` usually means the model uses a different path rather than
  that the camera has no stream.
- **No Events:** confirm that camera-side motion or object-detection rules are
  enabled and that validation accepts the event subscription.
- **No snapshot:** the model or firmware may not implement the Baichuan
  snapshot command. Recording can still work through RTSP. A
  `snapshot_slot` counter set of `misses` rising with `hits` at zero means the
  pre-armed fetch lost the race against the request; a rising `expired` means the
  camera was slower than `snapshot_prearm_ttl`; a rising `failed` means the
  capture itself is failing, which `snapshot_timeout` may need to raise.
- **Preview priming does nothing:** compare `preview_attempted` against
  `preview_observed` and `preview_failed_total`. A rising
  `preview_timeout_total` means passes completed but produced no picture at
  all (the camera stayed silent, or refused the request), while
  `preview_failed_total` means the pass itself raised. Since priming does not
  change when an RTSP recording starts, priming on its own is expected to look
  like no change at all — raise `preview_timeout` only if
  `preview_first_iframe_ms` is close to the current limit.
- **Frequent reconnects:** inspect Episode logs and verify network stability
  between Episode and the camera.

The initial compatibility information comes from the models tested by the PR
contributor. Additional model and firmware reports are welcome; Episode should
not infer compatibility solely from a product family name.

The Baichuan protocol is undocumented. This implementation was informed by
the MIT-licensed [nodelink-js protocol documentation](https://github.com/apocaliss92/nodelink-js).
Episode is not affiliated with or endorsed by Reolink.
