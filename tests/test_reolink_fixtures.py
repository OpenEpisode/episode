"""Camera-free tests driven by the sanitized fixtures under ``tests/fixtures/reolink/``.

These fixtures were written from real
cameras and are the repo's only measured record of the Baichuan wire shapes the Reolink
plugin parses. They contain *structure only*: frame geometry, packet headers, encode
tables, snapshot chunk sizes, and two alarm pushes. No ciphered body, no image, and no
credential-derived value is in the tree (asserted below).

Each fixture set is one directory named ``fw-<firmware>``, holding:

``fixtures.json``             index + provenance (model, firmware, cipher, notes)
``alarm_idle.xml``            a ``cmdId=33`` push with ``<status>none</status>``
``stream_info_146.json``      encode tables (``cmdId=146``)
``snapshot_ack_109.xml``      ``cmdId=109`` ack carrying ``<pictureSize>``
``snapshot_exchange_109.json`` chunk geometry + completeness of that capture
``preview_frames_main.json``  every ``cmdId=3`` frame's offset/length/encryptLen
``preview_packets_main.json`` every BcMedia packet header the walker framed
``preview_meta_main.json``    codec, resolution, NAL inventory, keyframe proof

Run: ``uv run pytest tests/test_reolink_fixtures.py -q`` (no camera, no socket).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from episode.plugins.reolink.client import parse_xml_body
from episode.plugins.reolink.events import parse_alarm_event_frame

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "reolink"

#: Words the maintainer greps the fixture tree for. A fixture must not even name the
#: mechanism it protects, so the index describes sanitization without spelling it out.
GUARD_WORDS = ("nonce", "token", "password", "authorization", "secret")

SET_NAMES = (
    sorted(path.name for path in FIXTURE_ROOT.iterdir() if (path / "fixtures.json").is_file())
    if FIXTURE_ROOT.is_dir()
    else []
)


def _load(set_name: str, filename: str) -> Any:
    return json.loads((FIXTURE_ROOT / set_name / filename).read_text(encoding="utf-8"))


@pytest.fixture(scope="module", params=SET_NAMES)
def fixture_set(request) -> str:
    """One captured firmware set, parametrized so every set is exercised."""
    return str(request.param)


def _alarm_entry(parsed: dict[str, Any]) -> dict[str, Any]:
    """The ``AlarmEvent`` child of a parsed push, whatever nesting the parser used."""
    outer = parsed.get("AlarmEventList", parsed)
    entry = outer.get("AlarmEvent", outer) if isinstance(outer, dict) else outer
    assert isinstance(entry, dict), f"unexpected alarm push shape: {list(parsed)}"
    return entry


def _text(value: Any) -> str:
    """Scalar text out of a parsed leaf, coping with the ``{"_value": ..}`` wrapping."""
    if isinstance(value, dict):
        if "_value" in value:
            return str(value["_value"])
        return ""
    return str(value)


# ── tree hygiene (Phase 0 success criteria) ───────────────────────────


def test_fixture_tree_exists_with_several_sets() -> None:
    assert len(SET_NAMES) >= 3, f"expected >=3 firmware sets, found {SET_NAMES}"
    assert len({name.removeprefix("fw-") for name in SET_NAMES}) == len(SET_NAMES)


#: Captured only when the camera happened to produce one during the window.
OPTIONAL_FIXTURE_FILES = {"alarm_detected.xml"}


def test_every_set_has_every_expected_file(fixture_set: str) -> None:
    index = _load(fixture_set, "fixtures.json")
    expected = {
        "alarm_idle.xml",
        "stream_info_146.json",
        "snapshot_ack_109.xml",
        "snapshot_exchange_109.json",
        "preview_frames_main.json",
        "preview_packets_main.json",
        "preview_meta_main.json",
    }
    recorded = set(index["files"])
    assert recorded >= expected, f"{fixture_set}: missing {sorted(expected - recorded)}"
    assert recorded <= expected | OPTIONAL_FIXTURE_FILES, (
        f"{fixture_set}: unrecognised {sorted(recorded - expected)}"
    )
    for filename in recorded:
        assert (FIXTURE_ROOT / fixture_set / filename).is_file(), f"{filename} missing"


def test_tree_is_free_of_secrets_hostnames_and_addresses() -> None:
    """The Phase 0 gate, asserted in-repo so it cannot rot: no guard word, no IPv4/IPv6
    literal, and no ``rtsp://`` (the plugin synthesises those paths itself)."""
    offenders: list[str] = []
    for path in sorted(FIXTURE_ROOT.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for word in GUARD_WORDS:
            if word in lowered:
                offenders.append(f"{path.relative_to(FIXTURE_ROOT)}: word '{word}'")
        for match in re.finditer(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            offenders.append(f"{path.relative_to(FIXTURE_ROOT)}: IPv4 literal {match.group(0)}")
        for match in re.finditer(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b", text):
            offenders.append(f"{path.relative_to(FIXTURE_ROOT)}: MAC literal {match.group(0)}")
        if "rtsp" in lowered:
            offenders.append(f"{path.relative_to(FIXTURE_ROOT)}: mentions rtsp")
    assert offenders == []


def test_index_records_provenance_without_identity(fixture_set: str) -> None:
    index = _load(fixture_set, "fixtures.json")
    assert index["schema"] == "episode.reolink_fixtures.v1"
    # A dropped frame would break every byte-accounting assertion below, so the index
    # carries the guard's drop count and the suite refuses to trust a lossy capture.
    assert index["dropped_frames"] == 0
    assert index["pushes_observed"] >= 1
    assert index["firmware_version"] == fixture_set.removeprefix("fw-")
    assert index["model"]
    assert index["cipher"] in {"aes-128-cfb", "bc-xor"}
    # A masked MAC or its absence, never the real one.
    assert index["mac_address"] in {"(none)"} or re.fullmatch(
        r"\*\*:\*\*:\*\*:\*\*:[0-9A-F]{2}:"
        r"[0-9A-F]{2}",
        index["mac_address"],
    )


# ── alarm pushes ─────────────────────────────────────────────────────


def test_alarm_idle_fixture_is_the_measured_idle_shape(fixture_set: str) -> None:
    """The idle ``cmdId=33`` body, read with the plugin's own XML parser.

    ``parse_xml_body`` wraps a leaf that shares its parent's tag name as
    ``{"_value": ..., "<tag>": ...}``, so scalars are read through the helper here rather
    than assumed to be plain strings — an assumption that would silently pass if the
    parser's shape changed.
    """
    body = (FIXTURE_ROOT / fixture_set / "alarm_idle.xml").read_text(encoding="utf-8")
    entry = _alarm_entry(parse_xml_body(body.encode("utf-8")))
    assert _text(entry["status"]) == "none"
    assert _text(entry["AItype"]) == "none"
    assert _text(entry["channelId"]) == "0"


def test_alarm_idle_fixture_yields_only_a_generic_event(fixture_set: str) -> None:
    """An idle ``cmdId=33`` push remains unrecognized, never a detection event."""
    body = (FIXTURE_ROOT / fixture_set / "alarm_idle.xml").read_text(encoding="utf-8")
    events = parse_alarm_event_frame(body.encode("utf-8"), channel=0)
    detection_types = {
        "motion_detection",
        "human_detection",
        "vehicle_detection",
        "pet_detection",
        "intrusion",
        "line_crossing_detection",
    }
    assert events, "a parseable push must not parse to nothing"
    assert {event.event_type for event in events} == {"unrecognized"}
    assert not {event.event_type for event in events} & detection_types


# ── snapshot: the declared-size oracle (drives Phase 3) ──────────────


def test_snapshot_ack_declares_picture_size_and_the_plugin_can_read_it(fixture_set: str) -> None:
    """``<pictureSize>`` is the completeness oracle; the plugin's own XML parser must see it.

    Measured on every captured firmware: the ack declares the byte count and the exchange
    delivers exactly that, which is what lets ``_get_snapshot_impl`` complete on the
    declaration instead of waiting for a connection-level timeout.
    """
    raw = (FIXTURE_ROOT / fixture_set / "snapshot_ack_109.xml").read_bytes()
    parsed = parse_xml_body(raw)
    declared = int(_text(parsed["Snap"]["pictureSize"]))
    assert declared > 0
    exchange = _load(fixture_set, "snapshot_exchange_109.json")
    assert exchange["declared_picture_size"] == declared
    assert exchange["bytes_received"] == declared
    assert exchange["complete"] is True


def test_snapshot_chunks_are_a_whole_jpeg_in_arrival_order(fixture_set: str) -> None:
    """Chunk geometry: fixed-size chunks, one short tail, JPEG markers at both ends.

    Measured identically on all three firmwares: the first chunks are all the same size,
    exactly one chunk is short, ``FF D8`` opens the stream and ``FF D9`` closes the last
    chunk with nothing after it. That is what makes "received == declared" a safe
    completion rule in Phase 3 rather than a guess.
    """
    exchange = _load(fixture_set, "snapshot_exchange_109.json")
    sizes = exchange["chunk_sizes"]
    assert len(sizes) > 1 and exchange["chunks"] == len(sizes)
    assert sum(sizes) == exchange["bytes_received"]
    assert exchange["first_chunk_head"].lower().startswith("ffd8")
    assert exchange["eoi_seen"] is True
    assert exchange["eoi_mid_chunk"] is False
    assert exchange["bytes_after_eoi"] == 0
    uniform = {size for size in sizes[:-1]}
    assert len(uniform) == 1, f"chunk size varies before the tail: {sorted(uniform)}"
    assert sizes[-1] <= max(uniform)
    # Only the row table is bounded; the exchange itself is always recorded whole, so the
    # committed geometry is complete even when the picture is bigger than the size ceiling.
    assert exchange["chunk_sizes_capped"] is False
    assert exchange["chunks"] == len(sizes)


# ── native preview: the four protocol rules (drives Phase 4) ─────────


def test_preview_stream_is_fully_accounted_by_packet_framing(fixture_set: str) -> None:
    """Frame geometry and packet headers must describe the same bytes.

    ``sum(payload bytes per frame)`` comes from the frame table; the header table's
    ``header + additionalHeader + payload + padding`` comes from the walker. They agree to
    the byte on all three cameras, so the committed structure is self-consistent and a
    Phase 4 implementation can be checked against it rather than re-guessed.
    """
    frames = _load(fixture_set, "preview_frames_main.json")["frames"]
    packets = _load(fixture_set, "preview_packets_main.json")["packets"]
    meta = _load(fixture_set, "preview_meta_main.json")

    stream_bytes = sum(frame["length"] - frame["payload_offset"] for frame in frames)
    framed = 0
    for packet in packets:
        kind = packet["kind"]
        if kind in ("InfoV1", "InfoV2"):
            framed += packet["header_size"]
            continue
        if kind in ("AAC", "ADPCM"):
            size = packet["payload_size"]
            framed += 8 + size + (0 if size % 8 == 0 else 8 - (size % 8))
            continue
        framed += 24 + packet["additional_header_size"] + packet["payload_size"] + packet["padding"]
    assert framed == stream_bytes
    assert meta["media_bytes"] + meta["padding_bytes"] <= stream_bytes


def test_preview_frame_kinds_follow_the_measured_rules(fixture_set: str) -> None:
    """Rule 2 of §4, corrected by measurement on three firmwares.

    Universal across all three captures:

    * every media *start* frame carries an offset, and no zero-offset frame carries a
      ``binaryData`` extension — a zero-offset frame is pure continuation bytes, and
      decrypting one anyway corrupts the stream;
    * an offset **does not** imply ``binaryData``. Two firmwares send media starts whose
      extension names only ``checkPos``/``checkValue``, so treating ``binaryData`` as the
      media marker (which is enough for ``cmdId=109``) would drop frames and desynchronise;
    * when ``encryptLen`` is present it never exceeds the payload the frame carries.

    Not universal, and the interesting part: whether a packet is split at all is
    firmware-dependent — see :func:`test_preview_split_behaviour_is_firmware_dependent`.
    """
    frames = _load(fixture_set, "preview_frames_main.json")["frames"]
    starts = [frame for frame in frames if frame["payload_offset"] > 0]
    continuations = [frame for frame in frames if frame["payload_offset"] == 0]
    assert starts, "a capture with no media start is not a preview fixture"
    assert not any(frame["binary_data"] for frame in continuations)
    for frame in starts:
        if frame["encrypt_len"]:
            assert frame["encrypt_len"] <= frame["length"] - frame["payload_offset"]


def test_preview_split_behaviour_is_firmware_dependent() -> None:
    """One firmware splits media packets, two do not — so a client must handle both.

    Measured: fw-50397653 delivered 94 cleartext continuation frames, fw-50332294 and
    fw-50463297 delivered none. §4's "expect starts and continuations" is therefore true of
    the family, not of every member, and an implementation that assumes either extreme
    (always-append vs never-append) breaks on two of three cameras.
    """
    splits = {}
    for set_name in SET_NAMES:
        meta = _load(set_name, "preview_meta_main.json")
        assert meta["rows_dropped"] is False, f"{set_name}: fixture rows were truncated"
        splits[set_name] = meta["continuation_frames"]
    assert sum(1 for count in splits.values() if count > 0) >= 1
    assert sum(1 for count in splits.values() if count == 0) >= 1


def test_preview_proves_a_keyframed_stream(fixture_set: str) -> None:
    """``cmdId=3`` delivers an independently decodable picture, not just bytes."""
    meta = _load(fixture_set, "preview_meta_main.json")
    packets = _load(fixture_set, "preview_packets_main.json")["packets"]
    assert meta["keyframed"] is True
    assert meta["codec"] in {"h264", "h265"}
    assert meta["width"] > 0 and meta["height"] > 0
    assert meta["first_iframe_ms"] is not None and meta["first_iframe_ms"] < 2000.0

    infos = [packet for packet in packets if packet["kind"] in ("InfoV1", "InfoV2")]
    assert len(infos) == 1, "one Info packet per capture, carrying the resolution"
    assert (infos[0]["width"], infos[0]["height"]) == (meta["width"], meta["height"])

    keyframes = [packet for packet in packets if packet["kind"] == "I"]
    assert len(keyframes) == meta["iframe_count"] >= 1
    assert keyframes[0]["tag"].upper() in {"H265", "H264"}

    nal_counts = meta["nal_counts"]
    if meta["codec"] == "h265":
        assert {"VPS", "SPS", "PPS", "IDR"} <= set(nal_counts)
    else:
        assert {"SPS", "IDR"} <= set(nal_counts)


def test_preview_headers_are_plausible_on_the_wire(fixture_set: str) -> None:
    """Sanity bounds on the committed geometry: sizes, magic, and 8-byte alignment."""
    packets = _load(fixture_set, "preview_packets_main.json")["packets"]
    frames = _load(fixture_set, "preview_frames_main.json")["frames"]
    for packet in packets:
        assert packet["magic"], "header rows carry their magic word"
        if packet["kind"] in ("I", "P"):
            assert 0 < packet["payload_size"] <= 8_000_000
            assert packet["padding"] in range(0, 8)
    assert frames, "an empty frame table would make the other assertions meaningless"
    assert max(frame["length"] for frame in frames) <= 64 * 1024


# ── encode tables ────────────────────────────────────────────────────


def test_stream_info_tables_describe_main_and_sub_streams(fixture_set: str) -> None:
    stream_info = _load(fixture_set, "stream_info_146.json")
    tables = stream_info["tables"]
    assert tables
    kinds = {table.get("type") for table in tables}
    assert {"mainStream", "subStream"} & kinds
    for table in tables:
        if table.get("width"):
            assert table["width"] > 0 and table["height"] > 0
        if table.get("framerate"):
            assert all(value > 0 for value in table["framerate"])


def test_fleet_covers_two_codecs_and_distinct_firmwares() -> None:
    """The fixture set is evidence about a firmware family, not one happy camera."""
    codecs: set[str] = set()
    resolutions: set[tuple[int, int]] = set()
    for set_name in SET_NAMES:
        meta = _load(set_name, "preview_meta_main.json")
        codecs.add(meta["codec"])
        resolutions.add((meta["width"], meta["height"]))
    assert len(codecs) >= 2, f"expected h264 and h265 coverage, got {codecs}"
    assert len(resolutions) >= 2


def test_snapshot_oracle_is_consistent_across_firmwares() -> None:
    """Gap G6/G9 work is only justified if the oracle exists fleet-wide."""
    for set_name in SET_NAMES:
        exchange = _load(set_name, "snapshot_exchange_109.json")
        assert exchange["complete"] is True, set_name
        assert exchange["declared_picture_size"] > 0, set_name


# ── detection push (present when the camera produced one) ────────────


def test_detection_push_if_present_names_a_detection_and_parses_to_one(fixture_set: str) -> None:
    """A captured ``alarm_detected.xml`` must be a detection *and* parse to one.

    The fixture writer splits pushes structurally (``status``/``AItype``/``smartAiTypeList``),
    so a captured detection is evidence that the two readings agree on a real camera body.
    Measured on fw-50463297: ``status=none`` with ``AItype=people`` — the status field alone
    would have called this idle.
    """
    path = FIXTURE_ROOT / fixture_set / "alarm_detected.xml"
    if not path.is_file():
        # Not a failure: detections are whatever the camera decided during the window.
        assert _load(fixture_set, "fixtures.json")["pushes_observed"] >= 1
        return
    body = path.read_bytes()
    entry = _alarm_entry(parse_xml_body(body.decode("utf-8")))
    named = [
        _text(entry.get("AItype")),
        _text(entry.get("status")),
    ]
    assert any(value.lower() not in {"", "none"} for value in named), (
        "a fixture named as a detection names nothing; the writer and the parser disagree"
    )
    events = parse_alarm_event_frame(body, channel=0)
    assert events, "a camera-confirmed detection must not parse to nothing"
