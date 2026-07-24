from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


@dataclass(frozen=True)
class CameraMedia:
    device_id: str
    stream_uri: str = ""
    snapshot_uri: str = ""
    username: str = ""
    password: str = ""
    profile_token: str = ""
    source: str = ""

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

    async def fetch_snapshot(self, device_id: str) -> tuple[bytes, str]:
        source = self.get(device_id)
        if not source or not source.snapshot_uri:
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
