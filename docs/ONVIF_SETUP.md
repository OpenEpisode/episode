# ONVIF camera setup

ONVIF is Episode's primary camera integration. Episode uses it to discover
media profiles, choose an RTSP stream, optionally request JPEG snapshots, and subscribe to
motion, tamper, and other advertised events. Vendor connectors can run beside
ONVIF to add richer metadata without replacing the original ONVIF receipts.

## Camera requirements

- Enable ONVIF in the camera's network or integration settings.
- Create a dedicated ONVIF user when the camera supports one.
- Grant permission to view live media, snapshots, and events.
- Synchronize the camera and Episode host with NTP.
- Keep ONVIF, RTSP, and camera administration on a trusted network.

For Hikvision cameras, use **Digest & WS-Username Token** authentication. Plain
Digest mode is supported with `"auth_mode": "digest"`, but the combined mode is
the recommended and tested setting.

## Configure a camera

Add `onvif` to the device capabilities in `episode.json`. The IP address is the
stable device identifier used for collection:

```json
{
  "id": "cam-front-door",
  "name": "Front Door",
  "device_type": "camera",
  "area_id": "front-door",
  "capabilities": ["onvif"],
  "ip_address": "192.168.1.100",
  "username": "episode",
  "password": "replace-me",
  "configs": {
    "onvif": {
      "protocol": "http",
      "port": 80,
      "path": "/onvif/device_service",
      "settings": {
        "auth_mode": "digest_wsse",
        "events_enabled": false
      }
    }
  }
}
```

The protocol, port, path, and settings shown above are defaults. `configs.onvif`
can therefore be omitted for cameras using them. ONVIF event polling is disabled
unless `settings.events_enabled` is explicitly set to `true`. This setting only
controls ONVIF event subscriptions; discovery, media profiles, RTSP and FTP remain
independent. Episode currently registers
cameras by IP; multicast WS-Discovery is intentionally not required by the
Docker installation.

To select a media profile explicitly, add its advertised token:

```json
"settings": {
  "auth_mode": "digest_wsse",
  "profile_token": "Profile_1"
}
```

Without a token, Episode selects the advertised profile with the highest pixel
resolution. The Devices page shows discovered profiles, capabilities, and connection health.

## What happens at runtime

1. Episode reads the camera clock to make WS-Security authentication tolerant
   of normal host/camera clock differences.
2. It discovers ONVIF services and media profiles.
3. It registers the selected RTSP and snapshot endpoints with the media layer.
4. If `events_enabled` is true, it creates a pull-point subscription for camera
   events. This is disabled by default because generic ONVIF motion can be noisy.
5. Active events create or join an Episode and start configured actions such as
   recording. ONVIF snapshot capture is available but disabled by default.

To enable Episode-requested ONVIF snapshots, add:

```json
"actions": {
  "snapshot": {"enabled": true}
}
```

FTP snapshot ingestion is independent of this action. Camera-pushed FTP images
continue to be accepted when the FTP connector is enabled.

Initial ONVIF property values are preserved as ignored ingestion receipts, but
do not create Episodes. Changed motion and tamper values are normalized into
vendor-neutral Events. Equivalent topics that describe the same device-level
state are aggregated, so a camera exposing both `MotionAlarm` and
`CellMotionDetector/Motion` produces one semantic transition. Every raw
notification remains artifact-backed.

An active transition can open or extend an Episode. Its matching inactive
transition is retained and attached to an open Episode, but does not open one or
extend its inactivity timeout. Raw SOAP responses, downloaded snapshots, and
recordings are checksummed and stored without overlays or modification.

## Vendor enhancements

Keep a vendor capability such as `isapi` when desired:

```json
"capabilities": ["onvif", "isapi"]
```

ONVIF remains the primary media and standard-event path. A standard ONVIF
motion event may represent broad scene motion, while a vendor event can represent
a later human or vehicle classification. These are related observations rather
than necessarily duplicate messages. The Hikvision connector can also contribute
regions and original vendor payloads. Exact duplicate deliveries share one
canonical Event; complementary observations remain together in the Episode.

## Troubleshooting

- **HTTP 401 or a closed connection:** verify the ONVIF-specific username,
  password, and authentication mode. Hikvision should normally use the combined
  Digest and WS-Username Token option.
- **Authentication fails intermittently:** check NTP and the camera time zone.
- **Connected but no events:** enable motion or tamper detection on the camera;
  ONVIF exposes configured camera events rather than creating detection rules.
- **No snapshot:** the camera may stream video without advertising the optional
  snapshot operation. Recording can still work.
- **Wrong stream:** set `profile_token` to one of the profiles shown on the
  System page.
- **ONVIF fails but a vendor connector works:** keep both enabled and include
  the model, firmware, and sanitized System status when opening an issue.
