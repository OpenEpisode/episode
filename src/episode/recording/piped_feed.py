"""Relay a plugin handler's video and audio bytes into one recorder attempt's pipes.

The recorder owns the ffmpeg child; a plugin only calls ``push_video`` and, when its
source declares audio, ``push_audio``. Without audio this is a thin pass-through to the
child's stdin. With audio the handler starts *before* ffmpeg: ffmpeg refuses to start on an
audio input that closes without data, and blocks on one that never delivers, so the
recorder waits for audio to actually arrive (:meth:`PipedFeed.has_audio`) before it commits
the child to an audio input. Everything the handler pushed in the meantime is held, bounded,
and flushed in order once the child exists (:meth:`PipedFeed.attach`).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

#: Bytes held while ffmpeg is not running yet. Past this the handler's ``push`` waits for
#: the child, which is the backpressure that keeps a plugin from growing recorder memory.
MAX_HELD_BYTES = 8 * 1024 * 1024


class PipeClosedError(RuntimeError):
    """Raised into a video handler when its recorder child has already gone."""


class PipedFeed:
    """The bytes one recorder attempt's handler pushes, relayed to ffmpeg's pipes."""

    def __init__(self, *, write_timeout: float, max_held_bytes: int = MAX_HELD_BYTES) -> None:
        self._write_timeout = write_timeout
        self._max_held_bytes = max_held_bytes
        self._proc: asyncio.subprocess.Process | None = None
        self._audio: asyncio.StreamWriter | None = None
        self._audio_dead = False
        self._held_video: list[bytes] = []
        self._held_audio: list[bytes] = []
        self._held_bytes = 0
        self._video_seen = asyncio.Event()
        self._audio_seen = asyncio.Event()
        self._attached = asyncio.Event()
        self._finished = False
        #: Audio chunks not written: no audio input was negotiated, or its pipe failed.
        self.audio_dropped = 0

    @property
    def attached(self) -> bool:
        return self._proc is not None

    @property
    def audio_arrived(self) -> bool:
        """True once the handler has pushed any audio."""
        return self._audio_seen.is_set()

    @property
    def audio_recorded(self) -> bool:
        """True while an audio input is attached and has not failed."""
        return self._audio is not None and not self._audio_dead

    async def push_video(self, chunk: bytes) -> None:
        """Hand one framed video chunk to ffmpeg, or hold it until ffmpeg exists."""
        proc = self._proc
        if proc is None:
            self._held_video.append(chunk)
            self._held_bytes += len(chunk)
            self._video_seen.set()
            if self._held_bytes > self._max_held_bytes:
                await self._attached.wait()
            return
        if proc.stdin is None or proc.returncode is not None:
            raise PipeClosedError("recorder child process is no longer reading")
        proc.stdin.write(chunk)
        await asyncio.wait_for(proc.stdin.drain(), timeout=self._write_timeout)

    async def push_audio(self, chunk: bytes) -> None:
        """Hand one framed audio chunk to ffmpeg; never raises into the handler.

        Audio is a companion of the picture: a stalled or closed audio pipe loses audio
        and is counted, but must not fail the video the same handler is also feeding.
        """
        if self._proc is None:
            if self._held_bytes > self._max_held_bytes:
                self.audio_dropped += 1
                return
            self._held_audio.append(chunk)
            self._held_bytes += len(chunk)
            self._audio_seen.set()
            return
        if self._audio is None or self._audio_dead:
            self.audio_dropped += 1
            return
        try:
            self._audio.write(chunk)
            await asyncio.wait_for(self._audio.drain(), timeout=self._write_timeout)
        except (OSError, asyncio.TimeoutError, RuntimeError) as error:
            self._audio_dead = True
            self.audio_dropped += 1
            logger.warning("Recording audio input stopped (%s); continuing with video", error)

    async def has_audio(self, *, first_video_timeout: float, audio_wait: float) -> bool:
        """Whether audio arrives with the picture, judged from what the handler pushed.

        The audio wait starts when the first video chunk lands, so a slow camera start does
        not read as a camera without a microphone; ``first_video_timeout`` bounds that wait.
        """
        try:
            await asyncio.wait_for(self._video_seen.wait(), timeout=first_video_timeout)
            await asyncio.wait_for(self._audio_seen.wait(), timeout=audio_wait)
        except asyncio.TimeoutError:
            return False
        return True

    async def attach(
        self, proc: asyncio.subprocess.Process, audio: asyncio.StreamWriter | None = None
    ) -> None:
        """Connect the running child, flushing everything held so far in arrival order."""
        stdin = proc.stdin
        while self._held_video or self._held_audio:
            if self._held_video:
                chunk = self._held_video.pop(0)
                if stdin is None or proc.returncode is not None:
                    raise PipeClosedError("recorder child process is no longer reading")
                stdin.write(chunk)
                await asyncio.wait_for(stdin.drain(), timeout=self._write_timeout)
            if self._held_audio:
                chunk = self._held_audio.pop(0)
                if audio is None:
                    self.audio_dropped += 1
                else:
                    audio.write(chunk)
                    await asyncio.wait_for(audio.drain(), timeout=self._write_timeout)
        # No await between the last emptiness check and these assignments, so a chunk that
        # arrived during a drain above was flushed and later ones go straight through.
        self._audio = audio
        self._proc = proc
        self._held_bytes = 0
        self._attached.set()
        if self._finished:
            self.close()

    def finish(self) -> None:
        """The handler has nothing more to push: end both inputs once everything is flushed."""
        self._finished = True
        if self._proc is not None:
            self.close()

    def close(self) -> None:
        """Close ffmpeg's inputs so it flushes its final fragment and exits."""
        proc, audio = self._proc, self._audio
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001 - closing an already-closed pipe is the goal
                pass
        if audio is not None:
            try:
                audio.close()
            except Exception:  # noqa: BLE001
                pass
