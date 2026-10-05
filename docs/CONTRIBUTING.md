# Contributing

Thank you for helping improve Episode. Episode is an active pre-1.0 project,
so a contribution should make the current product more reliable,
understandable, or useful while preserving its evidence guarantees.

The public documentation index is in [`docs/README.md`](README.md). Start with
the guide that matches the part of the system you want to change. Automated
contributors must also follow the project implementation contract in
[`AGENTS.md`](../AGENTS.md); it records the invariants and review questions that
apply to code changes.

## Before you start

Read the [README](../README.md), [architecture guide](ARCHITECTURE.md), and
this document. Then read the guide for the affected boundary:

| Change | Read first |
| --- | --- |
| Public REST representations or endpoints | [REST API](API.md) |
| Generic external observations | [Event API](EVENT_API.md) |
| Built-in or third-party plugins | [Plugin authoring](PLUGINS.md) |
| ONVIF discovery, events, or media | [ONVIF setup](ONVIF_SETUP.md) |
| Hikvision protocols or HCNetSDK | [Hikvision setup](HIKVISION_SETUP.md) |
| Reolink protocols or media | [Reolink setup](REOLINK-SETUP.md) |
| Exposure, credentials, or vulnerability reporting | [Security](SECURITY.md) |
| Operator behavior, recording, notifications, or retention | [Operations](OPERATIONS.md) |
| Packaging, deployment, or first-run behavior | [Installation](INSTALLATION.md), [`.env.example`](../.env.example), and [`compose.yaml`](../compose.yaml) |

Documentation describes the behavior the repository is intended to provide. If
code and documentation disagree, identify which decision is current and update
the stale side in the same change. A statement marked planned or future is not
a supported feature or compatibility promise.

## Development setup

Use the repository's default branch or the target branch named by the issue or
maintainer. Do not infer a release or branch policy from the branch currently
checked out in another clone. Create a topic branch for your change:

```bash
git clone https://github.com/OpenEpisode/Episode.git
cd Episode
git switch -c docs/first-contribution
```

Episode development requires Python 3.12, [uv](https://docs.astral.sh/uv/),
Node.js for browser-module checks and tests, and Docker with the Compose plugin
for Compose validation and the container smoke test. Install `ffmpeg` and
`ffprobe` to run the full media test set; the tests that exercise real media skip
when those tools are unavailable. You do not need a camera or native vendor SDK
to run the unit and contract suites.

Install the locked development environment and create local files only when
they do not already exist:

```bash
uv sync --locked --all-groups
test -f .env || cp .env.example .env
test -f episode.json || cp episode.example.json episode.json
mkdir -p data plugins
```

The developer Compose override builds the source checkout and uses the same
`./data`, `./plugins`, `.env`, and `episode.json` bind mounts as the normal
Compose file. Use a separate checkout and separate data/configuration when
running a development container beside a live installation. Do not point it at
production evidence or credentials. Compose also declares the project name
`episode` and fixed host ports: a second checkout alone does not isolate running
containers. Stop an isolated test instance before starting another, or configure
a distinct project name and non-conflicting port bindings before running both.

Before starting, replace the example FTP password or disable the FTP connector,
and set `.env` UID/GID values so the container can write its test data. See
[Installation](INSTALLATION.md#prepare-and-start) for these preparations. Build
and run the current checkout with:

```bash
docker compose --env-file .env -f compose.yaml -f compose.dev.yaml up -d --build
```

Repeat the build command after source changes. The override does not mount the
source tree or provide live reload. The normal Compose path uses the published
image; an explicit `docker compose --env-file .env pull` fetches it. It does not
run your checkout's edits.

## Project map

| Area | Source | Useful tests and documentation |
| --- | --- | --- |
| Domain, correlation, and Episode lifecycle | [src/episode/domain/](../src/episode/domain/), [src/episode/engine/](../src/episode/engine/) | [tests/test_lifecycle.py](../tests/test_lifecycle.py), [tests/test_event_correlation_policy.py](../tests/test_event_correlation_policy.py), [Architecture](ARCHITECTURE.md) |
| Raw-first ingestion and shared transports | [src/episode/ingestion/](../src/episode/ingestion/), [src/episode/connectors/](../src/episode/connectors/) | [tests/test_ingestion.py](../tests/test_ingestion.py), [tests/test_event_api.py](../tests/test_event_api.py), [Event API](EVENT_API.md) |
| Device and ingress integrations | [src/episode/plugins/](../src/episode/plugins/), [src/episode/plugin_api/](../src/episode/plugin_api/) | [tests/test_plugin_manager.py](../tests/test_plugin_manager.py), [tests/test_external_plugins.py](../tests/test_external_plugins.py), [Plugin authoring](PLUGINS.md) |
| Recording and media | [src/episode/recording/](../src/episode/recording/), [src/episode/media/](../src/episode/media/), [src/episode/actions/](../src/episode/actions/) | [tests/test_recording.py](../tests/test_recording.py), [tests/test_hls_recording.py](../tests/test_hls_recording.py), [Operations](OPERATIONS.md) |
| Persistence, bundles, and retention | [src/episode/storage/](../src/episode/storage/) | [tests/test_provenance.py](../tests/test_provenance.py), [tests/test_storage_recovery.py](../tests/test_storage_recovery.py), [tests/test_retention.py](../tests/test_retention.py), [Architecture](ARCHITECTURE.md) |
| REST API | [src/episode/api/](../src/episode/api/) | [tests/test_api_contract.py](../tests/test_api_contract.py), [tests/test_api_security.py](../tests/test_api_security.py), [REST API](API.md) |
| Browser UI | [src/episode/ui/](../src/episode/ui/) | [tests/ui/](../tests/ui/), [Operations](OPERATIONS.md) |

Keep vendor protocol interpretation in the relevant plugin. Shared transports
preserve deliveries; the core owns persistence, correlation, Episode lifetime,
actions, retention, and public projections. Out-of-tree plugins may import only
from the versioned `episode.plugin_api` facade.

## Make a change

Start with a small, reproducible change: a focused bug fix, a documentation
correction, or a test using an existing fixture. Before adding an abstraction,
search for an existing boundary or pattern and identify which component owns
the behavior. Keep public API changes backward-compatible during the beta when
possible. If a contract must change, document the compatibility impact and
update the contract tests and guide with it.

Preserve the following rules in every feature and fix:

- Persist exact input and its receipt before parsing, deduplication,
  correlation, or plugin interpretation.
- Keep raw artifacts and original evidence immutable; overlays, thumbnails,
  transcodes, and interpretations are derived material with provenance.
- Keep canonical Events stable while allowing Episode associations to evolve
  under the documented lifecycle rules.
- Bound input size, processing time, queues, retries, and network operations.
- Keep credentials write-only and out of logs, diagnostics, public responses,
  and issue reports.
- Make failures, interrupted media, reconnects, and cleanup explicit and
  recoverable.

Do not add a database migration, compatibility shim, legacy importer, or
speculative framework during pre-1.0 development unless the maintainer has
requested it. Update public documentation and examples when behavior,
configuration, or supported hardware changes. Protocol fixtures must be
sanitized, legally shareable, and accompanied by their source and license
information where applicable; never commit credentials, private addresses, or
captured evidence.

## Tests and quality checks

For example, while editing an external plugin or browser review behavior:

```bash
uv run pytest -q tests/test_external_plugins.py
node --test tests/ui/episode-view.test.mjs
```


Run the focused tests for the area you change while developing. Before opening
a pull request, run the checks used by CI. The authoritative list is
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml); it currently covers:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q tests/test_external_plugins.py
uv run pytest -q --ignore=tests/test_external_plugins.py
uv build
node --test tests/ui/*.test.mjs
docker compose --env-file .env.example config -q
docker compose --env-file .env.example -f compose.yaml -f compose.dev.yaml config -q
```

CI also syntax-checks every browser module as an ES module and builds and
smoke-tests the container. Run those checks locally when changing browser
modules or packaging. If a check cannot run in your environment, report the
exact command, limitation, and focused checks you did run in the pull request;
do not hide or skip a failure.

When changing protocol handling, cover valid, malformed, duplicate, timeout,
restart, and cleanup paths in proportion to the risk. When changing an API or
UI, cover bounded collections and loading, empty, and error states. Tests must
verify behavior and invariants rather than merely mirror implementation lines.

## Pull requests

Keep a pull request focused on one problem and explain the user or operational
reason for the change. Include:

- the resulting behavior and relevant scope;
- tests and quality checks that passed;
- known limitations, hardware or firmware assumptions, and any check that was
  unavailable;
- documentation or configuration updates needed by an operator or plugin
  author.

Use a clear commit subject consistent with the repository history, such as
`feat:`, `fix:`, `docs:`, or `chore:`. Do not force-push shared branches, commit
generated files, or include AI attribution. Maintainers decide whether a change
is ready to merge and which target branch or release it belongs to.

## Release retention

Git tags remain as the permanent source history. After a successful container
release, the `Cleanup old releases` workflow retains the two newest published
GitHub Releases and the two newest usable GHCR images, including the manifests
required by their multi-platform images, and removes older release records and
package versions. It never deletes Git tags.

Each Release prepends the commit subjects, GitHub usernames where resolvable,
and links since the preceding version tag to GitHub's generated pull-request
notes. It falls back to the recorded Git author name when an account cannot be
resolved. Commit subjects should therefore describe the user-visible or
operational change clearly.

Maintainers can run the workflow manually with its default dry-run option to
review the proposed cleanup without deleting anything. The `episode` package
must grant this repository the **Admin** Actions access role for its
`GITHUB_TOKEN` to delete package versions.

## Discussions

Use the [bug report](../.github/ISSUE_TEMPLATE/bug_report.yml) and
[feature request](../.github/ISSUE_TEMPLATE/feature_request.yml) templates when
opening an issue. Ideas and architectural questions are welcome there too. For a feature request, describe the situation, current
experience, and desired outcome. For a bug, include the Episode version,
environment, smallest reproduction, and redacted logs. Do not publish
credentials, private camera addresses, raw evidence, or vulnerability details;
follow the [security reporting guidance](SECURITY.md) for security issues.

The beta remains focused on dependable preservation, correct correlation,
simple operation, and a useful Episode-first interface. Contributions that
strengthen those foundations are especially valuable.
