"""Rebuild a real ``cmdId=3`` capture from its recorded geometry.

The committed fixtures in ``tests/fixtures/reolink/`` record **geometry, not media bytes**
(sanitization stripped the ciphered bodies and image data), so they cannot be replayed as-is.
This module rebuilds a BcMedia byte stream whose per-packet sizes come from a capture's packet
table, then re-slices that stream into Baichuan frames whose sizes come from the same capture's
frame table. Anything fed the result must account for every byte and reproduce that capture's
recorded summary — which makes it an oracle rather than a hand-written fixture.

Used by both halves of the Phase 4 tests: ``test_reolink_preview.py`` proves the I/O-free
walker, and ``test_reolink_preview_socket.py`` proves the socket half through a scripted camera
that pushes exactly these frames over a real TCP connection.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reolink"
SET_NAMES = sorted(p.name for p in FIXTURE_DIR.iterdir() if p.is_dir())

IFRAME_MAGIC = 0x63643030  # "00dc"
PFRAME_MAGIC = 0x63643130  # "10dc"
INFO_MAGIC = 0x32303031  # InfoV2 "2001"
AAC_MAGIC = 0x62773530  # "05wb"


def load(set_name: str, name: str) -> dict[str, Any]:
    """One recorded JSON table from one firmware's fixture set."""
    return json.loads((FIXTURE_DIR / set_name / name).read_text(encoding="utf-8"))


def capture_fixture(set_name: str) -> dict[str, Any]:
    """One camera capture: its recorded frame table, packet table, and summary."""
    return {
        "name": set_name,
        "frames": load(set_name, "preview_frames_main.json")["frames"],
        "packets": load(set_name, "preview_packets_main.json")["packets"],
        "meta": load(set_name, "preview_meta_main.json"),
    }


def padding(size: int) -> int:
    """Bytes of 8-byte alignment padding that follow a payload of ``size``."""
    return 0 if size % 8 == 0 else 8 - (size % 8)


def nal(codec: str, nal_type: int) -> bytes:
    """One Annex-B NAL whose header bytes encode ``nal_type`` for ``codec``."""
    if codec == "h265":
        return b"\x00\x00\x00\x01" + bytes([(nal_type << 1) & 0xFF, 0x01])
    return b"\x00\x00\x00\x01" + bytes([0x60 | (nal_type & 0x1F)])


def picture_payload(codec: str, kind: str, size: int) -> bytes:
    """A payload that really is ``size`` bytes and really carries ``kind``'s NALs."""
    header_types = {"h265": {"I": [33, 34, 19], "P": [1]}, "h264": {"I": [7, 8, 5], "P": [1]}}
    body = bytearray()
    for nal_type in header_types[codec][kind]:
        body += nal(codec, nal_type)
    if len(body) > size:
        raise AssertionError("fixture payload too small for its own keyframe NALs")
    body += b"\x11" * (size - len(body))
    return bytes(body)


def stream_bytes(packets: list[dict], meta: dict) -> bytes:
    """Serialize a recorded packet table back to wire bytes, size for size."""
    codec = meta["codec"]
    out = bytearray()
    for packet in packets:
        kind = packet["kind"]
        if kind in ("InfoV1", "InfoV2"):
            size = packet["header_size"]
            head = struct.pack("<IIII", INFO_MAGIC, size, packet["width"], packet["height"])
            out += head + b"\x22" * (size - len(head))
            continue
        if kind in ("AAC", "ADPCM"):
            size = packet["payload_size"]
            out += struct.pack("<IHH", AAC_MAGIC, size, size) + b"\x33" * size
            out += b"\x00" * padding(size)
            continue
        size = packet["payload_size"]
        extra = packet["additional_header_size"]
        magic = IFRAME_MAGIC if kind == "I" else PFRAME_MAGIC
        tag = b"H265" if codec == "h265" else b"H264"
        out += struct.pack("<I", magic) + tag + struct.pack("<IIQ", size, extra, 0)
        out += b"\x44" * extra
        out += picture_payload(codec, kind, size)
        out += b"\x00" * padding(size)
    return bytes(out)


class Cipher:
    """A reversible stand-in for the session cipher, so ciphering is observable.

    The client's real cipher is AES-128-CFB or BC; framing does not care which, so the tests
    use something reversible and *asymmetric*, which catches a chunk that was decrypted when it
    should not have been (or vice versa) instead of silently surviving.
    """

    def __init__(self) -> None:
        self.calls = 0

    def encrypt(self, data: bytes) -> bytes:
        return bytes(byte ^ 0x5A for byte in data)

    def decrypt(self, data: bytes) -> bytes:
        self.calls += 1
        return bytes(byte ^ 0x5A for byte in data)


def wire_frames(stream: bytes, frames: list[dict], cipher: Cipher) -> list[dict]:
    """Slice ``stream`` into frames of the recorded media sizes, ciphering where recorded.

    Each returned item is what the socket layer would hand over: ``payload_offset`` (0 marks a
    cleartext continuation), an extension naming ``encryptLen`` only when the camera named it,
    and the payload with its ciphered prefix already ciphered.
    """
    out: list[dict] = []
    position = 0
    for frame in frames:
        size = frame["length"] - frame["payload_offset"]
        chunk = stream[position : position + size]
        position += size
        encrypt_len = frame["encrypt_len"] if frame["payload_offset"] else None
        if encrypt_len:
            encrypt_len = min(encrypt_len, len(chunk))
            chunk = cipher.encrypt(chunk[:encrypt_len]) + chunk[encrypt_len:]
            extension = (
                "<LPreview><binaryData>1</binaryData>"
                f"<encryptLen>{encrypt_len}</encryptLen></LPreview>"
            )
        elif frame["payload_offset"]:
            # Measured on two of three firmwares: a media start whose extension says nothing
            # about binary data at all.
            extension = "<LPreview><checkPos>0</checkPos><checkValue>123</checkValue></LPreview>"
        else:
            extension = ""
        out.append(
            {
                "payload_offset": frame["payload_offset"],
                "extension": extension,
                "payload": chunk,
                "encrypt_len": encrypt_len,
                "elapsed": 0.0,
            }
        )
    assert position == len(stream), "frame table does not cover the recorded stream"
    return out


def packet_wire_size(packet: dict) -> int:
    """Bytes one recorded packet occupies in :func:`stream_bytes`."""
    kind = packet["kind"]
    if kind in ("InfoV1", "InfoV2"):
        return packet["header_size"]
    if kind in ("AAC", "ADPCM"):
        return 8 + packet["payload_size"] + padding(packet["payload_size"])
    return (
        24
        + packet["additional_header_size"]
        + packet["payload_size"]
        + padding(packet["payload_size"])
    )


def stream_offset_of(packets: list[dict], target: dict) -> int:
    """Where ``target`` begins in the rebuilt stream.

    An offset search would be wrong here: a magic word also occurs inside picture data, so the
    only safe way to find one packet's header is to add up the packets in front of it.
    """
    offset = 0
    for packet in packets:
        if packet is target:
            return offset
        offset += packet_wire_size(packet)
    raise AssertionError("packet is not in its own capture")


def frames_to_first_picture(fixture: dict[str, Any]) -> tuple[int, int]:
    """How many recorded frames a first independently decodable picture needs.

    Returns ``(frames, continuations)``: the frame table is walked until the bytes of the
    ``InfoV2`` header plus the first video packet are covered, and the continuations among
    them are counted. On the split-packet firmware that number is large, which is exactly why
    a socket test cannot assume every media frame starts a packet.
    """
    budget = len(first_picture_bytes(fixture))
    position = 0
    frames = 0
    continuations = 0
    for frame in fixture["frames"]:
        position += frame["length"] - frame["payload_offset"]
        frames += 1
        if frame["payload_offset"] == 0:
            continuations += 1
        if position >= budget:
            break
    return frames, continuations


def first_video_packet(packets: list[dict]) -> dict:
    """The first I/P packet in a recorded packet table (the one an observe pass must return)."""
    for packet in packets:
        if packet["kind"] in ("I", "P"):
            return packet
    raise AssertionError("capture recorded no video packet")


def first_video_payload(set_name: str) -> bytes:
    """The exact payload bytes of that capture's first video packet.

    Located by summing the packets in front of it rather than searching the stream: picture
    bytes are filler, but filler bytes can and do spell a packet magic, and an offset search
    would then hand back a window of fake picture data that still "unpacks".
    """
    fixture = capture_fixture(set_name)
    stream = stream_bytes(fixture["packets"], fixture["meta"])
    packet = first_video_packet(fixture["packets"])
    # Packet header: magic(4) + tag(4) + payloadSize(4) + additionalHeaderSize(4) + µs(8),
    # then the additional header, then the payload.
    start = stream_offset_of(fixture["packets"], packet) + 24 + packet["additional_header_size"]
    return stream[start : start + packet["payload_size"]]


def first_picture_bytes(fixture: dict[str, Any]) -> bytes:
    """Stream bytes covering the InfoV2 header through the first video packet's end.

    This is the budget a socket observer must receive before it can report one picture, so it
    is what both the event-latency test and the buffer-bound test are sized from.
    """
    packets = fixture["packets"]
    first = first_video_packet(packets)
    through = stream_offset_of(packets, first) + packet_wire_size(first)
    return stream_bytes(packets, fixture["meta"])[:through]
