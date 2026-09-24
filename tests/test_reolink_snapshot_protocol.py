"""Snapshot protocol tests for ``cmdId=109``: declared-size completion and the ack-only probe.

Both behaviours come from measured fixtures in ``tests/fixtures/reolink/``: every captured
firmware answers the snapshot request with ``<pictureSize>`` and then delivers *exactly* that
many bytes (asserted for the committed sets in ``tests/test_reolink_fixtures.py``). That makes the
declaration a sound completion rule, so a slow camera fails in its own ``snapshot_timeout`` instead
of the 10 s client default, and a camera that acknowledges is known to support snapshots without
paying for a picture we would throw away (gap G6).

The fake dispatcher replays frames with the body layout the cameras use —
``[encrypted extension][encrypted payload]`` — so these exercise the real decryption and parsing
paths with no socket and no camera.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from episode.plugins.reolink.client import (
    SNAPSHOT_CEILING_BYTES,
    BaichuanApiClient,
    ReolinkError,
    SnapshotAck,
    bc_encrypt,
    build_channel_extension_xml,
    parse_snapshot_ack,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "reolink"
SET_NAMES = sorted(
    path.name for path in FIXTURE_ROOT.iterdir() if (path / "fixtures.json").is_file()
)

ACK_XML = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n'
    '<Snap version="1.1">\n<channelId>0</channelId>\n'
    "<fileName>01_20260921183047605.jpg</fileName>\n<time>0</time>\n"
    "<pictureSize>{size}</pictureSize>\n</Snap>\n</body>\n"
)
BINARY_EXT = (
    b'<Extension version="1.1"><channelId>0</channelId><binaryData>1</binaryData></Extension>'
)


def _load(set_name: str, filename: str) -> Any:
    return json.loads((FIXTURE_ROOT / set_name / filename).read_text(encoding="utf-8"))


class Writer:
    """Minimal stand-in for the asyncio stream writer."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        pass


class FakeDispatcher:
    """Replay canned ``cmdId=109`` frames through the iterator the client consumes.

    Each frame is ``(response_code, payload_offset, body)``. The requested ``timeout`` is
    recorded so the plumbing can be asserted, and iteration ends after the last frame.
    """

    def __init__(self, frames: list[tuple[int, int, bytes]] | None = None) -> None:
        self.frames_in = frames or []
        self.timeouts: list[float] = []
        self.consumed = 0

    async def iter_matching(self, cmd_id, *, timeout, predicate=None, send=None):
        self.timeouts.append(timeout)
        if send is not None:
            await send()
        for code, offset, body in self.frames_in:
            self.consumed += 1
            yield cmd_id, code, offset, body


def make_client(dispatcher: FakeDispatcher, timeout: float = 10.0) -> BaichuanApiClient:
    """An authenticated-looking client whose socket is replaced by ``dispatcher``."""
    client = BaichuanApiClient("192.168.1.10", "admin", "pw", timeout=timeout)
    client._token = "tok"
    client._connected = True
    client._writer = Writer()
    client._dispatcher = dispatcher
    return client


def wire_frame(
    client: BaichuanApiClient, extension: bytes, payload: bytes
) -> tuple[int, int, bytes]:
    """Encode one response frame the way the camera lays it out: [ext][payload], BC-ciphered."""
    offset = client._host_channel_id
    enc_ext = bc_encrypt(extension, offset)
    enc_payload = bc_encrypt(payload, offset)
    return 200, len(enc_ext), enc_ext + enc_payload


def ack_frame(client: BaichuanApiClient, declared: int) -> tuple[int, int, bytes]:
    xml = ACK_XML.format(size=declared).encode("utf-8")
    return wire_frame(client, build_channel_extension_xml(0).encode("utf-8"), xml)


def binary_frames(client: BaichuanApiClient, chunks: list[bytes]) -> list[tuple[int, int, bytes]]:
    return [wire_frame(client, BINARY_EXT, chunk) for chunk in chunks]


def jpeg_like(total: int) -> bytes:
    """A blob that opens with SOI and contains no EOI anywhere.

    No filler byte is ``FF``, so an ``FF D9`` pair cannot occur by accident. The absence of the
    end-of-image marker is the point: completion *must* then come from the declaration, so a
    passing assertion proves the oracle — not a marker — ended the capture.
    """
    filler = bytes(bytearray(range(0x01, 0xFF))) * ((total // 254) + 2)
    return (b"\xff\xd8" + filler)[:total]


def split_blob(blob: bytes, sizes: list[int]) -> list[bytes]:
    """Cut ``blob`` into pieces of the given sizes (the measured chunk geometry)."""
    chunks: list[bytes] = []
    position = 0
    for size in sizes:
        chunks.append(blob[position : position + size])
        position += size
    return chunks


# ── the ack oracle ───────────────────────────────────────────────────


@pytest.mark.parametrize("set_name", SET_NAMES)
def test_parse_snapshot_ack_reads_the_measured_declaration(set_name: str) -> None:
    raw = (FIXTURE_ROOT / set_name / "snapshot_ack_109.xml").read_bytes()
    ack = parse_snapshot_ack(raw)
    assert ack is not None
    exchange = _load(set_name, "snapshot_exchange_109.json")
    assert ack.declared_bytes == exchange["declared_picture_size"]
    assert ack.declared_bytes > 0


def test_parse_snapshot_ack_ignores_binary_and_unusable_bodies() -> None:
    assert parse_snapshot_ack(b"\xff\xd8\xff\xe0\x00\x10JFIF") is None
    assert parse_snapshot_ack(b"") is None
    without_size = b"<body><Snap version='1.1'><channelId>0</channelId></Snap></body>"
    assert parse_snapshot_ack(without_size).declared_bytes is None
    negative = ACK_XML.format(size=-5).encode("utf-8")
    assert parse_snapshot_ack(negative).declared_bytes is None


@pytest.mark.parametrize("set_name", SET_NAMES)
def test_real_chunk_geometry_completes_on_the_declaration(set_name: str) -> None:
    """Measured chunk sizes + no EOI: only the declared size can end the capture."""
    exchange = _load(set_name, "snapshot_exchange_109.json")
    declared = exchange["declared_picture_size"]
    sizes = exchange["chunk_sizes"][: exchange["chunks"]]
    assert sum(sizes) == declared

    client = make_client(FakeDispatcher())
    frames = [ack_frame(client, declared)]
    frames.extend(binary_frames(client, split_blob(jpeg_like(declared), sizes)))

    dispatcher = FakeDispatcher(frames)
    client = make_client(dispatcher)
    captured = asyncio.run(client._get_snapshot_impl(channel=0))
    assert captured is not None
    assert captured.startswith(b"\xff\xd8")
    assert b"\xff\xd9" not in captured  # no EOI was ever sent
    assert len(captured) == declared


def test_truncated_capture_is_rejected_instead_of_padded() -> None:
    """Stopping short of the declaration is a failure, not a repaired file."""
    declared = 10_000
    client = make_client(FakeDispatcher())
    frames = [ack_frame(client, declared)]
    frames.extend(binary_frames(client, split_blob(jpeg_like(6_000), [1_000, 2_000, 3_000])))
    client = make_client(FakeDispatcher(frames))
    assert asyncio.run(client._get_snapshot_impl(channel=0)) is None


def test_undersized_picture_ending_in_eoi_is_kept() -> None:
    """No overshoot and no truncation: the EOI fallback still completes the capture."""
    declared = 5_000
    blob = jpeg_like(2_000) + b"\xff\xd9"
    client = make_client(FakeDispatcher())
    frames = [ack_frame(client, declared)]
    frames.extend(binary_frames(client, split_blob(blob, [1_000, 1_002])))
    client = make_client(FakeDispatcher(frames))
    captured = asyncio.run(client._get_snapshot_impl(channel=0))
    assert captured is not None
    assert captured.endswith(b"\xff\xd9")


def test_ceiling_stops_a_runaway_capture() -> None:
    """A declaration above the core's 25 MiB ceiling aborts rather than buffering."""
    declared = SNAPSHOT_CEILING_BYTES + 1
    client = make_client(FakeDispatcher())
    frames = [ack_frame(client, declared)]
    frames.extend(binary_frames(client, split_blob(jpeg_like(1_000_000), [500_000, 500_000])))
    dispatcher = FakeDispatcher(frames)
    client = make_client(dispatcher)
    assert asyncio.run(client._get_snapshot_impl(channel=0)) is None
    assert dispatcher.consumed == 1, "the ceiling must stop before the picture frames"


def test_capture_without_a_declaration_still_completes_on_eoi() -> None:
    """Firmware that omits ``pictureSize`` keeps the pre-existing behaviour."""
    blob = jpeg_like(2_000) + b"\xff\xd9"
    client = make_client(FakeDispatcher())
    frames = [
        wire_frame(client, build_channel_extension_xml(0).encode("utf-8"), b"<body><Snap/></body>")
    ]
    frames.extend(binary_frames(client, split_blob(blob, [1_000, 1_002])))
    client = make_client(FakeDispatcher(frames))
    captured = asyncio.run(client._get_snapshot_impl(channel=0))
    assert captured is not None
    assert captured.endswith(b"\xff\xd9")


# ── explicit timeout plumbing ────────────────────────────────────────


@pytest.mark.asyncio
async def test_split_eoi_is_covered_by_the_declaration() -> None:
    """``FF`` at the end of one chunk and ``D9`` at the start of the next is one marker.

    A per-chunk marker scan cannot see it, so this is the case where the declared size is
    not merely faster but strictly more correct.
    """
    declared = 4_000
    picture = jpeg_like(3_998) + b"\xff\xd9"
    assert len(picture) == declared
    chunks = split_blob(picture, [declared - 1, 1])
    assert chunks[0].endswith(b"\xff") and chunks[1] == b"\xd9"

    client = make_client(FakeDispatcher())
    frames = [ack_frame(client, declared)]
    frames.extend(binary_frames(client, chunks))
    dispatcher = FakeDispatcher(frames)
    client = make_client(dispatcher)
    captured = await client._get_snapshot_impl(channel=0)
    assert captured is not None
    assert captured == picture
    assert captured.endswith(b"\xff\xd9")


class StalledDispatcher(FakeDispatcher):
    """Yield the ack, then block until the caller's own deadline expires."""

    def __init__(self, frames: list[tuple[int, int, bytes]]) -> None:
        super().__init__(frames)

    async def iter_matching(self, cmd_id, *, timeout, predicate=None, send=None):
        self.timeouts.append(timeout)
        if send is not None:
            await send()
        for code, offset, body in self.frames_in:
            self.consumed += 1
            yield cmd_id, code, offset, body
        await asyncio.sleep(timeout)


@pytest.mark.asyncio
async def test_explicit_timeout_bounds_one_capture_attempt() -> None:
    """A stalled camera costs the caller its own timeout, not the 10 s client default."""
    client = make_client(FakeDispatcher(), timeout=10.0)
    frames = [ack_frame(client, 50_000)]
    dispatcher = StalledDispatcher(frames)
    client = make_client(dispatcher, timeout=10.0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    captured = await client._get_snapshot_impl(channel=0, timeout=0.2)
    elapsed = loop.time() - started

    assert captured is None
    assert elapsed < 1.5, f"the explicit timeout was ignored ({elapsed:.2f}s)"
    assert dispatcher.timeouts and dispatcher.timeouts[0] <= 0.25


def test_get_snapshot_impl_forwards_the_timeout_to_the_iterator() -> None:
    dispatcher = FakeDispatcher()
    client = make_client(dispatcher, timeout=10.0)
    asyncio.run(client._get_snapshot_impl(channel=0, timeout=2.5))
    assert dispatcher.timeouts[0] == pytest.approx(2.5, abs=0.05)

    default_dispatcher = FakeDispatcher()
    default_client = make_client(default_dispatcher, timeout=7.0)
    asyncio.run(default_client._get_snapshot_impl(channel=0))
    assert default_dispatcher.timeouts[0] == pytest.approx(7.0, abs=0.05)


# ── ack-only capability probe (G6) ───────────────────────────────────


async def _noop_reset() -> None:
    return None


def test_probe_returns_the_ack_without_reading_the_picture() -> None:
    """The probe stops at the acknowledgment; the picture frames are never consumed."""
    declared = 3791559
    client = make_client(FakeDispatcher())
    frames = [ack_frame(client, declared)]
    frames.extend(binary_frames(client, split_blob(jpeg_like(declared), [10_000] * 380)))

    dispatcher = FakeDispatcher(frames)
    client = make_client(dispatcher)
    resets: list[bool] = []

    async def _record_reset() -> None:
        resets.append(True)

    client._reset_connection = _record_reset
    ack = asyncio.run(client.snapshot_probe(channel=0, timeout=3.0))

    assert isinstance(ack, SnapshotAck)
    assert ack.declared_bytes == declared
    assert ack.response_code == 200
    assert dispatcher.consumed == 1, "a probe must not walk into the picture frames"
    # A probe that left the camera streaming would desynchronise the next real snapshot.
    assert resets == [True]


def test_probe_without_an_acknowledgement_reports_absence() -> None:
    client = make_client(FakeDispatcher())
    client._reset_connection = _noop_reset
    assert asyncio.run(client.snapshot_probe(channel=0, timeout=0.1)) is None


def test_probe_reports_a_rejection_as_a_response_code() -> None:
    client = make_client(FakeDispatcher([(404, 0, b"")]))
    client._reset_connection = _noop_reset
    ack = asyncio.run(client.snapshot_probe(channel=0, timeout=0.5))
    assert ack is not None
    assert ack.response_code == 404
    assert ack.declared_bytes is None


def test_probe_requires_authentication() -> None:
    client = BaichuanApiClient("192.168.1.10", "admin", "pw")
    with pytest.raises(ReolinkError):
        asyncio.run(client.snapshot_probe())
