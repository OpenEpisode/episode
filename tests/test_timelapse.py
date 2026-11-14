import asyncio
import os
import re
from datetime import datetime, timezone

import pytest

from episode.domain.models import Evidence
from episode.media.timelapse import TimelapseService


class _Process:
    returncode = 0

    async def wait(self):
        return 0


@pytest.mark.asyncio
async def test_generate_links_relative_snapshot_paths_so_ffmpeg_can_open_them(
    tmp_path, monkeypatch
):
    """A data directory configured relatively must not produce dangling frames."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("data/snapshots")
    snapshots = []
    for index in range(2):
        path = f"data/snapshots/frame{index}.jpg"
        with open(path, "wb") as stream:
            stream.write(b"jpeg")
        snapshots.append(
            Evidence(
                id=f"snapshot-{index}",
                device_id="camera",
                area_id="area",
                timestamp=datetime(2026, 10, 30, 22, 0, index, tzinfo=timezone.utc),
                evidence_type="snapshot",
                file_path=path,
            )
        )

    opened: list[bool] = []

    async def fake_ffmpeg(*args, **_kwargs):
        concat_file = args[args.index("-i") + 1]
        with open(concat_file, encoding="utf-8") as stream:
            frames = re.findall(r"^file '(.+)'$", stream.read(), flags=re.MULTILINE)
        opened.extend(os.path.isfile(frame) for frame in frames)
        with open(args[-1], "wb") as stream:
            stream.write(b"mp4")
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_ffmpeg)

    cache_path = str(tmp_path / "cache" / "timelapse.mp4")
    await TimelapseService(repository=None, data_dir="data")._generate(snapshots, cache_path)

    assert opened == [True, True]
    assert os.path.isfile(cache_path)
