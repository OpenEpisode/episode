# Hikvision setup

This guide covers optional Hikvision enhancements: ISAPI events, Alarm Server
deliveries, and FTP snapshots. Configure the primary camera path with the
[ONVIF guide](ONVIF_SETUP.md) first. Menu names vary by firmware.

## Before you begin

- Give the Episode host a stable address reachable from the cameras.
- Keep the cameras and Episode on a trusted network or isolated VLAN.
- Synchronize the Episode host, cameras, and NVR with NTP. Correlation depends on
  observation times being reasonably close.
- Do not expose Episode, FTP, RTSP, or camera administration directly to the
  Internet.

Copy `episode.example.json` to `episode.json`. For each camera, define one device
whose `ip_address` is the address from which Episode receives data. Keep device
IDs stable after collecting evidence.

## Start Episode

```bash
cp episode.example.json episode.json
cp .env.example .env
mkdir -p data
docker compose --env-file .env pull
docker compose --env-file .env up -d
```

Open <http://localhost:8989>. The System page should report the server, engine,
recorder, and configured connectors.

## ISAPI event stream

For enhanced vendor event monitoring, keep `isapi` beside `onvif` in the device's `capabilities` and set
the camera address and credentials in `episode.json`:

```json
{
  "capabilities": ["onvif", "isapi"],
  "ip_address": "192.168.1.100",
  "username": "admin",
  "password": "replace-me"
}
```

Episode connects to `/ISAPI/Event/notification/alertStream` using the configured
ISAPI protocol, port, and path. A connection or authentication failure appears
in `docker compose --env-file .env logs -f episode`.

## Alarm Server events

Alarm Server pushes require the camera or NVR to reach Episode's HTTP port.
Change `.env` on a trusted network:

```dotenv
EPISODE_HTTP_BIND=0.0.0.0
```

Restart Episode, then configure the camera's Alarm Server destination with:

- Host: the Episode host's LAN address
- Port: `8989`
- Path or URL: `/alarm` or `http://EPISODE_HOST:8989/alarm`, depending on firmware
- Protocol: HTTP

If an NVR sends events for several cameras, each event still needs an address or
channel identity that can be matched to a configured device. Episode records
each Alarm Server and ISAPI delivery separately and deduplicates matching
observations into one canonical Event.

## FTP snapshots

The example configuration enables an FTP server with:

- Host: the Episode host's LAN address
- Port: `2121`
- Username: `episode`
- Password: the value you set in `episode.json`
- Passive TCP ports: `30000-30009`

Configure event-triggered picture uploads in the camera or NVR. Allow TCP 2121
and 30000-30009 through host and network firewalls. Do not reuse the example
password.

The source address and filename metadata help associate a snapshot with its
device and nearby Event. The received bytes are preserved unchanged; bounding
boxes and future annotations remain separate metadata.

## Recording and ONVIF

Episode normally discovers the RTSP URI and snapshot endpoint through ONVIF. A
manual `video` configuration remains a fallback for devices with incomplete
ONVIF media support. Hikvision's common main-stream path is
`/Streaming/Channels/101`; channel `102` is normally a lower-bandwidth stream.

Keep the camera's ONVIF service enabled and use **Digest & WS-Username Token**
authentication. See the [ONVIF setup guide](ONVIF_SETUP.md) for the primary
configuration and profile selection.

## Verify the flow

1. Open the Episode System page and confirm the connectors are running.
2. Trigger one configured camera event.
3. Watch `docker compose --env-file .env logs -f episode` for a canonical Event
   and Episode.
4. Open the new Episode and confirm its Events, receipts, snapshots, and
   recording.
5. Inspect `data/episodes/<episode-id>/manifest.json` to confirm the portable
   relationships and SHA-256 checksums.

Several receipts for one Event are expected when ONVIF, ISAPI, and Alarm Server
observe the same activity. They demonstrate provenance rather than duplicate
incidents.

## Troubleshooting

### The UI is reachable only from the Docker host

This is the safe default. Set `EPISODE_HTTP_BIND=0.0.0.0` only when LAN devices
must reach the Alarm Server endpoint or you intentionally want LAN UI access.

### FTP connects but uploads fail

Check the passive port range as well as port 2121. Verify that Docker publishes
30000-30009 and that no host firewall blocks the camera subnet.

### Events arrive but snapshots do not correlate

Confirm that the camera's source IP matches the device `ip_address`, check NTP on
all devices, and inspect the FTP filenames in the logs. Preserve the original
files when reporting a reproducible parser problem, but never attach private
evidence to a public issue.

### Recording does not start

Test the configured RTSP address and credentials from the Episode host. Check
that the camera permits another concurrent stream and inspect FFmpeg errors in
the Episode logs.

### Permission denied below `data`

Set `EPISODE_UID` and `EPISODE_GID` in `.env` to the host user that owns the data
directory, then restart the container.
