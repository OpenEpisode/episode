from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from episode.domain.models import Area, Device, Episode, Event
from episode.engine.bus import EventBus, Message
from episode.installation import get_external_episode_url, normalize_external_episode_url

logger = logging.getLogger(__name__)

EPISODE_STARTED_WEBHOOK_SETTING = "episode_started_webhook"
MIN_WEBHOOK_TIMEOUT_SECONDS = 0.5
MAX_WEBHOOK_TIMEOUT_SECONDS = 30.0
MAX_WEBHOOK_URL_LENGTH = 2048
DISCORD_ACCENT_COLOR = 0x00C2C7


@dataclass(frozen=True, slots=True)
class EpisodeStartedWebhookSettings:
    """Validated, private settings for the Episode-started webhook.

    ``url`` deliberately has a non-default representation so an accidental
    dataclass repr/log statement cannot disclose the bearer-like webhook URL.
    The URL is only used inside this module when making a request.
    """

    enabled: bool = False
    payload_format: str = "generic"
    timeout_seconds: float = 5.0
    url: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("episode-started webhook enabled must be a boolean")
        if self.payload_format not in {"generic", "discord"}:
            raise ValueError("episode-started webhook payload_format must be generic or discord")
        if isinstance(self.timeout_seconds, bool):
            raise ValueError("episode-started webhook timeout_seconds must be between 0.5 and 30")
        try:
            timeout = float(self.timeout_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "episode-started webhook timeout_seconds must be between 0.5 and 30"
            ) from error
        if not MIN_WEBHOOK_TIMEOUT_SECONDS <= timeout <= MAX_WEBHOOK_TIMEOUT_SECONDS:
            raise ValueError("episode-started webhook timeout_seconds must be between 0.5 and 30")
        object.__setattr__(self, "timeout_seconds", timeout)

        if not isinstance(self.url, str):
            raise ValueError("episode-started webhook url must be a string")
        url = self.url.strip()
        object.__setattr__(self, "url", url)
        _validate_webhook_url(url)
        if self.enabled and not url:
            raise ValueError("episode-started webhook url is required when enabled")

    @property
    def url_configured(self) -> bool:
        return bool(self.url)

    def public(self) -> dict[str, bool | float | str]:
        """Return the complete credential-free API projection."""

        return {
            "enabled": self.enabled,
            "payload_format": self.payload_format,
            "timeout_seconds": self.timeout_seconds,
            "url_configured": self.url_configured,
        }

    def storage_value(self) -> str:
        return json.dumps(
            {
                "enabled": self.enabled,
                "payload_format": self.payload_format,
                "timeout_seconds": self.timeout_seconds,
                "url": self.url,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_storage(cls, value: str | None) -> EpisodeStartedWebhookSettings:
        if value is None:
            return cls()
        try:
            stored = json.loads(value)
            if not isinstance(stored, dict):
                raise ValueError("stored settings are not an object")
            return cls(
                enabled=stored.get("enabled", False),
                payload_format=stored.get("payload_format", "generic"),
                timeout_seconds=stored.get("timeout_seconds", 5.0),
                url=stored.get("url", ""),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            # Never include the stored value in this message: it may contain a
            # secret URL. The safe disabled default keeps startup isolated from
            # corrupt operator-managed state.
            logger.error("Invalid stored Episode-started webhook settings; using default")
            return cls()


def _validate_webhook_url(url: str) -> None:
    if not url:
        return
    if len(url) > MAX_WEBHOOK_URL_LENGTH:
        raise ValueError("episode-started webhook url must not exceed 2048 characters")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("episode-started webhook url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("episode-started webhook url must not contain user information")
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("episode-started webhook url has an invalid port") from error
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise ValueError("episode-started webhook url must not contain control characters")


def _episode_url(base_url: str, episode_id: str) -> str | None:
    """Build an absolute hash URL without allowing an opaque ID to escape it."""

    base = normalize_external_episode_url(base_url)
    if not base:
        return None
    url = f"{base}/#episode/{quote(str(episode_id), safe='')}"
    # Keep the Discord embed within its documented URL limit.  A normal
    # Episode identifier is far below this bound; an overlong custom ID simply
    # omits the link rather than producing a malformed payload.
    return url if len(url) <= 2048 else None


@dataclass(frozen=True, slots=True)
class WebhookDeliveryResult:
    success: bool
    message: str
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class _EpisodeStarted:
    episode_id: str
    event_id: str


class EpisodeStartedWebhook:
    """Deliver best-effort notifications without blocking Episode capture."""

    _QUEUE_SIZE = 64
    _SHUTDOWN_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        repo,
        bus: EventBus,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._repo = repo
        self._bus = bus
        self._client = client
        self._owns_client = client is None
        self._queue: asyncio.Queue[_EpisodeStarted] = asyncio.Queue(self._QUEUE_SIZE)
        self._worker: asyncio.Task | None = None
        self._subscribed = False
        self._started = False
        self._settings = EpisodeStartedWebhookSettings()
        self._state_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()

    async def start(self, settings: EpisodeStartedWebhookSettings | None = None) -> None:
        async with self._state_lock:
            if self._started:
                if settings is not None:
                    self._settings = settings
                    await self._apply_enabled_state_locked()
                return
            self._started = True
            self._settings = settings or EpisodeStartedWebhookSettings()
            await self._apply_enabled_state_locked()

    async def stop(self) -> None:
        async with self._state_lock:
            self._started = False
            await self._disable_locked()
            # Do not close an injected client owned by a test or caller. Wait
            # for an in-flight explicit test delivery before closing our own.
            if self._owns_client and self._client is not None:
                client = self._client
                try:
                    await asyncio.wait_for(
                        self._close_client(client), timeout=self._SHUTDOWN_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # A test request is independently bounded. Do not make
                    # application shutdown wait for an uncooperative peer.
                    logger.warning("Episode-started webhook client did not close promptly")
                else:
                    self._client = None

    async def _close_client(self, client: httpx.AsyncClient) -> None:
        async with self._delivery_lock:
            await client.aclose()

    async def reconfigure(self, settings: EpisodeStartedWebhookSettings) -> None:
        """Apply settings immediately while preserving the bounded worker."""

        async with self._state_lock:
            self._settings = settings
            if not self._started:
                return
            await self._apply_enabled_state_locked()

    async def test_delivery(self, settings: EpisodeStartedWebhookSettings) -> WebhookDeliveryResult:
        """Send a labeled test notification using a settings snapshot.

        This path never publishes an EventBus message and therefore cannot
        create an Episode. It intentionally works while the setting is
        disabled when a URL has been saved, which lets an operator verify a
        destination before enabling notifications.
        """

        if not settings.url:
            return WebhookDeliveryResult(False, "No webhook URL is configured")
        payload = _test_payload(settings.payload_format)
        return await self._post(settings, payload, event_header="episode.started.test")

    async def _apply_enabled_state_locked(self) -> None:
        if self._settings.enabled:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=MAX_WEBHOOK_TIMEOUT_SECONDS)
            if not self._subscribed:
                self._bus.subscribe("episode.created", self._on_episode_created)
                self._subscribed = True
            if self._worker is None:
                self._worker = asyncio.create_task(
                    self._run(),
                    name="episode-started-webhook",
                )
            if self._settings.url.startswith("http://"):
                logger.warning(
                    "Episode-started webhook uses unencrypted HTTP; use it only on a "
                    "trusted network"
                )
            return
        await self._disable_locked()

    async def _disable_locked(self) -> None:
        if self._subscribed:
            self._bus.unsubscribe("episode.created", self._on_episode_created)
            self._subscribed = False
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await asyncio.wait_for(worker, timeout=self._SHUTDOWN_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning("Episode-started webhook worker did not stop promptly")
        self._clear_queue()

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self._queue.task_done()

    async def _on_episode_created(self, message: Message) -> None:
        episode_id = message.data.get("episode_id")
        event_id = message.data.get("event_id")
        if not isinstance(episode_id, str) or not isinstance(event_id, str):
            logger.warning("Ignored incomplete episode.created notification")
            return
        try:
            self._queue.put_nowait(_EpisodeStarted(episode_id, event_id))
        except asyncio.QueueFull:
            logger.warning(
                "Dropped Episode-started webhook for %s because the notification queue is full",
                episode_id,
            )

    async def _run(self) -> None:
        while True:
            notification = await self._queue.get()
            try:
                await self._deliver(notification)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Episode-started webhook failed for %s: %s",
                    notification.episode_id,
                    _safe_error(error),
                )
            finally:
                self._queue.task_done()

    async def _deliver(self, notification: _EpisodeStarted) -> None:
        settings = self._settings
        if not settings.enabled or not settings.url:
            return
        episode = await self._repo.get_episode(notification.episode_id)
        event = await self._repo.get_event(notification.event_id)
        if episode is None or event is None:
            logger.warning(
                "Skipped Episode-started webhook for %s because its projection is unavailable",
                notification.episode_id,
            )
            return

        area = await self._repo.get_area(episode.primary_area_id)
        device = await self._repo.get_device(event.device_id)
        external_url = await get_external_episode_url(self._repo)
        payload = _generic_payload(
            episode,
            event,
            area,
            device,
            external_url=external_url,
        )
        if settings.payload_format == "discord":
            payload = _discord_payload(payload)
        result = await self._post(settings, payload, event_header="episode.started")
        if not result.success:
            logger.warning(
                "Episode-started webhook failed for %s: %s",
                notification.episode_id,
                result.message,
            )
            return
        logger.info("Delivered Episode-started webhook for %s", notification.episode_id)

    async def _post(
        self,
        settings: EpisodeStartedWebhookSettings,
        payload: dict,
        *,
        event_header: str,
    ) -> WebhookDeliveryResult:
        if not settings.url:
            return WebhookDeliveryResult(False, "No webhook URL is configured")
        try:
            # Bound both time waiting behind another test/delivery and the
            # network request itself. Concurrent API calls cannot build an
            # unbounded backlog outside the Episode notification queue.
            async with asyncio.timeout(settings.timeout_seconds):
                async with self._delivery_lock:
                    temporary_client = False
                    client = self._client
                    if client is None:
                        client = httpx.AsyncClient(timeout=MAX_WEBHOOK_TIMEOUT_SECONDS)
                        if self._owns_client:
                            self._client = client
                        else:
                            temporary_client = True
                    try:
                        async with client.stream(
                            "POST",
                            settings.url,
                            json=payload,
                            headers={"X-Episode-Event": event_header},
                        ) as response:
                            status_code = response.status_code
                            if not 200 <= status_code < 300:
                                return WebhookDeliveryResult(
                                    False,
                                    f"Webhook returned HTTP {status_code}",
                                    status_code,
                                )
                            return WebhookDeliveryResult(
                                True,
                                "Test notification delivered"
                                if event_header == "episode.started.test"
                                else "Notification delivered",
                                status_code,
                            )
                    finally:
                        if temporary_client:
                            await client.aclose()
        except TimeoutError:
            return WebhookDeliveryResult(False, "Webhook request timed out")
        except httpx.TimeoutException:
            return WebhookDeliveryResult(False, "Webhook request timed out")
        except httpx.RequestError:
            return WebhookDeliveryResult(False, "Webhook request failed")
        except Exception:
            # Do not expose exception text: httpx exceptions can carry the
            # complete credential-bearing request URL.
            return WebhookDeliveryResult(False, "Webhook request failed")


class EpisodeStartedWebhookSettingsService:
    """Persist and apply UI-managed webhook settings.

    The SQLite ``system_settings`` row is authoritative. There is no config
    file fallback: an absent row is the disabled, URL-less default.
    """

    def __init__(
        self,
        repository,
        bus: EventBus,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._repository = repository
        self._notifier = EpisodeStartedWebhook(repository, bus, client=client)
        self._settings = EpisodeStartedWebhookSettings()
        self._lock = asyncio.Lock()
        self._started = False

    @property
    def notifier(self) -> EpisodeStartedWebhook:
        return self._notifier

    async def start(self) -> None:
        async with self._lock:
            self._settings = await self._load()
            await self._notifier.start(self._settings)
            self._started = True

    async def stop(self) -> None:
        async with self._lock:
            self._started = False
            await self._notifier.stop()

    async def get_settings(self) -> EpisodeStartedWebhookSettings:
        async with self._lock:
            return self._settings

    async def update(
        self,
        *,
        enabled: bool | None = None,
        payload_format: str | None = None,
        timeout_seconds: float | None = None,
        url: str | None = None,
        clear_url: bool = False,
    ) -> EpisodeStartedWebhookSettings:
        async with self._lock:
            current = self._settings
            if clear_url and url and url.strip():
                raise ValueError("clear_url cannot be combined with a new URL")
            saved_url = "" if clear_url else current.url
            if url and url.strip():
                saved_url = url
            updated = EpisodeStartedWebhookSettings(
                enabled=current.enabled if enabled is None else enabled,
                payload_format=current.payload_format if payload_format is None else payload_format,
                timeout_seconds=current.timeout_seconds
                if timeout_seconds is None
                else timeout_seconds,
                url=saved_url,
            )
            await self._repository.set_system_setting(
                EPISODE_STARTED_WEBHOOK_SETTING,
                updated.storage_value(),
            )
            self._settings = updated
            if self._started:
                await self._notifier.reconfigure(updated)
            return updated

    async def test(self) -> WebhookDeliveryResult:
        # Snapshot under the lock, but do not hold the settings lock during a
        # bounded network operation: PUT must remain responsive and can apply
        # a new setting immediately while a test is in flight.
        async with self._lock:
            settings = self._settings
        return await self._notifier.test_delivery(settings)

    async def _load(self) -> EpisodeStartedWebhookSettings:
        value = await self._repository.get_system_setting(EPISODE_STARTED_WEBHOOK_SETTING)
        return EpisodeStartedWebhookSettings.from_storage(value)


def _generic_payload(
    episode: Episode,
    event: Event,
    area: Area | None,
    device: Device | None,
    *,
    external_url: str = "",
) -> dict:
    episode_url = _episode_url(external_url, episode.id)
    episode_projection = {
        "id": episode.id,
        "area_id": episode.primary_area_id,
        "area_name": area.name if area else None,
        "start_time": episode.start_time.isoformat(),
        "state": episode.state.value,
        # Keep the original relative path stable for existing generic JSON
        # consumers.  The absolute URL is additive and only present when the
        # operator has explicitly configured an external installation URL.
        "path": f"/#episode/{episode.id}",
    }
    if episode_url:
        episode_projection["url"] = episode_url
    return {
        "schema_version": 1,
        "type": "episode.started",
        "episode": episode_projection,
        "trigger": {
            "event_id": event.id,
            "event_type": event.event_type,
            "device_id": event.device_id,
            "device_name": device.name if device else None,
            "timestamp": event.timestamp.isoformat(),
        },
    }


def _test_payload(payload_format: str) -> dict:
    if payload_format == "discord":
        return {
            "embeds": [
                {
                    "title": "Episode webhook test",
                    "description": "Preview only — no Episode was created.",
                    "color": DISCORD_ACCENT_COLOR,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "footer": {"text": "Test notification · preview only"},
                }
            ],
            "allowed_mentions": {"parse": []},
        }
    return {
        "schema_version": 1,
        "type": "episode.started.test",
        "test": True,
        "message": "Episode webhook test notification",
        "sent_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _discord_payload(payload: dict) -> dict:
    episode = payload["episode"]
    trigger = payload["trigger"]
    area = _discord_text(episode.get("area_name") or episode.get("area_id"), "Unknown area")
    device = _discord_text(
        trigger.get("device_name") or trigger.get("device_id"),
        "Unknown device",
    )
    event_type = _discord_text(trigger.get("event_type"), "Unknown event")
    event_type = event_type.replace("_", " ").title()[:256]
    episode_url = episode.get("url")
    has_episode_url = _is_absolute_http_url(episode_url)
    description = "A new Episode is now ongoing."
    if has_episode_url:
        description += f"\n\n[Open ongoing Episode →]({episode_url})"
    embed = {
        "title": "Episode started",
        "description": description,
        "color": DISCORD_ACCENT_COLOR,
        "timestamp": str(trigger.get("timestamp") or episode.get("start_time") or "")[:64],
        "fields": [
            {"name": "Area", "value": area, "inline": True},
            {"name": "Trigger", "value": event_type, "inline": True},
            {"name": "Device", "value": device, "inline": True},
        ],
        "footer": {"text": f"Episode · {_discord_text(episode.get('id'), 'Unknown')[:2000]}"},
    }
    if has_episode_url:
        embed["url"] = episode_url
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def _discord_text(value: object, fallback: str) -> str:
    text = str(value or fallback).strip()
    return (text or fallback)[:256]


def _is_absolute_http_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def _safe_error(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.RequestError):
        return type(error).__name__
    return type(error).__name__
