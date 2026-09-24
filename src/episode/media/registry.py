from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

# A plugin-native snapshot fetcher: returns (jpeg_bytes, content_type) or raises.
SnapshotFetcher = Callable[[], Awaitable[tuple[bytes, str]]]

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
    # Optional plugin-native video source: Annex-B access units pushed to the recorder,
    # which keeps writing the same HLS bundle it would from stream_uri. Takes precedence
    # over stream_uri when set. In-tree plugins only: not exposed on plugin_api.MediaSource.
    video_handler: VideoStreamHandler | None = field(default=None, compare=False)
    # Elementary-stream demuxer for the handler's bytes ("h264" | "hevc"); "" lets the
    # recorder's child guess. Set it from a real capability (e.g. cmdId=146), never a guess.
    codec_hint: str = ""

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
        self._sources: dict[str, CameraMedia] = {}

    def register(self, source: CameraMedia) -> None:
        self._sources[source.device_id] = source

    def get(self, device_id: str) -> CameraMedia | None:
        return self._sources.get(device_id)

    def unregister(self, device_id: str, *, source: str | None = None) -> None:
        current = self._sources.get(device_id)
        if current is not None and (source is None or current.source == source):
            self._sources.pop(device_id, None)

    async def fetch_snapshot(self, device_id: str) -> tuple[bytes, str]:
        source = self.get(device_id)
        if not source:
            raise LookupError(f"No snapshot endpoint for device {device_id}")
        # Plugin-native fetcher (e.g. Reolink binary protocol) takes precedence.
        if source.snapshot_fetcher is not None:
            data, content_type = await source.snapshot_fetcher()
            if not content_type.startswith("image/"):
                raise ValueError(f"Snapshot fetcher returned {content_type}")
            if len(data) > 25 * 1024 * 1024:
                raise ValueError("Snapshot exceeds the 25 MiB safety limit")
            return data, content_type
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
        return response.content, content_type
