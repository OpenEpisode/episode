FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/

RUN uv sync --locked --no-dev

FROM python:3.12-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r episode \
    && useradd -r -g episode -d /var/episode -s /bin/false episode

ARG EPISODE_VERSION=0.1.0-alpha.7
ARG EPISODE_REVISION=unknown

LABEL org.opencontainers.image.title="Episode" \
      org.opencontainers.image.description="Local-first, event-driven incident capture platform" \
      org.opencontainers.image.source="https://github.com/OpenEpisode/Episode" \
      org.opencontainers.image.url="https://github.com/OpenEpisode/Episode" \
      org.opencontainers.image.documentation="https://github.com/OpenEpisode/Episode#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${EPISODE_VERSION}" \
      org.opencontainers.image.revision="${EPISODE_REVISION}"

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY episode.example.json /app/config.json

ENV EPISODE_CONFIG=/app/config.json \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN mkdir -p /var/episode/data && chown -R episode:episode /var/episode /app

USER episode
WORKDIR /app

EXPOSE 8989 2121 30000-30009

ENTRYPOINT ["python", "-m", "episode"]
