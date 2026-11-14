"""Caller-only RTC audio reader for a task's in-band DTMF detector."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from livekit import rtc
from loguru import logger

from utils.async_utils import await_through_cancellation
from utils.inband_dtmf import InbandDtmfDetector


class CallerDtmfAudio:
    """Own bounded AudioStreams independently of AgentSession audio input.

    Each track has a fresh detector, so buffered PCM cannot cross track changes.
    Generation checks fence old readers before replacement or cleanup awaits.
    """

    def __init__(
        self,
        room: rtc.Room,
        caller_identity: str,
        on_digit: Callable[[str], None],
        is_active: Callable[[], bool],
        *,
        event_prefix: str = "GREETING_DTMF",
        log_digits: bool = True,
    ) -> None:
        self._event_prefix = event_prefix
        self._log_digits = log_digits
        self._room = room
        self._caller_identity = caller_identity
        self._on_digit = on_digit
        self._is_active = is_active
        self._generation = 0
        self._track = None
        self._reader: asyncio.Task | None = None
        self._changed = asyncio.Event()
        self._closed = False
        self._listeners = {
            "track_subscribed": self._on_subscribed,
            "track_unsubscribed": self._on_unsubscribed,
            "track_unpublished": self._on_unpublished,
        }

    def start(self) -> None:
        for event, callback in self._listeners.items():
            self._room.on(event, callback)
        participant = self._room.remote_participants.get(self._caller_identity)
        if participant is not None:
            for publication in participant.track_publications.values():
                track = publication.track
                if track is not None and track.kind == rtc.TrackKind.KIND_AUDIO:
                    self._replace(track)
                    break

    def _on_subscribed(self, track, publication, participant) -> None:
        if (
            participant.identity == self._caller_identity
            and track.kind == rtc.TrackKind.KIND_AUDIO
        ):
            self._replace(track)

    def _on_unsubscribed(self, track, publication, participant) -> None:
        if participant.identity == self._caller_identity and track is self._track:
            self._replace(None)

    def _on_unpublished(self, publication, participant) -> None:
        if (
            participant.identity == self._caller_identity
            and self._track is not None
            and publication.sid == self._track.sid
        ):
            self._replace(None)

    def _replace(self, track) -> None:
        if self._closed or not self._is_active() or track is self._track:
            return
        self._generation += 1
        self._track = track
        self._changed.set()
        if self._reader is None:
            self._reader = asyncio.create_task(self._manage_tracks())

    async def _manage_tracks(self) -> None:
        worker = None
        try:
            while not self._closed:
                self._changed.clear()
                if self._track is not None and self._is_active():
                    worker = asyncio.create_task(
                        self._read(self._track, self._generation)
                    )
                await self._changed.wait()
                if worker is not None:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    worker = None
        finally:
            if worker is not None:
                if not worker.cancelling():
                    worker.cancel()
                await asyncio.gather(asyncio.shield(worker), return_exceptions=True)

    async def _read(self, track, generation: int) -> None:
        stream = None
        try:
            if self._closed or generation != self._generation or not self._is_active():
                return
            detector = InbandDtmfDetector(sample_rate=8000)
            stream = rtc.AudioStream(
                track,
                sample_rate=8000,
                num_channels=1,
                frame_size_ms=20,
                capacity=5,
            )
            async for event in stream:
                if (
                    self._closed
                    or generation != self._generation
                    or not self._is_active()
                ):
                    break
                for digit in detector.feed(event.frame.data):
                    if generation != self._generation or not self._is_active():
                        break
                    logger.bind(
                        event=(
                            "GREETING_INBAND_DTMF_DETECTED"
                            if self._event_prefix == "GREETING_DTMF"
                            else f"{self._event_prefix}_INBAND_DETECTED"
                        ),
                        **({"digit": digit} if self._log_digits else {}),
                    ).info("In-band DTMF detected")
                    self._on_digit(digit)
                # A full queue must not let DSP monopolize the event loop.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._log_digits:
                logger.exception("In-band DTMF audio reader failed")
            else:
                # A traceback may include local PCM or digit values.
                logger.error("In-band DTMF audio reader failed")
        finally:
            if stream is not None:
                # Cancellation can arrive during natural end-of-stream cleanup or
                # again while a prior cancellation is already closing RTC resources.
                await await_through_cancellation(asyncio.create_task(stream.aclose()))

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._generation += 1
            for event, callback in self._listeners.items():
                try:
                    self._room.off(event, callback)
                except Exception:
                    logger.exception("Failed to unregister DTMF track listener")
        self._changed.set()
        reader = self._reader
        if reader is not None:
            await await_through_cancellation(reader)
        self._track = None
        self._reader = None
