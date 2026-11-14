"""Conservative, bounded in-band DTMF detection for mono signed-16 PCM.

This detector intentionally drops uncertain signals. It does not classify speech,
produce invalid keys, or route menu choices. Feed PCM at the configured sample rate;
no resampling or channel mixing happens here.
"""

import numpy as np

_LOW = (697, 770, 852, 941)
_HIGH = (1209, 1336, 1477, 1633)
_KEYS = ("123A", "456B", "789C", "*0#D")


class InbandDtmfDetector:
    """Detect all sixteen keys using 40 ms windows advanced every 20 ms.

    Three consecutive windows must contain a stable pair (60 ms of confirmation).
    Clean tones are confirmed after 80 ms of PCM. A held key produces one event;
    at least 40 ms without a valid pair rearms detection. The analysis window can
    add up to 40 ms of release latency. Retained PCM is bounded to one window plus
    an incomplete hop, regardless of the size or boundaries of incoming frames.
    """

    def __init__(self, sample_rate: int = 8000):
        if sample_rate < 8000 or sample_rate % 50:
            raise ValueError("sample_rate must be >= 8000 and divisible by 50")
        self.sample_rate = sample_rate
        self._hop_samples = sample_rate // 50
        self._window_samples = self._hop_samples * 2
        self._hop_bytes = self._hop_samples * 2
        self._pending = bytearray()
        self._window = np.zeros(self._window_samples, dtype=np.float64)
        self._filled_hops = 0
        self._weights = np.hanning(self._window_samples)
        self._weight_sum = float(self._weights.sum())
        offsets = np.arange(-0.04, 0.0401, 0.005)
        self._offsets = offsets
        frequencies = np.asarray((*_LOW, *_HIGH))[:, None] * (1 + offsets)
        time = np.arange(self._window_samples) / sample_rate
        self._projections = np.exp(-2j * np.pi * frequencies[..., None] * time)
        self._harmonics = np.exp(
            -2j * np.pi * 2 * np.asarray((*_LOW, *_HIGH))[:, None] * time
        )
        self.reset()

    def reset(self) -> None:
        """Drop PCM and debounce state, for a new stream or task."""
        self._pending.clear()
        self._window.fill(0)
        self._filled_hops = 0
        self._candidate: str | None = None
        self._candidate_ms = 0
        self._held: str | None = None
        self._release_ms = 0

    def feed(self, pcm: bytes | bytearray | memoryview) -> list[str]:
        """Consume little-endian int16 mono PCM, even across odd byte boundaries."""
        data = memoryview(pcm).cast("B")
        keys: list[str] = []
        offset = 0
        while offset < len(data):
            take = min(self._hop_bytes - len(self._pending), len(data) - offset)
            self._pending.extend(data[offset : offset + take])
            offset += take
            if len(self._pending) < self._hop_bytes:
                continue
            samples = np.frombuffer(self._pending, dtype="<i2").astype(np.float64)
            self._pending.clear()
            self._window[: self._hop_samples] = self._window[self._hop_samples :]
            self._window[self._hop_samples :] = samples / 32768
            self._filled_hops = min(2, self._filled_hops + 1)
            if self._filled_hops == 2:
                key = self._advance(self._classify())
                if key is not None:
                    keys.append(key)
        return keys

    def _classify(self) -> str | None:
        samples = self._window - self._window.mean()
        weighted = samples * self._weights
        energy = float(np.dot(samples * samples, self._weights) / self._weight_sum)
        if energy < 0.008**2:
            return None
        powers = abs(self._projections @ weighted) ** 2
        powers *= 2 / self._weight_sum**2
        peaks = powers.argmax(axis=1)
        strongest = powers[np.arange(8), peaks]
        row = int(strongest[:4].argmax())
        column = int(strongest[4:].argmax()) + 4
        # Search a wider band than we accept so detuned tones cannot masquerade
        # as valid keys through spectral leakage into an allowed frequency.
        if any(abs(self._offsets[peaks[i]]) > 0.0151 for i in (row, column)):
            return None
        low = float(strongest[row])
        high = float(strongest[column])
        if not 0.25 <= low / max(high, 1e-20) <= 4:
            return None
        if low < 6 * float(np.delete(strongest[:4], row).max()):
            return None
        if high < 6 * float(np.delete(strongest[4:], column - 4).max()):
            return None
        if (low + high) / energy < 0.82:
            return None
        harmonics = abs(self._harmonics @ weighted) ** 2
        harmonics *= 2 / self._weight_sum**2
        if harmonics[row] > low * 0.12 or harmonics[column] > high * 0.12:
            return None
        return _KEYS[row][column - 4]

    def _advance(self, key: str | None) -> str | None:
        if self._held is not None:
            if key == self._held:
                self._release_ms = 0
            else:
                self._release_ms += 20
                if self._release_ms >= 40:
                    self._held = None
                    self._candidate = None
                    self._candidate_ms = 0
            return None
        if key is None:
            self._candidate = None
            self._candidate_ms = 0
            return None
        if key != self._candidate:
            self._candidate = key
            self._candidate_ms = 20
        else:
            self._candidate_ms += 20
        if self._candidate_ms >= 60:
            self._held = key
            self._release_ms = 0
            return key
        return None
