# ONVIF camera setup

ONVIF is Episode's primary camera integration. It is loaded lazily as a
per-Device plugin only when an enabled Device has ONVIF configured. The plugin
discovers media profiles, chooses an RTSP stream, exposes optional JPEG
snapshots, and can subscribe to motion, tamper, and other advertised Events.
Vendor integrations can run beside ONVIF to add richer detail without replacing
original receipts.

## Camera requirements

- Enable ONVIF in the camera's network or integration settings.
- Create a dedicated ONVIF user when the camera supports one.
- Grant permission to view live media, snapshots, and events.
- Synchronize the camera and Episode host with NTP.
- Keep ONVIF, RTSP, and camera administration on a trusted network.

For Hikvision cameras, use **Digest & WS-Username Token** authentication. Digest
only is also supported, but the combined mode is the recommended and tested
setting.

## Add a camera

1. Start Episode and open <http://localhost:8989>.
2. Open **Devices → Manage Areas** and create an Area if needed.
3. Select **Add Device**.
4. Enter a name, Area, camera IP address, username, and password.
5. Select **Validate and discover**. A successful ONVIF response reports
   discovery, media, snapshot, and Event capabilities without enabling them.
6. Leave **Video recording** and **ONVIF** enabled when validation supports
   them.
7. Choose **Own Events only** to record only this camera's Events, or **Any
   Episode in this Area** when the camera should join activity opened by another
   Device.
8. Save the Device. Episode activates its integrations without restarting the
   container.

Credentials are write-only in the API and UI: after saving, they are reported
only as configured and are never returned to the browser. Leaving credential
fields blank while editing keeps their stored values.

The default ONVIF service is HTTP port 80 at `/onvif/device_service`.
Change this configured bootstrap endpoint under **Manual connection overrides**
when a camera uses non-standard values. ONVIF-discovered manufacturer, model,
firmware, selected profile, and media profiles are shown read-only on the Device
page.

ONVIF-discovered media is preferred for capture and is kept in the runtime media
registry rather than written back into editable configuration. A manual RTSP
fallback can be enabled explicitly for cameras with incomplete media discovery;
the common Hikvision path is `/Streaming/Channels/101` on port 554.

Episode registers Devices by IP; multicast WS-Discovery is intentionally not
required by the Docker installation. Without an explicit profile preference,
Episode selects the advertised profile with the highest pixel resolution. The
Device detail page shows discovered profiles, capabilities, and connection
health.

Validation, configuration, and runtime health are distinct. A timeout or
authentication error does not mean that ONVIF is unsupported and therefore
does not permanently disable it; correct the connection and validate again.
Only an explicit unsupported endpoint response disables the option.

ONVIF Event polling is disabled by default because generic motion state can be
noisy. Enable **Receive ONVIF Events** only when those Events are useful. This
toggle does not disable ONVIF discovery, media profiles, RTSP recording, or FTP
uploads.

## What happens at runtime

1. Episode reads the camera clock to tolerate normal WS-Security clock skew.
2. It discovers ONVIF services and media profiles.
3. It registers the selected RTSP and snapshot endpoints with the media layer.
4. If ONVIF Events are enabled, it creates a pull-point subscription.
5. Active Events create or join an Episode and start configured actions.

Initial ONVIF property values are preserved as ignored ingestion receipts but
do not create Episodes. Changed motion and tamper values are normalized into
vendor-neutral Events. Equivalent topics describing the same device state are
aggregated. The complete SOAP response is preserved exactly, and each derived
notification remains separately traceable to its source receipt.

An active transition can open or extend an Episode. Its matching inactive
transition is retained and attached to an open Episode, but does not open one or
extend its inactivity timeout. Raw SOAP responses, downloaded snapshots, and
recordings are checksummed and stored without overlays or modification.

### Optional Episode-requested snapshots

Automatic ONVIF snapshot capture is a system-wide setting and remains
disabled by default. To enable it, set this in `episode.json` and restart:

```json
"actions": {
  "snapshot": {"enabled": true}
}
```

FTP snapshot ingestion is independent. Camera-pushed FTP images continue to be
accepted whenever the shared FTP connector is enabled.

## Vendor enhancements

ONVIF remains the primary media and standards-based path. Edit the Device to
enable a vendor integration such as **Hikvision ISAPI** when richer Events,
classifications, or regions are useful. Exact duplicate deliveries share one
canonical Event; complementary observations remain together in the Episode.
The original vendor payload remains immutable and UI overlays remain separate.

## Troubleshooting

- **HTTP 401 or a closed connection:** verify the ONVIF-specific username,
  password, and authentication mode. Hikvision should normally use the combined
  Digest and WS-Username Token option.
- **Authentication fails intermittently:** check NTP and the camera time zone.
- **Connected but no Events:** enable a detection rule on the camera and enable
  **Receive ONVIF Events** on the Device. ONVIF exposes configured camera rules;
  it does not create them.
- **No snapshot:** the camera may stream video without advertising the optional
  snapshot operation. Recording can still work.
- **Wrong stream:** inspect the profiles shown on the Device page and include
  sanitized diagnostics in an issue.
- **Changes appear saved but are not active:** check the System page for the
  ONVIF integration state and inspect the Episode logs for its Device entry.
- **ONVIF fails but a vendor integration works:** keep both enabled and include
  the model, firmware, and sanitized System diagnostics when opening an issue.
