from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Episode, EpisodeState, Event
from episode.engine.bus import EventBus, Message
from episode.installation import set_external_episode_url
from episode.notifications import (
    EpisodeStartedWebhook,
    EpisodeStartedWebhookSettings,
    EpisodeStartedWebhookSettingsService,
    _discord_payload,
    _generic_payload,
    _test_payload,
)
from episode.storage.repository import Repository


async def _stored_episode(repo: Repository) -> tuple[Episode, Event]:
    observed_at = datetime(2026, 9, 9, 12, 30, tzinfo=timezone.utc)
    await repo.upsert_area(Area(id="entrance", name="Front Entrance"))
    await repo.upsert_device(
        Device(
            id="doorbell",
            name="Front Doorbell",
            device_type="doorbell",
            area_id="entrance",
        )
    )
    episode = Episode(
        id="20260909_123000_test0001",
        primary_area_id="entrance",
        start_time=observed_at,
        last_event_time=observed_at,
        state=EpisodeState.ACTIVE,
    )
    event = Event(
        id="event-1",
        device_id="doorbell",
        area_id="entrance",
        timestamp=observed_at,
        event_type="doorbell",
    )
    await repo.create_episode(episode)
    await repo.create_event(event)
    await repo.add_event_to_episode(event.id, episode.id)
    return episode, event


def _repo(tmp_path):
    return Repository(EpisodeConfig(data_dir=str(tmp_path)))


@pytest.mark.asyncio
async def test_webhook_settings_are_persisted_and_write_only(tmp_path):
    repo = _repo(tmp_path)
    await repo.initialize()
    service = EpisodeStartedWebhookSettingsService(repo, EventBus())
    await service.start()
    secret_url = "https://notifications.example/hooks/secret-token"
    try:
        assert (await service.get_settings()).public() == {
            "enabled": False,
            "payload_format": "generic",
            "timeout_seconds": 5.0,
            "url_configured": False,
        }
        updated = await service.update(
            enabled=True,
            url=secret_url,
        )
        assert updated.public() == {
            "enabled": True,
            "payload_format": "generic",
            "timeout_seconds": 5.0,
            "url_configured": True,
        }
        assert secret_url not in repr(updated)
        stored = await repo.get_system_setting("episode_started_webhook")
        assert stored is not None and secret_url in stored

        restarted = EpisodeStartedWebhookSettingsService(repo, EventBus())
        await restarted.start()
        try:
            assert (await restarted.get_settings()).public() == updated.public()
        finally:
            await restarted.stop()
    finally:
        await service.stop()
        await repo.close()


@pytest.mark.asyncio
async def test_api_preserves_url_on_blank_put_and_applies_enable_disable_immediately(tmp_path):
    repo = _repo(tmp_path)
    await repo.initialize()
    service = EpisodeStartedWebhookSettingsService(repo, EventBus())
    await service.start()
    secret_url = "https://notifications.example/hooks/secret-token"
    app = create_api(repo, str(tmp_path), episode_started_webhook=service)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            initial = await client.get("/api/v1/settings/notifications/episode-started")
            enabled = await client.put(
                "/api/v1/settings/notifications/episode-started",
                json={"enabled": True, "url": secret_url},
            )
            preserved = await client.put(
                "/api/v1/settings/notifications/episode-started",
                json={"enabled": True, "url": ""},
            )
            disabled = await client.put(
                "/api/v1/settings/notifications/episode-started",
                json={"enabled": False},
            )
            cleared = await client.put(
                "/api/v1/settings/notifications/episode-started",
                json={"enabled": False, "clear_url": True},
            )
            invalid = await client.put(
                "/api/v1/settings/notifications/episode-started",
                json={"enabled": True},
            )

        assert initial.status_code == 200
        assert set(initial.json()) == {
            "enabled",
            "payload_format",
            "timeout_seconds",
            "url_configured",
        }
        assert enabled.status_code == 200
        assert secret_url not in enabled.text
        assert preserved.status_code == 200
        assert preserved.json()["url_configured"] is True
        assert disabled.status_code == 200
        assert service.notifier._worker is None
        assert cleared.status_code == 200
        assert cleared.json()["url_configured"] is False
        assert invalid.status_code == 422
        assert "url" in invalid.text.lower()
    finally:
        await service.stop()
        await repo.close()


def test_generic_payload_adds_absolute_url_without_changing_relative_path():
    # The fixture is intentionally synchronous here; payload construction is a
    # pure projection and does not need a repository or network operation.
    observed_at = datetime(2026, 9, 9, 12, 30, tzinfo=timezone.utc)
    episode = Episode(
        id="episode-1",
        primary_area_id="entrance",
        start_time=observed_at,
        last_event_time=observed_at,
        state=EpisodeState.ACTIVE,
    )
    event = Event(
        id="event-1",
        device_id="doorbell",
        area_id="entrance",
        timestamp=observed_at,
        event_type="human_detected",
    )
    area = Area(id="entrance", name="Front Entrance")
    device = Device(id="doorbell", name="Front Doorbell", device_type="doorbell")

    payload = _generic_payload(
        episode,
        event,
        area,
        device,
        external_url="https://episode.example/install/",
    )
    assert payload["episode"]["path"] == "/#episode/episode-1"
    assert payload["episode"]["url"] == "https://episode.example/install/#episode/episode-1"

    without_address = _generic_payload(episode, event, area, device)
    assert "url" not in without_address["episode"]


def test_discord_payload_uses_embed_and_link_only_when_configured():
    payload = {
        "episode": {
            "id": "episode-1",
            "area_id": "entrance",
            "area_name": "Front Entrance",
            "start_time": "2026-09-09T12:30:00+00:00",
            "state": "active",
            "path": "/#episode/episode-1",
            "url": "https://episode.example/#episode/episode-1",
        },
        "trigger": {
            "event_id": "event-1",
            "event_type": "human_detected",
            "device_id": "doorbell",
            "device_name": "Front Doorbell",
            "timestamp": "2026-09-09T12:30:00+00:00",
        },
    }
    discord = _discord_payload(payload)
    assert discord["allowed_mentions"] == {"parse": []}
    assert "content" not in discord
    embed = discord["embeds"][0]
    assert embed["title"] == "Episode started"
    assert embed["url"] == payload["episode"]["url"]
    assert embed["color"] == 0x00C2C7
    assert embed["timestamp"] == payload["trigger"]["timestamp"]
    assert {field["name"] for field in embed["fields"]} == {
        "Area",
        "Trigger",
        "Device",
    }
    assert "[Open ongoing Episode →]" in embed["description"]
    assert embed["footer"]["text"] == "Episode · episode-1"

    payload["episode"].pop("url")
    without_link = _discord_payload(payload)
    assert "url" not in without_link["embeds"][0]
    assert "Open ongoing Episode" not in without_link["embeds"][0]["description"]


def test_discord_test_payload_previews_style_without_episode_or_link():
    payload = _test_payload("discord")
    assert payload["allowed_mentions"] == {"parse": []}
    assert "content" not in payload
    assert payload["embeds"][0]["color"] == 0x00C2C7
    assert "url" not in payload["embeds"][0]
    assert "episode-" not in payload["embeds"][0]["footer"]["text"]


@pytest.mark.asyncio
async def test_test_endpoint_sends_labeled_payload_without_episode(tmp_path):
    repo = _repo(tmp_path)
    await repo.initialize()
    delivered: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        delivered.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = EpisodeStartedWebhookSettingsService(repo, EventBus(), client=client)
    await service.start()
    secret_url = "https://notifications.example/hooks/secret-token"
    app = create_api(repo, str(tmp_path), episode_started_webhook=service)
    try:
        await service.update(enabled=False, url=secret_url)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api_client:
            response = await api_client.post("/api/v1/settings/notifications/episode-started/test")

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "message": "Test notification delivered",
            "status_code": 204,
        }
        assert len(delivered) == 1
        assert delivered[0].headers["X-Episode-Event"] == "episode.started.test"
        payload = json.loads(delivered[0].content)
        assert payload["type"] == "episode.started.test"
        assert payload["test"] is True
        assert "Episode webhook test notification" in payload["message"]
        assert await repo.list_episodes() == []
    finally:
        await service.stop()
        await client.aclose()
        await repo.close()


@pytest.mark.asyncio
async def test_test_failure_is_bounded_and_redacts_secret(tmp_path):
    repo = _repo(tmp_path)
    await repo.initialize()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = EpisodeStartedWebhookSettingsService(repo, EventBus(), client=client)
    await service.start()
    secret_url = "https://notifications.example/hooks/secret-token"
    app = create_api(repo, str(tmp_path), episode_started_webhook=service)
    try:
        await service.update(url=secret_url)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as api_client:
            response = await api_client.post("/api/v1/settings/notifications/episode-started/test")

        assert response.status_code == 200
        assert response.json() == {
            "success": False,
            "message": "Webhook returned HTTP 500",
            "status_code": 500,
        }
        assert secret_url not in response.text
        assert "secret-token" not in response.text
    finally:
        await service.stop()
        await client.aclose()
        await repo.close()


@pytest.mark.asyncio
async def test_episode_started_webhook_delivers_projection_and_isolated_failure(tmp_path, caplog):
    repo = _repo(tmp_path)
    await repo.initialize()
    episode, event = await _stored_episode(repo)
    await set_external_episode_url(repo, "https://episode.example/install/")
    delivered: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        delivered.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bus = EventBus()
    notifier = EpisodeStartedWebhook(repo, bus, client=client)
    await notifier.start(
        EpisodeStartedWebhookSettings(
            enabled=True,
            url="https://notifications.example/hooks/secret-token",
        )
    )
    try:
        await bus.publish(
            Message(
                type="episode.created",
                data={"episode_id": episode.id, "event_id": event.id},
            )
        )
        await notifier._queue.join()
        payload = json.loads(delivered[0].content)
        assert payload["type"] == "episode.started"
        assert payload["episode"]["id"] == episode.id
        assert payload["episode"]["url"] == (
            f"https://episode.example/install/#episode/{episode.id}"
        )

        async def failure_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, request=request)

        await notifier.reconfigure(
            EpisodeStartedWebhookSettings(
                enabled=True,
                url="https://notifications.example/hooks/secret-token",
            )
        )
        await client.aclose()
        failing_client = httpx.AsyncClient(transport=httpx.MockTransport(failure_handler))
        notifier._client = failing_client
        with caplog.at_level(logging.WARNING):
            await bus.publish(
                Message(
                    type="episode.created",
                    data={"episode_id": episode.id, "event_id": event.id},
                )
            )
            await notifier._queue.join()
        assert "HTTP 500" in caplog.text
        assert "secret-token" not in caplog.text
    finally:
        await notifier.stop()
        if notifier._client is not None:
            await notifier._client.aclose()
        await repo.close()


@pytest.mark.asyncio
async def test_webhook_queue_is_bounded_and_cleanup_is_idempotent():
    notifier = EpisodeStartedWebhook(object(), EventBus())
    await notifier.start(
        EpisodeStartedWebhookSettings(
            enabled=True,
            url="https://notifications.example/hook",
        )
    )
    try:
        for sequence in range(notifier._QUEUE_SIZE + 1):
            await notifier._on_episode_created(
                Message(
                    type="episode.created",
                    data={"episode_id": f"episode-{sequence}", "event_id": "event-1"},
                )
            )
        assert notifier._queue.qsize() == notifier._QUEUE_SIZE
    finally:
        await notifier.stop()
        await notifier.stop()
        assert notifier._worker is None


@pytest.mark.asyncio
async def test_test_delivery_is_bounded_while_another_delivery_owns_the_client():
    notifier = EpisodeStartedWebhook(object(), EventBus())
    settings = EpisodeStartedWebhookSettings(
        url="https://notifications.example/hook",
        timeout_seconds=0.5,
    )

    async with notifier._delivery_lock:
        result = await asyncio.wait_for(notifier.test_delivery(settings), timeout=1)

    assert result.success is False
    assert result.message == "Webhook request timed out"
    assert result.status_code is None


@pytest.mark.asyncio
async def test_concurrent_updates_leave_valid_persisted_settings(tmp_path):
    repo = _repo(tmp_path)
    await repo.initialize()
    service = EpisodeStartedWebhookSettingsService(repo, EventBus())
    await service.start()
    try:
        await asyncio.gather(
            service.update(enabled=False, payload_format="generic", timeout_seconds=1),
            service.update(enabled=False, payload_format="discord", timeout_seconds=2),
            service.update(enabled=False, payload_format="generic", timeout_seconds=3),
        )
        settings = await service.get_settings()
        assert settings.payload_format in {"generic", "discord"}
        assert 0.5 <= settings.timeout_seconds <= 30
        assert (
            json.loads(await repo.get_system_setting("episode_started_webhook"))["enabled"] is False
        )
    finally:
        await service.stop()
        await repo.close()
