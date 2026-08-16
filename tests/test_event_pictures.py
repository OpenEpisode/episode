from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

import httpx
import pytest

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Event
from episode.storage.repository import Repository


@pytest.mark.asyncio
async def test_streams_embedded_event_picture_and_supports_legacy_metadata(tmp_path):
    repository = Repository(
        EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "episode.db"))
    )
    await repository.initialize()
    await repository.upsert_area(Area(id="front-door", name="Front door"))
    await repository.upsert_device(
        Device(
            id="front-doorbell",
            name="Front doorbell",
            device_type="doorbell",
            area_id="front-door",
        )
    )
    picture = b"\xff\xd8embedded unlock picture\xff\xd9"
    header = b"vendor structure"
    payload = tmp_path / "unlock.bin"
    payload.write_bytes(header + picture)
    checksum = sha256(picture).hexdigest()
    observed_at = datetime(2026, 8, 14, 10, 30, tzinfo=timezone.utc)

    try:
        described = await repository.create_event(
            Event(
                device_id="front-doorbell",
                area_id="front-door",
                timestamp=observed_at,
                event_type="door_access",
                source="hikvision:sdk",
                raw_payload_path=str(payload),
                metadata={
                    "embedded_picture": {
                        "offset": len(header),
                        "byte_size": len(picture),
                        "mime_type": "image/jpeg",
                        "filename": "door-unlock.jpg",
                        "sha256": checksum,
                    }
                },
            )
        )
        legacy = await repository.create_event(
            Event(
                device_id="front-doorbell",
                area_id="front-door",
                timestamp=observed_at + timedelta(seconds=1),
                event_type="door_access",
                source="hikvision:sdk",
                raw_payload_path=str(payload),
                metadata={
                    "picture_transport": "binary",
                    "structure_size": len(header),
                    "picture_byte_size": len(picture),
                    "picture_sha256": checksum,
                },
            )
        )
        invalid = await repository.create_event(
            Event(
                device_id="front-doorbell",
                area_id="front-door",
                timestamp=observed_at + timedelta(seconds=2),
                event_type="door_access",
                source="hikvision:sdk",
                raw_payload_path=str(payload),
                metadata={
                    "embedded_picture": {
                        "offset": len(header),
                        "byte_size": len(picture) + 1,
                        "mime_type": "image/jpeg",
                    }
                },
            )
        )

        transport = httpx.ASGITransport(app=create_api(repository, str(tmp_path)))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/v1/events/{described.id}/picture")
            legacy_response = await client.get(f"/api/v1/events/{legacy.id}/picture")
            invalid_response = await client.get(f"/api/v1/events/{invalid.id}/picture")

        assert response.status_code == 200
        assert response.content == picture
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["content-length"] == str(len(picture))
        assert response.headers["content-disposition"] == 'inline; filename="door-unlock.jpg"'
        assert response.headers["etag"] == f'"{checksum}"'
        assert legacy_response.status_code == 200
        assert legacy_response.content == picture
        assert invalid_response.status_code == 404
    finally:
        await repository.close()
