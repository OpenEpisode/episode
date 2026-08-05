# Hikvision setup

This guide covers optional Hikvision enhancements: ISAPI events, Alarm Server
deliveries, FTP snapshots, and the user-supplied HCNetSDK runtime. Configure the
primary camera path with the [ONVIF guide](ONVIF_SETUP.md) first. Menu names vary
by firmware.

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

## Hikvision HCNetSDK

Episode can discover and validate an optional Hikvision HCNetSDK installation.
The SDK remains user-supplied: Episode does not download, redistribute, or add
vendor binaries to its container image.

Episode validates HCNetSDK, then starts one isolated worker process for each
device that explicitly enables the capability. Each worker logs in on the SDK
service port and subscribes to alarm callbacks. A native crash affects that
device worker, not the Episode server or other devices.

Every callback buffer is preserved as an immutable raw delivery. Episode also
interprets narrowly validated video-intercom callbacks emitted by supported
Hikvision devices:

- `COMM_ALARM_VIDEO_INTERCOM` (`0x1133`) subtype `17` creates an active
  canonical `doorbell` Event;
- subtype `18` creates the matching inactive doorbell observation;
- `COMM_UPLOAD_VIDEO_INTERCOM_EVENT` (`0x1132`) unlock records create
  `door_access` Events with the reported method, lock and embedded-picture
  fingerprint. HCNetSDK does not report the unlock outcome, so Episode does
  not claim that the door successfully opened;
- unknown commands and subtypes remain raw-only and never create guessed Events.

Doorbell JPEGs delivered separately through FTP are preserved as Episode
evidence but marked as event attachments, so they are not used as timelapse
frames.

An active doorbell Event enters the normal Area-scoped action flow. A doorbell
configured with `recording_mode: on_event` records its own stream, while video
devices in the same Area configured with `recording_mode: on_episode` join the
same Episode.

### Activate the plugin

Episode activates plugins from explicit device capabilities. Add `hikvision_sdk`
only to devices that should connect through the SDK:

```json
{
  "id": "front-doorbell",
  "name": "Front Doorbell",
  "device_type": "hikvision",
  "area_id": "front-door",
  "capabilities": ["doorbell", "onvif", "video", "hikvision_sdk"],
  "ip_address": "192.168.1.120",
  "username": "admin",
  "password": "replace-me",
  "configs": {
    "hikvision_sdk": {
      "port": 8000
    }
  }
}
```

The SDK port defaults to `8000` when omitted. The device ID, name, area, IP
address, username, and password are required. Credentials are sent to the
worker over standard input; they are not included in process arguments, plugin
status responses, or routine log messages.

Installing SDK files alone does not import or validate the plugin. If no
configured device declares `hikvision_sdk`, the module remains unloaded and is
omitted from `/api/v1/plugins`. This keeps optional integrations lazy as the
plugin catalog grows.

### Install the SDK files

1. Download the Linux 64-bit HCNetSDK package for your host architecture from
   Hikvision's official developer resources and accept its terms.
2. Extract the archive outside the Episode repository.
3. Copy the complete contents of the SDK package's `lib/` directory:

```bash
mkdir -p plugins/hikvision-sdk
cp -a /path/to/EN-HCNetSDK*/lib/. plugins/hikvision-sdk/
docker compose --env-file .env up -d
```

Copy the whole `lib/` directory contents, including `HCNetSDKCom/`. Copying only
`libhcnetsdk.so` is not enough. The resulting layout starts like this:

```text
plugins/
└── hikvision-sdk/
    ├── libhcnetsdk.so
    ├── libHCCore.so
    ├── libhpr.so
    └── HCNetSDKCom/
        └── libHCAlarm.so
```

The SDK architecture must match the container host: use an x86-64 SDK on
`amd64`, or an AArch64 SDK on `arm64`. Episode checks the ELF architecture
before any native library is loaded.

### Verify the SDK

Open Episode's **System** page and find **Configured plugins**. A working install
shows `Hikvision HCNetSDK`, its SDK version and architecture, plus one health
entry per configured device. It includes connection state, preserved
notification count, and last notification time. The same state is available from:

```bash
curl http://localhost:8989/api/v1/plugins
```

Normal plugin and worker lifecycle messages use Episode's main container log.
HCNetSDK's own diagnostic file logging is not enabled, and no files are written
into the read-only `plugins/` mount.

The reported states are:

- `not_installed`: the plugin is configured but its SDK directory is absent;
  Episode runs normally.
- `incomplete`: required runtime files or `HCNetSDKCom/` are missing.
- `incompatible`: the SDK is not a supported 64-bit ELF library or its CPU
  architecture does not match the host.
- `validating`: the isolated validation process is running.
- `ready`: validation succeeded and every configured device worker is connected.
- `degraded`: at least one device worker is connected and at least one is not.
- `failed`: validation failed, or no configured device worker is available.

Validation runs in a disposable child process, and each configured SDK device
runs in its own long-lived child process. A broken library or native crash
changes plugin health but does not stop Episode. Failed login and subscription
attempts are not automatically retried in a tight loop, avoiding accidental
device lockouts; correct the configuration and restart Episode.

Every successfully copied callback buffer is initially sealed below
`data/orphans/plugin-deliveries/hikvision-sdk/<device-id>/` and registered as an
accepted ingestion receipt. When an explicitly supported callback creates an
Event, Episode links the receipt and moves the sealed artifact into that
Episode's `events/` directory. Uninterpreted callbacks remain in the orphan
location for future inspection and reprocessing.

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
