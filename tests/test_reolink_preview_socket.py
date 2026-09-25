"""Phase 4's socket half: ``cmdId=3`` observed over a real connection.

Nothing here mocks the dispatcher. A scripted camera listens on a real TCP socket, pushes frames
encoded with the plugin's own header codec, and the client connects, reads, frames, and stops
through the real :class:`BaichuanFrameReader` and :class:`BaichuanFrameDispatcher`. Only the login
handshake is stubbed (a session token is set directly), because the handshake is not what is under
test — framing, routing, queue bounds, event fairness, and the ``cmdId=6`` guarantee are.

The media frames pushed are the rebuilt geometry from ``tests/fixtures/reolink/`` (see
``reolink_wire``), so what the client parses is a real capture's byte stream rather than a
convenient invention. Run: ``uv run pytest tests/test_reolink_preview_socket.py -q``.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
import reolink_wire as wire

from episode.plugins.reolink.client import (
    BC_CMD_ID_ALARM_EVENT_LIST,
    BC_CMD_ID_PREVIEW,
    BC_CMD_ID_STOP_PREVIEW,
    BaichuanApiClient,
    BaichuanFrameDispatcher,
    BaichuanFrameReader,
    ReolinkError,
    bc_encrypt,
    encode_frame,
)
from episode.plugins.reolink.preview import (
    PREVIEW_QUEUE_MAXSIZE,
    PREVIEW_STREAMS,
    PreviewStats,
)

pytestmark = pytest.mark.asyncio

#: The client's default Baichuan cipher offset, which is also the offset a camera ciphered a
# response body with on this connection. Everything the scripted camera sends uses it.
CHANNEL = 250

#: A capture is ~1.5 MB across 136–213 frames; a socket test only needs the frames in front of
# the first independently decodable picture, which is 13–29 frames.
XOR = 0x5A


def _bc(data: bytes) -> bytes:
    """Cipher exactly as the client would decrypt it, so the round trip is real."""
    return bc_encrypt(data, CHANNEL)


def _frame(cmd_id: int, body: bytes, *, payload_offset: int = 0, response_code: int = 200) -> bytes:
    return encode_frame(
        cmd_id,
        body,
        use_24_header=True,
        channel=CHANNEL,
        payload_offset=payload_offset,
        response_code=response_code,
    )


class _BcWireCipher:
    """:class:`wire.Cipher` with the plugin's real Baichuan cipher in place of the XOR stand-in.

    The rebuilt byte geometry comes from the shared fixture helper; only the cipher differs, so
    what arrives on the socket is ciphered the way a camera ciphers it and the client's own
    decryption is what turns it back into the recorded bytes.
    """

    def encrypt(self, data: bytes) -> bytes:
        return _bc(data)


def _media_frames(capture: dict[str, Any], *, limit: int | None = None) -> list[bytes]:
    """One capture's frames as the camera puts them on the wire.

    The extension is ciphered whole (that is what ``payloadOffset`` points at) and, when the
    capture recorded an ``encryptLen``, exactly that many leading payload bytes are ciphered —
    the remainder of a media start's payload stays in the clear, as measured. Continuation
    frames carry no extension and their ``payloadOffset`` is 0, so nothing about them is ciphered.
    """
    stream = wire.stream_bytes(capture["packets"], capture["meta"])
    out: list[bytes] = []
    for item in wire.wire_frames(stream, capture["frames"], _BcWireCipher()):
        extension = _bc(item["extension"].encode("utf-8"))
        out.append(
            _frame(BC_CMD_ID_PREVIEW, extension + item["payload"], payload_offset=len(extension))
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def _first_picture_frames(capture: dict[str, Any]) -> int:
    """Frames the camera must send before this capture's first picture is decodable."""
    return wire.frames_to_first_picture(capture)[0]


class ScriptedCamera:
    """A camera that answers ``cmdId=3`` with scripted frames on a real socket."""

    def __init__(
        self,
        *,
        mode: str = "media",
        frames: list[bytes] | None = None,
        frame_delay: float = 0.0,
        events: list[bytes] | None = None,
        eof_after: int | None = None,
        response_code: int = 405,
    ) -> None:
        self.mode = mode
        self.frames = frames or []
        self.frame_delay = frame_delay
        self.events = events or []
        self.eof_after = eof_after
        self.response_code = response_code
        self.preview_requests: list[bytes] = []
        self.stops: list[bytes] = []
        self.other: list[int] = []
        self.pushed = 0
        self._server: asyncio.Server | None = None
        self._push_task: asyncio.Task[None] | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> ScriptedCamera:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Stop streaming, close our own side of every connection, then the listener.

        Closing the listener is not enough: an accepted connection left open locally keeps
        ``Server.wait_closed()`` waiting, so a test would hang on teardown instead of finishing.
        """
        if self._push_task is not None and not self._push_task.done():
            self._push_task.cancel()
            await asyncio.gather(self._push_task, return_exceptions=True)
        for writer in self._writers:
            if not writer.is_closing():
                writer.close()
        await asyncio.gather(*(w.wait_closed() for w in self._writers), return_exceptions=True)
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def port(self) -> int:
        assert self._server is not None and self._server.sockets
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            frames_in = BaichuanFrameReader(reader).iter_frames()
            async for cmd_id, _msg, _resp, _offset, body in frames_in:
                if cmd_id == BC_CMD_ID_PREVIEW:
                    self.preview_requests.append(body)
                    self._push_task = asyncio.create_task(self._push(writer))
                elif cmd_id == BC_CMD_ID_STOP_PREVIEW:
                    self.stops.append(body)
                else:
                    self.other.append(cmd_id)
        except Exception:  # a client closing first is normal here
            pass

    async def _push(self, writer: asyncio.StreamWriter) -> None:
        """Stream the scripted media frames, interleaving alarm pushes as asked."""
        if self.mode == "silent":
            return
        if self.mode == "reject":
            body = _bc(b"<Preview><status>Rejected</status></Preview>")
            writer.write(_frame(BC_CMD_ID_PREVIEW, body, response_code=self.response_code))
            await writer.drain()
            return
        event_index = 0
        for index, raw in enumerate(self.frames):
            if self.eof_after is not None and index >= self.eof_after:
                # Half-close the *media* direction only, so the client sees the stream end while
                # still being able to send its stop: closing both directions here would make the
                # guarantee under test unobservable rather than absent.
                writer.write_eof()
                return
            if self.frame_delay:
                await asyncio.sleep(self.frame_delay)
            writer.write(raw)
            self.pushed += 1
            while event_index < self.events_until(index):
                writer.write(_frame(BC_CMD_ID_ALARM_EVENT_LIST, self.events[event_index]))
                event_index += 1
            await writer.drain()
        while event_index < len(self.events):
            writer.write(_frame(BC_CMD_ID_ALARM_EVENT_LIST, self.events[event_index]))
            event_index += 1
        await writer.drain()

    def events_until(self, frame_index: int) -> int:
        """How many events belong in front of ``frame_index`` (events are pre-spread)."""
        return (
            len(self.events)
            if not self.frames
            else min(len(self.events), (frame_index * len(self.events)) // len(self.frames) + 1)
        )


async def _connect(camera: ScriptedCamera) -> BaichuanApiClient:
    """A real socket, reader, and dispatcher; only the negotiated session is asserted."""
    client = BaichuanApiClient("127.0.0.1", "admin", "secret", api_port=camera.port, timeout=2.0)
    await client.connect()
    # The login handshake has its own tests. Here it is replaced by the state a completed
    # handshake would have produced, so the encrypted round trip below is genuinely encrypted.
    client._token = "session-token"
    assert client.authenticated
    return client


async def _collect_events(client: BaichuanApiClient, wanted: int) -> list[tuple[float, bytes]]:
    """Timestamped alarm pushes, in arrival order, until ``wanted`` of them arrive."""
    loop = asyncio.get_running_loop()
    received: list[tuple[float, bytes]] = []

    async for cmd_id, body in client.event_frame_iterator:
        if cmd_id == BC_CMD_ID_ALARM_EVENT_LIST:
            received.append((loop.time(), body))
            if len(received) >= wanted:
                return received
    return received


# ── Observing a real capture ──────────────────────────────────────────


@pytest.mark.parametrize("set_name", wire.SET_NAMES)
async def test_preview_observes_first_packet_from_fixture(set_name: str) -> None:
    """``cmdId=3`` framed through the real dispatcher reports that capture's first picture."""
    capture = wire.capture_fixture(set_name)
    frames = _media_frames(capture, limit=_first_picture_frames(capture))
    async with ScriptedCamera(frames=frames) as camera:
        client = await _connect(camera)
        try:
            stats = await client.observe_preview_first_packet(timeout=3.0)
        finally:
            await client.close()

    meta = capture["meta"]
    assert stats.variant == "main"
    assert stats.codec == meta["codec"]
    assert stats.info_seen is True
    assert (stats.width, stats.height) == (meta["width"], meta["height"])
    # Exactly one picture: the pass exits at the first video packet, never at the stream end.
    assert stats.frames() == 1
    assert stats.iframe_count == 1 and stats.pframe_count == 0
    assert stats.first_packet_ms is not None and stats.first_packet_ms >= 0.0
    assert stats.first_iframe_ms is not None
    assert stats.resyncs == 0 and stats.truncated == 0 and stats.dropped_frames == 0
    assert stats.dropped_bytes == 0
    # One session, one stop, and the stop repeated the handle rather than sending an empty body.
    assert len(camera.preview_requests) == 1
    assert len(camera.stops) == 1
    assert b"<handle>0</handle>" in _bc(camera.stops[0])


@pytest.mark.parametrize("set_name", wire.SET_NAMES)
async def test_preview_media_reaches_the_consumer_unchanged(set_name: str) -> None:
    """The bytes a sink receives are the camera's payload, decrypted once and only once.

    This is the assertion that catches a client which runs every payload through the cipher:
    packet sizes come from headers, so framing stays in lockstep and every tally still looks
    healthy while the picture content is wrecked. Comparing bytes is what reveals it.
    """
    capture = wire.capture_fixture(set_name)
    frames = _media_frames(capture, limit=_first_picture_frames(capture))
    expected = wire.first_video_payload(set_name)
    received: list[bytes] = []
    async with ScriptedCamera(frames=frames) as camera:
        client = await _connect(camera)
        try:
            stats = await client.observe_preview_first_packet(sink=received.append)
        finally:
            await client.close()

    assert stats.frames() == 1
    assert len(received) == 1
    assert received[0] == expected, "media must be ciphered exactly as the camera ciphered it"
    assert received[0] != bytes(byte ^ XOR for byte in expected), "the cipher must be asymmetric"


@pytest.mark.parametrize("set_name", wire.SET_NAMES)
async def test_stream_preview_pushes_annexb_access_units(set_name: str) -> None:
    """``stream_preview`` hands each framed picture to ``push`` as Annex-B access units.

    Unlike ``observe_preview_first_packet`` (which stops at the first picture), the streaming
    path is the F1 recorder source: it keeps framing every picture in the burst until the camera
    stops, and each unit must carry an Annex-B start code so the recorder's elementary-stream
    demuxer can consume it without dropping the first frame.
    """
    capture = wire.capture_fixture(set_name)
    frames = _media_frames(capture)
    received: list[bytes] = []
    async with ScriptedCamera(frames=frames) as camera:
        client = await _connect(camera)
        try:

            async def push(chunk: bytes) -> None:
                received.append(chunk)

            stats = await client.stream_preview(push, timeout=5.0)
        finally:
            await client.close()

    # The stream pushed every framed picture, not just the first.
    assert stats.frames() >= 2, "a full capture has several pictures, not one"
    assert stats.iframe_count >= 1
    assert len(received) == stats.frames(), "one Annex-B access unit per framed picture"
    # Every unit starts with an Annex-B start code (the recorder needs one).
    assert all(
        chunk.startswith(b"\x00\x00\x00\x01") or chunk.startswith(b"\x00\x00\x01")
        for chunk in received
    )
    # One session, one stop, handle repeated.
    assert len(camera.preview_requests) == 1
    assert len(camera.stops) == 1
    assert b"<handle>0</handle>" in _bc(camera.stops[0])


async def test_stream_preview_cancels_stalled_consumer_and_still_stops() -> None:
    """A full output queue cannot prevent consumer cleanup or the camera stop."""
    capture = wire.capture_fixture(wire.SET_NAMES[0])
    frames = _media_frames(capture)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()

    async def push(_chunk: bytes) -> None:
        entered.set()
        try:
            await never.wait()
        finally:
            cancelled.set()

    async with ScriptedCamera(frames=frames) as camera:
        client = await _connect(camera)
        try:
            stream = asyncio.create_task(client.stream_preview(push, timeout=2.0))
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            await asyncio.sleep(0.05)
            stream.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream
        finally:
            await client.close()

    assert entered.is_set(), "the stalled consumer must have received a frame"
    assert cancelled.is_set(), "teardown must cancel a consumer that cannot drain"
    assert len(camera.stops) == 1, "consumer backpressure must not skip cmdId=6"


async def test_stalled_preview_consumer_cleanup_is_bounded_without_socket() -> None:
    """Consumer cancellation is contained even when the loopback socket test is unavailable."""
    client = object.__new__(BaichuanApiClient)
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def push(_chunk: bytes) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    consumer = asyncio.create_task(client._drain_queue(queue, push))
    await queue.put(b"in-flight")
    await entered.wait()
    await queue.put(b"queued")

    await asyncio.wait_for(client._finish_preview_consumer(queue, consumer), timeout=4.0)

    assert cancelled.is_set()
    assert consumer.done()
    assert consumer.cancelled()


async def test_stream_preview_is_not_truncated_by_preview_timeout() -> None:
    """``preview_timeout`` bounds the first packet, never the recording duration.

    This is the ONVIF-parity guarantee: the recorder cancels the native burst at episode end,
    and a small ``timeout`` must not cut the recording short just because the first packet was
    quick. Here the camera keeps sending frames well past ``timeout``; the stream must keep
    pushing until the caller cancels it, not silently stop at the wall clock.
    """
    capture = wire.capture_fixture(wire.SET_NAMES[0])
    # A burst that keeps arriving long after a deliberately tiny preview timeout. Frames are
    # paced so the elapsed wall-clock time crosses ``timeout`` while the camera still has
    # frames left to send — the exact shape a live episode extends past a preview deadline.
    frames = _media_frames(capture)
    received: list[bytes] = []
    offsets: list[float] = []  # seconds since the stream started, per access unit
    loop = asyncio.get_running_loop()
    start = loop.time()

    async with ScriptedCamera(frames=frames, frame_delay=0.02) as camera:
        client = await _connect(camera)
        stream_task: asyncio.Task[PreviewStats] | None = None
        try:

            async def push(chunk: bytes) -> None:
                received.append(chunk)
                offsets.append(loop.time() - start)

            # A timeout far smaller than the paced burst would need to complete. If the
            # wall clock bounded the stream, the first picture would arrive quickly and the
            # iteration would end at ~0.2s with a handful of frames. It must instead keep
            # running until we cancel it, many frames later.
            stream_task = asyncio.create_task(client.stream_preview(push, timeout=0.2))
            # The camera keeps streaming, so keep waiting until the burst has clearly
            # exceeded the preview deadline several times over before cancelling it.
            for _ in range(60):
                await asyncio.sleep(0.05)
                if offsets and offsets[-1] >= 0.8:
                    break
            stream_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream_task
        finally:
            await client.close()

    # The recording outlived the preview timeout: it was cancelled by us, not by the clock.
    assert len(received) >= 3, (
        "the stream must keep pushing past preview_timeout; got %d access units" % len(received)
    )
    # Access units kept arriving well past the 0.2s preview timeout, so the wall clock
    # did not end the recording — only our cancellation did.
    assert offsets[-1] - offsets[0] >= 0.4, (
        "pushes must continue past preview_timeout, not stop at it "
        "(first=%.3fs last=%.3fs)" % (offsets[0], offsets[-1])
    )
    # Every unit still carries an Annex-B start code.
    assert all(
        chunk.startswith(b"\x00\x00\x00\x01") or chunk.startswith(b"\x00\x00\x01")
        for chunk in received
    )
    # One session, one stop even on cancellation.
    assert len(camera.preview_requests) == 1
    assert len(camera.stops) == 1


async def test_preview_handles_cleartext_continuation_frames() -> None:
    """The split-packet firmware's first picture arrives through continuation frames intact.

    One measured capture sliced every packet across ~40 KB frames, so its first keyframe needed
    19 of them. Those frames carry no extension at all (``payloadOffset == 0``) and must be
    appended raw: decrypting them corrupts the stream, and since sizes come from headers the
    tallies would still look fine — so the byte comparison is the only thing that catches it.
    """
    set_name = "fw-50397653"
    capture = wire.capture_fixture(set_name)
    frames_needed, continuations = wire.frames_to_first_picture(capture)
    assert continuations > 0, "this capture is the one with split packets"
    frames = _media_frames(capture, limit=frames_needed)
    received: list[bytes] = []
    async with ScriptedCamera(frames=frames) as camera:
        client = await _connect(camera)
        try:
            stats = await client.observe_preview_first_packet(sink=received.append)
        finally:
            await client.close()

    packet = wire.first_video_packet(capture["packets"])
    assert stats.frames() == 1
    # Every byte of that picture came from a continuation frame except its ciphered head.
    assert stats.media_bytes == packet["payload_size"]
    assert received == [wire.first_video_payload(set_name)]
    assert stats.resyncs == 0 and stats.truncated == 0


async def test_preview_stop_repeats_handle() -> None:
    """Each variant's stop repeats its own measured handle (an empty body is answered 405)."""
    capture = wire.capture_fixture(wire.SET_NAMES[0])
    frames = _media_frames(capture, limit=_first_picture_frames(capture))
    for variant, (handle, _name, _header_stream) in PREVIEW_STREAMS.items():
        async with ScriptedCamera(frames=frames) as camera:
            client = await _connect(camera)
            try:
                await client.observe_preview_first_packet(variant=variant, timeout=3.0)
            finally:
                await client.close()
        assert len(camera.stops) == 1, f"{variant}: one stop per pass"
        assert f"<handle>{handle}</handle>".encode() in _bc(camera.stops[0])


# ── The stop is unconditional ─────────────────────────────────────────


async def test_preview_always_stops_after_observe() -> None:
    """``cmdId=6`` follows every way a pass can end, including a cancelled one.

    A camera left streaming keeps feeding whatever reads the socket next, which would corrupt the
    *next* capture or an unrelated command's response — so the stop is the guarantee this feature
    is judged on, and every exit path has to be shown to pay it.
    """
    capture = wire.capture_fixture(wire.SET_NAMES[2])

    async def _observe(
        mode: str, *, cancel: bool = False, timeout: float = 1.0, **kwargs: Any
    ) -> ScriptedCamera:
        frames = _media_frames(capture, limit=kwargs.pop("limit", 3))
        async with ScriptedCamera(mode=mode, frames=frames, **kwargs) as camera:
            client = await _connect(camera)
            try:
                task = asyncio.create_task(client.observe_preview_first_packet(timeout=timeout))
                if cancel:
                    await asyncio.sleep(0.05)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await task
                for _ in range(50):
                    if camera.stops:
                        break
                    await asyncio.sleep(0.02)
            finally:
                await client.close()
            return camera

    silent = await _observe("silent", timeout=0.4)
    assert len(silent.preview_requests) == 1
    assert len(silent.stops) == 1, "a timed-out pass must still release the camera's encoder"

    rejected = await _observe("reject")
    assert len(rejected.preview_requests) == 1
    assert len(rejected.stops) == 1, "a rejected request is not a reason to skip the stop"

    half_closed = await _observe("media", limit=30, eof_after=4)
    assert len(half_closed.stops) == 1, "the stop is sent whatever happened to the stream"

    cancelled = await _observe("media", limit=40, frame_delay=0.02, cancel=True)
    assert len(cancelled.stops) == 1, "a cancelled pass must not leave the camera streaming"


async def test_preview_timed_out_observe_returns_a_result_not_an_error() -> None:
    """A camera that answers nothing yields an empty tally rather than an exception."""
    async with ScriptedCamera(mode="silent") as camera:
        client = await _connect(camera)
        try:
            stats = await client.observe_preview_first_packet(timeout=0.4)
        finally:
            await client.close()
    assert isinstance(stats, PreviewStats)
    assert stats.frames() == 0
    assert stats.first_packet_ms is None
    assert stats.first_iframe_ms is None


async def test_preview_rejection_is_reported_not_raised() -> None:
    """A rejected request is a result with no frames; the caller's event flow is untouched."""
    capture = wire.capture_fixture(wire.SET_NAMES[0])
    async with ScriptedCamera(mode="reject", frames=_media_frames(capture, limit=3)) as camera:
        client = await _connect(camera)
        try:
            stats = await client.observe_preview_first_packet(timeout=1.0)
        finally:
            await client.close()
    assert stats.frames() == 0
    assert stats.keyframed() is False


async def test_preview_requires_authentication() -> None:
    """No ``cmdId=3`` may go out on an unauthenticated socket."""
    client = BaichuanApiClient("127.0.0.1", "admin", "secret", api_port=1, timeout=1.0)
    with pytest.raises(ReolinkError):
        await client.observe_preview_first_packet()


# ── Bounds, and what happens when they are hit ────────────────────────


async def test_preview_buffers_are_bounded() -> None:
    """The caller's ``max_bytes`` reaches the reassembler, not just the running total.

    The largest measured keyframe is ~1 MB. A bound that only limited a *total* would let a single
    750 KB picture through under a 64 KiB cap, because nothing ever accumulated long enough to
    trip it — so the cap has to apply to one packet as well, and be refused while the packet's own
    24-byte header is the only thing buffered. Recovery from the refusal is a counted resync, and
    the camera is still told to stop.
    """
    capture = wire.capture_fixture(wire.SET_NAMES[1])
    frames = _media_frames(capture, limit=_first_picture_frames(capture))
    packet = wire.first_video_packet(capture["packets"])
    bound = 64 * 1024
    assert packet["payload_size"] > bound, (
        "the keyframe must be bigger than the bound to mean anything"
    )
    async with ScriptedCamera(frames=frames) as camera:
        client = await _connect(camera)
        try:
            tight = await client.observe_preview_first_packet(timeout=3.0, max_bytes=bound)
            # Same capture, a bound that fits its keyframe: the pass still yields that picture.
            fits = len(wire.first_picture_bytes(capture)) + 1024
            loose = await client.observe_preview_first_packet(timeout=3.0, max_bytes=fits)
        finally:
            await client.close()

    assert tight.frames() == 0, "an over-bound packet is refused, not buffered whole"
    assert tight.media_bytes == 0, "nothing above the bound is held"
    assert tight.truncated >= 1 and tight.resyncs >= 1, "the refusal is reported and recovered from"
    assert loose.frames() == 1 and loose.media_bytes == packet["payload_size"]
    assert len(camera.preview_requests) == 2 and len(camera.stops) == 2


async def test_preview_requests_are_serialized_per_camera() -> None:
    """Two concurrent observers cannot hold two media sessions open on one camera.

    Their frames would interleave into a single reassembly, and the camera itself is the resource
    being protected: an encoder session costs a real encode whether or not anyone reads it.
    """
    capture = wire.capture_fixture(wire.SET_NAMES[0])
    frames = _media_frames(capture, limit=_first_picture_frames(capture))
    async with ScriptedCamera(frames=frames) as camera:
        client = await _connect(camera)
        try:
            results = await asyncio.gather(
                *[client.observe_preview_first_packet(timeout=3.0) for _ in range(3)]
            )
        finally:
            await client.close()

    assert [stats.frames() for stats in results] == [1, 1, 1], "no pass borrowed another's frames"
    assert len(camera.preview_requests) == 3
    assert len(camera.stops) == 3


async def test_preview_capture_uses_a_bounded_waiter_queue() -> None:
    """The waiter the dispatcher buffers for a preview is bounded, so memory is."""
    capture = wire.capture_fixture(wire.SET_NAMES[0])
    frames = _media_frames(capture, limit=_first_picture_frames(capture))
    seen: list[Any] = []
    async with ScriptedCamera(frames=frames) as camera:
        client = await _connect(camera)
        dispatcher = client._dispatcher
        real_iter = dispatcher.iter_matching

        async def _spy(cmd_id: int, **kwargs: Any):
            seen.append(kwargs.get("queue"))
            async for frame in real_iter(cmd_id, **kwargs):
                yield frame

        dispatcher.iter_matching = _spy
        try:
            await client.observe_preview_first_packet(timeout=3.0)
        finally:
            await client.close()

    assert seen and seen[0] is not None, "the preview must supply its own queue"
    assert seen[0].maxsize == PREVIEW_QUEUE_MAXSIZE


class _QueueFrameReader:
    """A frame source a test can feed, so dispatcher routing is exercised without a socket."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[int, int, int, int, bytes] | None] = asyncio.Queue()

    async def iter_frames(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def feed(self, cmd_id: int, body: bytes) -> None:
        await self._queue.put((cmd_id, 0, 200, 0, body))

    async def close(self) -> None:
        await self._queue.put(None)


async def test_dispatcher_drops_and_counts_when_a_bounded_consumer_falls_behind() -> None:
    """The consequence of not blocking is measured, not hidden.

    A preview shares its socket with the alarm pushes it exists to speed up, so the dispatcher may
    not block on a slow media consumer — which means the drops have to be counted, or an
    overloaded consumer looks identical to a camera that stopped sending.
    """
    reader = _QueueFrameReader()
    dispatcher = BaichuanFrameDispatcher(reader)  # type: ignore[arg-type]
    dispatcher.start()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    fed = 6

    async def _send() -> None:
        """Push every frame *and wait for the dispatcher to have decided about all of them*.

        ``send`` runs before the read loop, so holding it back until ``dropped + buffered == fed``
        means the numbers asserted below describe a settled dispatcher rather than a race between
        the read task and this one.
        """
        for _ in range(fed):
            await reader.feed(BC_CMD_ID_PREVIEW, b"x")
        while dispatcher.dropped_frames + queue.qsize() < fed:
            await asyncio.sleep(0.01)

    received = 0
    leftover = -1
    walker = dispatcher.iter_matching(BC_CMD_ID_PREVIEW, timeout=2.0, send=_send, queue=queue)
    try:
        async for _frame in walker:
            received += 1
            if queue.empty():
                # Drained everything the bound let through. Measured here, at the moment the
                # consumer stopped, because teardown pushes a sentinel into registered queues.
                leftover = queue.qsize()
                break
    finally:
        # Closed explicitly rather than left to GC: an abandoned continuous waiter would keep
        # matching frames forever, so deregistration on exit is part of what is being proven.
        await walker.aclose()
        waiters = list(dispatcher._waiters)  # noqa: SLF001 - read before teardown clears it
        await reader.close()
        await dispatcher.stop()

    assert received == 2, "only a queue's worth of frames survives a consumer that stopped reading"
    assert dispatcher.dropped_frames == fed - 2, "every refused frame is counted"
    assert leftover == 0
    assert not waiters, "the finished consumer is no longer registered"
    assert not waiters, "the finished consumer is no longer registered"


# ── Preview must not starve the events it exists to help ──────────────


async def test_preview_does_not_delay_event_frames() -> None:
    """Alarm pushes keep flowing at full rate while preview media shares the socket.

    Measured native rates are 12–15 fps, hundreds of frames per capture, on the same socket as
    ``cmdId=33`` — so this is the regression that matters most. Every event has to arrive, in
    order, with nothing dropped, and a share of them have to arrive *during* the preview pass
    rather than after it, because "queued behind the media" and "delayed" are the same thing.
    """
    capture = wire.capture_fixture(wire.SET_NAMES[1])  # the split-packet capture: most frames
    frames_needed, _ = wire.frames_to_first_picture(capture)
    frames = _media_frames(capture, limit=frames_needed)
    events = [f"<Event><index>{index}</index></Event>".encode() for index in range(12)]
    async with ScriptedCamera(frames=frames, frame_delay=0.004, events=events) as camera:
        client = await _connect(camera)
        collector = asyncio.create_task(_collect_events(client, len(events)))
        try:
            stats = await client.observe_preview_first_packet(timeout=3.0)
            done_at = asyncio.get_running_loop().time()
            await asyncio.wait_for(collector, timeout=5.0)
        finally:
            if not collector.done():
                collector.cancel()
            await client.close()

        arrived = collector.result()
        assert stats.frames() == 1
        assert len(arrived) == len(events), "every alarm push must survive a concurrent preview"
        assert [body for _, body in arrived] == events, "order is preserved"
        during = sum(1 for stamp, _ in arrived if stamp <= done_at)
        assert during > 0, "events must be delivered during the pass, not after it"
        assert client._dispatcher is None or client._dispatcher.dropped_frames == 0
        assert stats.dropped_frames == 0
        assert camera.pushed == len(frames)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
