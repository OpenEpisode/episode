from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import pytest

from episode.engine.bus import EventBus
from episode.recording.engine import RecordingEngine, _EpisodeRecording
from episode.recording.hls import HLSCaptureState, HLSRecordingBundle


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_bundle", [False, True])
async def test_real_child_partial_stays_incomplete_after_failed_publication(
    tmp_path, reload_bundle
):
    now = datetime.now(tz=timezone.utc)
    bundle = HLSRecordingBundle.create(
        tmp_path / "bundle",
        HLSCaptureState(
            evidence_id="evidence",
            episode_id="episode",
            device_id="camera",
            area_id="area",
            session_id="session",
            started_at=now,
        ),
    )
    attempts = []

    async def publish(evidence):
        attempts.append(evidence.evidence_type)
        if len(attempts) == 1:
            raise OSError("publication failed")
        return evidence

    class Repository:
        async def append_episode_journal(self, *_args):
            pass

    recorder = RecordingEngine(Repository(), EventBus(), str(tmp_path), evidence_sink=publish)
    child = recorder._spawn_ffmpeg(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "Path('init.mp4').write_bytes(b'init'); "
            "Path('segments/segment-000000.m4s').write_bytes(b'fragment'); "
            "Path('segments/segment-000001.m4s.tmp').write_bytes(b'partial'); "
            "Path('index.m3u8').write_text('#EXTM3U\\n#EXT-X-MAP:URI=\"init.mp4\"\\n"
            "#EXTINF:4.0,\\nsegments/segment-000000.m4s\\n')",
        ],
        cwd=str(bundle.root),
    )
    try:
        assert await asyncio.wait_for(child.wait(), 3) == 0
    finally:
        if child.returncode is None:
            child.kill()
            await asyncio.wait_for(child.wait(), 3)
        child.stderr.close()

    def recording(current_bundle):
        return _EpisodeRecording(
            episode_id="episode",
            device_id="camera",
            area_id="area",
            session_id="session",
            bundle=current_bundle,
            start_time=now,
        )

    rec = recording(bundle)
    with pytest.raises(OSError, match="publication failed"):
        await recorder._finalize_bundle(rec)
    assert json.loads(bundle.component_manifest_path.read_text())["state"] == "incomplete"
    if reload_bundle:
        rec = recording(HLSRecordingBundle.load(bundle.capture_state_path))
    await recorder._finalize_bundle(rec)
    assert attempts == ["incomplete_recording", "incomplete_recording"]
    manifest = json.loads(bundle.component_manifest_path.read_text())
    assert manifest["state"] == "incomplete"
    assert manifest["playlist_validation"]["temporary_components_preserved"] is True
    assert (bundle.root / "incomplete/segment-000001.m4s.tmp").read_bytes() == b"partial"
