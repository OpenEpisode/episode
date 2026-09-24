"""Native preview framing (`cmdId=3`) proved against captured camera geometry.

These tests do not trust a hand-written fixture: they rebuild a BcMedia byte stream whose
per-packet sizes come from a real capture's packet table, slice it back into Baichuan frames
whose sizes come from that capture's *frame* table, and require the in-tree walker to account
for every byte. That is the check §4 asked for — "a Phase 4 implementation can be checked
against it rather than re-guessed" — and it is what proves the frame rules below hold on
three different firmwares rather than on one convenient capture.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

from episode.plugins.reolink.preview import (
    PREVIEW_MAX_BYTES,
    PREVIEW_STREAMS,
    BcMediaWalker,
    PreviewFeed,
    media_chunk,
    nal_types,
    packet_kind,
    parse_encrypt_len,
    preview_request_payload,
    preview_stop_payload,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reolink"
SET_NAMES = sorted(p.name for p in FIXTURE_DIR.iterdir() if p.is_dir())

IFRAME_MAGIC = 0x63643030  # "00dc"
PFRAME_MAGIC = 0x63643130  # "10dc"
INFO_MAGIC = 0x32303031  # InfoV2 "2001"
AAC_MAGIC = 0x62773530  # "05wb"


def _load(set_name: str, name: str) -> dict:
    return json.loads((FIXTURE_DIR / set_name / name).read_text(encoding="utf-8"))


@pytest.fixture(params=SET_NAMES)
def capture(request: pytest.FixtureRequest) -> dict:
    """One camera capture: its recorded frame table, packet table, and summary."""
    set_name = request.param
    return {
        "name": set_name,
        "frames": _load(set_name, "preview_frames_main.json")["frames"],
        "packets": _load(set_name, "preview_packets_main.json")["packets"],
        "meta": _load(set_name, "preview_meta_main.json"),
    }


def _padding(size: int) -> int:
    return 0 if size % 8 == 0 else 8 - (size % 8)


def _nal(codec: str, nal_type: int) -> bytes:
    """One Annex-B NAL whose header bytes encode ``nal_type`` for ``codec``."""
    if codec == "h265":
        return b"\x00\x00\x00\x01" + bytes([(nal_type << 1) & 0xFF, 0x01])
    return b"\x00\x00\x00\x01" + bytes([0x60 | (nal_type & 0x1F)])


def _picture_payload(codec: str, kind: str, size: int) -> bytes:
    """A payload that really is ``size`` bytes and really carries ``kind``'s NALs."""
    header_types = {"h265": {"I": [33, 34, 19], "P": [1]}, "h264": {"I": [7, 8, 5], "P": [1]}}
    body = bytearray()
    for nal_type in header_types[codec][kind]:
        body += _nal(codec, nal_type)
    if len(body) > size:
        raise AssertionError("fixture payload too small for its own keyframe NALs")
    body += b"\x11" * (size - len(body))
    return bytes(body)


def _stream_bytes(packets: list[dict], meta: dict) -> bytes:
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
            out += b"\x00" * _padding(size)
            continue
        size = packet["payload_size"]
        extra = packet["additional_header_size"]
        magic = IFRAME_MAGIC if kind == "I" else PFRAME_MAGIC
        tag = b"H265" if codec == "h265" else b"H264"
        out += struct.pack("<I", magic) + tag + struct.pack("<IIQ", size, extra, 0)
        out += b"\x44" * extra
        out += _picture_payload(codec, kind, size)
        out += b"\x00" * _padding(size)
    return bytes(out)


class _Cipher:
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


def _wire_frames(stream: bytes, frames: list[dict], cipher: _Cipher) -> list[dict]:
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
                "elapsed": 0.0,
            }
        )
    assert position == len(stream), "frame table does not cover the recorded stream"
    return out


def test_stream_rebuilder_matches_the_recorded_geometry(capture) -> None:
    """Guard for every other test here: the rebuilt stream is the captured byte count."""
    meta = capture["meta"]
    stream = _stream_bytes(capture["packets"], meta)
    from_frames = sum(f["length"] - f["payload_offset"] for f in capture["frames"])
    assert len(stream) == from_frames == meta["stream_bytes"]


def test_recorded_capture_reassembles_with_the_measured_tallies(capture) -> None:
    """Feed every recorded frame in order: the walker must report that capture's summary."""
    meta = capture["meta"]
    cipher = _Cipher()
    feed = PreviewFeed(variant="main")
    for index, frame in enumerate(
        _wire_frames(_stream_bytes(capture["packets"], meta), capture["frames"], cipher)
    ):
        feed.push_frame(
            payload_offset=frame["payload_offset"],
            extension=frame["extension"],
            payload=frame["payload"],
            decrypt=cipher.decrypt,
            elapsed_ms=float(index) * 10.0,
        )

    stats = feed.stats
    assert feed.full is False
    assert stats.truncated == 0 and stats.resyncs == 0, "framing must not lose or guess"
    assert stats.codec == meta["codec"]
    assert stats.iframe_count == meta["iframe_count"]
    assert stats.pframe_count == meta["pframe_count"]
    assert stats.audio_count == meta["audio_count"]
    assert stats.media_bytes == meta["media_bytes"]
    assert stats.padding_bytes == meta["padding_bytes"]
    assert feed.pending == 0, "a complete capture must leave no partial packet behind"
    assert stats.keyframed() is True, "the recorded stream is independently decodable"
    assert stats.first_iframe_ms is not None


def test_continuation_frames_are_appended_undecrypted(capture) -> None:
    """Rule 2, the direction that costs the most if wrong.

    ``fw-50397653`` sent 80 cleartext continuation frames and the other two sent none, so a
    client that decrypts a zero-offset frame works on part of the fleet and corrupts the rest.
    Decrypting one must visibly break framing, and the same capture must survive when it is
    appended as-is.
    """
    meta = capture["meta"]
    if not any(frame["payload_offset"] == 0 for frame in capture["frames"]):
        pytest.skip(f"{capture['name']} recorded no split packets")
    stream = _stream_bytes(capture["packets"], meta)
    cipher = _Cipher()
    wired = _wire_frames(stream, capture["frames"], cipher)

    # The bug under test is a client that runs every payload through the cipher, which is
    # exactly what a snapshot-shaped implementation does here: on the two captures with no
    # continuation frames it is invisible, and on this one it corrupts the stream.
    broken_payloads: list[bytes] = []
    broken = BcMediaWalker(sink=broken_payloads.append)
    broken.push(b"".join(cipher.decrypt(frame["payload"]) for frame in wired))
    clean_payloads: list[bytes] = []
    clean = BcMediaWalker(sink=clean_payloads.append)
    clean.push(stream)
    # What makes this bug dangerous: packet sizes come from headers, so framing stays in
    # lockstep and every tally matches. Only the payload contents are wrecked. One resync did
    # happen to appear on this capture, but that is incidental to where the ciphered bytes fell
    # out; the assertion that carries weight is that the delivered bytes differ.
    assert (
        broken.stats.frames() == clean.stats.frames() == meta["iframe_count"] + meta["pframe_count"]
    )
    assert broken.stats.media_bytes == clean.stats.media_bytes == meta["media_bytes"]
    assert clean.stats.keyframed() is True
    assert broken_payloads != clean_payloads, "the wrecked capture must differ in content"
    assert clean.stats.truncated == 0 and clean.stats.resyncs == 0

    clean_feed = PreviewFeed()
    for frame in wired:
        clean_feed.push_frame(
            payload_offset=frame["payload_offset"],
            extension=frame["extension"],
            payload=frame["payload"],
            decrypt=cipher.decrypt,
        )
    assert clean_feed.stats.media_bytes == meta["media_bytes"]


def test_media_start_without_binarydata_is_still_media(capture) -> None:
    """Two of three firmwares label media starts with ``checkPos``/``checkValue`` only.

    The rule a client may rely on is the ``payloadOffset``, never the extension's vocabulary.
    """
    labelled = [
        frame
        for frame in capture["frames"]
        if frame["payload_offset"] > 0 and not frame["encrypt_len"]
    ]
    if not labelled:
        pytest.skip(f"{capture['name']} labelled every media start")
    chunk = media_chunk(
        payload_offset=106,
        extension="<LPreview><checkPos>0</checkPos></LPreview>",
        payload=b"\x00\x00\x00\x01nalu",
        decrypt=lambda data: pytest.fail("an unlabelled media start carries no ciphered bytes"),
    )
    assert chunk == b"\x00\x00\x00\x01nalu"


def test_decrypt_only_the_declared_cipher_prefix(capture) -> None:
    """``encryptLen`` says exactly how many leading payload bytes are ciphered."""
    frames = [frame for frame in capture["frames"] if frame["encrypt_len"]]
    if not frames:
        pytest.skip(f"{capture['name']} recorded no ciphered media start")
    declared = frames[0]["encrypt_len"]
    plain = b"".join(bytes([index % 251]) for index in range(declared + 64))
    cipher = _Cipher()
    payload = cipher.encrypt(plain[:declared]) + plain[declared:]
    recovered = media_chunk(
        payload_offset=106,
        extension=f"<LPreview><encryptLen>{declared}</encryptLen></LPreview>",
        payload=payload,
        decrypt=cipher.decrypt,
    )
    assert recovered == plain
    assert cipher.calls == 1, "only the declared prefix is decrypted, in one call"


def test_a_zero_offset_frame_is_never_decrypted(capture) -> None:
    """The cheap half of rule 2, asserted directly."""
    payload = b"\x00\x00\x00\x01untouched"
    assert (
        media_chunk(
            payload_offset=0,
            extension="<LPreview><encryptLen>4</encryptLen></LPreview>",
            payload=payload,
            decrypt=lambda data: pytest.fail("continuation bytes are cleartext"),
        )
        == payload
    )


def test_resync_recovers_after_garbage_but_says_so() -> None:
    """Byte loss has no checksum to help it, so framing resyncs and counts it."""
    codec = "h264"
    packet = struct.pack("<I", PFRAME_MAGIC) + b"H264" + struct.pack("<IIQ", 32, 0, 0)
    packet += _picture_payload(codec, "P", 32)
    walker = BcMediaWalker()
    walker.push(b"\x01\x02\x03\x04\x05\x06\x07\x08" + packet)
    assert walker.stats.pframe_count == 1
    assert walker.stats.resyncs >= 1, "the garbage must be visible in the tallies"


def test_an_impossible_packet_size_resyncs_instead_of_raising() -> None:
    """A declared size above the bound is a framing fault, not a memory allocation."""
    packet = struct.pack("<I", IFRAME_MAGIC) + b"H264"
    packet += struct.pack("<IIQ", 2 * PREVIEW_MAX_BYTES, 0, 0)
    walker = BcMediaWalker()
    walker.push(packet)
    assert walker.stats.truncated == 1
    assert walker.stats.iframe_count == 0


def test_a_capture_stops_buffering_at_its_bound() -> None:
    """Buffering is bounded: past the limit media is refused and counted, never truncated."""
    codec = "h264"
    size = 1024
    one = struct.pack("<I", PFRAME_MAGIC) + b"H264" + struct.pack("<IIQ", size, 0, 0)
    one += _picture_payload(codec, "P", size)
    feed = PreviewFeed(max_bytes=size * 2 + _padding(size) * 2)
    for _ in range(10):
        feed.push_frame(payload_offset=106, extension="", payload=one, decrypt=lambda data: data)
    assert feed.full is True
    assert feed.stats.pframe_count == 2
    assert feed.stats.dropped_bytes > 0
    assert feed.pending == 0, "a refused capture must not hold a half packet"


def test_a_dead_sink_does_not_stop_framing() -> None:
    """A consumer that dies mid-capture leaves the tallies intact and the reason recorded."""
    codec = "h264"
    size = 64
    packet = struct.pack("<I", IFRAME_MAGIC) + b"H264" + struct.pack("<IIQ", size, 0, 0)
    packet += _picture_payload(codec, "I", size)

    def sink(payload: bytes) -> None:
        raise ConnectionResetError("pipe closed")

    walker = BcMediaWalker(sink=sink)
    walker.push(packet)
    assert walker.stats.iframe_count == 1
    assert walker.sink_errors and "ConnectionResetError" in walker.sink_errors[0]


def test_nal_types_use_the_codecs_own_bit_layout() -> None:
    """Reading H.264 with the HEVC shift turns SPS/IDR into nonsense (it has happened)."""
    assert 7 in nal_types(_nal("h264", 7), "h264")
    assert 5 in nal_types(_nal("h264", 5), "h264")
    assert 33 in nal_types(_nal("h265", 33), "h265")
    assert 19 in nal_types(_nal("h265", 19), "h265")
    assert set(nal_types(b"", "h264")) == set()


def test_packet_kind_only_answers_for_real_magnics() -> None:
    assert packet_kind(IFRAME_MAGIC) == "I"
    assert packet_kind(PFRAME_MAGIC) == "P"
    assert packet_kind(AAC_MAGIC) == "AAC"
    assert packet_kind(INFO_MAGIC) == "InfoV2"
    assert packet_kind(0xDEADBEEF) is None


def test_stop_repeats_the_requests_handle() -> None:
    """An empty ``cmdId=6`` body is rejected (405) and the camera keeps streaming."""
    handle, stream_name, _header = PREVIEW_STREAMS["sub"]
    request = preview_request_payload(handle, stream_name, channel=0)
    stop = preview_stop_payload(handle, channel=0)
    assert f"<handle>{handle}</handle>" in request
    assert f"<handle>{handle}</handle>" in stop
    assert "<streamType>" in request and "<streamType>" not in stop


def test_stream_variants_carry_the_measured_handle_and_header_pair() -> None:
    assert PREVIEW_STREAMS["main"] == (0, "mainStream", 0)
    assert PREVIEW_STREAMS["sub"] == (256, "subStream", 1)


def test_parse_encrypt_len_reads_only_what_it_names() -> None:
    assert parse_encrypt_len("<LPreview><encryptLen>1024</encryptLen></LPreview>") == 1024
    assert parse_encrypt_len("<LPreview><checkPos>1024</checkPos></LPreview>") is None
    assert parse_encrypt_len("") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
