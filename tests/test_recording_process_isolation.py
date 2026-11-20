from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Episode, EpisodeState
from episode.engine.bus import EventBus
from episode.engine.engine import EpisodeEngine
from episode.recording.engine import RecordingEngine
from episode.storage.repository import Repository


def test_finalization_reaps_child_with_two_worker_executor_and_unrelated_live_capture(tmp_path):
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        config = EpisodeConfig(data_dir=str(tmp_path), db_path=str(tmp_path / "test.db"))
        repo = Repository(config)
        await repo.initialize()
        bus = EventBus()
        engine = EpisodeEngine(repo, bus, timeout=30, quiescent_grace_seconds=1)
        recorder = RecordingEngine(repo, bus, str(tmp_path))
        recorder.set_evidence_sink(engine.ingest_recording_evidence)
        engine.set_finalizer(recorder.finalize_episode)
        now = datetime.now(tz=timezone.utc)
        await repo.upsert_area(Area(id="area", name="Area"))
        device = Device(id="camera", name="Camera", device_type="camera", area_id="area")
        await repo.upsert_device(device)
        await repo.create_episode(
            Episode(id="live", primary_area_id="area", state=EpisodeState.ACTIVE)
        )
        await repo.create_episode(
            Episode(
                id="closing",
                primary_area_id="area",
                state=EpisodeState.QUIESCENT,
                minimum_end_at=now - timedelta(seconds=30),
            )
        )
        recorder._rtsp_command = lambda *_args: [
            sys.executable,
            "-c",
            "from pathlib import Path; import time; "
            "Path('segments/segment-000000.m4s.tmp').write_bytes(b'partial'); "
            "time.sleep(60)",
        ]
        recordings = []
        processes = []
        try:
            await recorder.start()
            for episode_id in ("live", "closing"):
                await recorder._start_recording(episode_id, device, "rtsp://example/stream")
                recording = recorder._recordings[(episode_id, device.id)]
                recordings.append(recording)
                async with asyncio.timeout(3):
                    while (
                        recording.proc is None
                        or not (recording.bundle.root / "segments/segment-000000.m4s.tmp").exists()
                    ):
                        await asyncio.sleep(0.01)
                processes.append(recording.proc)
            # A live recording's lifetime workers must leave the shared pool usable.
            assert await asyncio.wait_for(loop.run_in_executor(None, lambda: 42), 1) == 42

            await asyncio.wait_for(engine._close_timed_out_episodes(now=now), timeout=3)
            assert (await repo.get_episode("closing")).state == EpisodeState.CLOSED
            assert processes[1].returncode is not None
            assert processes[1].stderr.closed
            assert processes[0].returncode is None
            evidence = await repo.get_evidence(recordings[1].evidence_id)
            assert evidence.evidence_type == "incomplete_recording"
            files = {
                path: (path.stat().st_size, path.stat().st_mtime_ns)
                for path in recordings[1].bundle.root.rglob("*")
                if path.is_file()
            }
            await asyncio.sleep(0.05)
            assert files == {
                path: (path.stat().st_size, path.stat().st_mtime_ns)
                for path in recordings[1].bundle.root.rglob("*")
                if path.is_file()
            }
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), timeout=3)
            await asyncio.wait_for(recorder.stop(), timeout=5)
            await repo.close()

    asyncio.run(scenario())
