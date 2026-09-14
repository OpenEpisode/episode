# REST API v1

Episode exposes its product API under `/api/v1` and publishes an interactive
OpenAPI description at `/docs` with the machine-readable schema at
`/openapi.json`. The unversioned `/health` endpoint is intended for container
health checks.

The API currently assumes a trusted local network. It has no authentication or
authorization layer and should not be published directly to the Internet.

## Conventions

- Identifiers are opaque, case-sensitive strings. Clients must not infer dates,
  relationships, or resource types from an identifier's format.
- Timestamps are RFC 3339 values. Episode returns timezone-aware UTC values and
  uses `Z` where supported by the serializer.
- Defined states and configuration choices use lowercase `snake_case` values.
  Event types and integration sources remain extensible strings because plugins
  may introduce new values.
- Response fields defined by a schema remain present when their value is `null`.
  Metadata objects are additive and must be treated as integration-owned data.
- Credentials, private storage paths, and raw payload bytes are excluded from
  normal JSON resource responses.

Episode resources use these lifecycle states:

- `active`: accepts related active Events and Evidence associations;
- `quiescent`: within the configured settling grace and still mutable;
- `finalizing`: the grace has expired; normal Events and Evidence are preserved
  but cannot extend or join the Episode while already-started recording output
  is finalized;
- `closed`: a sealed historical Episode whose final manifest has been written;
- `archived`: retained historical metadata outside the active lifecycle.

`finalizing` is transient during successful operation, but may remain visible
after a failure or restart until recovery retries it. Late or inactive
observations that arrive after the grace period are returned as preserved,
unassigned resources; they do not reopen or amend `finalizing` or `closed`
Episodes. Retention may later expire managed Evidence bytes while preserving
the Evidence identity and integrity metadata.

## Collections

Time-based collections—Episodes, Events, Evidence, and ingestion Receipts—use:

- `limit`: 1–500 items, default 100;
- `offset`: number of items to skip, default 0.

Collections are returned as JSON arrays. A shorter page means the collection is
exhausted. Episodes, Events, and Evidence use stable newest-first ordering with
the resource identifier as a tie-breaker. Receipts use oldest-first ordering so
their delivery chain reads chronologically. Clients that need a complete Episode
should continue paging its Events, Evidence, or Receipts until a short page is
returned.

Areas and Devices are deliberately unpaginated because they are bounded
configuration inventory and are returned alphabetically. Batch cover lookup is
a mapping operation rather than a pageable collection.

Capture profiles are also bounded configuration. `GET /api/v1/capture-profiles`
returns the built-in and custom profiles, including their explicit `active`,
`builtin`, `include_all_devices`, and `device_ids` state. The built-in
`all-devices` profile is immutable and dynamic; an empty custom `device_ids`
array is valid.

Offset pagination is intentionally simple for the beta lifecycle. New activity
arriving while a client walks older pages may move offsets; consumers requiring
a stable historical export should first work from a closed Episode.

The global Event collection accepts `episode_id`, `area_id`, `device_id`,
`event_type`, `event_state`, and `has_episode` filters. It also accepts the
optional `observed_from` and `observed_before` RFC 3339 bounds. The lower bound
is inclusive (`timestamp >= observed_from`) and the upper bound is exclusive
(`timestamp < observed_before`); both values must include a timezone offset,
and `observed_before` must be later than `observed_from`. The Activity view
uses these bounds for local-calendar presets and custom whole-day ranges. The
global Evidence collection accepts `episode_id`, `event_id`, `area_id`,
`device_id`, `evidence_type`, and `has_episode`. It also accepts optional
`captured_from` and `captured_before` RFC 3339 bounds with the same inclusive
lower and exclusive upper semantics and timezone requirements. These bounds
filter the Evidence `timestamp`, which is the capture/start timestamp. A long
recording is selected by its start time only; the filter does not use interval
overlap. The Evidence view uses these bounds for the same local-calendar
presets and custom whole-day ranges. `has_episode=false` is the supported way
to find observations or artifacts that have not been associated with an
Episode; absence of a direct `event_id` is not itself an error because
recordings and other Episode-level Evidence need not belong to one Event.

## Errors

JSON API errors use one envelope:

```json
{
  "error": {
    "code": "not_found",
    "message": "Event not found",
    "details": []
  }
}
```

Validation errors use `validation_error` and include details with `location`,
`message`, and `type`. Unexpected failures return a generic `internal_error`;
private exception details remain in server logs.

The Event input is the deliberate exception: once a delivery has been preserved,
an `unmatched` or `rejected` result returns its receipt-shaped outcome so the
sender can retain the `receipt_id`. Failures that occur before preservation use
the normal error envelope.

Binary and media endpoints return their native content type. A successful
artifact response is the preserved file; JSON errors are returned only when the
requested resource or file cannot be served.

`/evidence/{evidence_id}/file` serves the preserved Evidence bytes.
`/evidence/{evidence_id}/thumbnail` serves a fixed-size JPEG derived on demand
for collection and timeline presentation. Thumbnails are disposable cache
entries below `data/cache/thumbnails`; they are not Raw Artifacts, Evidence, or
Episode bundle contents. Removing the cache never removes or changes Evidence.

`/evidence/{evidence_id}/closest-event` is an optional annotation lookup. When
the Evidence exists but no Event falls inside the configured snapshot window,
it returns `200` with `event`, `bounding_box`, and `target_type` set to `null`.
A missing Evidence resource still returns `404`.

Evidence resources expose `availability`, `expired_at`, and
`expiration_reason`. Retention-expired Evidence remains as a tombstone in JSON
and Episode manifests, while its file and thumbnail endpoints return `410`.

`GET /api/v1/settings/retention` returns the global visual Evidence policy,
confirmation state, and cleanup status. A new installation reports an active
30-day policy with `policy_state: "unconfirmed"`.
`PUT /api/v1/settings/retention` accepts `enabled` and `retention_days` from 1
through 3650; any successful update records explicit administrator confirmation.
Updating an enabled policy immediately runs one cleanup pass. A disabled policy
does not delete Evidence and reports `policy_state: "disabled"` so clients can
keep the condition visible.

Active Episodes expose `/api/v1/episodes/{episode_id}/current-views` as a small
operational projection of Devices currently recording that Episode. Once the
first recording fragment is ready, `mode: "hls"` provides a local
`stream_url`; before then, a registered snapshot provider may supply
`mode: "snapshot"`. Neither response exposes Device credentials. Devices
without a ready stream or snapshot provider remain in the collection with
`mode: "unavailable"` so preview support is never confused with recording
health. `recording_state`, `fragment_count`, and `last_fragment_at` explain
whether capture is starting, progressing, reconnecting, or recovering without
revealing the upstream stream URL.

`GET /api/v1/diagnostics` includes a bounded active `recordings` collection and
up to ten persisted `recording_issues`. Active entries report fragment progress,
reconnect count, and safe exit information. Recent issues identify incomplete
recording Evidence across application restarts. The compact `/api/v1/status`
endpoint continues to expose only aggregate health and active-recording count.

HLS recording playlists and components are served from
`/api/v1/recordings/{evidence_id}/{component_path}`. Active playlists are never
cached; finalized media fragments are immutable and may be cached. The route
only exposes the known playlist, initialization file, component manifest, and
media-fragment paths within the exact recording bundle. Legacy MP4 Evidence
continues to use `/evidence/{evidence_id}/file`.

Device detail exposes `capture_policy.activity_window_seconds`. An active
Event from that Device contributes this minimum duration to its Episode.
Episode resources expose the resulting persisted `minimum_end_at`; later
active Events can move that deadline forward, while inactive Events cannot
shorten it. Clients should treat the deadline as lifecycle state, not as a
countdown owned by any individual recording Device.

`GET /api/v1/settings/episode` exposes the installation's bounded
`quiescent_grace_seconds` setting. `PUT` accepts a value from 0 through 60
seconds. When a minimum Episode deadline passes, the Episode enters
`quiescent`; recordings remain active for this Area-level continuation window.
An active Event received within the window continues the same Episode. A value
of zero disables the settling period. This setting is distinct from the
per-Device `activity_window_seconds` policy.

`GET /api/v1/settings/installation` returns the non-secret global
`external_url`. `PUT` accepts the address operators use to reach this Episode
installation, or a blank value to clear it. It must be an absolute HTTP(S) URL
without credentials, query, or fragment; a deployment path is allowed and
trailing slashes are removed. The backend never derives this value from a
request host. Outbound integrations may use it to create absolute UI links.

`GET /api/v1/settings/notifications/episode-started` returns the current
best-effort webhook policy as `enabled`, `payload_format`, `timeout_seconds`,
and `url_configured`. The credential-bearing URL is write-only and is never
returned. `PUT` accepts those policy fields plus an optional `url`; an omitted
or blank webhook URL preserves the saved value through the API.
`clear_url: true` explicitly removes the webhook destination and requires
notifications to be disabled.

`POST /api/v1/settings/notifications/episode-started/test` sends a labeled,
bounded test request using the saved URL and format without creating an Event
or Episode. It may be used while notifications are disabled. Its response
reports only immediate success, a sanitized message, and an optional HTTP
status—not the destination or a persisted delivery record. Discord tests
preview the embed style without creating an Episode or including an Episode
link. Settings changes apply immediately and do not require an application
restart.

`POST /api/v1/capture-profiles`, and `GET`, `PUT`, or `DELETE` on
`/api/v1/capture-profiles/{profile_id}`, manage custom profiles. The built-in or
currently active profile cannot be deleted, and the built-in profile cannot be
edited. `GET /api/v1/capture-profiles/active` returns `{profile,
recent_changes}` with bounded newest-first activation history. `PUT` on that
resource accepts `{"profile_id": "..."}` and atomically changes the active
selection; activating the already-active profile is an idempotent no-op.

The active selection is capture policy, not Device connectivity. New active
Events from excluded Devices are still preserved and returned with a
`participation` object containing the durable decision, but have no Episode.
Internal recording-target IDs are not exposed in Event API projections. Profile
changes apply to later canonical Events and never interrupt current recordings.

## Compatibility during beta

The `/api/v1` resource shapes and Device/ingress plugin API v1 are compatibility
boundaries during the beta cycle. Changes should be additive wherever practical.
New metadata keys, Event types, sources, capabilities, and enum values from
plugins must not break clients. Any unavoidable incompatible change will be
called out in release notes before the version is published.

The inbound automation endpoint has additional trust and idempotency rules; see
the [Event API guide](EVENT_API.md).
