from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from episode.plugins.hikvision.sdk.worker import (
    MAX_NOTIFICATION_BYTES,
    MAX_SNAPSHOT_BYTES,
    WORKER_MESSAGE_PREFIX,
)
from episode.plugins.models import (
    PluginDeviceInfo,
    PluginInstanceState,
    PluginInstanceStatus,
    RawPluginDelivery,
    RawPluginDeliverySink,
)

logger = logging.getLogger(__name__)

SNAPSHOT_REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class SDKDeviceConfig:
    id: str
    name: str
    area_id: str
    address: str
    port: int
    username: str
    password: str


def default_worker_command(plugin_path: Path) -> Sequence[str]:
    return [
        sys.executable,
        "-m",
        "episode.plugins.hikvision.sdk.worker",
        str(plugin_path),
    ]


class SDKDeviceWorker:
    """Supervises one native HCNetSDK subprocess for one configured device."""

    def __init__(
        self,
        plugin_path: Path,
        config: SDKDeviceConfig,
        delivery_sink: RawPluginDeliverySink,
        *,
        command: Sequence[str] | None = None,
        startup_timeout: float = 15.0,
        snapshot_timeout: float = SNAPSHOT_REQUEST_TIMEOUT_SECONDS,
    ):
        self._plugin_path = plugin_path
        self._config = config
        self._delivery_sink = delivery_sink
        self._command = tuple(command or default_worker_command(plugin_path))
        self._startup_timeout = startup_timeout
        self._snapshot_timeout = snapshot_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._wait_task: asyncio.Task | None = None
        self._startup: asyncio.Future[bool] | None = None
        self._snapshot_requests: dict[str, asyncio.Future[tuple[bytes, str]]] = {}
        self._snapshot_lock = asyncio.Lock()
        self._stopping = False
        self._status = PluginInstanceStatus(
            id=config.id,
            name=config.name,
            state=PluginInstanceState.STARTING,
            capabilities=("events",),
        )

    @property
    def device_id(self) -> str:
        return self._config.id

    def status(self) -> PluginInstanceStatus:
        return self._status

    async def start(self) -> bool:
        if self._process is not None:
            return self._status.state == PluginInstanceState.RUNNING

        self._stopping = False
        self._status = replace(
            self._status,
            state=PluginInstanceState.STARTING,
            error=None,
        )
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
                limit=MAX_NOTIFICATION_BYTES * 2,
            )
        except (OSError, ValueError):
            logger.exception("Could not start HCNetSDK worker for device %s", self._config.id)
            self._set_failed("The HCNetSDK worker could not be started.")
            return False

        loop = asyncio.get_running_loop()
        self._startup = loop.create_future()
        self._reader_task = asyncio.create_task(
            self._read_messages(),
            name=f"hikvision-sdk-reader:{self._config.id}",
        )
        self._wait_task = asyncio.create_task(
            self._watch_exit(),
            name=f"hikvision-sdk-wait:{self._config.id}",
        )

        worker_input = json.dumps(
            {
                "address": self._config.address,
                "port": self._config.port,
                "username": self._config.username,
                "password": self._config.password,
            },
            separators=(",", ":"),
        ).encode()
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(worker_input + b"\n")
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._set_failed("The HCNetSDK worker exited during startup.")

        try:
            return await asyncio.wait_for(
                asyncio.shield(self._startup),
                timeout=self._startup_timeout,
            )
        except TimeoutError:
            self._set_failed("HCNetSDK device startup timed out.")
            await self.stop(preserve_failure=True)
            return False

    async def stop(self, *, preserve_failure: bool = False) -> None:
        self._stopping = True
        process = self._process
        if process is not None:
            if process.returncode is None:
                process.terminate()
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
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

        self._fail_snapshot_requests("HCNetSDK worker stopped.")

        self._process = None
        self._reader_task = None
        self._wait_task = None
        if not preserve_failure:
            self._status = replace(self._status, state=PluginInstanceState.STOPPED)
        self._resolve_startup(False)

    async def capture_snapshot(self) -> tuple[bytes, str]:
        """Request one JPEG from the already-authenticated native worker."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._snapshot_timeout
        try:
            await asyncio.wait_for(self._snapshot_lock.acquire(), timeout=self._snapshot_timeout)
        except TimeoutError as exc:
            raise TimeoutError("HCNetSDK snapshot request timed out.") from exc
        try:
            process = self._process
            if process is None or process.returncode is not None:
                raise RuntimeError("HCNetSDK worker is not running.")
            if self._status.state != PluginInstanceState.RUNNING:
                raise RuntimeError("HCNetSDK worker is not ready for snapshots.")
            if process.stdin is None:
                raise RuntimeError("HCNetSDK worker command channel is unavailable.")
            request_id = uuid.uuid4().hex
            future = loop.create_future()
            self._snapshot_requests[request_id] = future
            try:
                command = json.dumps(
                    {"type": "snapshot", "request_id": request_id},
                    separators=(",", ":"),
                ).encode()
                process.stdin.write(command + b"\n")
                remaining = deadline - loop.time()
                await asyncio.wait_for(process.stdin.drain(), timeout=max(remaining, 0.001))
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=max(deadline - loop.time(), 0.001)
                )
            except (BrokenPipeError, ConnectionResetError, ValueError) as exc:
                self._set_failed("HCNetSDK worker command channel closed.")
                raise RuntimeError("HCNetSDK worker command channel closed.") from exc
            except TimeoutError as exc:
                raise TimeoutError("HCNetSDK snapshot request timed out.") from exc
            finally:
                self._snapshot_requests.pop(request_id, None)
                if future.done() and not future.cancelled():
                    future.exception()
        finally:
            self._snapshot_lock.release()

    async def _read_messages(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                if not line.startswith(WORKER_MESSAGE_PREFIX.encode()):
                    continue
                try:
                    message = json.loads(line[len(WORKER_MESSAGE_PREFIX) :])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning(
                        "HCNetSDK worker for %s returned an invalid message",
                        self._config.id,
                    )
                    continue
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except (ValueError, asyncio.LimitOverrunError):
            logger.exception("HCNetSDK worker protocol failed for device %s", self._config.id)
            self._set_failed("The HCNetSDK worker returned an invalid notification.")

    async def _handle_message(self, message: object) -> None:
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return
        message_type = message["type"]
        if message_type == "ready":
            now = datetime.now(tz=timezone.utc)
            device_info = self._device_info(message.get("device_info"))
            capabilities = ("events",)
            if message.get("snapshot_supported") is True:
                capabilities = (*capabilities, "snapshot")
            self._status = replace(
                self._status,
                state=PluginInstanceState.RUNNING,
                connected_at=now,
                error=None,
                device_info=device_info,
                capabilities=capabilities,
            )
            self._resolve_startup(True)
            return
        if message_type == "error":
            stage = message.get("stage")
            code = message.get("code")
            if isinstance(stage, str) and isinstance(code, int):
                error = f"HCNetSDK {stage} failed (error {code})."
            else:
                detail = message.get("message")
                error = detail if isinstance(detail, str) else "HCNetSDK worker startup failed."
            self._set_failed(error)
            return
        if message_type == "alarm":
            await self._preserve_alarm(message)
            return
        if message_type in {"snapshot", "snapshot_error"}:
            self._resolve_snapshot(message)
            return
        if message_type in {"notification_rejected", "callback_error"}:
            self._set_failed("HCNetSDK could not safely copy a device notification.")

    @staticmethod
    def _device_info(value: object) -> PluginDeviceInfo | None:
        if not isinstance(value, dict):
            return None

        def optional_text(field: str) -> str | None:
            candidate = value.get(field)
            return candidate if isinstance(candidate, str) and candidate else None

        info = PluginDeviceInfo(
            manufacturer=optional_text("manufacturer"),
            model=optional_text("model"),
            firmware_version=optional_text("firmware_version"),
        )
        return info if any((info.manufacturer, info.model, info.firmware_version)) else None

    async def _preserve_alarm(self, message: dict) -> None:
        command = message.get("command")
        declared_length = message.get("length")
        encoded = message.get("payload")
        if not isinstance(command, int) or not isinstance(declared_length, int):
            self._set_failed("HCNetSDK returned invalid notification metadata.")
            return
        if not isinstance(encoded, str):
            self._set_failed("HCNetSDK returned an invalid notification payload.")
            return
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            self._set_failed("HCNetSDK returned an invalid notification payload.")
            return
        if len(payload) != declared_length or len(payload) > MAX_NOTIFICATION_BYTES:
            self._set_failed("HCNetSDK returned an invalid notification length.")
            return

        received_at = datetime.now(tz=timezone.utc)
        worker_received_at = message.get("received_at")
        if isinstance(worker_received_at, str):
            try:
                received_at = datetime.fromisoformat(worker_received_at)
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        try:
            await self._delivery_sink(
                RawPluginDelivery(
                    plugin_id="hikvision-sdk",
                    device_id=self._config.id,
                    area_id=self._config.area_id,
                    received_at=received_at,
                    payload=payload,
                    metadata={
                        "command": command,
                        "sdk_buffer_length": declared_length,
                    },
                )
            )
        except Exception:
            logger.exception(
                "Could not preserve HCNetSDK notification for device %s",
                self._config.id,
            )
            self._set_failed("A raw HCNetSDK notification could not be preserved.")
            return

        self._status = replace(
            self._status,
            messages_received=self._status.messages_received + 1,
            last_message_at=received_at,
        )

    async def _watch_exit(self) -> None:
        assert self._process is not None
        return_code = await self._process.wait()
        if self._stopping:
            return
        if self._status.state != PluginInstanceState.FAILED:
            self._set_failed(f"The HCNetSDK worker exited unexpectedly (code {return_code}).")

    def _set_failed(self, error: str) -> None:
        self._status = replace(
            self._status,
            state=PluginInstanceState.FAILED,
            error=error,
        )
        self._fail_snapshot_requests(error)
        self._resolve_startup(False)

    def _resolve_snapshot(self, message: dict) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str):
            return
        future = self._snapshot_requests.get(request_id)
        if future is None or future.done():
            return
        if message.get("type") == "snapshot_error":
            detail = message.get("message")
            if not isinstance(detail, str) or not detail:
                detail = "HCNetSDK snapshot capture failed."
            code = message.get("code")
            if isinstance(code, int):
                detail = f"{detail} (error {code})."
            future.set_exception(RuntimeError(detail))
            return

        encoded = message.get("payload")
        declared_length = message.get("length")
        content_type = message.get("content_type")
        if not isinstance(encoded, str) or not isinstance(declared_length, int):
            future.set_exception(RuntimeError("HCNetSDK returned an invalid snapshot response."))
            return
        if not 4 <= declared_length <= MAX_SNAPSHOT_BYTES:
            future.set_exception(RuntimeError("HCNetSDK returned an invalid snapshot size."))
            return
        if len(encoded) != ((declared_length + 2) // 3) * 4:
            future.set_exception(RuntimeError("HCNetSDK returned an invalid snapshot length."))
            return
        if content_type != "image/jpeg":
            future.set_exception(RuntimeError("HCNetSDK returned an unsupported snapshot type."))
            return
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            future.set_exception(RuntimeError("HCNetSDK returned invalid snapshot bytes."))
            return
        if (
            len(payload) != declared_length
            or not 4 <= len(payload) <= MAX_SNAPSHOT_BYTES
            or payload[:2] != b"\xff\xd8"
            or payload[-2:] != b"\xff\xd9"
        ):
            future.set_exception(RuntimeError("HCNetSDK returned invalid JPEG data."))
            return
        future.set_result((payload, content_type))

    def _fail_snapshot_requests(self, message: str) -> None:
        for future in self._snapshot_requests.values():
            if not future.done():
                future.set_exception(RuntimeError(message))

    def _resolve_startup(self, result: bool) -> None:
        if self._startup is not None and not self._startup.done():
            self._startup.set_result(result)
