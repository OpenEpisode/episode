# Contributing

Thank you for your interest in Episode.

Episode is currently in active pre-1.0 development.

The primary goal at this stage is to validate the Episode model rather than expand the feature set.

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

> Does this help validate the Episode concept?

If not, it is probably better suited for a future release.

## Code Style

- Keep modules focused.
- Prefer composition over inheritance.
- Keep vendor interpretation inside plugins; shared connectors remain vendor-neutral.
- Preserve raw deliveries before interpretation.
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

## Discussions

Ideas and architectural discussions are always welcome.

The MVP intentionally remains small so that the core concept can be validated before additional functionality is introduced.

Thank you for helping shape Episode.
