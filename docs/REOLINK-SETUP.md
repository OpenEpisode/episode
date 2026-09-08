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
4. Enable **Reolink API** and select **Validate and discover**.
5. Enable Reolink media and/or Events only when validation reports those
   capabilities.
6. Select the desired recording mode and save the Device.

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

Channel numbers are one-based and zero-padded. Episode requests snapshots over
the Baichuan connection rather than modifying the received image.

If a model uses different RTSP paths, configure its video endpoint manually
and use Baichuan only for discovery, snapshots or Events.

## Troubleshooting

- **Connection refused:** confirm that TCP port 9000 is reachable from the
  Episode container and that the camera supports Baichuan access.
- **Authentication failed:** verify the camera's local credentials. Cloud-only
  account credentials do not apply.
- **No recording:** test the discovered RTSP stream from the Episode host and
  confirm that port 554 and RTSP are enabled.
- **No Events:** confirm that camera-side motion or object-detection rules are
  enabled and that validation accepts the event subscription.
- **No snapshot:** the model or firmware may not implement the Baichuan
  snapshot command. Recording can still work through RTSP.
- **Frequent reconnects:** inspect Episode logs and verify network stability
  between Episode and the camera.

The initial compatibility information comes from the models tested by the PR
contributor. Additional model and firmware reports are welcome; Episode should
not infer compatibility solely from a product family name.

The Baichuan protocol is undocumented. This implementation was informed by
the MIT-licensed [nodelink-js protocol documentation](https://github.com/apocaliss92/nodelink-js).
Episode is not affiliated with or endorsed by Reolink.
