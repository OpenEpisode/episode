from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from episode.media.registry import CameraMedia, VideoMode, VideoSourceDescriptor
from episode.plugins.hikvision.sdk.runtime import SDKDeviceConfig
from episode.plugins.hikvision.sdk.video_worker import (
    MAX_VIDEO_PACKET_BYTES,
    MESSAGE_PREFIX,
)
from episode.plugins.models import (
    PluginDeviceInfo,
    PluginInstanceState,
    PluginInstanceStatus,
    PluginMediaRegistry,
)

logger = logging.getLogger(__name__)
MAX_WORKER_LINE_BYTES = MAX_VIDEO_PACKET_BYTES * 2
VIDEO_QUEUE_PACKETS = 4
VIDEO_START_TIMEOUT_SECONDS = 10.0
VIDEO_STOP_TIMEOUT_SECONDS = 5.0

WorkerCommand = Callable[[Path], Sequence[str]]


def default_video_worker_command(plugin_path: Path) -> Sequence[str]:
    return [
        sys.executable,
        "-m",
        "episode.plugins.hikvision.sdk.video_worker",
        str(plugin_path),
    ]


class HikvisionSDKVideoWorker:
    """Keeps one isolated HCNetSDK login per enabled camera and starts previews on demand."""

    def __init__(
        self,
        plugin_path: Path,
        config: SDKDeviceConfig,
        media_registry: PluginMediaRegistry | None,
        *,
        command: Sequence[str] | None = None,
        startup_timeout: float = 15.0,
    ):
        self._plugin_path = plugin_path
        self._config = config
        self._media_registry = media_registry
        self._command = tuple(command or default_video_worker_command(plugin_path))
        self._startup_timeout = startup_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._wait_task: asyncio.Task | None = None
        self._startup: asyncio.Future[bool] | None = None
        self._preview_started: asyncio.Future[None] | None = None
        self._preview_stopped: asyncio.Future[None] | None = None
        self._video_queue: asyncio.Queue[bytes | Exception | None] | None = None
        self._capture_lock = asyncio.Lock()
        self._stopping = False
        self._status = PluginInstanceStatus(
            id=config.id,
            name=config.name,
            state=PluginInstanceState.STARTING,
            capabilities=("media",),
        )

    def status(self) -> PluginInstanceStatus:
        return self._status

    async def start(self) -> bool:
        if self._process is not None:
            return self._status.state == PluginInstanceState.RUNNING

        self._stopping = False
        loop = asyncio.get_running_loop()
        self._startup = loop.create_future()
        environment = os.environ.copy()
        library_paths = [str(self._plugin_path), str(self._plugin_path / "HCNetSDKCom")]
        if environment.get("LD_LIBRARY_PATH"):
            library_paths.append(environment["LD_LIBRARY_PATH"])
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(library_paths)

        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                limit=MAX_WORKER_LINE_BYTES,
            )
        except (OSError, ValueError):
            logger.exception("Could not start HCNetSDK video worker for device %s", self._config.id)
            self._set_failed("The HCNetSDK video worker could not be started.")
            return False

        self._reader_task = asyncio.create_task(
            self._read_messages(),
            name=f"hikvision-sdk-video-reader:{self._config.id}",
        )
        self._wait_task = asyncio.create_task(
            self._watch_exit(),
            name=f"hikvision-sdk-video-wait:{self._config.id}",
        )
        config = json.dumps(
            {
                "address": self._config.address,
                "port": self._config.port,
                "username": self._config.username,
                "password": self._config.password,
            },
            separators=(",", ":"),
        ).encode()
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(config + b"\n")
            await self._process.stdin.drain()
            return await asyncio.wait_for(
                asyncio.shield(self._startup),
                timeout=self._startup_timeout,
            )
        except (BrokenPipeError, ConnectionResetError):
            self._set_failed("The HCNetSDK video worker exited during startup.")
        except TimeoutError:
            self._set_failed("HCNetSDK video connection startup timed out.")
            await self.stop(preserve_failure=True)
        return False

    async def stop(self, *, preserve_failure: bool = False) -> None:
        self._stopping = True
        self._enqueue_terminal(RuntimeError("HCNetSDK video worker stopped."))
        process = self._process
        if process is not None and process.returncode is None:
            # EOF asks the child to stop previews, logout, and clean up the SDK.
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=VIDEO_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()

        tasks = [
            task
            for task in (self._reader_task, self._wait_task)
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._process = None
        self._reader_task = None
        self._wait_task = None
        self._video_queue = None
        if self._media_registry is not None:
            self._media_registry.unregister(self._config.id, source="hikvision-sdk")
        if not preserve_failure:
            self._status = replace(self._status, state=PluginInstanceState.STOPPED)
        self._resolve_startup(False)

    async def _read_messages(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                if not line.startswith(MESSAGE_PREFIX.encode()):
                    continue
                try:
                    message = json.loads(line[len(MESSAGE_PREFIX) :])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._set_failed("The HCNetSDK video worker returned invalid data.")
                    continue
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except (ValueError, asyncio.LimitOverrunError):
            logger.exception("HCNetSDK video worker protocol failed for device %s", self._config.id)
            self._set_failed("The HCNetSDK video worker returned an oversized message.")

    async def _handle_message(self, message: object) -> None:
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return
        message_type = message["type"]
        if message_type == "ready":
            streams = self._register_sources(message.get("streams"))
            summary = (
                f"{len(streams)} selectable stream(s) discovered"
                if streams
                else "Connected, but no supported H.264/H.265 streams were discovered."
            )
            self._status = replace(
                self._status,
                state=PluginInstanceState.RUNNING,
                connected_at=datetime.now(tz=timezone.utc),
                error=None,
                summary=summary,
                device_info=self._device_info(message.get("device_info")),
            )
            self._resolve_startup(True)
            return
        if message_type == "video":
            await self._handle_video_packet(message)
            return
        if message_type == "preview_started":
            self._resolve_future(self._preview_started)
            return
        if message_type == "preview_stopped":
            self._resolve_future(self._preview_stopped)
            if self._video_queue is not None:
                await self._video_queue.put(None)
            return
        if message_type == "error":
            stage = message.get("stage")
            code = message.get("code")
            detail = message.get("message")
            error = (
                f"HCNetSDK {stage} failed (error {code})."
                if isinstance(stage, str) and isinstance(code, int)
                else detail
                if isinstance(detail, str)
                else "HCNetSDK video worker failed."
            )
            if stage in {"initialize", "login", "configuration"} or self._startup_pending():
                self._set_failed(error)
                return
            if stage in {"preview", "preview_callback"}:
                self._reject_future(self._preview_started, RuntimeError(error))
                return
            self._enqueue_terminal(RuntimeError(error))

    async def _handle_video_packet(self, message: dict) -> None:
        declared_length = message.get("length")
        encoded = message.get("payload")
        if not isinstance(declared_length, int) or not isinstance(encoded, str):
            self._enqueue_terminal(RuntimeError("HCNetSDK returned invalid video data."))
            return
        if declared_length <= 0 or declared_length > MAX_VIDEO_PACKET_BYTES:
            self._enqueue_terminal(RuntimeError("HCNetSDK video packet exceeded its limit."))
            return
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            self._enqueue_terminal(RuntimeError("HCNetSDK returned invalid video data."))
            return
        if len(payload) != declared_length:
            self._enqueue_terminal(RuntimeError("HCNetSDK video packet length did not match."))
            return
        if self._video_queue is not None:
            await self._video_queue.put(payload)

    def _register_sources(self, value: object) -> tuple[dict[str, object], ...]:
        if not isinstance(value, list):
            return ()
        streams = tuple(
            stream
            for stream in value
            if isinstance(stream, dict)
            and stream.get("variant") in {"main", "sub"}
            and stream.get("stream_type") in {0, 1}
            and stream.get("codec") in {"h264", "hevc"}
        )
        if self._media_registry is None:
            return streams
        self._media_registry.unregister(self._config.id, source="hikvision-sdk")
        for stream in streams:
            variant = str(stream["variant"])
            codec = str(stream["codec"])
            width = stream.get("width") if isinstance(stream.get("width"), int) else None
            height = stream.get("height") if isinstance(stream.get("height"), int) else None
            frame_rate = (
                stream.get("frame_rate") if isinstance(stream.get("frame_rate"), int) else None
            )
            modes = (
                (VideoMode(width=width, height=height, frame_rates=(frame_rate,), codec=codec),)
                if frame_rate is not None
                else (VideoMode(width=width, height=height, codec=codec),)
            )
            self._media_registry.register(
                CameraMedia(
                    device_id=self._config.id,
                    source="hikvision-sdk",
                    video_handler=self._handler_for(int(stream["stream_type"])),
                    codec_hint=codec,
                    video_source=VideoSourceDescriptor(
                        id=f"hikvision-sdk:{variant}",
                        name=f"HCNetSDK · {variant.title()} stream",
                        provider="Hikvision HCNetSDK",
                        protocol="HCNetSDK",
                        metadata_kind="capabilities",
                        width=width,
                        height=height,
                        frame_rate=frame_rate,
                        codec=codec,
                        modes=modes,
                    ),
                )
            )
        return streams

    def _handler_for(self, stream_type: int):
        async def handle(push: Callable[[bytes], Awaitable[None]]) -> None:
            async with self._capture_lock:
                process = self._process
                if process is None or process.returncode is not None:
                    raise RuntimeError("HCNetSDK video worker is unavailable.")
                queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(
                    maxsize=VIDEO_QUEUE_PACKETS
                )
                self._video_queue = queue
                loop = asyncio.get_running_loop()
                self._preview_started = loop.create_future()
                self._preview_stopped = loop.create_future()
                try:
                    await self._send_command({"type": "start", "stream_type": stream_type})
                    await asyncio.wait_for(
                        asyncio.shield(self._preview_started),
                        timeout=VIDEO_START_TIMEOUT_SECONDS,
                    )
                    while True:
                        chunk = await queue.get()
                        if chunk is None:
                            return
                        if isinstance(chunk, Exception):
                            raise chunk
                        await push(chunk)
                finally:
                    self._video_queue = None
                    await self._stop_preview()

        return handle

    def _enqueue_terminal(self, item: Exception) -> None:
        queue = self._video_queue
        if queue is None:
            return
        # A terminal error must not sit behind stale packets if the recorder has
        # stopped consuming. Emptying this small bounded queue also keeps plugin
        # shutdown from leaving the recorder handler blocked on queue.get().
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    async def _stop_preview(self) -> None:
        if self._stopping:
            return
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            await self._send_command({"type": "stop"})
            if self._preview_stopped is not None:
                await asyncio.wait_for(
                    asyncio.shield(self._preview_stopped),
                    timeout=VIDEO_STOP_TIMEOUT_SECONDS,
                )
        except (BrokenPipeError, ConnectionResetError, TimeoutError, RuntimeError):
            # A stuck native callback must not survive the capture that owns it.
            await self.stop(preserve_failure=True)

    async def _send_command(self, command: dict[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RuntimeError("HCNetSDK video worker is unavailable.")
        process.stdin.write(json.dumps(command, separators=(",", ":")).encode() + b"\n")
        await asyncio.wait_for(process.stdin.drain(), timeout=3)

    @staticmethod
    def _device_info(value: object) -> PluginDeviceInfo | None:
        if not isinstance(value, dict):
            return None
        info = PluginDeviceInfo(
            manufacturer=(
                value.get("manufacturer") if isinstance(value.get("manufacturer"), str) else None
            ),
            model=value.get("model") if isinstance(value.get("model"), str) else None,
            firmware_version=(
                value.get("firmware_version")
                if isinstance(value.get("firmware_version"), str)
                else None
            ),
        )
        return info if any((info.manufacturer, info.model, info.firmware_version)) else None

    async def _watch_exit(self) -> None:
        assert self._process is not None
        return_code = await self._process.wait()
        if self._stopping:
            return
        if self._media_registry is not None:
            self._media_registry.unregister(self._config.id, source="hikvision-sdk")
        if self._status.state != PluginInstanceState.FAILED:
            self._set_failed(f"The HCNetSDK video worker exited unexpectedly (code {return_code}).")
        self._enqueue_terminal(RuntimeError("HCNetSDK video worker exited."))

    def _set_failed(self, error: str) -> None:
        self._status = replace(
            self._status,
            state=PluginInstanceState.FAILED,
            error=error,
        )
        self._resolve_startup(False)

    def _startup_pending(self) -> bool:
        return self._startup is not None and not self._startup.done()

    @staticmethod
    def _resolve_future(future: asyncio.Future | None) -> None:
        if future is not None and not future.done():
            future.set_result(None)

    @staticmethod
    def _reject_future(future: asyncio.Future | None, error: Exception) -> None:
        if future is not None and not future.done():
            future.set_exception(error)

    def _resolve_startup(self, result: bool) -> None:
        if self._startup is not None and not self._startup.done():
            self._startup.set_result(result)
