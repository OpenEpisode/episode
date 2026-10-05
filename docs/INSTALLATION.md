# Installation

Episode is distributed as a multi-platform container for 64-bit Intel/AMD and
ARM Linux hosts. Docker Compose is the supported first-run path.

## Requirements

- Docker with the Compose plugin.
- A Linux user that can write to the project directory.
- An ONVIF or supported vendor camera when capturing video. Episode can also
  start without cameras for evaluation.

## Prepare and start

Clone the repository or download a release source archive, then prepare the
local configuration and managed directories:

```bash
cp episode.example.json episode.json
cp .env.example .env
mkdir -p data plugins
```

Before starting, replace the example FTP password in `episode.json`, or disable
the FTP connector if uploads are not needed. The example enables FTP and
Compose publishes its ports on all host interfaces by default. Set
`EPISODE_UID` and `EPISODE_GID` in `.env` to a user that can write `data/`
(`id -u` and `id -g` show your Linux user/group IDs). Areas and Devices are
configured in the web interface; the JSON file
contains only system-wide settings such as shared transports and action
defaults.

The commands below run the release image pinned in `.env`, not the source in
your checkout. Use the documentation and example configuration from that
release tag. For a local source build, follow [contributor setup](CONTRIBUTING.md#development-setup).

Pull and start the pinned release:

```bash
docker compose --env-file .env pull
docker compose --env-file .env up -d
```

Open <http://localhost:8989>. A fresh installation guides you through creating
an Area, discovering a Device with ONVIF, selecting only integrations matching
the discovered identity, and confirming the Evidence retention policy. A
Device may be saved for later while it is not ready; it remains visible but is
not eligible for new Episodes or captures. Device and Area changes activate
immediately without restarting Episode. If ONVIF is unavailable, a manually
validated RTSP endpoint can still provide recording-only capture; it does not
provide Events or snapshots.

Inspect service health or follow logs with:

```bash
docker compose --env-file .env ps
docker compose --env-file .env logs -f episode
```

Stop Episode without deleting captured data:

```bash
docker compose --env-file .env down
```

## Local files

Compose uses `.env` explicitly for `${...}` interpolation; those values are not
injected into the Episode container. The important local paths are:

- `.env`: image version, host bindings, and container UID/GID;
- `episode.json`: shared service and action defaults;
- `data/`: SQLite state and portable Episode directories;
- `plugins/`: optional user-supplied runtime plugin files.

These paths are ignored by Git and must not be committed. Ensure `./data` is
writable by `EPISODE_UID` and `EPISODE_GID`; on Linux, their values commonly
match the output of `id -u` and `id -g`.

## Network access

The UI/API binds to `127.0.0.1:8989` by default. FTP listens on port `2121`,
with passive ports `30000-30009`, so cameras can upload snapshots.

If cameras or trusted local automations must push Alarm Server or Event API
deliveries directly to Episode, set this in `.env`:

```dotenv
EPISODE_HTTP_BIND=0.0.0.0
```

Only expose it on a trusted network. Episode beta does not provide API
authentication. See the [security guide](SECURITY.md) before changing exposure.

Allow TCP 2121 and 30000-30009 between the camera network and Docker host when
using FTP. The [Event API guide](EVENT_API.md) explains how local automation
systems can trigger the same Area-scoped capture flow.

## Integrations and plugins

Start with the [ONVIF guide](ONVIF_SETUP.md). Optional vendor integrations are
covered by the [Hikvision](HIKVISION_SETUP.md) and
[Reolink](REOLINK-SETUP.md) guides.

Native or third-party integrations use the generic read-only `./plugins`
mount. Hikvision HCNetSDK is supplied by the user and is never included in the
Episode image. Built-in SDK integrations remain inactive until a Device
explicitly enables them. External Device and ingress plugins instead require
an entry in the top-level `plugins` array in `episode.json` and a restart; see
[external plugin activation](PLUGINS.md#activate-a-plugin).

Third-party plugins are trusted code and are not sandboxed. Their manifest,
lifecycle, compatibility, and raw-first requirements are documented in the
[plugin authoring guide](PLUGINS.md).

## Upgrade

The image version is pinned by `EPISODE_IMAGE` in `.env`. To upgrade:

1. read the target release notes and compare the target configuration examples;
2. stop Episode and make a consistent backup of `data/`, `episode.json`, and
   `.env` before changing versions; keep credentials and evidence private, and
   record any separately installed plugin/SDK versions needed to restore;
3. update `EPISODE_IMAGE`;
4. run:

```bash
docker compose --env-file .env pull
docker compose --env-file .env up -d
```

During pre-1.0 development, compatibility is not guaranteed for every schema
change. Episode applies explicitly supported additive schema steps, while an
incompatible release may require a clean database and will say so in its
release notes. Never reset or delete your existing database as a routine upgrade
step. Keep a pre-upgrade backup; switching the image tag alone is not a reliable
rollback after a schema change. The [release-retention policy](CONTRIBUTING.md#release-retention)
keeps Git tags but prunes older release assets and images, so an old pinned image
may need to be retained locally or rebuilt from its tag.

## Troubleshooting

- If `./data` is not writable, correct its ownership or set `EPISODE_UID` and
  `EPISODE_GID` to the directory owner's numeric IDs.
- If FTP uploads fail, verify the password, TCP 2121, and passive ports
  30000-30009.
- If the UI works locally but push Events do not arrive, confirm
  `EPISODE_HTTP_BIND=0.0.0.0` and the host firewall policy.
- Use **System → Integrations** for connector and Device health.
- Use **System → Recordings** for active fragment progress, reconnecting
  streams, and recent incomplete captures.
- Use **System → Download diagnostics** to create a sanitized report without
  camera credentials or private data paths.

The [ONVIF troubleshooting checklist](ONVIF_SETUP.md#troubleshooting) covers
camera discovery, authentication, media, and Events in more detail.
