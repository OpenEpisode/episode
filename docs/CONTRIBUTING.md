# Contributing

Thank you for your interest in Episode.

Episode is currently in active pre-1.0 development.

The primary goal at this stage is to harden the Episode model and turn the
working beta into a dependable, understandable product rather than expand the
feature set indiscriminately.

Coding agents and automated contributors must also follow the project-level
[`AGENTS.md`](../AGENTS.md) implementation guide. Human contributors may find it
useful as a concise statement of the invariants a change must preserve.

## Guiding Principles

When contributing, please keep the following principles in mind:

- Raw artifacts and evidence bytes are immutable.
- Canonical Event observations remain stable while associations may evolve.
- Episodes and review timelines represent interpretation.
- Shared connectors preserve deliveries and plugins interpret them without owning correlation policy.
- The core domain remains vendor-independent.

Architectural consistency is preferred over rapid feature growth.

## Before Adding Features

Please ask:

> Does this make Episode more reliable, understandable, or meaningfully more useful?

If not, it is probably better suited for a future release. Prefer a demonstrated
use case over a general policy framework introduced in anticipation of one.

## Code Style

- Keep modules focused.
- Prefer composition over inheritance.
- Keep vendor interpretation inside plugins; shared connectors remain vendor-neutral.
- Preserve raw deliveries before interpretation.
- Keep restart, timeout, and partial-write behavior explicit and testable.
- Write code that is easy to understand and test.
- Out-of-tree plugins must import only from the documented `episode.plugin_api`
  facade; internal plugin, storage, engine, and ingestion modules are not public
  contracts.

## Development setup

Episode uses Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked --all-groups
```

Run the same checks used by CI before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv build
node --test tests/ui/*.test.mjs
```

Build and run the current source with the developer override:

```bash
docker compose --env-file .env -f compose.yaml -f compose.dev.yaml up -d --build
```

The normal `docker compose --env-file .env up -d` path intentionally pulls the
published image and is the supported installation path for users.

## Pull Requests

Small, focused pull requests are preferred over large feature drops.

Each pull request should solve one problem.

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

Ideas and architectural discussions are always welcome.

The beta intentionally remains focused so that operational behavior and public
extension contracts can mature before broader automation and AI functionality
is introduced.

Thank you for helping shape Episode.
