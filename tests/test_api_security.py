from __future__ import annotations

import tomllib
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from episode import __version__
from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Event, Evidence
from episode.storage.repository import Repository


def test_package_and_application_versions_match():
    root = Path(__file__).parents[1]
    with (root / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert project["project"]["version"] == __version__
    assert f"ARG EPISODE_VERSION={__version__}" in (root / "Dockerfile").read_text()
    assert (
        f"EPISODE_IMAGE=ghcr.io/openepisode/episode:{__version__}"
        in (root / ".env.example").read_text()
    )


@pytest.mark.asyncio
async def test_health_reports_release_version():
    transport = httpx.ASGITransport(app=create_api(object()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        schema = await client.get("/openapi.json")

    assert health.json() == {"status": "ok", "version": __version__}
    assert schema.json()["info"]["version"] == __version__


@pytest.mark.asyncio
async def test_public_api_does_not_expose_secrets_or_paths(tmp_path):
    config = EpisodeConfig(
        data_dir=str(tmp_path),
        db_path=str(tmp_path / "episode.db"),
    )
    repo = Repository(config)
    await repo.initialize()
    try:
        await repo.upsert_area(Area(id="front", name="Front"))
        await repo.upsert_device(
            Device(
                id="camera-front",
                name="Front camera",
                device_type="hikvision",
                area_id="front",
                capabilities=["video"],
                ip_address="192.0.2.10",
                username="admin",
                password="top-secret",
            )
        )
        event = await repo.create_event(
            Event(
                device_id="camera-front",
                area_id="front",
                timestamp=datetime.now(tz=timezone.utc),
                event_type="motion_detection",
                source="hikvision:isapi",
                raw_payload_path="/private/events/source.xml",
            )
        )
        evidence = await repo.create_evidence(
            Evidence(
                device_id="camera-front",
                area_id="front",
                timestamp=datetime.now(tz=timezone.utc),
                evidence_type="snapshot",
                file_path="/private/evidence/source.jpg",
                mime_type="image/jpeg",
            )
        )

        transport = httpx.ASGITransport(app=create_api(repo, str(tmp_path)))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            device_body = (await client.get("/api/v1/devices/camera-front")).json()
            event_body = (await client.get(f"/api/v1/events/{event.id}")).json()
            evidence_body = (await client.get(f"/api/v1/evidence/{evidence.id}")).json()

        assert "username" not in device_body
        assert "password" not in device_body
        assert "configs" not in device_body
        assert "raw_payload_path" not in event_body
        assert event_body["has_raw_payload"] is True
        assert "file_path" not in evidence_body
    finally:
        await repo.close()
