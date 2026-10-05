# Episode documentation

Episode is a self-hosted, local-first incident capture tool. It combines related
camera, doorbell, sensor and automation observations into an Area-scoped Episode,
with original evidence and traceable provenance. Start with the
[project overview](../README.md) for the supported experience and current limits.

## Understand and evaluate the project

Read [what works today](../README.md#what-works-today), then
[operator behavior](OPERATIONS.md) and [known architectural constraints](ARCHITECTURE.md#known-constraints).
Episode is a pre-1.0 project for technical self-hosters. It has no built-in
API authentication and should remain on a trusted network or behind a trusted
access layer. See [Security](SECURITY.md) before deploying it.

Documentation describes the source revision you are reading. The default
Compose workflow runs the image pinned in `.env`; that image may differ from
your checkout. Use docs and configuration examples from the chosen release tag.
Features explicitly marked planned or future are not currently supported.
A protocol integration does not imply support for every model or firmware.

## Install and verify capture

1. Follow [Installation](INSTALLATION.md) for prerequisites, configuration,
   network exposure, start/stop, upgrades and troubleshooting.
2. Add one Area and Device using the matching setup guide below.
3. Follow [Verify your first capture](OPERATIONS.md#verify-your-first-capture)
   to check an Event, an Episode and finalized playable Evidence.
4. Review [capture profiles](OPERATIONS.md#capture-profiles),
   [notifications](OPERATIONS.md#episode-start-notifications), and
   [retention](OPERATIONS.md#retention) before relying on unattended capture.

## Connect a Device or automation

| Source or integration | Guide |
| --- | --- |
| Standards-based camera discovery, media and Events | [ONVIF setup](ONVIF_SETUP.md) |
| Hikvision ISAPI, Alarm Server, FTP or optional native SDK | [Hikvision setup](HIKVISION_SETUP.md) |
| Reolink Baichuan, snapshots and media | [Reolink setup](REOLINK-SETUP.md) |
| Trusted local scripts, sensors and home automation | [Event API](EVENT_API.md) |
| Third-party Device or ingress plugin | [Plugin authoring](PLUGINS.md) and [runtime directory](../plugins/README.md) |

Connection validation, Event delivery and media capture are separate checks.
Read each integration's limitations and troubleshooting notes, and record the
model, firmware and configuration when reporting compatibility.

## Contribute or build an integration

Start with [Contributing](CONTRIBUTING.md) for a development checkout,
prerequisites, a source/test map, quality checks and pull-request expectations.
Then choose the appropriate reference:

- [Architecture](ARCHITECTURE.md): domain ownership, lifecycle, storage,
  evidence guarantees and extension boundaries.
- [REST API](API.md): public resource representations, pagination, media,
  diagnostics and settings. [Event API](EVENT_API.md) covers inbound observations.
- [Plugin authoring](PLUGINS.md): the versioned external Python facade,
  activation, lifecycle, media and raw-first ingestion.
- [Project implementation guide](../AGENTS.md): invariants and review criteria
  for automated contributors; also useful for human reviewers.
- [Security reporting](SECURITY.md#reporting-a-vulnerability): report
  vulnerabilities privately, with no credentials or identifiable footage in
  public issues.

Public documentation should explain the implemented behavior and how to verify
it. Keep proposed features visibly separate from current capabilities, update
affected examples alongside changes, and contribute only sanitized, shareable
fixtures. The project is [MIT licensed](../LICENSE); vendor SDK licensing is
separate.

## Short glossary

| Term | Meaning |
| --- | --- |
| Area | The physical grouping used for current correlation and recording policy. |
| Device | A configured source, such as a camera, doorbell or sensor, assigned to an Area. |
| Delivery | Bytes or a file received through a transport. |
| Raw Artifact | The sealed, checksummed content preserved from a delivery. |
| Receipt | How, when and from where a delivery arrived, its outcome and associations. |
| Event | A canonical observation interpreted from preserved input. |
| Evidence | Incident material such as a snapshot or recording; original bytes remain unchanged. |
| Episode | An evolving interpretation of related activity in an Area, with a defined closing boundary. |
| Annotation | Versioned derived information that does not change its source; general annotation/processing APIs remain planned. |

See [Architecture](ARCHITECTURE.md#domain-language) for precise lifecycle and
provenance rules. Checksums help detect changed bytes; they are not signatures,
trusted timestamps or an independent guarantee of scene authenticity.
