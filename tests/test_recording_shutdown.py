from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from episode.engine.bus import EventBus, Message
from episode.recording.engine import RecordingEngine, _EpisodeRecording
from episode.recording.hls import HLSCaptureState, HLSRecordingBundle


class PendingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.wait_started = asyncio.Event()
        self._finished = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.returncode = -9
        self._finished.set()

    async def wait(self) -> int:
        self.wait_started.set()
        await self._finished.wait()
        return self.returncode or 0

    def finish(self, returncode: int = -15) -> None:
        self.returncode = returncode
        self._finished.set()


class ExitedProcess:
    returncode = 0

    def __init__(self) -> None:
        self.terminate_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1


def _recording(tmp_path: Path, process=None) -> _EpisodeRecording:
    started_at = datetime.now(tz=timezone.utc)
    bundle = HLSRecordingBundle.create(
        tmp_path / "bundle",
        HLSCaptureState(
            evidence_id="evidence",
            episode_id="episode",
            device_id="camera",
            area_id="area",
            session_id="session",
            started_at=started_at,
        ),
    )
    return _EpisodeRecording(
        episode_id="episode",
        device_id="camera",
        area_id="area",
        session_id="session",
        bundle=bundle,
        start_time=started_at,
        proc=process,
    )


@pytest.mark.asyncio
async def test_lease_expiry_and_finalization_signal_a_pending_child_once(tmp_path):
    process = PendingProcess()
    recording = _recording(tmp_path, process)
    recorder = RecordingEngine(object(), EventBus(), str(tmp_path))
    recorder._recordings[(recording.episode_id, recording.device_id)] = recording

    await recorder._on_capture_lease_expired(
        Message(
            type="episode.capture_lease_expired",
            data={"episode_ids": [recording.episode_id]},
        )
    )

    stop_task = asyncio.create_task(recorder._stop_recordings([recording]))
    await process.wait_started.wait()
    assert process.terminate_calls == 1

    process.finish()
    await stop_task
    assert process.terminate_calls == 1


def test_replacement_child_receives_a_new_terminate(tmp_path):
    first = PendingProcess()
    recording = _recording(tmp_path, first)

    RecordingEngine._signal_child_process(recording)
    assert first.terminate_calls == 1
    first.finish()

    replacement = PendingProcess()
    recording.proc = replacement
    RecordingEngine._signal_child_process(recording)

    assert first.terminate_calls == 1
    assert replacement.terminate_calls == 1


@pytest.mark.asyncio
async def test_exited_or_missing_child_is_safe_and_handler_is_cancelled(tmp_path):
    recording = _recording(tmp_path, ExitedProcess())
    RecordingEngine._signal_child_process(recording)
    assert recording.proc.terminate_calls == 0

    recording.proc = None
    RecordingEngine._signal_child_process(recording)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    recording.handler_task = asyncio.create_task(handler())
    await started.wait()
    RecordingEngine._signal_process(recording)
    await asyncio.gather(recording.handler_task, return_exceptions=True)

    assert cancelled.is_set()
