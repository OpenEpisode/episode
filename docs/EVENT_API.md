# Event API

The Event API lets trusted local systems create vendor-neutral Events without a
camera or protocol-specific plugin. Home automation, alarm panels, scripts,
tripwires, sound detectors, and manual controls can therefore use the same Area
correlation and recording behavior as native integrations.

The endpoint is not a policy engine. It accepts an observation from a configured
Device and passes it through Episode's existing Event, Episode, and action
pipeline.

## Enable the input

Add the connector to `episode.json` and restart Episode:

```json
{
  "type": "event_api",
  "enabled": true,
  "settings": {
    "name": "Event API",
    "path": "/api/v1/events",
    "max_payload_bytes": 65536
  }
}
```

The published example configuration already includes it. Existing installations
must add this connector explicitly. Its health and delivery counters appear on
the **System** page.

Episode has no authentication during the public alpha. Keep the API on a trusted
network and never expose it directly to the Internet. If another machine must
submit Events, set `EPISODE_HTTP_BIND=0.0.0.0` and restrict access with the host
firewall or a trusted reverse proxy.

## Create the source Device

Every submitted Event must reference an active Device already assigned to an
Area:

1. Open **Devices → Add a Device**.
2. Choose `Sensor`, `Alarm panel`, `Doorbell`, or another appropriate physical
   type.
3. Assign its Area. A network address and direct integrations are optional for a
   Device driven only through this API.
4. Save the Device and use its displayed ID as `device_id`.

An active Event opens or updates the Device's Area Episode. Video Devices in
that Area configured as **Any Episode in this Area** (`on_episode`) start
recording. An inactive Event is retained in the Episode timeline without
extending recording time. If no Episode is active, an inactive Event does not
open one. A matching inactive transition that arrives just after timeout may be
attached to the recently closed Episode without reopening or extending it.

## Submit an Event

Send a JSON object to `POST /api/v1/events`:

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: doorbell-call-20260816-001' \
  -d '{
    "device_id": "front-doorbell",
    "event_type": "doorbell",
    "event_state": "active",
    "source": "home-assistant",
    "metadata": {
      "friendly_name": "Front door"
    }
  }' \
  http://episode-host:8989/api/v1/events
```

A newly accepted Event returns HTTP `201`:

```json
{
  "status": "accepted",
  "receipt_id": "...",
  "event_id": "...",
  "episode_id": "...",
  "duplicate": false,
  "reason": null,
  "message": null,
  "validation_errors": []
}
```

The fields are:

| Field | Required | Meaning |
| --- | --- | --- |
| `device_id` | Yes | Existing active Device ID; its stored Area is authoritative |
| `event_type` | Yes | Stable observation name such as `doorbell`, `tripwire`, or `alarm` |
| `event_state` | No | `active` by default, or `inactive` |
| `timestamp` | No | ISO 8601 timestamp with timezone; receipt time is used when omitted |
| `source` | No | Stable producer name; defaults to `external` |
| `external_id` | No | Producer-scoped idempotency identifier |
| `metadata` | No | Additional JSON object retained on the canonical Event |

Identifiers may contain letters, numbers, dots, underscores, colons, and
hyphens. Unknown fields and timestamps without a timezone are rejected.

For a one-shot trigger, send an active Event and omit the inactive transition.
The Episode closes normally after its configured inactivity timeout.

## Idempotency

Use either the `Idempotency-Key` header or `external_id` field when a sender may
retry. If both are supplied, they must match. The identifier is scoped by
producer and Device, so retrying the same delivery links a new immutable receipt
to the existing Event instead of opening another Episode. The retry returns HTTP
`200` with `duplicate: true`. Reusing the identifier for a different Event type
or state is rejected as an identity conflict.

Without an external identifier, Episode uses its normal canonical identity:
Device, observed timestamp, Event type, and state. Senders that omit both a
timestamp and an idempotency identifier should treat every request as a new
observation.

## Raw preservation and errors

For payloads within the configured size limit, Episode stores and checksums the
exact request body before JSON parsing, Device resolution, or correlation.
Malformed JSON, invalid schemas, and unknown Devices therefore return HTTP `422`
with a `receipt_id` that remains available in diagnostics. Oversized requests
are rejected before storage with HTTP `413` because accepting them would violate
the configured safety bound.

The main outcomes are:

- `accepted`: a new or duplicate canonical Event was linked.
- `unmatched`: the schema was valid but its Device was unknown or disabled.
- `rejected`: the media type, JSON, schema, or idempotency key was invalid.
