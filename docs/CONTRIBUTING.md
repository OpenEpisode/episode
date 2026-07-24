# Contributing

Thank you for your interest in Episode.

Episode is currently in active MVP development.

The primary goal at this stage is to validate the Episode model rather than expand the feature set.

## Guiding Principles

When contributing, please keep the following principles in mind:

* Events are immutable.
* Evidence is immutable.
* Episodes represent interpretation.
* Connectors translate protocols.
* The Core Domain remains vendor-independent.

Architectural consistency is preferred over rapid feature growth.

## Before Adding Features

Please ask:

> Does this help validate the Episode concept?

If not, it is probably better suited for a future release.

## Code Style

* Keep modules focused.
* Prefer composition over inheritance.
* Avoid vendor-specific logic outside Connectors.
* Preserve raw evidence whenever possible.
* Write code that is easy to understand and test.

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
