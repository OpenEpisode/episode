"""Reolink Baichuan binary protocol client.

Implements the subset of the proprietary protocol that Episode uses over raw
TCP port 9000. The implementation was informed by the MIT-licensed nodelink-js
protocol documentation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import struct as _struct
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

# ── Protocol constants ────────────────────────────────────────────────

MAGIC: int = 0xF0DEBC0A
MAGIC_BYTES: bytes = MAGIC.to_bytes(4, "big")
MAGIC_REV: bytes = b"\xa0\xcb\xed\x0f"  # Reversed magic (some cameras)

HEADER_20 = 20
HEADER_24 = 24
MAX_FRAME_BYTES = 32 * 1024 * 1024

BC_CLASS_LEGACY = 0x6514
BC_CLASS_MODERN_20 = 0x6614
BC_CLASS_MODERN_24 = 0x6414
BC_CLASS_MODERN_24_ALT = 0x0000
BC_CLASS_FILE_DOWNLOAD = 0x6482

# Encryption negotiation response codes
BC_ENC_NONE = 0xDC00
BC_ENC_BC = 0xDC01
BC_ENC_AES = 0xDC02
BC_ENC_FULL_AES = 0xDC12
BC_ENC_RESPONSE_PREFIX = 0xDD

# Command IDs
BC_CMD_ID_LOGIN = 1
BC_CMD_ID_LOGOUT = 2
BC_CMD_ID_PREVIEW = 3
BC_CMD_ID_STOP_PREVIEW = 6
BC_CMD_ID_ALARM_EVENT_LIST = 33
BC_CMD_ID_ABILITY_INFO = 151
BC_CMD_ID_VERSION_INFO = 80
BC_CMD_ID_SNAPSHOT = 109
BC_CMD_ID_STREAM_INFO_LIST = 146
BC_CMD_ID_BATTERY_STATUS = 252
BC_CMD_ID_PING = 93
# Subscribe to push events: Reolink cameras only push alarm-event frames
# (cmdId=33) to an authenticated client that first subscribes via cmdId=31.
BC_CMD_ID_SUBSCRIBE_EVENTS = 31

# Response codes
BC_RESP_CODE_OK = 0


# ── Encryption ─────────────────────────────────────────────────────────

BCEXPAND_KEY = [0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF]
BC_AES_IV = b"0123456789abcdef"


def bc_encrypt(data: bytes, offset: int = 0) -> bytes:
    """Encrypt data using BCEncrypt (XOR-based stream cipher with offset)."""
    off = offset & 0xFF
    result = bytearray(len(data))
    key_len = len(BCEXPAND_KEY)
    for i in range(len(data)):
        result[i] = data[i] ^ BCEXPAND_KEY[(off + i) % key_len] ^ off
    return bytes(result)


def bc_decrypt(data: bytes, offset: int = 0) -> bytes:
    """Decrypt data using BCEncrypt (XOR is symmetric)."""
    return bc_encrypt(data, offset)


def derive_aes_key(nonce: str, password: str) -> bytes:
    """Derive AES-128 key: md5(nonce + '-' + password)[:16] as UTF-8 bytes.

    The dash separator is critical — it's how Reolink derives the key.
    """
    key_str = hashlib.md5((nonce + "-" + password).encode()).hexdigest().upper()[:16]
    return key_str.encode("utf-8")


def md5_str_modern(text: str) -> str:
    """Compute MD5 hex digest, uppercase, truncated to 31 chars."""
    return hashlib.md5(text.encode()).hexdigest().upper()[:31]


def aes_encrypt_cfb(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt using AES-128-CFB (full CFB mode)."""
    cipher = AES.new(key, AES.MODE_CFB, iv=BC_AES_IV, segment_size=128)
    return cipher.encrypt(plaintext)


def aes_decrypt_cfb(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt using AES-128-CFB."""
    cipher = AES.new(key, AES.MODE_CFB, iv=BC_AES_IV, segment_size=128)
    return cipher.decrypt(ciphertext)


def encrypt_payload(xml_data: bytes, nonce: str, password: str, use_aes: bool = False) -> bytes:
    """Encrypt payload XML data for transmission."""
    if use_aes:
        key = derive_aes_key(nonce, password)
        return aes_encrypt_cfb(xml_data, key)
    else:
        return bc_encrypt(xml_data)


def decrypt_payload(data: bytes, nonce: str, password: str, use_aes: bool = False) -> bytes:
    """Decrypt received payload data."""
    if use_aes:
        key = derive_aes_key(nonce, password)
        return aes_decrypt_cfb(data, key)
    else:
        return bc_decrypt(data)


# ── Frame encoding/decoding ───────────────────────────────────────────


def _header_has_payload_offset(message_class: int) -> bool:
    """Check if a message class uses a 24-byte header with payloadOffset."""
    return message_class in (BC_CLASS_MODERN_24, BC_CLASS_MODERN_24_ALT, BC_CLASS_FILE_DOWNLOAD)


def encode_header_20(
    cmd_id: int,
    body_len: int,
    msg_num: int = 0,
    channel: int = 0,
    stream_type: int = 0,
    response_code: int = 0,
    message_class: int = BC_CLASS_LEGACY,
) -> bytes:
    """Encode a 20-byte header."""
    buf = bytearray(20)
    buf[0:4] = MAGIC_BYTES
    struct_pack("<I", buf, 4, cmd_id)
    struct_pack("<I", buf, 8, body_len)
    buf[12] = channel & 0xFF
    buf[13] = stream_type & 0xFF
    struct_pack("<H", buf, 14, msg_num & 0xFFFF)
    struct_pack("<H", buf, 16, response_code & 0xFFFF)
    struct_pack("<H", buf, 18, message_class & 0xFFFF)
    return bytes(buf)


def encode_header_24(
    cmd_id: int,
    body_len: int,
    msg_num: int = 0,
    channel: int = 0,
    stream_type: int = 0,
    payload_offset: int = 0,
    response_code: int = 0,
    message_class: int = BC_CLASS_MODERN_24,
) -> bytes:
    """Encode a 24-byte modern header."""
    buf = bytearray(24)
    buf[0:4] = MAGIC_BYTES
    struct_pack("<I", buf, 4, cmd_id)
    struct_pack("<I", buf, 8, body_len)
    buf[12] = channel & 0xFF
    buf[13] = stream_type & 0xFF
    struct_pack("<H", buf, 14, msg_num & 0xFFFF)
    struct_pack("<H", buf, 16, response_code & 0xFFFF)
    struct_pack("<H", buf, 18, message_class & 0xFFFF)
    struct_pack("<I", buf, 20, payload_offset & 0xFFFFFFFF)
    return bytes(buf)


def struct_pack(fmt: str, buf: bytearray, offset: int, value: int) -> None:
    """Pack value into bytearray at offset."""
    _struct.pack_into(fmt, buf, offset, value)


def encode_frame(
    cmd_id: int,
    body: bytes,
    use_24_header: bool = False,
    channel: int = 0,
    stream_type: int = 0,
    msg_num: int = 0,
    payload_offset: int = 0,
    response_code: int = 0,
    message_class: int = BC_CLASS_MODERN_24,
) -> bytes:
    """Encode a complete Baichuan frame."""
    if use_24_header:
        header = encode_header_24(
            cmd_id,
            len(body),
            msg_num,
            channel,
            stream_type,
            payload_offset,
            response_code,
            message_class,
        )
    else:
        header = encode_header_20(
            cmd_id, len(body), msg_num, channel, stream_type, response_code, message_class
        )
    return header + body


def decode_frame(data: bytes) -> tuple[int, bytes] | None:
    """Decode a single Baichuan frame from raw bytes.

    Returns (cmd_id, body) or None if no complete frame.
    Handles magic byte realignment for both normal and reversed magic.
    """
    # Find magic bytes (normal or reversed)
    idx_normal = data.find(MAGIC_BYTES)
    idx_rev = data.find(MAGIC_REV)

    if idx_normal < 0 and idx_rev < 0:
        return None

    idx = idx_rev if (idx_rev >= 0 and (idx_normal < 0 or idx_rev < idx_normal)) else idx_normal
    data = data[idx:]

    if len(data) < HEADER_20:
        return None

    cmd_id = _struct.unpack_from("<I", data, 4)[0]
    body_len = _struct.unpack_from("<I", data, 8)[0]
    msg_class = _struct.unpack_from("<H", data, 18)[0]

    header_size = HEADER_24 if _header_has_payload_offset(msg_class) else HEADER_20
    if len(data) < header_size:
        return None

    total_frame_len = header_size + body_len

    if len(data) < total_frame_len:
        return None

    body = data[header_size : header_size + body_len]
    logger.debug("Decoded frame: cmdId=%d bodyLen=%d class=0x%04X", cmd_id, body_len, msg_class)
    return cmd_id, body


# ── XML helpers ───────────────────────────────────────────────────────


def _serialize_xml(root: ET.Element) -> str:
    """Serialize an ElementTree root to a string with the XML declaration."""
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def build_login_xml(user_hash: str, pass_hash: str) -> str:
    """Build the login XML payload for the second login step."""
    root = ET.Element("body")
    login_user = ET.SubElement(root, "LoginUser", version="1.1")
    ET.SubElement(login_user, "userName").text = user_hash
    ET.SubElement(login_user, "password").text = pass_hash
    ET.SubElement(login_user, "userVer").text = "1"
    login_net = ET.SubElement(root, "LoginNet", version="1.1")
    ET.SubElement(login_net, "type").text = "LAN"
    ET.SubElement(login_net, "udpPort").text = "0"
    return _serialize_xml(root)


def build_logout_xml() -> str:
    """Build the <Logout> XML payload for session termination."""
    root = ET.Element("body")
    ET.SubElement(root, "Logout", version="1.1")
    return _serialize_xml(root)


def build_snapshot_xml(channel: int = 0, stream_type: str = "main") -> str:
    """Build <Snap> XML to request a snapshot image.

    Matches the protocol ersion 1.1 with logicChannel,
    time, fullFrame and streamType fields. For a standalone camera the
    logicChannel is the channel index.
    """
    root = ET.Element("body")
    snap = ET.SubElement(root, "Snap", version="1.1")
    ET.SubElement(snap, "channelId").text = str(channel)
    ET.SubElement(snap, "logicChannel").text = str(channel)
    ET.SubElement(snap, "time").text = "0"
    ET.SubElement(snap, "fullFrame").text = "0"
    ET.SubElement(snap, "streamType").text = stream_type
    return _serialize_xml(root)


def build_ability_info_xml(username: str = "admin") -> str:
    """Build <Extension> XML for ability info query."""
    root = ET.Element("Extension", version="1.1")
    ET.SubElement(root, "userName").text = username
    ET.SubElement(
        root, "token"
    ).text = (
        "system, streaming, PTZ, IO, security, replay, disk, network, alarm, record, video, image"
    )
    return _serialize_xml(root)


def build_channel_extension_xml(channel_id: int | None = None) -> str:
    """Build a channel <Extension> XML for the extension slot of a command body."""
    root = ET.Element("Extension", version="1.1")
    if channel_id is not None:
        ET.SubElement(root, "channelId").text = str(channel_id)
    return _serialize_xml(root)


def parse_xml_body(body: bytes) -> dict[str, Any]:
    """Parse XML body into a dict."""
    try:
        root = ET.fromstring(body)
        return _xml_to_dict(root)
    except ET.ParseError as e:
        logger.warning("Failed to parse XML: %s", e)
        return {"_raw_xml": body.decode("utf-8", errors="replace")}


def _xml_to_dict(element, prefix: str = "") -> dict[str, Any]:
    """Recursively convert XML element to dict."""
    result: dict[str, Any] = {}
    if element.attrib:
        result["@attributes"] = dict(element.attrib)
    children = list(element)
    if not children:
        text = (element.text or "").strip()
        if text:
            try:
                result["_value"] = int(text)
            except ValueError:
                try:
                    result["_value"] = float(text)
                except ValueError:
                    result["_value"] = text
            if element.tag:
                result[element.tag] = result.get(element.tag, result.get("_value", text))
        elif element.tag:
            result[element.tag] = ""
    else:
        for child in children:
            child_data = _xml_to_dict(child, prefix)
            if child.tag in result:
                if not isinstance(result[child.tag], list):
                    result[child.tag] = [result[child.tag]]
                result[child.tag].append(child_data)
            else:
                result[child.tag] = child_data
    return result


# ── TCP Frame Reader ──────────────────────────────────────────────────


class BaichuanFrameReader:
    """Async reader that buffers TCP data and yields complete frames.

    Handles both normal magic (0xf0debc0a) and reversed magic (0xa0cb ed0f)
    for realignment when frames are misaligned.
    """

    def __init__(self, reader: asyncio.StreamReader):
        """Initialize the frame reader over the given stream reader."""
        self._reader = reader
        self._buffer = b""

    def _find_magic(self, data: bytes) -> int:
        """Find magic bytes in data, preferring normal magic."""
        idx_normal = data.find(MAGIC_BYTES)
        idx_rev = data.find(MAGIC_REV)

        if idx_normal < 0 and idx_rev < 0:
            return -1
        if idx_rev < 0:
            return idx_normal
        if idx_normal < 0:
            return idx_rev
        return min(idx_normal, idx_rev)

    def _find_frame_end(self, data: bytes, start: int) -> int | None:
        """Find end of frame starting at offset in data.

        Returns absolute offset of end-of-frame or None if incomplete.
        """
        if start + HEADER_20 > len(data):
            return None

        msg_class = _struct.unpack_from("<H", data, start + 18)[0]
        header_size = HEADER_24 if _header_has_payload_offset(msg_class) else HEADER_20
        if start + header_size > len(data):
            return None

        body_len = _struct.unpack_from("<I", data, start + 8)[0]
        if body_len > MAX_FRAME_BYTES:
            raise ReolinkError(f"Baichuan frame exceeds {MAX_FRAME_BYTES} bytes")
        total = header_size + body_len

        if start + total > len(data):
            return None
        return start + total

    async def _iter_frames(self) -> AsyncIterator[tuple[int, int, int, int, bytes]]:
        """Yield ``(cmd_id, msg_num, response_code, payload_offset, body)`` as
        frames arrive.

        The ``msg_num`` is read from the frame header so request/response
        waiters can correlate a response to the exact command that produced it
        (concurrent commands sharing a ``cmd_id`` would otherwise be
        indistinguishable).

        NOTE: the consumed bytes are removed from ``self._buffer`` *before* the
        ``yield`` so a suspended generator never sees stale frame bytes at the
        front of the shared ``self._buffer`` on resume.
        """
        while True:
            data = await self._reader.read(4096)
            if not data:
                logger.debug("TCP connection closed by peer")
                break

            self._buffer += data

            while True:
                magic_idx = self._find_magic(self._buffer)
                if magic_idx < 0:
                    # Keep last 3 bytes in case they start a magic prefix
                    if len(self._buffer) > 3:
                        self._buffer = self._buffer[-3:]
                    break

                frame_end = self._find_frame_end(self._buffer, magic_idx)
                if frame_end is None:
                    break

                # Extract frame
                frame_data = self._buffer[magic_idx:frame_end]
                cmd_id = _struct.unpack_from("<I", frame_data, 4)[0]
                resp_code = _struct.unpack_from("<H", frame_data, 16)[0]
                body_len = _struct.unpack_from("<I", frame_data, 8)[0]
                msg_class = _struct.unpack_from("<H", frame_data, 18)[0]
                msg_num = _struct.unpack_from("<H", frame_data, 14)[0]
                header_size = HEADER_24 if _header_has_payload_offset(msg_class) else HEADER_20
                payload_offset = (
                    _struct.unpack_from("<I", frame_data, 20)[0] if header_size == HEADER_24 else 0
                )
                body = frame_data[header_size : header_size + body_len]

                # Consume the parsed frame BEFORE yielding so a suspended
                # generator resumes with a clean buffer.
                self._buffer = self._buffer[frame_end:]

                yield cmd_id, msg_num, resp_code, payload_offset, body

    async def iter_frames(self) -> AsyncIterator[tuple[int, int, int, int, bytes]]:
        """Continuously yield ``(cmd_id, msg_num, response_code, payload_offset, body)``.

        Unlike the single-read helpers, this iterator yields every frame as it
        arrives, consuming the stream continuously. It is intended for a single
        dedicated consumer (the frame dispatcher) so that no other component
        races on the shared buffer.
        """
        async for cmd_id, msg_num, resp_code, payload_offset, body in self._iter_frames():
            yield cmd_id, msg_num, resp_code, payload_offset, body


# ── Frame Dispatcher ──────────────────────────────────────────────────


class BaichuanFrameDispatcher:
    """Single background consumer of the TCP frame stream.

    The camera continuously pushes frames (e.g. alarm events, snapshot JPEG
    chunks) over the same TCP connection used for request/response. Having
    multiple concurrent readers on the shared buffer causes frames to be
    stolen or dropped (especially alarm pushes during a command's response
    window). This dispatcher is the *only* consumer: it reads every frame and
    routes it to either:

    - a matching request/response waiter (by ``cmd_id``), or
    - the push-event queue (alarm/battery frames).

    All request/response reads and event reads go through this single task.
    """

    def __init__(self, frame_reader: BaichuanFrameReader):
        """Initialize the dispatcher over a frame reader, with waiter and event
        queues."""
        self._frame_reader = frame_reader
        self._lock = asyncio.Lock()
        self._waiters: list[
            tuple[
                Callable[[int, int], bool],
                asyncio.Future | asyncio.Queue[tuple[int, int, int, bytes] | None],
            ]
        ] = []
        self._event_queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(maxsize=256)
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        """Start the background dispatch loop."""
        if self._running or self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._dispatch_loop(),
            name="reolink:frame-dispatcher",
        )

    @property
    def running(self) -> bool:
        """Return whether the dispatcher is actively consuming the socket."""
        return self._running and self._task is not None and not self._task.done()

    async def stop(self) -> None:
        """Stop the background dispatch loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _dispatch_loop(self) -> None:
        """Read frames and route them to waiters or the event queue."""
        try:
            async for (
                cmd_id,
                msg_num,
                resp_code,
                payload_offset,
                body,
            ) in self._frame_reader.iter_frames():
                # 1. Resolve a matching request/response waiter (priority).
                resolved = False
                async with self._lock:
                    for i, (pred, target) in enumerate(self._waiters):
                        # Queue-backed waiters are continuous consumers: they
                        # stay registered across frames, buffering matches.
                        if isinstance(target, asyncio.Queue):
                            if pred(cmd_id, msg_num):
                                try:
                                    target.put_nowait((cmd_id, resp_code, payload_offset, body))
                                except asyncio.QueueFull:
                                    pass  # slow consumer; drop rather than block
                                resolved = True
                                break
                            continue
                        if target.done():
                            continue
                        if pred(cmd_id, msg_num):
                            self._waiters.pop(i)
                            target.set_result((cmd_id, resp_code, payload_offset, body))
                            resolved = True
                            break
                if resolved:
                    continue

                # 2. Route push-event frames to the event queue.
                if cmd_id in (
                    BC_CMD_ID_ALARM_EVENT_LIST,
                    BC_CMD_ID_BATTERY_STATUS,
                ):
                    await self._event_queue.put((cmd_id, body))
        except asyncio.CancelledError:
            logger.debug("Frame dispatcher cancelled")
        except Exception as exc:
            logger.debug("Frame dispatcher stopped: %s", exc)
        finally:
            self._running = False
            async with self._lock:
                for _, target in self._waiters:
                    if isinstance(target, asyncio.Future):
                        if not target.done():
                            target.set_exception(ConnectionError("Baichuan connection closed"))
                    else:
                        # Signal continuous consumers to stop.
                        try:
                            target.put_nowait(None)
                        except asyncio.QueueFull:
                            pass
                self._waiters.clear()
            try:
                self._event_queue.put_nowait(None)
            except asyncio.QueueFull:
                self._event_queue.get_nowait()
                self._event_queue.put_nowait(None)

    async def request(
        self,
        cmd_id: int,
        *,
        timeout: float,
        predicate: Callable[[int, int], bool] | None = None,
        send: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[int, int, int, bytes]:
        """Wait for the next frame matching ``cmd_id``.

        The waiter is registered *before* ``send`` is awaited, so a fast
        response arriving right after the request is not dropped by the
        dispatcher (which owns the socket read).

        ``predicate`` receives ``(cmd_id, msg_num)`` and defaults to a match
        on ``cmd_id`` alone. Pass a ``msg_num``-aware predicate to correlate a
        response to the exact command that produced it.

        Returns ``(cmd_id, response_code, payload_offset, body)``.
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        match = predicate or (lambda c, m: c == cmd_id)

        async with self._lock:
            self._waiters.append((match, fut))

        try:
            if send is not None:
                await send()
            return await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            # Remove the waiter on timeout OR send failure so a late frame
            # is never misrouted to a stale waiter.
            async with self._lock:
                for i, (_, f) in enumerate(self._waiters):
                    if f is fut:
                        self._waiters.pop(i)
                        break
            raise

    async def iter_matching(
        self,
        cmd_id: int,
        *,
        timeout: float,
        predicate: Callable[[int, int], bool] | None = None,
        send: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[tuple[int, int, int, bytes]]:
        """Continuously yield frames matching ``cmd_id`` without registration gaps.

        Unlike :meth:`request` (which registers a single-frame waiter), this
        registers a persistent queue-backed waiter for the lifetime of the
        iteration. Frames that arrive while the caller processes the previous
        frame are buffered instead of dropped, so bursty responses (e.g.
        binary JPEG snapshot chunks) are never lost between reads.

        ``send`` is awaited once, right after registration and before any frame
        is read. Yields ``(cmd_id, response_code, payload_offset, body)``.
        """
        queue: asyncio.Queue[tuple[int, int, int, bytes] | None] = asyncio.Queue()
        match = predicate or (lambda c, m: c == cmd_id)
        loop = asyncio.get_event_loop()

        async with self._lock:
            self._waiters.append((match, queue))

        try:
            if send is not None:
                await send()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                try:
                    frame = await asyncio.wait_for(
                        queue.get(),
                        timeout=min(1.0, deadline - loop.time()),
                    )
                except asyncio.TimeoutError:
                    continue
                if frame is None:
                    break
                yield frame
        finally:
            # Remove this continuous waiter so a late frame is never
            # buffered into a finished iterator.
            async with self._lock:
                for i, (_, target) in enumerate(self._waiters):
                    if target is queue:
                        self._waiters.pop(i)
                        break

    async def events(self) -> AsyncIterator[tuple[int, bytes]]:
        """Continuously yield push-event frames routed by the dispatcher."""
        while self._running or not self._event_queue.empty():
            try:
                item = await self._event_queue.get()
            except asyncio.CancelledError:
                return
            if item is None:
                return
            cmd_id, body = item
            yield cmd_id, body


# ── Main Client ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class StreamUrlInfo:
    """Parsed stream URL discovery result."""

    main_stream_url: str = ""
    sub_stream_url: str = ""
    snapshot_url: str = ""
    protocol: str = "rtsp"
    success: bool = False
    error: str = ""
    streams: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class ReolinkDeviceInfo:
    """Basic device information from login/response."""

    token: str = ""
    mac_address: str = ""
    model: str = ""
    firmware_version: str = ""
    channel_count: int = 1


class BaichuanApiClient:
    """Baichuan protocol client using raw TCP over port 9000.

    Implements the full binary protocol:
    - Two-step login handshake with encryption negotiation and fallback
    - Frame-based request/response with msgNum correlation
    - Push-based event frame parsing with state tracking
    - Snapshot capture with binary chunk handling
    - Keepalive pings
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        api_port: int = 9000,
        timeout: float = 10.0,
    ):
        """Initialize the client with connection parameters and reset state."""
        self.host = host
        self.username = username
        self.password = password
        self.api_port = api_port
        self.timeout = timeout

        # Connection state
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._frame_reader: BaichuanFrameReader | None = None
        self._dispatcher: BaichuanFrameDispatcher | None = None
        self._token: str = ""
        self._nonce: str = ""
        self._use_aes: bool = False
        self._info: ReolinkDeviceInfo | None = None
        self._msg_num: int = 0
        self._connected: bool = False
        self._host_channel_id: int = 250  # Default, may change to 0 on retry
        self._snapshot_lock = asyncio.Lock()

        # Event state tracking (per-channel, for transition detection)
        self._alarm_event_state: dict[int, dict[str, Any]] = defaultdict(dict)

    @property
    def token(self) -> str:
        """Return the negotiated session token."""
        return self._token

    @property
    def authenticated(self) -> bool:
        """Return True if connected and a session token has been negotiated."""
        writer_open = (
            self._writer is not None and not getattr(self._writer, "is_closing", lambda: False)()
        )
        dispatcher_running = self._dispatcher is not None and bool(
            getattr(self._dispatcher, "running", True)
        )
        return self._connected and bool(self._token) and writer_open and dispatcher_running

    @property
    def host_channel_id(self) -> int:
        """The channel id negotiated for this connection (BC cipher offset)."""
        return self._host_channel_id

    @property
    def nonce(self) -> str:
        """The nonce negotiated during login (used for AES key derivation)."""
        return self._nonce

    @property
    def use_aes(self) -> bool:
        """Whether the connection uses AES-128-CFB encryption (vs. XOR BC)."""
        return self._use_aes

    @property
    def decryption_params(self) -> dict[str, object]:
        """Parameters needed to decrypt incoming event bodies."""
        return {
            "nonce": self._nonce,
            "password": self.password,
            "use_aes": self._use_aes,
            "channel": self._host_channel_id,
        }

    async def connect(self) -> None:
        """Open TCP connection to the camera."""
        logger.info("Connecting to %s:%d", self.host, self.api_port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.api_port),
            timeout=self.timeout,
        )
        self._frame_reader = BaichuanFrameReader(self._reader)
        # Single-consumer dispatcher owns all frame reads going forward, so
        # the push-event loop and request/response reads never race on the
        # shared buffer.
        if self._dispatcher is not None:
            await self._dispatcher.stop()
        self._dispatcher = BaichuanFrameDispatcher(self._frame_reader)
        self._dispatcher.start()
        self._connected = True
        # Enable TCP keep-alive at OS level
        try:
            sock = self._reader.transport.get_extra_info("socket")
            if sock:
                sock.setsockopt(sock.SOL_SOCKET, sock.SO_KEEPALIVE, 1)
        except Exception:
            pass
        logger.info("TCP connection established to %s:%d", self.host, self.api_port)

    async def close(self) -> None:
        """Close the TCP connection and send logout."""
        try:
            if self._writer and not self._writer.is_closing():
                await self._do_logout()
        except Exception as exc:
            logger.debug("Logout error on close: %s", exc)
        finally:
            if self._writer:
                self._writer.close()
                try:
                    await self._writer.wait_closed()
                except Exception:
                    pass
            self._writer = None
            self._reader = None
            self._frame_reader = None
            if self._dispatcher is not None:
                await self._dispatcher.stop()
                self._dispatcher = None
            self._connected = False
            self._token = ""
            self._nonce = ""
            logger.debug("Connection closed for %s:%d", self.host, self.api_port)

    async def _reset_connection(self) -> None:
        """Force-close the current TCP connection without sending logout.

        Used on login retry after a connection reset: the socket may already
        be dead, so we must not attempt a logout exchange. Resets all socket
        state so the next attempt starts fresh.
        """
        if self._writer:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=2.0)
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._frame_reader = None
        if self._dispatcher is not None:
            await self._dispatcher.stop()
            self._dispatcher = None
        self._connected = False
        self._token = ""
        self._nonce = ""

    def _next_msg_num(self) -> int:
        """Generate next message number."""
        self._msg_num = (self._msg_num + 1) & 0xFFFF
        return self._msg_num

    def _encrypt_command_body(self, extension_xml: str, payload_xml: str) -> tuple[bytes, int]:
        """Encrypt extension+payload into a single command body.

        The body layout for modern (24-byte) frames is ``[extension][payload]``,
        with ``payloadOffset`` pointing at the start of the payload. Each part
        is encrypted with the negotiated mode (AES or BC).

        Returns ``(body, payload_offset)``.
        """
        channel_id = self._host_channel_id
        ext_bytes = extension_xml.encode("utf-8")
        payload_bytes = payload_xml.encode("utf-8")
        if self._use_aes:
            key = derive_aes_key(self._nonce, self.password)
            enc_ext = aes_encrypt_cfb(ext_bytes, key)
            enc_payload = aes_encrypt_cfb(payload_bytes, key)
        else:
            enc_ext = bc_encrypt(ext_bytes, channel_id)
            enc_payload = bc_encrypt(payload_bytes, channel_id)
        body = enc_ext + enc_payload
        return body, len(enc_ext)

    def _decrypt_response_body(self, body: bytes) -> bytes:
        """Decrypt a response body using the negotiated encryption mode."""
        channel_id = self._host_channel_id
        if self._use_aes:
            key = derive_aes_key(self._nonce, self.password)
            return aes_decrypt_cfb(body, key)
        return bc_decrypt(body, channel_id)

    def _decrypt_part(self, part: bytes) -> bytes:
        """Decrypt a single extension/payload part using the negotiated mode.

        Extension and payload parts are encrypted as separate AES-CFB streams,
        so each must be decrypted independently with a fresh cipher instance.
        """
        channel_id = self._host_channel_id
        if self._use_aes:
            key = derive_aes_key(self._nonce, self.password)
            return aes_decrypt_cfb(part, key)
        return bc_decrypt(part, channel_id)

    async def _read_command_response(
        self,
        cmd_id: int,
        msg_num: int,
        send: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[int, bytes]:
        """Read the response frame correlating to a sent command.

        If ``send`` is provided, the response waiter is registered *before*
        sending, so a fast response is never dropped. Returns ``(cmd_id, body)``.
        """
        if self._dispatcher is None:
            raise ReolinkError("Frame dispatcher not initialized")
        try:
            resp_cmd, _resp_code, _po, resp_body = await self._dispatcher.request(
                cmd_id,
                timeout=self.timeout,
                send=send,
                # Correlate the response to the exact command: concurrent
                # commands sharing a cmd_id must not steal each other's frames.
                predicate=lambda c, m: c == cmd_id and m == msg_num,
            )
        except asyncio.TimeoutError:
            raise ReolinkError("No response received from camera (timeout)")
        return resp_cmd, resp_body

    async def _send_frame(
        self,
        cmd_id: int,
        body: bytes,
        use_24_header: bool = False,
        **header_kwargs,
    ) -> None:
        """Send a complete frame over TCP."""
        frame = encode_frame(cmd_id, body, use_24_header, **header_kwargs)
        self._writer.write(frame)
        await self._writer.drain()

    async def _do_logout(self) -> None:
        """Send logout with encrypted XML body.

        Logout sends cmdId=2 with the encrypted <Logout> XML.
        """
        try:
            logout_xml = build_logout_xml()
            logout_bytes = logout_xml.encode("utf-8")
            encrypted = bc_encrypt(logout_bytes, self._host_channel_id)

            await self._send_frame(
                BC_CMD_ID_LOGOUT,
                encrypted,
                use_24_header=True,
                channel=self._host_channel_id,
                payload_offset=0,
            )
            # Give camera a moment to respond
            await asyncio.sleep(0.1)
        except Exception:
            pass

    async def _negotiate_encryption(self) -> tuple[str, bool]:
        """Step 1 of login: send header-only frame, receive nonce and encryption type.

        Returns (nonce, use_aes) tuple.
        """
        channel_id = self._host_channel_id
        logger.debug("Step 1: Sending encryption negotiation frame (channel=%d)", channel_id)

        msg_num = self._next_msg_num()

        if self._dispatcher is None:
            raise ReolinkError("Frame dispatcher not initialized")

        nonce = ""
        use_aes = False

        async def _send_negotiation() -> None:
            """Send the legacy-header encryption negotiation request."""
            # Legacy header with full_aes negotiation request
            await self._send_frame(
                BC_CMD_ID_LOGIN,
                b"",
                use_24_header=False,
                msg_num=msg_num,
                channel=channel_id,
                stream_type=0,
                response_code=BC_ENC_FULL_AES,
                message_class=BC_CLASS_LEGACY,
            )

        try:
            # Read the full frame including the header response code. The
            # response code (0xDDxx) carries the negotiated encryption type,
            # so it MUST come from the frame header, not the encrypted body.
            resp_cmd, resp_code, _po, resp_body = await self._dispatcher.request(
                BC_CMD_ID_LOGIN,
                timeout=self.timeout,
                send=_send_negotiation,
            )
            logger.debug(
                "Received encryption negotiation: cmdId=0x%04X bodyLen=%d respCode=0x%04X",
                resp_cmd,
                len(resp_body),
                resp_code,
            )

            # The nonce is BC-encrypted during negotiation
            decrypted = bc_decrypt(resp_body, channel_id)

            root = ET.fromstring(decrypted.rstrip(b"\x00"))

            # Look for nonce in various XML structures
            nonce_elem = root.find("nonce")
            if nonce_elem is not None and nonce_elem.text:
                nonce = nonce_elem.text.strip()
            else:
                enc_elem = root.find("Encryption")
                if enc_elem is not None:
                    nonce_elem = enc_elem.find("nonce")
                    if nonce_elem is not None and nonce_elem.text:
                        nonce = nonce_elem.text.strip()

            # Determine if we should use AES. The response code 0xDDxx means
            # the lower byte encodes the negotiated encryption type:
            #   0x00 none, 0x01 bc, 0x02 aes, 0x12 full_aes
            enc_type = resp_code & 0xFF if resp_code >> 8 == 0xDD else 0
            use_aes = enc_type in (0x02, 0x12)  # AES or full_aes

            logger.debug(
                "Nonce received: %s, use_aes=%s (encType=0x%02X)", nonce, use_aes, enc_type
            )

        except asyncio.TimeoutError:
            logger.warning("Encryption negotiation timed out")
            nonce = md5_str_modern(self.username)[:31]
            use_aes = False
        except Exception as e:
            logger.warning("Encryption negotiation error: %s", e)
            nonce = md5_str_modern(self.username)[:31]
            use_aes = False

        self._nonce = nonce
        self._use_aes = use_aes
        return nonce, use_aes

    async def _perform_login(
        self,
        nonce: str,
        channel_id: int | None = None,
    ) -> ReolinkDeviceInfo:
        """Step 2 of login: send hashed credentials in encrypted XML.

        Login payload is encrypted with BC cipher regardless
        of negotiated encryption type.
        """
        if channel_id is None:
            channel_id = self._host_channel_id

        user_hash = md5_str_modern(self.username + nonce)
        pass_hash = md5_str_modern(self.password + nonce)

        logger.debug("Step 2: Login with nonce=%s...%s", nonce[:8], nonce[-4:])
        logger.debug("  userHash: %s, passHash: %s...", user_hash[:8], pass_hash[:8])

        login_xml = build_login_xml(user_hash, pass_hash)
        login_bytes = login_xml.encode("utf-8")

        # Encrypt with BC cipher using channel_id as offset
        encrypted = bc_encrypt(login_bytes, channel_id)

        msg_num = self._next_msg_num()

        if self._dispatcher is None:
            raise ReolinkLoginError("Login failed: frame dispatcher not initialized")

        async def _send_login() -> None:
            """Send the encrypted login frame."""
            frame = encode_frame(
                BC_CMD_ID_LOGIN,
                encrypted,
                use_24_header=True,
                channel=channel_id,
                stream_type=0,
                msg_num=msg_num,
                response_code=0,
                payload_offset=0,
            )
            if self._writer is None:
                raise ReolinkError("Writer not initialized")
            self._writer.write(frame)
            await self._writer.drain()

        try:
            resp_cmd, resp_code, _po, resp_body = await self._dispatcher.request(
                BC_CMD_ID_LOGIN,
                timeout=self.timeout,
                send=_send_login,
            )
        except asyncio.TimeoutError:
            raise ReolinkLoginError("Login failed: no response from camera (timeout)")

        logger.debug(
            "Login response: cmdId=%d bodyLen=%d respCode=0x%04X",
            resp_cmd,
            len(resp_body),
            resp_code,
        )

        try:
            decrypted = bc_decrypt(resp_body, channel_id)
            root = ET.fromstring(decrypted.rstrip(b"\x00"))
        except Exception as e:
            raise ReolinkLoginError(
                f"Login failed: cannot decrypt/parse response (respCode=0x{resp_code:04X}): {e}"
            )

        # Check for explicit success code first
        code_elem = root.find("code")
        if code_elem is not None:
            code = int(code_elem.text) if code_elem.text else -1
            if code != 0:
                raise ReolinkLoginError(
                    f"Login failed: camera rejected with code={code} (respCode=0x{resp_code:04X})"
                )
            logger.debug("Login successful: got code=0")
            param = root.find("param")
            if param is not None:
                user = param.find("User")
                if user is not None:
                    token_elem = user.find("token")
                    self._token = (
                        token_elem.text if token_elem is not None and token_elem.text else ""
                    )
                    mac_elem = user.find("macAddress")
                    model_elem = user.find("model")
                    fw_elem = user.find("firmwareVersion")
                    ch_elem = user.find("channelNum")
                    self._info = ReolinkDeviceInfo(
                        token=self._token,
                        mac_address=mac_elem.text if mac_elem is not None and mac_elem.text else "",
                        model=model_elem.text if model_elem is not None and model_elem.text else "",
                        firmware_version=fw_elem.text
                        if fw_elem is not None and fw_elem.text
                        else "",
                        channel_count=int(ch_elem.text)
                        if ch_elem is not None and ch_elem.text
                        else 1,
                    )
                    logger.info(
                        "Login successful: model=%s fw=%s channels=%d",
                        self._info.model,
                        self._info.firmware_version,
                        self._info.channel_count,
                    )
                    self._connected = True
                    return self._info

        # Check for token directly
        token_elem = root.find("token")
        if token_elem is not None and token_elem.text:
            self._token = token_elem.text
            self._connected = True
            self._info = ReolinkDeviceInfo(token=self._token)
            logger.info("Login successful: got token")
            return self._info

        # Check for DeviceInfo element
        device_info = root.find("DeviceInfo")
        if device_info is not None:
            model_elem = device_info.find("typeInfo")
            if model_elem is None:
                model_elem = device_info.find("type")
            fw_elem = device_info.find("softVer")
            if fw_elem is None:
                fw_elem = device_info.find("firmVersion")
            ch_elem = device_info.find("channelNum")
            secret_elem = device_info.find("secretCode")

            self._token = secret_elem.text if secret_elem is not None and secret_elem.text else ""
            model = model_elem.text if model_elem is not None and model_elem.text else ""
            fw = fw_elem.text if fw_elem is not None and fw_elem.text else ""
            ch = int(ch_elem.text) if ch_elem is not None and ch_elem.text else 1

            self._info = ReolinkDeviceInfo(
                token=self._token,
                model=model,
                firmware_version=fw,
                channel_count=ch,
            )
            logger.info("Login successful: model=%s fw=%s channels=%d", model, fw, ch)
            self._connected = True
            return self._info

        # Final fallback: valid XML response = login success
        logger.debug("Login response XML root tags: %s", [child.tag for child in root])
        self._connected = True
        self._info = ReolinkDeviceInfo(token=self._token)
        logger.info("Login successful (generic XML response)")
        return self._info

    async def _do_login_with_channel_id(self, channel_id: int) -> ReolinkDeviceInfo:
        """Perform full login flow with a specific channel ID."""
        logger.debug("Attempting login with channelId=%d", channel_id)

        # Step 1: Encryption negotiation
        nonce, use_aes = await self._negotiate_encryption()

        # Step 2: Credential login
        info = await self._perform_login(nonce, channel_id=channel_id)
        return info

    async def login(self) -> ReolinkDeviceInfo:
        """Full two-step login handshake with retries and channel ID fallback.

        Some cameras intermittently reset connections during login. We retry
        with backoff and a fresh connection to ride out transient resets.
        Also tries channelId=250 first, falling back to channelId=0.
        """
        max_attempts = 4
        last_error: Exception | None = None

        # Ordered channel IDs to try (deduplicated)
        channel_ids = [self._host_channel_id]
        alt = 0 if self._host_channel_id == 250 else 250
        if alt not in channel_ids:
            channel_ids.append(alt)

        logger.info("Starting Baichuan login for %s (host=%s)", self.username, self.host)

        for attempt in range(1, max_attempts + 1):
            for channel_id in channel_ids:
                self._host_channel_id = channel_id
                try:
                    connection_alive = (
                        self._connected
                        and self._writer is not None
                        and not self._writer.is_closing()
                        and self._dispatcher is not None
                        and self._dispatcher.running
                    )
                    if not connection_alive:
                        if self._connected:
                            await self._reset_connection()
                        await self.connect()
                    info = await self._do_login_with_channel_id(channel_id)
                    logger.info("Login succeeded with channelId=%d", channel_id)
                    return info
                except Exception as e:
                    last_error = e
                    logger.debug(
                        "Login attempt %d (channelId=%d) failed: %s",
                        attempt,
                        channel_id,
                        e,
                    )
                    # Reset the socket so the next attempt starts clean
                    await self._reset_connection()

            if attempt < max_attempts:
                logger.debug("Login retry %d/%d after backoff", attempt, max_attempts)
                await asyncio.sleep(1.5)  # backoff for camera to recover

        raise ReolinkLoginError(
            f"Login failed after {max_attempts} attempts across channel IDs "
            f"{channel_ids}: {last_error}"
        )

    async def _run_with_retry(self, func, max_retries: int = 3, backoff: float = 1.5):
        """Run a command coroutine with retry + re-login on transient failures.

        Some cameras intermittently fail to respond to read-only commands
        (e.g. due to push-frame flooding). On failure we reset the connection
        and re-login before retrying, which is safe for idempotent reads.

        ``func`` is a zero-arg async callable. If it returns an object with a
        ``success`` attribute, that flag is honored (e.g. ``StreamUrlInfo``).
        """
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                if not self.authenticated:
                    await self.login()
                result = await func()
                if hasattr(result, "success") and not result.success:
                    err = getattr(result, "error", "") or "command failed"
                    raise ReolinkError(err)
                return result
            except Exception as e:
                last_error = e
                logger.debug("Command attempt %d/%d failed: %s", attempt, max_retries, e)
                await self._reset_connection()
                if attempt < max_retries:
                    await asyncio.sleep(backoff)
        raise last_error  # type: ignore[misc]

    async def subscribe_events(self) -> bool:
        """Subscribe to push events so the camera will send alarm frames.

        Reolink cameras only push alarm-event frames (cmdId=33) after a client
        explicitly subscribes via cmdId=31 with an *empty* body. Without this
        the camera stays silent and no events reach the application.

        The message uses a modern 24-byte header with ``BC_CLASS_MODERN_24``
        and an empty payload, and we try a few channel-ID variants
        (0-based camera channel, then 251/250 push/host channels) until
        the camera accepts with responseCode 200.

        Returns True if the camera accepted the subscription.
        """
        if not self.authenticated:
            raise ReolinkError("Not authenticated")
        if self._dispatcher is None:
            raise ReolinkError("Frame dispatcher not initialized")

        base_channel = 0
        channel_variants: list[int] = []
        for candidate in (base_channel, 251, 250):
            if candidate not in channel_variants:
                channel_variants.append(candidate)

        last_code: int | None = None
        for channel_id in channel_variants:
            msg_num = self._next_msg_num()

            async def _send_subscribe(channel_id: int = channel_id) -> None:
                """Send the cmdId=31 subscription frame with empty body."""
                await self._send_frame(
                    BC_CMD_ID_SUBSCRIBE_EVENTS,
                    b"",
                    use_24_header=True,
                    channel=channel_id,
                    msg_num=msg_num,
                    message_class=BC_CLASS_MODERN_24,
                )

            try:
                _cmd_id, resp_code, _po, _body = await self._dispatcher.request(
                    BC_CMD_ID_SUBSCRIBE_EVENTS,
                    timeout=self.timeout,
                    send=_send_subscribe,
                )
                last_code = resp_code
                if resp_code in (0, 200):
                    logger.info(
                        "Reolink subscribed to events (channelId=%d)",
                        channel_id,
                    )
                    return True
                logger.debug(
                    "Reolink subscribe rejected (channelId=%d) responseCode=%s",
                    channel_id,
                    resp_code,
                )
            except asyncio.TimeoutError:
                logger.debug(
                    "Reolink subscribe timed out (channelId=%d)",
                    channel_id,
                )
            except ReolinkError:
                logger.debug(
                    "Reolink subscribe error (channelId=%d)",
                    channel_id,
                )
            except Exception as exc:
                logger.debug(
                    "Reolink subscribe failed (channelId=%d): %s",
                    channel_id,
                    exc,
                )

        logger.warning(
            "Reolink subscribe events failed: camera rejected cmdId=31 (last responseCode=%s)",
            last_code,
        )
        return False

    async def ping(self) -> bool:
        """Send a keepalive ping (cmdId=93, header-only / empty body).

        Periodic pings keep the authenticated TCP session (and its event
        subscription) alive on firmwares that would otherwise idle it out.
        """
        if not self.authenticated:
            return False
        msg_num = self._next_msg_num()

        async def _send_ping() -> None:
            """Send the cmdId=93 ping frame with an empty body."""
            await self._send_frame(
                BC_CMD_ID_PING,
                b"",
                use_24_header=True,
                channel=self._host_channel_id,
                msg_num=msg_num,
                message_class=BC_CLASS_MODERN_24,
            )

        try:
            _cmd_id, resp_code, _po, _body = await self._dispatcher.request(
                BC_CMD_ID_PING,
                timeout=self.timeout,
                send=_send_ping,
            )
            return resp_code in (0, 200)
        except (asyncio.TimeoutError, ReolinkError, Exception):
            logger.debug("Reolink ping failed (no response)")
            return False

    async def get_stream_url(self, channel: int = 0) -> StreamUrlInfo:
        """Request stream info via <StreamInfoList> command (cmdId=146).

        For a standalone camera the request carries a channel <Extension> in
        the extension slot (empty payload) and a header channelId of
        ``channel + 1``.
        """

        # Standalone cameras expect the header channelId to be channel + 1,
        # with the channel selected via the Extension XML.
        async def _do():
            """Discover the stream URL for the given channel."""
            if not self.authenticated:
                raise ReolinkError("Not authenticated")
            header_channel_id = channel + 1
            ext_xml = build_channel_extension_xml(channel)
            body, payload_offset = self._encrypt_command_body(ext_xml, "")
            return await self._get_stream_url_impl(
                channel, header_channel_id, ext_xml, body, payload_offset
            )

        return await self._run_with_retry(_do)

    async def _get_stream_url_impl(
        self, channel, header_channel_id, ext_xml, body, payload_offset
    ) -> StreamUrlInfo:
        """Send the stream-info request and parse the returned stream URL."""
        try:
            msg_num = self._next_msg_num()

            async def _send_stream() -> None:
                """Send the stream info list request frame."""
                await self._send_frame(
                    BC_CMD_ID_STREAM_INFO_LIST,
                    body,
                    use_24_header=True,
                    channel=header_channel_id,
                    msg_num=msg_num,
                    payload_offset=payload_offset,
                )

            resp_cmd, resp_body = await self._read_command_response(
                BC_CMD_ID_STREAM_INFO_LIST,
                msg_num,
                send=_send_stream,
            )

            try:
                decrypted = self._decrypt_response_body(resp_body)
                root = ET.fromstring(decrypted)

                stream_list = root.find("StreamInfoList")
                if stream_list is None:
                    body = root.find("body")
                    if body is not None:
                        stream_list = body.find("StreamInfoList")

                if stream_list is None:
                    logger.warning("No StreamInfoList in response")
                    return StreamUrlInfo(success=False, error="No StreamInfoList in response")

                streams = []
                for si in stream_list.findall("StreamInfo"):
                    enc_tables = []
                    for enc in si.findall("encodeTable"):
                        table = {}
                        width = enc.find("width")
                        height = enc.find("height")
                        fr = enc.find("framerateTable")
                        br = enc.find("bitrateTable")
                        vtype = enc.find("videoEncType")
                        if width is not None and width.text:
                            table["width"] = int(width.text)
                        if height is not None and height.text:
                            table["height"] = int(height.text)
                        if fr is not None and fr.text:
                            table["framerate"] = [int(x) for x in fr.text.split(",") if x.strip()]
                        if br is not None and br.text:
                            table["bitrate"] = [int(x) for x in br.text.split(",") if x.strip()]
                        if vtype is not None and vtype.text:
                            table["videoEncType"] = int(vtype.text)
                        type_tag = enc.find("type")
                        if type_tag is not None and type_tag.text:
                            table["type"] = type_tag.text.strip()
                        enc_tables.append(table)
                    streams.append({"encodeTables": enc_tables})

                if streams and streams[0].get("encodeTables"):
                    first_table = streams[0]["encodeTables"][0]
                    logger.info(
                        "Stream info: %d streams, first table: %s", len(streams), first_table
                    )
                    channel_number = channel + 1
                    stream_base = f"rtsp://{self.host}:554/Preview_{channel_number:02d}"
                    return StreamUrlInfo(
                        main_stream_url=f"{stream_base}_main",
                        sub_stream_url=f"{stream_base}_sub",
                        success=True,
                        streams=streams,
                    )
                else:
                    return StreamUrlInfo(success=False, error="No encode tables found")
            except ET.ParseError as e:
                logger.warning("Failed to parse stream URL response: %s", e)
                return StreamUrlInfo(success=False, error=f"Parse error: {e}")
        except Exception as e:
            logger.warning("Error getting stream URL: %s", e)
            return StreamUrlInfo(success=False, error=str(e))

    async def get_device_info(self) -> ReolinkDeviceInfo:
        """Query device version and capability info (retried on failure)."""
        return await self._run_with_retry(self._get_device_info_impl)

    async def _get_device_info_impl(self) -> ReolinkDeviceInfo:
        """Query device version and capability info (single attempt)."""
        if not self.authenticated:
            raise ReolinkError("Not authenticated")

        info = self._info or ReolinkDeviceInfo()
        channel_id = self._host_channel_id

        try:
            # VersionInfo (cmdId=80) is device-global: sends an
            # empty body with the host channelId in the header.
            body, payload_offset = self._encrypt_command_body("", "")
            msg_num = self._next_msg_num()

            async def _send_version() -> None:
                """Send the version info query frame."""
                await self._send_frame(
                    BC_CMD_ID_VERSION_INFO,
                    body,
                    use_24_header=True,
                    channel=channel_id,
                    msg_num=msg_num,
                    payload_offset=payload_offset,
                )

            resp_cmd, resp_body = await self._read_command_response(
                BC_CMD_ID_VERSION_INFO,
                msg_num,
                send=_send_version,
            )
            decrypted = self._decrypt_response_body(resp_body)
            root = ET.fromstring(decrypted)

            param = root.find("param")
            if param is not None:
                ver_elem = param.find("VersionInfo")
                if ver_elem is not None:
                    ver = {}
                    for child in ver_elem:
                        if child.text:
                            ver[child.tag] = child.text
                    if "model" in ver:
                        info = ReolinkDeviceInfo(
                            token=info.token,
                            model=ver.get("model", ""),
                            firmware_version=ver.get("firmware", ""),
                        )
        except Exception as exc:
            logger.debug("VersionInfo query failed: %s", exc)

        return info

    async def get_ability_info(self) -> dict[str, Any]:
        """Query device capabilities via <Extension> (cmdId=151).

        Sends the AbilityInfo request with the ability <Extension> XML in the
        extension slot (no payload).
        Retried on transient failure.
        """

        async def _do():
            """Query the device ability info."""
            if not self.authenticated:
                raise ReolinkError("Not authenticated")
            ext_xml = build_ability_info_xml(self.username)
            return await self.send_command(
                BC_CMD_ID_ABILITY_INFO,
                "",
                extension_xml=ext_xml,
            )

        return await self._run_with_retry(_do)

    async def get_snapshot(self, channel: int = 0) -> bytes | None:
        """Request a snapshot image from the camera (retried on failure).

        Handles the push-based binary JPEG response flow:
        1. Request: encrypted <Extension> + <Snap> payload (negotiated AES/BC)
        2. Response: XML acknowledgment followed by binary JPEG chunks with
           <binaryData>1</binaryData> in the extension. The binary payload is
           decrypted with the negotiated encryption mode.
        """

        async def _do():
            """Fetch a snapshot, retrying on transient failures."""
            return await self._get_snapshot_impl(channel)

        async with self._snapshot_lock:
            return await self._run_with_retry(_do)

    async def _get_snapshot_impl(self, channel: int = 0) -> bytes | None:
        """Request a snapshot image (single attempt)."""
        if not self.authenticated:
            raise ReolinkError("Not authenticated")

        # Standalone cameras expect the header channelId to be channel + 1,
        # with the channel selected via the Extension XML.
        header_channel_id = channel + 1
        ext_xml = build_channel_extension_xml(channel)
        snap_xml = build_snapshot_xml(channel)
        body, payload_offset = self._encrypt_command_body(ext_xml, snap_xml)

        msg_num = self._next_msg_num()

        if self._dispatcher is None:
            raise ReolinkError("Frame dispatcher not initialized")

        async def _send_snapshot() -> None:
            """Send the snapshot request frame."""
            await self._send_frame(
                BC_CMD_ID_SNAPSHOT,
                body,
                use_24_header=True,
                channel=header_channel_id,
                msg_num=msg_num,
                payload_offset=payload_offset,
            )

        # Collect binary JPEG chunks from push frames
        chunks: list[bytes] = []
        timeout_at = asyncio.get_event_loop().time() + self.timeout
        soi_found = False

        def _find_soi(buf: bytes) -> int:
            """Find the JPEG start-of-image marker (FF D8)."""
            # JPEG SOI: FF D8
            for i in range(len(buf) - 1):
                if buf[i] == 0xFF and buf[i + 1] == 0xD8:
                    return i
            return -1

        def _has_eoi(buf: bytes) -> bool:
            """Return True if the buffer contains a JPEG end-of-image marker."""
            # JPEG EOI: FF D9
            for i in range(len(buf) - 1):
                if buf[i] == 0xFF and buf[i + 1] == 0xD9:
                    return True
            return False

        # Register a single continuous waiter for the whole capture window so
        # binary JPEG chunks arriving between frames are buffered, not dropped
        # by the dispatcher. The request is sent once, right after registration.
        remaining = timeout_at - asyncio.get_event_loop().time()
        async for resp_cmd, resp_code, payload_offset, resp_body in self._dispatcher.iter_matching(
            BC_CMD_ID_SNAPSHOT,
            timeout=remaining,
            send=_send_snapshot,
        ):
            if resp_cmd != BC_CMD_ID_SNAPSHOT:
                continue

            # Request rejected (some firmwares reply with an empty error body)
            if resp_code >= 400:
                logger.warning("Snapshot request rejected: responseCode=%d", resp_code)
                break

            # The body layout is [encrypted extension][encrypted payload],
            # where payloadOffset marks the start of the payload. The extension
            # and payload are encrypted as SEPARATE AES-CFB streams, so they
            # must be decrypted independently (decrypting the whole body as one
            # stream would corrupt the payload).
            enc_ext = resp_body[:payload_offset]
            enc_payload = resp_body[payload_offset:]

            # Decrypt the extension to detect the <binaryData> marker
            try:
                ext_part = self._decrypt_part(enc_ext)
            except Exception:
                ext_part = enc_ext

            is_binary = b"<binaryData>1</binaryData>" in ext_part

            # If not marked as binary, it may be the XML ack frame.
            if not is_binary:
                # Skip the initial XML acknowledgment (no JPEG payload)
                continue

            # Decrypt the payload as its own AES-CFB stream
            try:
                payload_part = self._decrypt_part(enc_payload)
            except Exception:
                payload_part = enc_payload

            # Binary chunk: the payload holds the JPEG data
            if not soi_found:
                soi = _find_soi(payload_part)
                if soi >= 0:
                    soi_found = True
                    payload_part = payload_part[soi:]
                else:
                    # Not JPEG yet; wait for the next binary chunk
                    continue

            chunks.append(payload_part)
            if _has_eoi(payload_part):
                break  # Complete JPEG received

        if chunks:
            jpeg = b"".join(chunks)
            # Ensure we have a complete JPEG
            if len(jpeg) >= 2 and jpeg[0] == 0xFF and jpeg[1] == 0xD8:
                if not jpeg.endswith(b"\xff\xd9"):
                    jpeg += b"\xff\xd9"
                logger.info("Snapshot captured: %d bytes", len(jpeg))
                return jpeg

        logger.warning("Snapshot capture failed: no JPEG data received")
        return None

    async def send_command(
        self,
        cmd_id: int,
        xml_payload: str,
        decrypt_response: bool = True,
        channel_id: int | None = None,
        extension_xml: str = "",
        channel: int | None = None,
    ) -> dict[str, Any]:
        """Send an arbitrary Baichuan command and parse response.

        Supports the extension+payload body structure used by most commands:
        the ``extension_xml`` (e.g. ``<Extension>`` with a channelId) is
        placed in the extension slot and ``payload_xml`` in the payload slot,
        with the correct ``payloadOffset`` in the header.

        Encryption follows the negotiated mode (AES or BC).
        """
        if not self.authenticated:
            raise ReolinkError("Not authenticated")

        header_channel_id = channel_id or self._host_channel_id
        body, payload_offset = self._encrypt_command_body(extension_xml, xml_payload)

        msg_num = self._next_msg_num()

        async def _send_cmd() -> None:
            """Send the command frame for the requested cmd_id."""
            await self._send_frame(
                cmd_id,
                body,
                use_24_header=True,
                channel=header_channel_id,
                msg_num=msg_num,
                payload_offset=payload_offset,
            )

        resp_cmd, resp_body = await self._read_command_response(
            cmd_id,
            msg_num,
            send=_send_cmd,
        )

        if decrypt_response:
            decrypted = self._decrypt_response_body(resp_body)
        else:
            decrypted = resp_body

        return parse_xml_body(decrypted)

    @property
    def event_frame_iterator(self) -> AsyncIterator[tuple[int, bytes]]:
        """Async iterator over incoming push-event frames from the TCP connection.

        Events are routed by the frame dispatcher (which owns the socket read),
        so they are never stolen or dropped by concurrent request/response
        reads.
        """
        if self._dispatcher is None:
            raise ReolinkError("Frame dispatcher not initialized")
        return self._dispatcher.events()


# ── Exceptions ────────────────────────────────────────────────────────


class ReolinkError(Exception):
    """Base exception for Reolink API errors."""


class ReolinkLoginError(ReolinkError):
    """Authentication failure with the device."""


class ReolinkStreamError(ReolinkError):
    """Failure to discover or access a stream URL."""


class ReolinkEventError(ReolinkError):
    """Failure related to event subscription or polling."""
