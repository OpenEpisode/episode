from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

PROBE_RESULT_PREFIX = "EPISODE_PLUGIN_RESULT="


@dataclass(frozen=True)
class ProbeResult:
    payload: dict | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.payload is not None and self.error is None


class SubprocessProbeRunner:
    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None

    async def run(
        self,
        command: Sequence[str],
        *,
        environment: dict[str, str] | None = None,
        redact_paths: Sequence[Path] = (),
    ) -> ProbeResult:
        child_environment = os.environ.copy()
        child_environment.update(environment or {})

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_environment,
            )
        except (OSError, TypeError, ValueError) as exc:
            detail = self._sanitize(str(exc), redact_paths)
            return ProbeResult(error=f"Could not start plugin validation: {detail}.")

        try:
            stdout, stderr = await asyncio.wait_for(
                self._process.communicate(), timeout=self._timeout
            )
        except TimeoutError:
            self._process.kill()
            await self._process.wait()
            return ProbeResult(error="Plugin validation timed out.")
        except asyncio.CancelledError:
            if self._process.returncode is None:
                self._process.kill()
                await self._process.wait()
            raise
        finally:
            process = self._process
            self._process = None

        output = stdout.decode("utf-8", errors="replace")
        payload = None
        for line in output.splitlines():
            if line.startswith(PROBE_RESULT_PREFIX):
                try:
                    candidate = json.loads(line.removeprefix(PROBE_RESULT_PREFIX))
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    payload = candidate

        return_code = process.returncode if process is not None else None
        if payload is not None and payload.get("ok") is False:
            error = payload.get("error")
            if isinstance(error, str) and error:
                return ProbeResult(error=self._sanitize(error, redact_paths))
        if return_code == 0 and payload is not None and payload.get("ok") is True:
            return ProbeResult(payload=payload)
        if return_code:
            detail = self._sanitize(stderr.decode("utf-8", errors="replace"), redact_paths)
            message = f"Plugin validation exited unexpectedly (code {return_code})."
            if detail:
                message = f"{message} {detail}"
            return ProbeResult(error=message)
        return ProbeResult(error="Plugin validation returned an invalid result.")

    async def stop(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        self._process = None

    @staticmethod
    def _sanitize(message: str, paths: Sequence[Path]) -> str:
        for path in paths:
            message = message.replace(str(path), "<plugin-dir>")
        return " ".join(message.split())[:300]
