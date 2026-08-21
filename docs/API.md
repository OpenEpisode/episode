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

Offset pagination is intentionally simple for the beta lifecycle. New activity
arriving while a client walks older pages may move offsets; consumers requiring
a stable historical export should first work from a closed Episode.

The global Event collection accepts `episode_id`, `area_id`, `device_id`,
`event_type`, `event_state`, and `has_episode` filters. The global Evidence
collection accepts `episode_id`, `event_id`, `area_id`, `device_id`,
`evidence_type`, and `has_episode`. `has_episode=false` is the supported way to
find observations or artifacts that have not been associated with an Episode;
absence of a direct `event_id` is not itself an error because recordings and
other Episode-level Evidence need not belong to one Event.

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

Active Episodes expose `/episodes/{episode_id}/current-views` as a small
operational projection of Devices currently recording that Episode. Snapshot
URLs returned by that collection are short-lived views fetched through Episode;
they never expose Device credentials and are not Raw Artifacts or Evidence.
Devices without a registered snapshot provider remain in the collection with
`mode: "unavailable"` so preview support is never confused with recording
health.

Device detail exposes `capture_policy.activity_window_seconds`. An active
Event from that Device contributes this minimum duration to its Episode.
Episode resources expose the resulting persisted `minimum_end_at`; later
active Events can move that deadline forward, while inactive Events cannot
shorten it. Clients should treat the deadline as lifecycle state, not as a
countdown owned by any individual recording Device.

## Compatibility during beta

The `/api/v1` resource shapes and Device/ingress plugin API v1 are compatibility
boundaries during the beta cycle. Changes should be additive wherever practical.
New metadata keys, Event types, sources, capabilities, and enum values from
plugins must not break clients. Any unavoidable incompatible change will be
called out in release notes before the version is published.

The inbound automation endpoint has additional trust and idempotency rules; see
the [Event API guide](EVENT_API.md).
