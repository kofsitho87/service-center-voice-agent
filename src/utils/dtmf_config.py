"""Shared code-only DTMF transport defaults for greeting and verification_code tasks."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DtmfConfig:
    source: str
    fallback_delay: float = 0.2
    dedup_window: float = 0.5


def resolve_dtmf_config(
    source: str | None = None,
    fallback_delay_ms: float | None = None,
    dedup_window_ms: float | None = None,
) -> DtmfConfig:
    """Use auto by default; only explicit constructor arguments can override it.

    Timing is in seconds in the result, and validated only for auto mode.
    Transport and timing environment variables are intentionally ignored.
    """
    source = "auto" if source is None else source
    source = source.strip().lower()
    if source not in {"sip", "inband", "auto"}:
        raise ValueError("DTMF source must be sip or inband or auto")
    if source != "auto":
        return DtmfConfig(source)
    fallback = float(200 if fallback_delay_ms is None else fallback_delay_ms)
    window = float(500 if dedup_window_ms is None else dedup_window_ms)
    if not (
        math.isfinite(fallback)
        and math.isfinite(window)
        and 0 <= fallback < window <= 5000
    ):
        raise ValueError("DTMF delays require 0 <= fallback < dedup <= 5000 ms")
    return DtmfConfig(source, fallback / 1000, window / 1000)
