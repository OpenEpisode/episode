<p align="center">
  <img src="brand/episode-mark.svg" width="112" height="112" alt="Episode">
</p>

<h1 align="center">Episode</h1>

<p align="center">
  <strong>Events tell you what was observed. Episodes show you what happened.</strong>
</p>

<p align="center">
  <a href="https://github.com/OpenEpisode/Episode/actions/workflows/ci.yml"><img src="https://github.com/OpenEpisode/Episode/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/OpenEpisode/Episode/releases"><img src="https://img.shields.io/github/v/release/OpenEpisode/Episode?include_prereleases&amp;sort=semver" alt="Latest release"></a>
  <a href="https://github.com/OpenEpisode/Episode/pkgs/container/episode"><img src="https://img.shields.io/badge/GHCR-container-2496ED?logo=docker&amp;logoColor=white" alt="Container image"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/OpenEpisode/Episode" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.12 or newer">
</p>

Episode is a local-first system that collects activity from cameras and other devices,
groups related events and evidence into self-contained incident records, and
coordinates actions such as recording and snapshot capture.

Episode uses ONVIF as its primary camera integration and keeps
Hikvision-specific inputs as optional enrichment. ONVIF event polling is
disabled by default because generic motion can be noisy; ONVIF discovery,
media profiles and RTSP remain available. It is intended for technical
self-hosters who want local, portable evidence.

## What it does

- Discovers ONVIF media profiles and can subscribe to motion and tamper events when enabled per device.
- Records from discovered RTSP streams and can optionally request ONVIF snapshots.
- Optionally enriches observations with Hikvision ISAPI and Alarm Server events.
- Preserves camera-created files through a generic FTP transport, then lets the
  configured Hikvision plugin interpret supported snapshot filenames.
- Preserves and checksums raw XML, snapshots, and recordings locally.
- Records every ingress delivery and deduplicates matching observations.
- Accepts normalized Events from trusted local automations through an optional
  raw-first HTTP Event API.
- Correlates observations from multiple cameras into Episodes.
- Starts and stops configured recordings around Episode activity.
- Reviews each Episode through a chronological Event timeline linked to its recordings and snapshots.
- Optionally projects vendor detection regions over snapshots and recordings without modifying evidence.
- Presents chronological Episodes, active camera views, Activity, Evidence,
  Devices, and system status in a web UI.

Episode does not currently provide authentication. Do not expose it directly to
the Internet. The Docker setup binds the web interface to localhost by default.

## Install with Docker

Requirements:

- Docker with the Compose plugin.
- A Linux user that can write to the project directory.
- ONVIF cameras for the primary live-ingestion path; the UI also starts without cameras.

The published image supports 64-bit Intel/AMD and ARM Linux hosts. Clone the
repository or download a release source archive, then prepare the local files:

```bash
cp episode.example.json episode.json
cp .env.example .env
mkdir -p data plugins
```

Replace the example FTP password in `episode.json` before allowing camera
access. Areas and Devices are configured in the web interface; the file now
contains only system-wide settings such as shared transports and action
defaults. Then start Episode:

```bash
docker compose --env-file .env pull
docker compose --env-file .env up -d
```

Open <http://localhost:8989>. A fresh installation opens the guided setup:

1. Create a physical Area that defines the correlation boundary.
2. Add a Device, choose its physical role, enter its address and credentials,
   then use **Validate and discover**. Episode reports protocol support
   independently from what is configured or currently running.
3. Select its capture behavior and integrations. Manufacturer, model, firmware,
   and media profiles discovered at runtime are shown read-only.
4. Save the Device. Episode activates its selected integrations immediately;
   the container does not need to restart.

Inspect service health with
`docker compose --env-file .env ps` and follow logs with
`docker compose --env-file .env logs -f episode`.

Stop it without deleting captured data:

```bash
docker compose --env-file .env down
```

The image version is pinned in `.env`. The commands pass that file explicitly to
Compose for `${...}` interpolation; it is not injected into the Episode
container. Episode reads shared service settings from the read-only
`episode.json` mount and stores UI-managed Area and Device inventory in SQLite.
During the pre-release lifecycle, database migrations are not guaranteed; a
release may require a clean database and will say so in its release notes. To
upgrade, change `EPISODE_IMAGE` to a new published version, review the release
notes, and run
`docker compose --env-file .env pull` followed by
`docker compose --env-file .env up -d`.

### Area recording

Each video Device has a **Recording behavior** selected in the UI:

- `on_event` (default) records when that video device emits an active Event.
- `on_episode` records whenever any active Event opens or updates an Episode in
  the device's Area, including Events from doorbells and non-video sensors.

All participating cameras record until the Episode closes after
`episode_timeout` seconds without active Event activity. Inactive observations
and duplicate connector deliveries do not start recordings.

Long recordings are finalized as immutable, sync-friendly segments while capture
continues through the same FFmpeg connection. Set
`actions.recording.segment_seconds` to control their length (default: 600 seconds).
The final partial segment is closed when the Episode closes; this setting limits
file size, not the Episode or recording duration.

### Network access

The UI/API binds to `127.0.0.1:8989` by default. FTP listens on port `2121` so
cameras on the local network can upload snapshots; passive FTP uses ports
`30000-30009`.

If cameras or local automation systems must push Alarm Server or Event API
deliveries to Episode, set the HTTP bind address in a local `.env` file:

```dotenv
EPISODE_HTTP_BIND=0.0.0.0
```

Only do this on a trusted network. Beta does not provide API authentication.

The [Event API guide](docs/EVENT_API.md) shows how Home Assistant, scripts, alarm
panels, and other local systems can trigger the same Area-scoped recording flow.

Start with the [ONVIF setup guide](docs/ONVIF_SETUP.md). Hikvision users can also
enable the [vendor-specific enhancements](docs/HIKVISION_SETUP.md).

Optional native integrations use the generic read-only `./plugins` mount.
HCNetSDK setup is covered alongside the other
[Hikvision enhancements](docs/HIKVISION_SETUP.md#hikvision-hcnetsdk); the SDK is
supplied by the user and never included in Episode's image.
Installed runtime files remain inactive until a Device explicitly enables the
matching integration configuration.

Beta supports explicitly configured third-party Device and ingress plugins
through the versioned plugin API v1 contract. The
[plugin authoring guide](docs/PLUGINS.md) documents manifests, lifecycle,
raw-first ingestion, scoped Device access, compatibility, and the included
dependency-free example. Third-party plugins are trusted code and are not
sandboxed.

### Troubleshooting

- If `./data` is not writable, set `EPISODE_UID` and `EPISODE_GID` in `.env` to
  the output of `id -u` and `id -g`.
- If a camera cannot upload over FTP, allow TCP 2121 and 30000-30009 between the
  camera network and the Docker host.
- If the UI works locally but cameras cannot send Alarm Server events, set
  `EPISODE_HTTP_BIND=0.0.0.0` and keep the host on a trusted network.
- Use `docker compose --env-file .env logs -f episode` to inspect integration and
  recording errors.
- Use **System → Download diagnostics** to attach a sanitized runtime report to
  an issue without exposing stored credentials or private data paths.

The [ONVIF troubleshooting guide](docs/ONVIF_SETUP.md#troubleshooting) contains a
more complete checklist.

## Data

Runtime data is written below `./data`. Each Episode folder contains its raw
event payloads, snapshots, recordings, an atomic `manifest.json`, and an
append-only `journal.ndjson`. The folder remains understandable if the SQLite
index is unavailable. Keep this directory backed up or synchronized separately.

Local `episode.json`, `.env`, and runtime data—including the SQLite-managed
inventory—are ignored by Git and must never be committed.

## Design principles

- Preserve raw inputs before parsing or processing them.
- Keep original evidence immutable and derived annotations separate.
- Record ingress deliveries as receipts; several receipts may describe one Event.
- Treat Episodes as interpretations and preserve how they were assembled.
- Keep connectors, correlation, and actions independently extensible.
- Prefer safe defaults, local operation, and recoverable data.

## Project status

Episode is a working ONVIF-first public beta for technical self-hosters using
IP cameras and Docker. Hikvision integrations provide optional enrichment. The
current priorities are reliable preservation, correct
correlation, simple installation, and an uncluttered Episode-first interface.

Authentication, action and processor plugins, AI processing, high availability,
and guaranteed compatibility with every ONVIF implementation are not part of
the current release. Beta supports Device and ingress plugin API v1 for the
duration of the beta cycle. Device capabilities are detected rather than
assumed.

See [the contribution guide](docs/CONTRIBUTING.md) before proposing broader
feature work.

## Identity

The open-source organization is **OpenEpisode**; the product is **Episode**. The
mark represents independent observations converging into a coherent Episode and
an action, without tying the project to cameras.

Ready-to-use assets live in [`brand/`](brand/), including transparent vector
artwork, light and dark organization avatars, and GitHub social previews.

## Documentation

- [Architecture and domain model](docs/ARCHITECTURE.md)
- [REST API v1 conventions](docs/API.md)
- [Generic Event API](docs/EVENT_API.md)
- [Plugin authoring and example](docs/PLUGINS.md)
- [ONVIF camera setup](docs/ONVIF_SETUP.md)
- [Hikvision setup and troubleshooting](docs/HIKVISION_SETUP.md)
- [Contributing](docs/CONTRIBUTING.md)
- [Security policy](docs/SECURITY.md)

## Support

If Episode is useful to you, you can support its continued development through
[PayPal.Me](https://paypal.me/nsenica). Contributions are appreciated but never
required.

## License

Episode is available under the [MIT License](LICENSE).
