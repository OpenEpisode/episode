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
</p>

Episode turns camera, doorbell, sensor, and automation alerts into one incident
timeline. When something happens, it can record every relevant camera in the
same physical Area and present the Events, snapshots, and video together as a
single Episode.

It is self-hosted and local-first. Instead of leaving you with disconnected
alerts and clips, Episode preserves the original material, records where it
came from, and keeps the whole incident portable.

## Why Episode?

- **One incident, multiple camera views.** A doorbell, camera, sensor, or
  automation can start recording across the relevant Area.
- **Evidence stays original.** Raw deliveries, snapshots, and recordings are
  preserved and checksummed; overlays and future AI results remain derived.
- **Local and portable.** Data remains on your infrastructure, and every
  Episode has a self-contained directory, manifest, and journal.
- **Standards first, vendor enhancements second.** ONVIF provides the primary
  camera path, while plugins add richer vendor-specific capabilities.
- **Built to extend.** A versioned plugin API lets integrations evolve without
  putting vendor logic into the core.

## What works today

| Source | Current support |
| --- | --- |
| ONVIF cameras | Discovery, media profiles, RTSP, snapshots, and optional Events |
| Hikvision | ISAPI, Alarm Server, FTP snapshots, and optional HCNetSDK |
| Reolink | Baichuan discovery, media, snapshots, and subscribed Events |
| Local automations | Raw-first HTTP Event API |

Episode can start Area-wide multi-camera recording, stream active HLS captures,
show current camera views, correlate snapshots and detection regions, switch
capture participation profiles, and send an Episode-start webhook or Discord
notification. A profile (or a single camera) can also filter event classes —
plain motion, device status, audio, tamper and video loss, classified detections
and wired alarm inputs, access records — so they stop opening Episodes by
themselves. Nothing is filtered until you name its class, every class is
available at both levels, and a filtered Event is still preserved and attributed
to the Episode that was already open.

## Start in five minutes

You need Docker with the Compose plugin. Clone the repository or download a
release source archive, then run:

```bash
cp episode.example.json episode.json
cp .env.example .env
mkdir -p data plugins
docker compose --env-file .env pull
docker compose --env-file .env up -d
```

Open <http://localhost:8989>. The guided setup will help you:

1. create an Area;
2. add and validate a Device;
3. choose its recording behavior and integrations;
4. confirm the Evidence retention policy.

Replace the example FTP password in `episode.json` before allowing cameras to
upload files. Areas, Devices, capture profiles, retention, notifications, and
other operator settings are managed from the UI and apply without restarting
the container.

See the [installation guide](docs/INSTALLATION.md) for network access, upgrades,
native plugins, and troubleshooting.

> [!WARNING]
> Episode does not currently provide authentication. Keep it on a trusted
> network or behind a trusted access layer. The default Docker configuration
> binds the web interface to localhost.

## How it works

1. A connector receives a camera, sensor, or automation delivery.
2. Episode preserves the exact input and records how it arrived.
3. A configured plugin interprets it as a vendor-neutral Event.
4. The core correlates related Events into an Area-scoped Episode.
5. Episode starts or extends configured actions such as camera recording.
6. The UI presents the resulting timeline and Evidence together.

The core decides correlation and actions; transports receive bytes; plugins
interpret protocols. This separation keeps Episode vendor-neutral and prevents
one integration from owning the incident model.

## Project status

Episode `0.1.0-beta.8` is a working public beta for technical self-hosters using
IP cameras and Docker. Current priorities are reliable preservation, correct
correlation, simple operation, and an uncluttered Episode-first interface.

Authentication, general policy evaluation, AI processing, high availability,
and compatibility with every camera implementation are not complete. Device
capabilities are detected rather than assumed, and the beta plugin API remains
versioned for external contributors.

## Documentation

- [Installation and troubleshooting](docs/INSTALLATION.md)
- [Using and operating Episode](docs/OPERATIONS.md)
- [ONVIF setup](docs/ONVIF_SETUP.md)
- [Hikvision setup](docs/HIKVISION_SETUP.md)
- [Reolink setup](docs/REOLINK-SETUP.md)
- [Generic Event API](docs/EVENT_API.md)
- [Plugin authoring](docs/PLUGINS.md)
- [Architecture and domain model](docs/ARCHITECTURE.md)
- [REST API v1](docs/API.md)
- [Security](docs/SECURITY.md)
- [Contributing](docs/CONTRIBUTING.md)

The open-source organization is **OpenEpisode**; the product is **Episode**.
Brand assets are available in [`brand/`](brand/).

## Support and license

Episode is released under the [MIT License](LICENSE). If it is useful to you,
you can support its continued development through
[PayPal.Me](https://paypal.me/nsenica). Contributions are appreciated but never
required.
