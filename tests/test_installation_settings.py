from __future__ import annotations

import httpx
import pytest

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.installation import normalize_external_episode_url
from episode.storage.repository import Repository


def test_external_episode_url_is_validated_and_normalized():
    assert (
        normalize_external_episode_url(" https://episode.example/install/// ")
        == "https://episode.example/install"
    )
    assert normalize_external_episode_url("") == ""

    for invalid in (
        "http://episode.example/?tenant=one",
        "https://episode.example/#episode/foo",
        "https://user:password@episode.example",
        "https://episode.example/with space",
        "ftp://episode.example",
        "episode.example",
    ):
        with pytest.raises(ValueError, match="external Episode URL"):
            normalize_external_episode_url(invalid)


@pytest.mark.asyncio
async def test_installation_settings_api_persists_updates_and_allows_clear(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    app = create_api(repository, str(tmp_path))
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            initial = await client.get("/api/v1/settings/installation")
            updated = await client.put(
                "/api/v1/settings/installation",
                json={"external_url": "https://episode.example/install///"},
            )
            invalid = await client.put(
                "/api/v1/settings/installation",
                json={"external_url": "https://episode.example/?token=secret"},
            )
            cleared = await client.put(
                "/api/v1/settings/installation",
                json={"external_url": ""},
            )

        assert initial.json() == {"external_url": ""}
        assert updated.json() == {"external_url": "https://episode.example/install"}
        assert invalid.status_code == 422
        assert cleared.json() == {"external_url": ""}
        assert await repository.get_system_setting("external_episode_url") == ""
    finally:
        await repository.close()
