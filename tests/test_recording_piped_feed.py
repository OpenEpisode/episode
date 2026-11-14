"""The relay between a plugin handler and one recorder attempt's ffmpeg pipes."""

from __future__ import annotations

import asyncio

import pytest

from episode.recording.piped_feed import PipeClosedError, PipedFeed


class FakeWriter:
    """Stands in for an asyncio StreamWriter: records writes, can be made to fail."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self._fail_with = fail_with

    def write(self, chunk: bytes) -> None:
        self.writes.append(chunk)

    async def drain(self) -> None:
        if self._fail_with is not None:
            raise self._fail_with

    def close(self) -> None:
        self.closed = True


class FakeProc:
    def __init__(self) -> None:
        self.stdin = FakeWriter()
        self.returncode: int | None = None


def _feed(**kwargs) -> PipedFeed:
    return PipedFeed(write_timeout=1.0, **kwargs)


@pytest.mark.asyncio
async def test_chunks_pushed_before_ffmpeg_exists_are_flushed_in_arrival_order():
    feed = _feed()
    proc, audio = FakeProc(), FakeWriter()

    await feed.push_video(b"v1")
    await feed.push_audio(b"a1")
    await feed.push_video(b"v2")
    assert proc.stdin.writes == [], "nothing reaches ffmpeg before it is attached"

    await feed.attach(proc, audio)
    assert proc.stdin.writes == [b"v1", b"v2"]
    assert audio.writes == [b"a1"]

    await feed.push_video(b"v3")
    await feed.push_audio(b"a2")
    assert proc.stdin.writes[-1] == b"v3"
    assert audio.writes[-1] == b"a2"
    assert feed.audio_recorded
    assert feed.audio_dropped == 0


@pytest.mark.asyncio
async def test_audio_is_recognised_only_when_it_arrives_with_the_picture():
    arrives = _feed()
    await arrives.push_video(b"v")
    await arrives.push_audio(b"a")
    assert await arrives.has_audio(first_video_timeout=0.2, audio_wait=0.2) is True

    silent = _feed()
    await silent.push_video(b"v")
    assert await silent.has_audio(first_video_timeout=0.2, audio_wait=0.05) is False

    no_picture = _feed()
    assert await no_picture.has_audio(first_video_timeout=0.05, audio_wait=0.05) is False


@pytest.mark.asyncio
async def test_audio_wait_starts_at_the_first_video_chunk_not_at_the_call():
    feed = _feed()

    async def late_start() -> None:
        await asyncio.sleep(0.15)  # a slow camera start must not read as "no microphone"
        await feed.push_video(b"v")
        await asyncio.sleep(0.02)
        await feed.push_audio(b"a")

    task = asyncio.create_task(late_start())
    assert await feed.has_audio(first_video_timeout=1.0, audio_wait=0.2) is True
    await task


@pytest.mark.asyncio
async def test_audio_without_an_input_is_dropped_and_counted_never_written():
    feed = _feed()
    proc = FakeProc()
    await feed.push_video(b"v")
    await feed.push_audio(b"held")
    await feed.attach(proc, None)

    await feed.push_audio(b"late")
    assert proc.stdin.writes == [b"v"]
    assert feed.audio_dropped == 2
    assert not feed.audio_recorded


@pytest.mark.asyncio
async def test_a_failing_audio_pipe_loses_audio_but_never_fails_the_video():
    feed = _feed()
    proc, audio = FakeProc(), FakeWriter(fail_with=BrokenPipeError("gone"))
    await feed.attach(proc, audio)

    await feed.push_audio(b"a1")  # must not raise into the handler
    await feed.push_audio(b"a2")
    await feed.push_video(b"v")

    assert proc.stdin.writes == [b"v"]
    assert feed.audio_dropped == 2
    assert not feed.audio_recorded


@pytest.mark.asyncio
async def test_a_dead_child_is_reported_to_the_handler_through_the_video_push():
    feed = _feed()
    proc = FakeProc()
    await feed.attach(proc)
    proc.returncode = 1
    with pytest.raises(PipeClosedError):
        await feed.push_video(b"v")


@pytest.mark.asyncio
async def test_finishing_closes_both_inputs_after_everything_was_flushed():
    feed = _feed()
    proc, audio = FakeProc(), FakeWriter()
    await feed.push_video(b"v")
    await feed.push_audio(b"a")
    feed.finish()  # the source ended before ffmpeg was running
    assert not proc.stdin.closed and not audio.closed

    await feed.attach(proc, audio)
    assert proc.stdin.writes == [b"v"] and audio.writes == [b"a"]
    assert proc.stdin.closed and audio.closed

    live = _feed()
    live_proc, live_audio = FakeProc(), FakeWriter()
    await live.attach(live_proc, live_audio)
    live.finish()
    assert live_proc.stdin.closed and live_audio.closed


@pytest.mark.asyncio
async def test_a_handler_cannot_grow_memory_while_ffmpeg_is_not_running():
    feed = _feed(max_held_bytes=10)
    proc = FakeProc()

    blocked = asyncio.create_task(feed.push_video(b"x" * 20))
    await asyncio.sleep(0.05)
    assert not blocked.done(), "push waits for the child once the held bound is passed"

    await feed.attach(proc)
    await asyncio.wait_for(blocked, timeout=1.0)
    assert proc.stdin.writes == [b"x" * 20]


@pytest.mark.asyncio
async def test_audio_past_the_held_bound_is_dropped_without_blocking():
    feed = _feed(max_held_bytes=10)
    await feed.push_audio(b"y" * 20)  # accepted: the bound is checked before each chunk
    await asyncio.wait_for(feed.push_audio(b"z"), timeout=0.5)
    assert feed.audio_dropped == 1
