# Episode implementation guide

This file is the project-level contract for coding agents and automated
contributors. It complements, rather than replaces, the public architecture and
contribution documents. Read it before changing code.

The objective is not to preserve today's implementation at all costs. It is to
preserve Episode's evidence guarantees and architectural boundaries while
keeping the codebase small, legible, and able to evolve.

## Read first

Before making any change, read:

1. [`README.md`](README.md) for the product and supported user experience.
2. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the current domain and
   runtime design.
3. [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) for workflow and quality gates.

Then follow the document map for the area being changed:

| Change area | Required reference |
| --- | --- |
| Public REST representations, pagination, or endpoints | [`docs/API.md`](docs/API.md) |
| Generic external observations | [`docs/EVENT_API.md`](docs/EVENT_API.md) |
| Built-in or third-party plugin contracts | [`docs/PLUGINS.md`](docs/PLUGINS.md) |
| ONVIF discovery, Events, media, or validation | [`docs/ONVIF_SETUP.md`](docs/ONVIF_SETUP.md) |
| Hikvision ISAPI, Alarm Server, FTP, or HCNetSDK | [`docs/HIKVISION_SETUP.md`](docs/HIKVISION_SETUP.md) |
| Exposure, credentials, reporting, or threat boundaries | [`docs/SECURITY.md`](docs/SECURITY.md) |
| Operator behavior, recording, notifications, or retention | [`docs/OPERATIONS.md`](docs/OPERATIONS.md) |
| Packaging, deployment, or first-run behavior | [`docs/INSTALLATION.md`](docs/INSTALLATION.md), [`.env.example`](.env.example), and [`compose.yaml`](compose.yaml) |

Read every reference relevant to a cross-cutting change. Documentation describes
the intended system; existing code alone is not an architectural specification.
When code and documentation conflict, determine which reflects the maintainer's
latest decision and update the stale side in the same change.

## Decision priorities

When possible approaches conflict, prefer the one that, in order:

1. preserves the original input;
2. maintains traceable provenance;
3. keeps the core vendor-neutral;
4. contains failures and makes recovery explicit;
5. is simplest to understand, operate, and test.

If a proposal cannot satisfy the first four, stop and raise the trade-off with
the maintainer instead of silently weakening a guarantee.

## Domain language

Use these terms consistently in code, APIs, UI, tests, and documentation:

- **Delivery**: bytes or a file received through a transport.
- **Raw Artifact**: the exact, sealed, checksummed content of a delivery.
- **Receipt**: how, when, and from where one delivery arrived, plus its outcome
  and later associations.
- **Event**: a canonical observation interpreted from one or more receipts.
- **Evidence**: meaningful incident material such as a snapshot or recording.
- **Episode**: an evolving, Area-scoped interpretation of related activity.
- **Annotation**: versioned derived information; it never changes its source.

The Raw Artifact and Receipt are deliberately separate. Identical bytes may be
delivered more than once, through different transports, at different times, or
with different outcomes. The artifact describes what; the receipt describes
how.

## Non-negotiable evidence rules

### Preserve first

- Persist the exact input and its Receipt before parsing, deduplicating,
  correlating, or invoking plugin interpretation.
- Malformed, unsupported, ignored, duplicate, and failed deliveries must remain
  auditable. A parse failure is an outcome, not a reason to discard input.
- Never reconstruct a supposedly raw payload from parsed fields.
- Bound payload size, processing time, queues, retries, and network operations.

### Keep originals immutable

- Never draw bounding boxes, labels, timestamps, or AI output into original
  Evidence or Raw Artifacts.
- Store thumbnails, overlays, transcodes, timelapses, and future AI output as
  derived, reproducible material with provenance.
- Never overwrite an Evidence path on collision. Use stable identities and
  collision-safe storage.
- Retention may intentionally remove managed bytes only through the documented
  lifecycle, leaving the Evidence identity, checksum, and expiration record.

### Preserve provenance and portability

- Every derived observation or asset must remain traceable to its source
  Receipt, Raw Artifact, Evidence, and processing version where applicable.
- Keep each Episode understandable from its portable directory, atomic
  `manifest.json`, and append-only `journal.ndjson`. SQLite is the operational
  index, not the only record of relationships.
- Association and derived projections may evolve; original bytes and canonical
  observation content do not.

## Architectural boundaries

### Core responsibilities

The core owns the vendor-neutral domain, raw-first ingestion, persistence,
deduplication, correlation, Episode lifecycle, target resolution, actions,
retention, portable bundles, and public operational projections.

The core must not interpret vendor protocols or construct vendor clients. Avoid
adding `if vendor == ...` branches to domain, engine, recording, storage, or API
business logic. Extend a generic contract or put the behavior in the relevant
plugin.

### Transport responsibilities

Shared transports such as HTTP Alarm Server, FTP, the Event API, and future
MQTT accept and preserve deliveries. They do not own vendor parsing,
correlation, Episode policy, or recording decisions.

A transport answers “how did bytes arrive?” A plugin answers “what do these
bytes mean?” The core answers “how does this observation affect an Episode?”

### Plugin responsibilities

- Device plugins own protocol clients, discovery, authentication, connection
  supervision, validation, media registration, and interpretation.
- Ingress plugins claim and interpret preserved deliveries from shared
  transports.
- Plugins report normalized observations. They do not choose an Episode,
  deadline, action target, or retention outcome.
- Installed code is inert until explicitly configured. Do not import or start
  every installed plugin.
- Plugin startup, handlers, status, reconnect, and shutdown must be bounded and
  failure-isolated. One integration must not bring down Episode.
- A native SDK capable of crashing the interpreter belongs behind a supervised
  process boundary.
- Unknown messages should remain preserved and unclaimed or explicitly
  rejected; do not guess their meaning.
- Do not deduplicate before the raw-first boundary.

Out-of-tree plugins may import only from the versioned `episode.plugin_api`
facade. Changing that contract requires contract tests and corresponding
updates to `docs/PLUGINS.md`; never make an incompatible change silently within
an existing API version.

### Service and presentation responsibilities

- Domain decisions belong in domain services or the engine, not FastAPI routes,
  serializers, browser code, or templates.
- The API exposes purpose-built, bounded, credential-free projections. Internal
  plugin dictionaries and storage rows are not public UI contracts.
- UI state may guide an operator, but it must not become the authoritative
  source of Episode, recording, correlation, or retention behavior.
- Prefer a focused existing abstraction over a new framework. Extract a new
  boundary when there are real consumers or demonstrated duplication.

## Episode and recording semantics

- An Event may create, join, or extend an Episode. Related inactive Events may
  attach after closure but must not reopen an Episode or restart actions.
- Area is the current correlation and action boundary.
- The Device emitting an active Event contributes its configured activity
  window to the Episode's minimum deadline. A later Event may extend that
  deadline; it cannot shorten it.
- Devices joining through `on_episode` follow the Episode's lifetime. Their own
  configured window applies only when they emit a contributing Event.
- New related activity should extend an existing Episode rather than create a
  parallel one solely because another connector or camera observed it.
- Chunking a recording creates more components, not more Evidence rows or
  Episodes. One participating camera has one logical recording Evidence item
  per Episode.
- HLS playlists, initialization data, and fragments are components of that
  Evidence item. Inventory all components and remove them together during
  retention.
- Active capture and interrupted working files must remain distinguishable from
  finalized Evidence. Never present incomplete media as successfully complete.
- Current views are operational previews. They become Evidence only through an
  explicit preservation action.

Cross-Area movement handoff, spatial camera topology, broad policy engines, and
alarm-wide activation are future features. Do not implement them speculatively,
but avoid assumptions that would make future target resolvers or correlation
strategies impossible.

## Security and operational rules

- Credentials are write-only. Never return them from APIs, render them in the
  UI, include them in diagnostics, or log credential-bearing stream URLs.
- Validate addresses, ports, paths, identifiers, media types, and all untrusted
  input. Use explicit timeouts and bounded reads.
- Keep cleanup idempotent and release resources in reverse startup order. One
  cleanup failure must not prevent the rest.
- Preserve transaction safety under cancellation. Roll back interrupted writes
  and do not leave SQLite locks held.
- Do not claim Device or protocol support without tested evidence. Treat a
  transient connection failure as degraded health, not proof of incompatibility.
- Make destructive retention behavior and disabled-retention risk visible to
  the operator.
- Respect third-party licensing and attribution. Do not add bundled dependencies
  or remotely loaded scripts casually; document the operational and security
  trade-off.

## User experience rules

- Organize screens around operator tasks: understand what happened, verify
  Evidence, configure Devices and Areas, and diagnose failures.
- Prefer chronological and Episode-first presentation over unstructured card
  collections.
- Use progressive disclosure for raw payloads and diagnostics. They must remain
  accessible without overwhelming the default view.
- Present one canonical source label to users. Transport, plugin, and vendor
  provenance may coexist in details, but must not appear as duplicate origins.
- Avoid redundant expected state. For example, emphasize an active Episode;
  closure need not dominate every card.
- Keep date and time formatting, badges, icons, spacing, terminology, and empty
  states consistent across collection and detail pages.
- Iconography supplements visible or accessible text; it never replaces meaning
  with an unexplained symbol.
- Preserve the established Episode palette and reusable UI patterns. Extend
  existing components before adding one-off styles.
- Paginate or bound every growing collection and make empty/error/loading states
  explicit.

## Pre-1.0 development policy

Episode currently supports only its current schema. Do not add database
migrations, legacy inventory importers, compatibility shims, or dead fallback
paths unless the maintainer explicitly requests them. A clean, direct model is
more valuable than preserving abandoned development formats.

This policy does **not** authorize deleting or resetting local data. A schema
break may be acceptable, but any destructive operation still requires explicit
maintainer approval.

Do not build speculative abstractions for AI, processors, action marketplaces,
cross-Area tracking, or general policy evaluation. It is appropriate to retain
small seams for versioned annotations, processing provenance, target resolution,
and topology, but implement them only with a concrete use case.

## Change discipline

Before editing:

- inspect `git status` and preserve unrelated or untracked work;
- trace the existing path from transport to storage, engine, API, and UI;
- identify the invariant and owner of the behavior being changed;
- search for an existing contract before adding a new abstraction.

While editing:

- keep diffs focused and modules cohesive;
- prefer explicit data flow and typed boundaries over clever indirection;
- remove obsolete paths when a decision replaces them; do not keep “just in
  case” code;
- update public documentation and examples when behavior or configuration
  changes;
- never add AI attribution or `Co-authored-by` metadata;
- do not commit, push, tag, publish, delete data, or restart a live installation
  unless the maintainer explicitly authorizes that action.

## Definition of done

A change is complete when:

- the relevant raw-first, failure, duplicate, timeout, restart, and cleanup paths
  are tested in proportion to its risk;
- protocol behavior uses sanitized real fixtures where practical;
- API and UI changes cover loading, empty, error, and bounded-list behavior;
- documentation describes what the released code actually does;
- no new secret, absolute path, or internal implementation detail leaks through
  logs or public projections.

Run the applicable CI checks before handing off code:

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
node --test tests/ui/*.test.mjs
docker compose --env-file .env.example config -q
docker compose --env-file .env.example -f compose.yaml -f compose.dev.yaml config -q
```

Run container smoke tests when changing packaging, startup, native dependencies,
lifecycles, media capture, or runtime configuration.

## Final review questions

Before handing a change to the maintainer, answer:

1. Are the original bytes still preserved before interpretation?
2. Can every Event and Evidence item be traced to what produced it?
3. Did vendor-specific behavior stay outside the core?
4. Can this fail without taking unrelated capture paths down?
5. Does it remain understandable without the database or hidden UI state?
6. Is this the smallest design that handles the demonstrated use case?
7. Did the tests and documentation change with the behavior?
