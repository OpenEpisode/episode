"""Bounded, credential-safe validation for explicitly configured video streams."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from episode.domain.models import CapabilityConfig, Device

_FFPROBE_TIMEOUT_SECONDS = 8.0
_MAX_STDOUT_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 4096
_CODEC_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MANUAL_PROTOCOLS = frozenset({"rtsp", "rtsps"})
_PROBE_SLOTS = asyncio.Semaphore(2)


def _result(status: str, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "details": details or {},
    }


def _manual_video_config(device: Device) -> CapabilityConfig | None:
    config = device.get_config("video")
    if config is None:
        return None
    protocol = config.protocol.lower()
    if protocol not in _MANUAL_PROTOCOLS or not config.path:
        return None
    if config.settings.get("origin") == "onvif":
        return None
    if config.settings.get("manual_endpoint") is False:
        return None
    return config


async def _read_stdout(reader: asyncio.StreamReader) -> bytes | None:
    output = bytearray()
    while True:
        chunk = await reader.read(_READ_CHUNK_BYTES)
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > _MAX_STDOUT_BYTES:
            return None


async def _terminate_and_reap(
    process: asyncio.subprocess.Process,
    stdout_task: asyncio.Task[bytes | None],
    wait_task: asyncio.Task[int],
) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    if not stdout_task.done():
        stdout_task.cancel()
    await asyncio.gather(stdout_task, return_exceptions=True)
    try:
        await asyncio.wait_for(wait_task, timeout=1)
    except asyncio.TimeoutError:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)


async def _run_ffprobe(url: str) -> tuple[str, int | None, bytes | None]:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return "unavailable", None, None

    stdout_task = asyncio.create_task(_read_stdout(process.stdout))
    wait_task = asyncio.create_task(process.wait())
    pending: set[asyncio.Task[Any]] = {stdout_task, wait_task}
    output: bytes | None = None
    returncode: int | None = None
    deadline = asyncio.get_running_loop().time() + _FFPROBE_TIMEOUT_SECONDS
    try:
        while pending:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _terminate_and_reap(process, stdout_task, wait_task)
                return "timeout", None, None
            done, pending = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await _terminate_and_reap(process, stdout_task, wait_task)
                return "timeout", None, None
            if stdout_task in done:
                output = stdout_task.result()
                if output is None:
                    await _terminate_and_reap(process, stdout_task, wait_task)
                    return "output_limit", None, None
            if wait_task in done:
                returncode = wait_task.result()
        return "completed", returncode, output
    except asyncio.CancelledError:
        await _terminate_and_reap(process, stdout_task, wait_task)
        raise


def _codec_details(output: bytes) -> dict[str, str] | None:
    try:
        payload = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec = stream.get("codec_name")
        if isinstance(codec, str) and _CODEC_PATTERN.fullmatch(codec):
            return {"codec": codec}
    return None


async def validate_manual_video(device: Device) -> dict:
    """Probe an explicit manual RTSP/RTSPS endpoint without creating Evidence."""
    config = _manual_video_config(device)
    if config is None or not device.ip_address:
        return _result(
            "unavailable",
            "An explicit manual RTSP or RTSPS endpoint is required",
        )

    url = config.build_url(device.ip_address, device.username, device.password)
    if not url:
        return _result("unavailable", "The manual video endpoint is incomplete")

    try:
        await asyncio.wait_for(_PROBE_SLOTS.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        return _result("unavailable", "Video validation is busy; retry shortly")
    try:
        outcome, returncode, output = await _run_ffprobe(url)
    finally:
        _PROBE_SLOTS.release()
    if outcome == "timeout":
        return _result(
            "unavailable",
            "The manual video stream did not respond before the validation timeout",
        )
    if outcome == "output_limit":
        return _result("unavailable", "Video validation returned too much output")
    if outcome == "unavailable":
        return _result("unavailable", "Video validation is unavailable")
    if returncode != 0 or output is None:
        return _result("unavailable", "The manual video stream could not be opened")

    details = _codec_details(output)
    if details is None:
        return _result("unavailable", "The manual endpoint did not return a video stream")
    return _result("supported", "The manual video stream responded", details)
