"""Prefer SIP DTMF per input while allowing a delayed audio fallback."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from loguru import logger

DtmfSource = Literal["sip", "inband"]


@dataclass
class _Observation:
    digit: str
    observed_at: float
    deadline: float
    sip: bool = False
    inband: bool = False
    accepted: bool = False
    ready_source: DtmfSource | None = None


class DtmfInputArbiter:
    """Bounded, one-to-one matching of opposite-source observations.

    Repeated observations from the same source remain separate inputs. Since SIP
    provides no shared press ID, densely interleaved same-key presses can only be
    correlated by FIFO order within a finite window, not identified exactly.
    """

    MAX_RECORDS = 64

    def __init__(
        self,
        on_digit: Callable[[str], None],
        is_active: Callable[[], bool],
        *,
        fallback_delay: float = 0.2,
        dedup_window: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        event_prefix: str = "GREETING_DTMF",
        log_digits: bool = True,
        log_accepted_digits: bool | None = None,
        preserve_order: bool = False,
    ) -> None:
        if not 0 <= fallback_delay < dedup_window <= 5:
            raise ValueError("DTMF delays require 0 <= fallback < dedup <= 5000 ms")
        self._preserve_order = preserve_order
        self._event_prefix = event_prefix
        self._log_digits = log_digits
        self._log_accepted_digits = (
            log_digits if log_accepted_digits is None else log_accepted_digits
        )
        self._on_digit = on_digit
        self._is_active = is_active
        self._fallback_delay = fallback_delay
        self._dedup_window = dedup_window
        self._clock = clock
        self._records: list[_Observation] = []
        self._changed = asyncio.Event()
        self._timer: asyncio.Task | None = None
        self._closed = False

    @property
    def has_pending(self) -> bool:
        return not self._closed and any(not record.accepted for record in self._records)

    def observe(self, digit: str, source: DtmfSource) -> None:
        if self._closed or not self._is_active():
            return
        now = self._clock()
        self._records = [
            record
            for record in self._records
            if not record.accepted or now - record.observed_at <= self._dedup_window
        ]
        opposite = "inband" if source == "sip" else "sip"
        counterpart = next(
            (
                record
                for record in self._records
                if record.digit == digit
                and getattr(record, opposite)
                and not getattr(record, source)
                and now - record.observed_at <= self._dedup_window
            ),
            None,
        )
        if counterpart is not None:
            setattr(counterpart, source, True)
            if not counterpart.accepted:
                self._accept(counterpart, "sip")
            else:
                logger.bind(
                    event=f"{self._event_prefix}_DEDUPLICATED",
                    source=source,
                    **({"digit": digit} if self._log_digits else {}),
                ).debug("DTMF counterpart suppressed")
            self._changed.set()
            return
        if len(self._records) >= self.MAX_RECORDS:
            settled = next(
                (record for record in self._records if record.accepted), None
            )
            if settled is None:
                logger.bind(
                    event=f"{self._event_prefix}_PENDING_OVERFLOW", source=source
                ).warning("DTMF pending input limit reached")
                if source == "inband":
                    return
                # Confirmed SIP input takes priority over the oldest unconfirmed
                # audio observation; retain its record for counterpart suppression.
                settled = next(
                    (record for record in self._records if record.ready_source is None),
                    None,
                )
                if settled is None:
                    return
            self._records.remove(settled)
            if self._preserve_order:
                self._drain_ready()
        record = _Observation(digit, now, now + self._fallback_delay)
        setattr(record, source, True)
        self._records.append(record)
        if source == "sip":
            self._accept(record, "sip")
        else:
            if self._timer is None or self._timer.done():
                self._timer = asyncio.create_task(self._run_pending())
            self._changed.set()

    def _accept(self, record: _Observation, source: DtmfSource) -> None:
        record.ready_source = source
        if self._preserve_order:
            self._drain_ready()
        else:
            self._deliver(record)

    def _drain_ready(self) -> None:
        for record in list(self._records):
            if record.accepted:
                continue
            if record.ready_source is None:
                break
            self._deliver(record)

    def _deliver(self, record: _Observation) -> None:
        record.accepted = True
        if self._closed or not self._is_active():
            return
        source = record.ready_source
        logger.bind(
            event=f"{self._event_prefix}_ACCEPTED",
            source=source,
            **({"digit": record.digit} if self._log_accepted_digits else {}),
            fallback=source == "inband",
        ).info("DTMF input accepted")
        self._on_digit(record.digit)

    def _deliver_due(self) -> None:
        """Process due observations in order; also supports deterministic clock tests."""
        now = self._clock()
        for record in list(self._records):
            if record.ready_source is None and record.deadline <= now:
                self._accept(record, "inband")

    async def _run_pending(self) -> None:
        while not self._closed and self._is_active():
            self._deliver_due()
            pending = [
                record for record in self._records if record.ready_source is None
            ]
            if not pending:
                return
            self._changed.clear()
            delay = max(0, min(record.deadline for record in pending) - self._clock())
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout=delay)

    async def aclose(self) -> None:
        # Synchronous fence executes before the first cleanup await.
        self._closed = True
        self._records.clear()
        self._changed.set()
        if self._timer is not None:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
