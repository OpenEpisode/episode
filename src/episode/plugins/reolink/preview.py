"""Baichuan-native live video framing (``cmdId=3`` / ``cmdId=6``).

The camera can stream encoded video over the *same* control socket it uses for login,
events, and snapshots: no RTSP, no second port, no extra credential. What it sends is a
``BcMedia`` byte stream sliced across Baichuan frames, and this module owns the reassembly
of that stream — the pure, I/O-free half of the feature. ``client.py`` owns the socket and
the command exchange; the recorder still owns the bundle the bytes end up in.

Measured rules these types encode, on three firmwares (``tests/fixtures/reolink/``):

* Two frame kinds arrive and **both are required.** A media *start* carries
  ``payloadOffset > 0`` and, when the extension names ``<encryptLen>N</encryptLen>``, its
  first ``N`` payload bytes are ciphered while the remainder is already cleartext. A
  continuation frame has ``payloadOffset == 0`` and must be appended as-is — decrypting one
  corrupts the stream.
* ``payloadOffset > 0`` does **not** imply ``<binaryData>``: two of the three firmwares send
  media starts whose extension names only ``checkPos``/``checkValue``. Treating
  ``binaryData`` as the media marker (which suffices for ``cmdId=109``) would drop frames.
* Whether a packet is split at all is firmware-dependent — one capture had 94 continuation
  frames, two had none — so neither extreme may be assumed.
* A packet declares its own size, is padded to an 8-byte boundary, and a packet start whose
  declared size is impossible must trigger a resync to the next plausible magic rather than
  an exception, because that is what byte-loss recovery has to look like on this transport.
* Stopping with an empty body is rejected (``responseCode=405``) and the camera keeps
  streaming, so ``cmdId=6`` must repeat the ``handle`` from the request.
"""

from __future__ import annotations

import logging
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from episode.plugins.reolink.settings import bounded_float, strict_bool

logger = logging.getLogger(__name__)

#: Request/response bodies. ``main`` uses handle 0 with header streamType 0, ``sub`` uses
#: handle 256 with header streamType 1; both were measured on every field camera.
PREVIEW_STREAMS: dict[str, tuple[int, str, int]] = {
    "main": (0, "mainStream", 0),
    "sub": (256, "subStream", 1),
}

#: One capture's worth of buffered media: larger than any measured keyframe (max 425 KiB)
#: and small enough that a confused camera cannot grow a device's memory.
PREVIEW_MAX_BYTES = 4 * 1024 * 1024

#: Keep diagnostics useful without retaining one tuple for every packet in a long live stream.
PREVIEW_PACKET_SAMPLE_LIMIT = 256

#: Seconds to wait for the first video packet. The measured first keyframe is 157 ms
#: (fw-50332294) to ~1 s on the slowest model, so this is a patient default, not a guess.
DEFAULT_PREVIEW_TIMEOUT = 3.0
PREVIEW_TIMEOUT_BOUNDS = (1.0, 10.0)

#: Seconds of silence after the first packet before a recording burst is considered
#: over. A live native stream is bounded by the episode (the recorder cancels the
#: handler), not by a fixed burst deadline, so this is an idle bound only: a source
#: that stops sending ends the burst, a live one keeps streaming until cancelled.
PREVIEW_IDLE_TIMEOUT_SECONDS = 10.0

#: Priming is off by default. It costs a real camera encode on the same socket as the
#: event pushes, and the plan's abort rule keeps it off until ≥5 measured events show the
#: time-to-first-fragment actually improving.
DEFAULT_PREVIEW_PRIMING = False
DEFAULT_PREVIEW_VARIANT = "main"

IFRAME_FIRST = 0x63643030  # "00dc" .. "09dc"
IFRAME_LAST = 0x63643039
PFRAME_FIRST = 0x63643130  # "10dc" .. "19dc"
PFRAME_LAST = 0x63643139
MAGIC_AAC = 0x62773530  # "05wb"
MAGIC_ADPCM = 0x62773130  # "01wb"
MAGIC_INFO_V1 = 0x31303031  # "1001"
MAGIC_INFO_V2 = 0x32303031  # "2001"

#: HEVC NAL types that, with SPS/PPS, prove an independently decodable picture.
HEVC_IRAP = frozenset({16, 17, 18, 19, 20, 21})
#: H.264 NAL types: 7 = SPS, 5 = IDR.
H264_SPS, H264_IDR = 7, 5

#: Video packet layout: magic(4) + tag(4) + payloadSize(4) + additionalHeaderSize(4) +
#: microseconds(8).
VIDEO_HEADER_BYTES = 24


def packet_kind(magic: int) -> str | None:
    """Classify a little-endian BcMedia magic word, or ``None`` if it is not one."""
    if IFRAME_FIRST <= magic <= IFRAME_LAST:
        return "I"
    if PFRAME_FIRST <= magic <= PFRAME_LAST:
        return "P"
    if magic == MAGIC_AAC:
        return "AAC"
    if magic == MAGIC_ADPCM:
        return "ADPCM"
    if magic == MAGIC_INFO_V1:
        return "InfoV1"
    if magic == MAGIC_INFO_V2:
        return "InfoV2"
    return None


def preview_request_payload(handle: int, stream_name: str, channel: int = 0) -> str:
    """``<Preview>`` body for ``cmdId=3``."""
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n<Preview version="1.0">\n'
        f"<channelId>{channel}</channelId>\n<handle>{handle}</handle>\n"
        f"<streamType>{stream_name}</streamType>\n</Preview>\n</body>\n"
    )


def preview_stop_payload(handle: int, channel: int = 0) -> str:
    """``<Preview>`` body for ``cmdId=6``, repeating the request's ``handle``.

    An empty stop body is rejected (``responseCode=405``) and the camera keeps streaming the
    old profile into whatever reads the socket next, so the handle is not optional here.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n<Preview version="1.0">\n'
        f"<channelId>{channel}</channelId>\n<handle>{handle}</handle>\n</Preview>\n</body>\n"
    )


def parse_encrypt_len(extension: str) -> int | None:
    """``<encryptLen>N</encryptLen>`` from a media frame's extension, else ``None``.

    ``N`` is exactly how many leading payload bytes are ciphered; the rest of that payload is
    already cleartext. Absent (or ``0``) means the media starts in the clear and must not be
    decrypted.
    """
    match = re.search(r"<encryptLen>(\d+)</encryptLen>", extension)
    if not match:
        return None
    return int(match.group(1))


def nal_types(payload: bytes, codec: str = "h265", limit: int = 4096) -> list[int]:
    """NAL types in ``payload``, decoded with the bit layout ``codec`` actually uses.

    HEVC stores the type in bits 1-6 of the first header byte, H.264 in bits 5-7 of its own.
    Reading H.264 with the HEVC shift turns SPS/PPS/IDR (7/8/5) into the meaningless
    51/52/50, which is how "no keyframe" got reported on an H.264 camera once already.
    """
    out: list[int] = []
    pos = 0
    scanned = 0
    while scanned < limit:
        match = re.search(rb"\x00\x00\x00\x01|\x00\x00\x01", payload[pos:])
        if not match:
            break
        index = pos + match.end()
        if index >= len(payload):
            break
        out.append((payload[index] >> 1) & 0x3F if codec == "h265" else payload[index] & 0x1F)
        scanned += 1
        pos = index + 1
    return out


@dataclass
class PreviewStats:
    """What one native preview delivery said, as far as it got."""

    variant: str = "main"
    codec: str = ""
    width: int = 0
    height: int = 0
    info_seen: bool = False
    iframe_count: int = 0
    pframe_count: int = 0
    audio_count: int = 0
    media_bytes: int = 0
    padding_bytes: int = 0
    resyncs: int = 0
    truncated: int = 0
    #: Bytes refused because the capture was already over ``PREVIEW_MAX_BYTES``.
    dropped_bytes: int = 0
    #: Frames the socket layer refused to buffer for us (its own queue was full). Counting
    #: them is what keeps an overloaded consumer from looking like a camera that stopped.
    dropped_frames: int = 0
    first_packet_ms: float | None = None
    first_iframe_ms: float | None = None
    # NAL tallies. The interesting types differ per codec, which is why ``keyframed()`` asks
    # the question instead of callers reading one counter and guessing.
    vps: int = 0
    sps: int = 0
    pps: int = 0
    irap: int = 0
    h264_sps: int = 0
    h264_idr: int = 0
    #: ``(kind, payload_size)`` in arrival order — the same table the fixtures record.
    packets: list[tuple[str, int]] = field(default_factory=list)
    #: Packet records omitted after ``PREVIEW_PACKET_SAMPLE_LIMIT`` was reached.
    packet_samples_omitted: int = 0

    def record_packet(self, kind: str, size: int) -> None:
        """Keep a bounded packet sample while retaining the exact omitted count."""
        if len(self.packets) < PREVIEW_PACKET_SAMPLE_LIMIT:
            self.packets.append((kind, size))
        else:
            self.packet_samples_omitted += 1

    def frames(self) -> int:
        """Picture count: one media packet is one picture on this transport."""
        return self.iframe_count + self.pframe_count

    def keyframed(self) -> bool:
        """True when at least one independently decodable picture was observed."""
        if self.codec == "h265":
            return self.irap > 0 and self.sps > 0 and self.pps > 0
        if self.codec == "h264":
            return self.h264_idr > 0 and self.h264_sps > 0
        return False


class BcMediaWalker:
    """Reassembly of the BcMedia byte stream across Baichuan frames.

    Feed every media byte in arrival order (:meth:`push`); the walker holds the tail of an
    incomplete packet and reports whole packets in :attr:`stats`. A packet start whose
    declared size is impossible triggers a resync to the next plausible magic instead of an
    exception, because that is how byte-loss recovery must behave on a transport with no
    checksum. ``sink`` receives each validated video payload as it is framed, so a live
    consumer sees exactly the bytes the tallies describe and can never disagree with them.
    """

    def __init__(
        self,
        *,
        stats: PreviewStats | None = None,
        max_packet_bytes: int = PREVIEW_MAX_BYTES,
        sink: Callable[[bytes], None] | None = None,
    ) -> None:
        self._buffer = bytearray()
        self._max = max_packet_bytes
        self.stats = stats if stats is not None else PreviewStats()
        self._sink = sink
        self._sink_errors: list[str] = []

    def push(self, data: bytes, *, elapsed_ms: float = 0.0) -> int:
        """Append arrived bytes; return how many complete packets were consumed."""
        if not data:
            return 0
        self._buffer += data
        consumed = 0
        while True:
            before = len(self._buffer)
            if self._take_packet(elapsed_ms) is not None:
                consumed += 1
                continue
            # A resync also shrinks the buffer, so retry rather than giving up on what is
            # left of this feed; otherwise the packet after the garbage would be lost.
            if len(self._buffer) == before:
                return consumed

    def _take_packet(self, elapsed_ms: float) -> tuple[str, int] | None:
        buf = self._buffer
        if len(buf) < 4:
            return None
        magic = struct.unpack_from("<I", buf, 0)[0]
        kind = packet_kind(magic)
        if kind is None:
            self._resync()
            return None
        if kind in ("InfoV1", "InfoV2"):
            if len(buf) < 16:
                return None
            header = struct.unpack_from("<I", buf, 4)[0]
            if len(buf) < max(header, 16):
                return None
            stats = self.stats
            stats.info_seen = True
            stats.width = struct.unpack_from("<I", buf, 8)[0]
            stats.height = struct.unpack_from("<I", buf, 12)[0]
            del buf[: max(header, 16)]
            stats.record_packet(kind, 0)
            return (kind, 0)
        if kind in ("AAC", "ADPCM"):
            # Audio: magic(4) + payloadSize(u16) + payloadSizeB(u16) [+ ADPCM sub-header].
            if len(buf) < 8:
                return None
            size_a = struct.unpack_from("<H", buf, 4)[0]
            size_b = struct.unpack_from("<H", buf, 6)[0]
            if size_a != size_b or size_a == 0 or size_a > self._max:
                self.stats.truncated += 1
                self._resync()
                return None
            total = 8 + size_a
            if len(buf) < total:
                return None
            pad = _padding(size_a)
            if pad and len(buf) < total + pad:
                return None  # padding has not fully arrived yet
            del buf[: total + pad]
            self.stats.audio_count += 1
            self.stats.record_packet(kind, size_a)
            return (kind, size_a)
        if len(buf) < VIDEO_HEADER_BYTES:
            return None
        tag = buf[4:8].decode("ascii", "ignore")
        if tag not in ("H264", "H265"):
            self._resync()
            return None
        payload_size = struct.unpack_from("<I", buf, 8)[0]
        extra = struct.unpack_from("<I", buf, 12)[0]
        total = VIDEO_HEADER_BYTES + extra + payload_size
        if payload_size == 0 or total > self._max:
            self.stats.truncated += 1
            self._resync()
            return None
        if len(buf) < total:
            return None
        payload = bytes(buf[VIDEO_HEADER_BYTES + extra : total])
        pad = _padding(payload_size)
        if pad and len(buf) < total + pad:
            return None
        del buf[: total + pad]
        self._record(kind, tag, payload, payload_size, pad, elapsed_ms)
        return (kind, payload_size)

    def _record(
        self, kind: str, tag: str, payload: bytes, size: int, pad: int, elapsed_ms: float
    ) -> None:
        stats = self.stats
        stats.codec = "h265" if tag == "H265" else "h264"
        stats.media_bytes += size
        stats.padding_bytes += pad
        if kind == "I":
            stats.iframe_count += 1
            if stats.first_iframe_ms is None:
                stats.first_iframe_ms = elapsed_ms
        else:
            stats.pframe_count += 1
        if stats.first_packet_ms is None:
            stats.first_packet_ms = elapsed_ms
        types = nal_types(payload[:4096], stats.codec)
        if stats.codec == "h265":
            stats.vps += types.count(32)
            stats.sps += types.count(33)
            stats.pps += types.count(34)
            stats.irap += sum(1 for nal in types if nal in HEVC_IRAP)
        else:
            stats.h264_sps += types.count(H264_SPS)
            stats.h264_idr += types.count(H264_IDR)
        stats.record_packet(kind, size)
        if self._sink is not None:
            # A consumer that dies (a closed pipe, a cancelled recording) must not stop
            # framing: the tallies are what the next decision is made from.
            try:
                self._sink(payload)
            except Exception as error:  # noqa: BLE001 - a dead sink is recorded, not raised
                if len(self._sink_errors) < 8:
                    self._sink_errors.append(f"{type(error).__name__}: {str(error)[:120]}")

    def _resync(self) -> None:
        """Drop bytes up to the next plausible magic; count it, never raise."""
        buf = self._buffer
        best = -1
        for offset in range(1, len(buf) - 3):
            if packet_kind(struct.unpack_from("<I", buf, offset)[0]) is not None:
                best = offset
                break
        self.stats.resyncs += 1
        if best < 0:
            del buf[: max(len(buf) - 3, 0)]
        else:
            del buf[:best]

    @property
    def sink_errors(self) -> list[str]:
        """Bounded reasons a sink stopped being fed, when one was attached."""
        return list(self._sink_errors)

    def clear_pending(self) -> None:
        """Discard the incomplete packet's bytes; used once the capture's bound is hit."""
        self._buffer.clear()

    @property
    def pending(self) -> int:
        """Bytes held for the current incomplete packet."""
        return len(self._buffer)


def _padding(size: int) -> int:
    """Bytes of 8-byte alignment padding that follow a payload of ``size``."""
    return 0 if size % 8 == 0 else 8 - (size % 8)


#: Annex-B start code an elementary-stream demuxer needs. A raw NAL without one is dropped
#: silently by ffmpeg, which turns a live feed into a bundle with nothing in it.
ANNEXB_START_CODE = b"\x00\x00\x00\x01"


def annexb(payload: bytes) -> bytes:
    """``payload`` with an Annex-B start code in front when it does not already have one.

    The camera is not consistent about including start codes inside a media packet, so the
    check is per-payload rather than once per capture. Counting it (rather than assuming) is
    what keeps a silently-empty recording from being possible.
    """
    if payload.startswith(b"\x00\x00\x00\x01") or payload.startswith(b"\x00\x00\x01"):
        return payload
    return ANNEXB_START_CODE + payload


def media_chunk(
    *, payload_offset: int, extension: str, payload: bytes, decrypt: Callable[[bytes], bytes]
) -> bytes:
    """Media bytes carried by one ``cmdId=3`` frame, cipher handled per the measured rule.

    A continuation frame (``payload_offset == 0``) is returned untouched, whatever the
    extension says — decrypting continuation bytes corrupts the stream. A media start has its
    first ``<encryptLen>N</encryptLen>`` payload bytes decrypted and the remainder kept as-is;
    when the extension carries no ``encryptLen`` the media starts in the clear.
    """
    if payload_offset == 0:
        return payload
    cipher_len = parse_encrypt_len(extension)
    if not cipher_len or cipher_len > len(payload):
        return payload
    return decrypt(payload[:cipher_len]) + payload[cipher_len:]


class PreviewFeed:
    """One capture's worth of framing: frame rules, byte bound, tallies.

    ``max_bytes`` is the buffered bound; pass ``None`` when the bytes are handed straight on
    to a consumer as they are framed (streaming), because then nothing accumulates here and a
    total would only cut a live feed short. ``packet_max`` always applies: one packet claiming
    more than that is a corrupt header, not a big picture.

    Sockets are not involved here on purpose. ``client.py`` hands over what each frame carried
    and this decides what the stream said, which is what makes the measured rules testable
    against committed fixtures and keeps a slow or broken consumer from becoming a protocol
    problem. Buffering is bounded: past ``max_bytes`` further media is refused and counted in
    ``dropped_bytes``, because a camera that keeps streaming into a device that stopped reading
    must not be able to grow that device's memory.
    """

    def __init__(
        self,
        *,
        variant: str = "main",
        max_bytes: int | None = PREVIEW_MAX_BYTES,
        packet_max_bytes: int = PREVIEW_MAX_BYTES,
        sink: Callable[[bytes], None] | None = None,
    ) -> None:
        self.stats = PreviewStats(variant=variant)
        self._max = max_bytes
        self._packet_max = packet_max_bytes
        self._walker = BcMediaWalker(stats=self.stats, max_packet_bytes=packet_max_bytes, sink=sink)
        self._full = False
        self.frames_in = 0
        self.bytes_in = 0
        self.packet_start_frames = 0
        self.continuation_frames = 0

    def push_frame(
        self,
        *,
        payload_offset: int,
        extension: str,
        payload: bytes,
        decrypt: Callable[[bytes], bytes],
        elapsed_ms: float = 0.0,
    ) -> int:
        """Feed one arrived frame; return packets completed by it."""
        self.frames_in += 1
        self.bytes_in += len(payload)
        if payload_offset:
            self.packet_start_frames += 1
        else:
            self.continuation_frames += 1
        if self._full or (self._max is not None and self.stats.media_bytes >= self._max):
            # Refuse before buffering: the bound is what a device's memory is worth, and
            # waiting until a packet has arrived to notice the limit defeats the purpose.
            self._full = True
            self.stats.dropped_bytes += len(payload)
            return 0
        chunk = media_chunk(
            payload_offset=payload_offset, extension=extension, payload=payload, decrypt=decrypt
        )
        consumed = self._walker.push(chunk, elapsed_ms=elapsed_ms)
        if self._max is not None and self.stats.media_bytes > self._max:
            # The packet that overshot is kept (a partial picture is worse than a capture
            # slightly over its budget), but nothing further is buffered and no half packet is
            # held open past the bound.
            self._full = True
            self.stats.dropped_bytes += self._walker.pending
            self._walker.clear_pending()
        return consumed

    @property
    def full(self) -> bool:
        """True once the bound was hit and further media is being refused."""
        return self._full

    @property
    def sink_errors(self) -> list[str]:
        """Bounded reasons a sink stopped being fed, when one was attached."""
        return self._walker.sink_errors

    @property
    def pending(self) -> int:
        """Bytes held for the current incomplete packet."""
        return self._walker.pending


#: Seconds allowed for the ``cmdId=6`` stop send. The stop is a courtesy the camera
#: deserves even when its socket is already unwell, so it is bounded rather than free.
PREVIEW_STOP_TIMEOUT = 3.0

#: Maximum time allowed for a recorder consumer to finish after the camera was stopped.
PREVIEW_CONSUMER_STOP_TIMEOUT = 1.0

#: Frames the streaming path lets the dispatcher buffer for it before it starts dropping.
PREVIEW_QUEUE_MAXSIZE = 96

#: BcMedia codec name -> ffmpeg elementary-stream demuxer, as accepted by
#: ``episode.media.registry.VIDEO_CODEC_HINTS``. Only these two were observed.
VIDEO_CODEC_HINTS_BY_STREAM_CODEC = {"h264": "h264", "h265": "hevc"}


@dataclass(frozen=True)
class PreviewSettings:
    """Validated native-preview configuration for one device.

    ``priming`` primes the encoder ahead of an event; ``native_video`` (F1) makes the on-demand
    ``cmdId=3`` burst the recording source instead of RTSP. Both keep Video Evidence core-owned:
    the plugin only hands Annex-B bytes to the recorder's pipe, never the bundle.
    """

    priming: bool = DEFAULT_PREVIEW_PRIMING
    variant: str = DEFAULT_PREVIEW_VARIANT
    timeout: float = DEFAULT_PREVIEW_TIMEOUT
    #: F1: prefer the on-demand native ``cmdId=3`` burst over RTSP as the recording source.
    #: The camera leads with an I-Frame, so the first access unit arrives ~157 ms after the
    #: command instead of after a fresh RTSP keyframe-wait. Off by default: it is a separate
    #: source-selection decision (Q3 keeps Video Evidence core-owned), not a preview tweak.
    native_video: bool = False

    def __post_init__(self) -> None:
        if self.variant not in PREVIEW_STREAMS:
            raise ValueError(f"variant must be one of {sorted(PREVIEW_STREAMS)}")


def parse_preview_settings(settings: dict[str, object]) -> tuple[PreviewSettings, list[str]]:
    """Read preview settings, falling back to defaults and reporting warnings.

    Never raises: a bad value must not stop a device from connecting, and the operator
    deserves a warning naming the key rather than a dead integration.
    """
    warnings: list[str] = []

    priming = strict_bool(settings, "media_priming", DEFAULT_PREVIEW_PRIMING, warnings)

    variant = settings.get("preview_variant", DEFAULT_PREVIEW_VARIANT)
    if not isinstance(variant, str) or variant not in PREVIEW_STREAMS:
        warnings.append(
            "preview_variant must be "
            f"{' or '.join(repr(name) for name in sorted(PREVIEW_STREAMS))}; using default"
        )
        variant = DEFAULT_PREVIEW_VARIANT

    timeout = bounded_float(
        settings, "preview_timeout", DEFAULT_PREVIEW_TIMEOUT, PREVIEW_TIMEOUT_BOUNDS, warnings
    )
    native_video = strict_bool(settings, "native_video", False, warnings)
    return PreviewSettings(priming, str(variant), timeout, native_video), warnings


def codec_hint(stats_or_codec: PreviewStats | str) -> str:
    """The recorder's elementary-stream demuxer for what this stream actually carried.

    Derived from the bytes the camera sent, never from ``videoEncType``: that field is
    present in the captured ``cmdId=146`` tables (as ``0``/``1``) but no capture records
    which value means H.264 and which means H.265, so mapping it would be a guess. The
    BcMedia packet header, on the other hand, states ``H264``/``H265`` per packet and was
    confirmed by decoding on all three firmwares.
    """
    codec = (
        stats_or_codec.codec if isinstance(stats_or_codec, PreviewStats) else str(stats_or_codec)
    )
    return VIDEO_CODEC_HINTS_BY_STREAM_CODEC.get(codec.lower(), "")
