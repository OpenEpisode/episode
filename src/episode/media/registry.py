from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

VIDEO_METADATA_KINDS = ("configured", "capabilities", "observed", "unknown")
MAX_VIDEO_SOURCES_PER_DEVICE = 64


@dataclass(frozen=True)
class VideoMode:
    """One advertised or configured encoding mode for a stream candidate."""

    width: int | None = None
    height: int | None = None
    frame_rates: tuple[int, ...] = ()
    codec: str = ""

    def __post_init__(self) -> None:
        for name, value in (("width", self.width), ("height", self.height)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16384
            ):
                raise ValueError(f"Video mode {name} must be between 1 and 16384")
        if not isinstance(self.frame_rates, (tuple, list)):
            raise ValueError("Video mode frame rates must be a tuple or list")
        frame_rates = tuple(self.frame_rates)
        if len(frame_rates) > 32 or any(
            not isinstance(rate, int) or isinstance(rate, bool) or not 1 <= rate <= 240
            for rate in frame_rates
        ):
            raise ValueError("Video mode frame rates must be between 1 and 240 fps")
        object.__setattr__(self, "frame_rates", frame_rates)
        if (
            not isinstance(self.codec, str)
            or len(self.codec) > 32
            or any(ord(character) < 32 for character in self.codec)
            or "://" in self.codec
        ):
            raise ValueError("Video mode codec is too long")


@dataclass(frozen=True)
class VideoSourceDescriptor:
    """Safe, vendor-neutral description of a selectable recording source."""

    id: str
    name: str
    provider: str
    protocol: str = "unknown"
    metadata_kind: str = "unknown"
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    codec: str = ""
    modes: tuple[VideoMode, ...] = ()
    default: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id or len(self.id) > 256:
            raise ValueError("Video source id must contain 1 to 256 characters")
        if any(ord(character) < 32 for character in self.id) or "://" in self.id:
            raise ValueError("Video source id cannot contain control characters or a URL")
        for name, value, limit in (
            ("name", self.name, 120),
            ("provider", self.provider, 128),
            ("protocol", self.protocol, 32),
            ("codec", self.codec, 32),
        ):
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"Video source {name} is too long")
            if any(ord(character) < 32 for character in value) or "://" in value:
                raise ValueError(f"Video source {name} cannot contain control characters or a URL")
        if not self.name or not self.provider:
            raise ValueError("Video source name and provider are required")
        if self.metadata_kind not in VIDEO_METADATA_KINDS:
            raise ValueError("Unsupported video source metadata kind")
        for name, value in (("width", self.width), ("height", self.height)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 16384
            ):
                raise ValueError(f"Video source {name} must be between 1 and 16384")
        if not isinstance(self.modes, (tuple, list)):
            raise ValueError("Video source modes must be a tuple or list")
        modes = tuple(self.modes)
        if len(modes) > 32 or any(not isinstance(mode, VideoMode) for mode in modes):
            raise ValueError("Video source has too many encoding modes")
        object.__setattr__(self, "modes", modes)
        if self.frame_rate is not None and (
            isinstance(self.frame_rate, bool)
            or not isinstance(self.frame_rate, (int, float))
            or not 0 < self.frame_rate <= 240
        ):
            raise ValueError("Video source frame rate must be between 0 and 240 fps")
        if not isinstance(self.default, bool):
            raise ValueError("Video source default must be a boolean")


# A plugin-native snapshot fetcher: returns (jpeg_bytes, content_type) or raises.
SnapshotFetcher = Callable[[], Awaitable[tuple[bytes, str]]]
# An event-bound snapshot fetcher. The token is opaque to the core and binds a
# pre-armed result to the delivery that requested it.
EventSnapshotFetcher = Callable[[str], Awaitable[tuple[bytes, str]]]

# A plugin-native video source: ``handler(push)`` streams Annex-B access units to the
# recorder until it returns, raises, or is cancelled. The recorder owns the encoder
# process and calls ``push``; bytes are never stored by the handler.
VideoStreamHandler = Callable[[Callable[[bytes], Awaitable[None]]], Awaitable[None]]

#: Elementary-stream demuxer names the recorder can pipe a handler's bytes into.
VIDEO_CODEC_HINTS = ("h264", "hevc")


@dataclass(frozen=True)
class CameraMedia:
    device_id: str
    stream_uri: str = ""
    snapshot_uri: str = ""
    username: str = ""
    password: str = ""
    profile_token: str = ""
    source: str = ""
    # Optional plugin-native fetcher used when snapshots are not available over
    # plain HTTP (e.g. Reolink binary protocol). Takes precedence over
    # snapshot_uri when set.
    snapshot_fetcher: SnapshotFetcher | None = field(default=None, compare=False)
    # Optional event-bound fetcher used only when an Event carries an opaque
    # snapshot token. Ordinary snapshots and current views never call this.
    event_snapshot_fetcher: EventSnapshotFetcher | None = field(default=None, compare=False)
    # Optional plugin-native video source: Annex-B access units pushed to the recorder,
    # which keeps writing the same HLS bundle it would from stream_uri. Takes precedence
    # over stream_uri when set. In-tree plugins only: not exposed on plugin_api.MediaSource.
    video_handler: VideoStreamHandler | None = field(default=None, compare=False)
    # Elementary-stream demuxer for the handler's bytes ("h264" | "hevc"); "" lets the
    # recorder's child guess. Set it from a real capability (e.g. cmdId=146), never a guess.
    codec_hint: str = ""
    # A source may be one of several choices a plugin registers for a Device. Its
    # descriptor contains no URI or credentials and is safe for the Device API.
    video_source: VideoSourceDescriptor | None = None

    def authenticated_stream_uri(self) -> str:
        if not self.stream_uri or not self.username:
            return self.stream_uri
        parsed = urlsplit(self.stream_uri)
        if parsed.username:
            return self.stream_uri
        credentials = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return urlunsplit(
            (parsed.scheme, f"{credentials}{parsed.netloc}", parsed.path, parsed.query, "")
        )


class MediaRegistry:
    """Runtime registry of media endpoints discovered by protocol adapters."""

    def __init__(self):
        self._sources: dict[str, dict[str, CameraMedia]] = {}
        self._defaults: dict[str, str] = {}

    @staticmethod
    def _descriptor(source: CameraMedia) -> VideoSourceDescriptor:
        if source.video_source is not None:
            return source.video_source
        provider = source.source or "media"
        protocol = urlsplit(source.stream_uri).scheme or "unknown"
        return VideoSourceDescriptor(
            id=f"{provider}:default",
            name=f"{provider} stream",
            provider=provider,
            protocol=protocol,
            default=True,
        )

    def register(self, source: CameraMedia) -> None:
        descriptor = self._descriptor(source)
        sources = self._sources.setdefault(source.device_id, {})
        if descriptor.id not in sources and len(sources) >= MAX_VIDEO_SOURCES_PER_DEVICE:
            raise ValueError("Device has reached the video source registration limit")
        sources[descriptor.id] = source
        current_default_id = self._defaults.get(source.device_id)
        current_default = sources.get(current_default_id)
        if descriptor.default and self._usable_for_recording(source):
            self._defaults[source.device_id] = descriptor.id
        elif current_default_id is None:
            self._defaults[source.device_id] = descriptor.id
        elif current_default is None or not self._usable_for_recording(current_default):
            fallback_id = self._automatic_source_id(sources)
            if fallback_id is not None:
                self._defaults[source.device_id] = fallback_id

    @staticmethod
    def _usable_for_recording(source: CameraMedia) -> bool:
        """Return whether a source can provide bytes to the recorder."""
        return bool(source.stream_uri or source.video_handler is not None)

    def _automatic_source_id(self, sources: dict[str, CameraMedia]) -> str | None:
        """Choose a stable automatic source when the stored default is stale.

        A plugin's explicit default is authoritative for Automatic selection. If
        several plugins remain after one unregisters, the first remaining
        usable registration is the deterministic fallback. Preserve insertion
        order here: it is also the documented tie-breaker for plugins that do
        not advertise a preferred source.
        """
        for source_id, source in sources.items():
            descriptor = self._descriptor(source)
            if descriptor.default and self._usable_for_recording(source):
                return source_id
        return next(
            (
                source_id
                for source_id, source in sources.items()
                if self._usable_for_recording(source)
            ),
            None,
        )

    def get(self, device_id: str, *, source_id: str | None = None) -> CameraMedia | None:
        sources = self._sources.get(device_id, {})
        if source_id is not None:
            return sources.get(source_id)
        selected_id = self._defaults.get(device_id)
        selected = sources.get(selected_id)
        if selected is not None and (
            self._usable_for_recording(selected)
            or not any(self._usable_for_recording(source) for source in sources.values())
        ):
            return selected
        fallback_id = self._automatic_source_id(sources)
        if fallback_id is not None:
            self._defaults[device_id] = fallback_id
            return sources[fallback_id]
        # Keep snapshot-only registrations usable when no recording source is
        # available. They cannot satisfy a recording request, but may still
        # provide a snapshot fetcher to the snapshot action.
        return next(iter(sources.values()), None) if sources else None

    def source_id(self, device_id: str, *, source_id: str | None = None) -> str | None:
        """Return the stable identity of the selected or automatic source."""
        source = self.get(device_id, source_id=source_id)
        return self._descriptor(source).id if source is not None else None

    def video_sources(self, device_id: str) -> tuple[VideoSourceDescriptor, ...]:
        """Return bounded, credential-free choices currently discovered for a Device."""
        sources = self._sources.get(device_id, {})
        default_id = self._defaults.get(device_id)
        descriptors = []
        for source in sources.values():
            if not source.stream_uri and source.video_handler is None:
                continue
            descriptor = self._descriptor(source)
            descriptors.append(replace(descriptor, default=descriptor.id == default_id))
        return tuple(
            sorted(
                descriptors,
                key=lambda descriptor: (
                    descriptor.id != default_id,
                    descriptor.name.casefold(),
                    descriptor.id,
                ),
            )
        )

    def unregister(self, device_id: str, *, source: str | None = None) -> None:
        sources = self._sources.get(device_id)
        if not sources:
            return
        removed = [
            source_id
            for source_id, current in sources.items()
            if source is None or current.source == source
        ]
        for source_id in removed:
            sources.pop(source_id, None)
        if not sources:
            self._sources.pop(device_id, None)
            self._defaults.pop(device_id, None)
        elif self._defaults.get(device_id) in removed:
            fallback_id = self._automatic_source_id(sources)
            if fallback_id is None:
                self._defaults.pop(device_id, None)
            else:
                self._defaults[device_id] = fallback_id

    @staticmethod
    def _has_snapshot_endpoint(source: CameraMedia) -> bool:
        return source.snapshot_fetcher is not None or bool(source.snapshot_uri)

    def _snapshot_source(self, device_id: str, snapshot_token: str | None) -> CameraMedia | None:
        """Select a source capable of serving the requested snapshot.

        Video-source selection and snapshot delivery are independent: a native
        video source may be the automatic recording source while another
        connector remains the only snapshot endpoint. Event-bound fetchers take
        precedence over ordinary snapshot endpoints for tokenized requests.
        """
        sources = self._sources.get(device_id, {})
        automatic = self.get(device_id)
        if snapshot_token is not None:
            if automatic is not None and automatic.event_snapshot_fetcher is not None:
                return automatic
            event_source = next(
                (
                    source
                    for source in sources.values()
                    if source.event_snapshot_fetcher is not None
                ),
                None,
            )
            if event_source is not None:
                return event_source
        if automatic is not None and self._has_snapshot_endpoint(automatic):
            return automatic
        return next(
            (source for source in sources.values() if self._has_snapshot_endpoint(source)),
            None,
        )

    async def fetch_snapshot(
        self, device_id: str, *, snapshot_token: str | None = None
    ) -> tuple[bytes, str]:
        data, content_type, _ = await self.fetch_snapshot_with_source(
            device_id, snapshot_token=snapshot_token
        )
        return data, content_type

    async def fetch_snapshot_with_source(
        self, device_id: str, *, snapshot_token: str | None = None
    ) -> tuple[bytes, str, str]:
        """Fetch a snapshot and return the registered source that supplied it."""
        source = self._snapshot_source(device_id, snapshot_token)
        if not source:
            raise LookupError(f"No snapshot endpoint for device {device_id}")
        if snapshot_token is not None and source.event_snapshot_fetcher is not None:
            data, content_type = await source.event_snapshot_fetcher(snapshot_token)
            if not content_type.startswith("image/"):
                raise ValueError(f"Snapshot fetcher returned {content_type}")
            if len(data) > 25 * 1024 * 1024:
                raise ValueError("Snapshot exceeds the 25 MiB safety limit")
            return data, content_type, source.source or "media"
        # Plugin-native fetcher (e.g. Reolink binary protocol) takes precedence.
        if source.snapshot_fetcher is not None:
            data, content_type = await source.snapshot_fetcher()
            if not content_type.startswith("image/"):
                raise ValueError(f"Snapshot fetcher returned {content_type}")
            if len(data) > 25 * 1024 * 1024:
                raise ValueError("Snapshot exceeds the 25 MiB safety limit")
            return data, content_type, source.source or "media"
        if not source.snapshot_uri:
            raise LookupError(f"No snapshot endpoint for device {device_id}")
        auth = httpx.DigestAuth(source.username, source.password) if source.username else None
        async with httpx.AsyncClient(auth=auth, timeout=15, follow_redirects=False) as client:
            response = await client.get(source.snapshot_uri)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise ValueError(f"Snapshot endpoint returned {content_type}")
        if len(response.content) > 25 * 1024 * 1024:
            raise ValueError("Snapshot exceeds the 25 MiB safety limit")
        return response.content, content_type, source.source or "media"
