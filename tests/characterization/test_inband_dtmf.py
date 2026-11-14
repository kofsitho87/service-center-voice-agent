"""Synthetic PCM checks; these do not substitute for a real SIP/audio call."""

import numpy as np
import pytest

from utils.inband_dtmf import InbandDtmfDetector

FREQUENCIES = {
    key: (low, high)
    for low, row in zip(
        (697, 770, 852, 941), ("123A", "456B", "789C", "*0#D"), strict=True
    )
    for high, key in zip((1209, 1336, 1477, 1633), row, strict=True)
}


def pcm(signals, duration=0.2, sample_rate=8000):
    time = np.arange(round(duration * sample_rate)) / sample_rate
    samples = sum(
        amplitude * np.sin(2 * np.pi * frequency * time + 0.3)
        for frequency, amplitude in signals
    )
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def tone(key, duration=0.2, sample_rate=8000):
    return pcm([(f, 0.2) for f in FREQUENCIES[key]], duration, sample_rate)


def silence(duration=0.1):
    return bytes(round(duration * 8000) * 2)


@pytest.mark.parametrize("key", FREQUENCIES)
@pytest.mark.parametrize("sample_rate", [8000, 16000])
def test_all_keys(key, sample_rate):
    assert InbandDtmfDetector(sample_rate).feed(tone(key, sample_rate=sample_rate)) == [
        key
    ]


def test_uneven_chunks_and_odd_byte_boundaries():
    data = silence() + tone("5") + silence() + tone("5") + silence() + tone("#")
    detector = InbandDtmfDetector()
    keys = []
    offset = 0
    sizes = (1, 37, 321, 7, 1203)
    index = 0
    while offset < len(data):
        size = sizes[index % len(sizes)]
        keys.extend(detector.feed(memoryview(data)[offset : offset + size]))
        assert len(detector._pending) < detector._hop_bytes
        assert len(detector._window) == 320
        offset += size
        index += 1
    assert keys == ["5", "5", "#"]


def test_held_key_and_same_key_after_release():
    detector = InbandDtmfDetector()
    assert detector.feed(tone("1", duration=5)) == ["1"]
    assert detector.feed(tone("1")) == []
    assert detector.feed(silence()) == []
    assert detector.feed(tone("1")) == ["1"]


@pytest.mark.parametrize(
    "signals",
    [
        [(770, 0.2)],  # One frequency is not a key.
        [(770, 0.001), (1336, 0.001)],  # Too quiet.
        [(770, 0.3), (1336, 0.04)],  # Excessive twist.
        [(770 * 1.04, 0.2), (1336 * 1.04, 0.2)],  # Detuned.
        [(770, 0.15), (1336, 0.15), (1540, 0.15), (2672, 0.15)],  # Harmonics.
        [(697, 0.2), (770, 0.2), (1209, 0.2)],  # Ambiguous row.
        [(120, 0.15), (240, 0.1), (360, 0.1), (720, 0.1), (1320, 0.08)],
    ],
)
def test_uncertain_and_voice_like_signals_do_not_emit(signals):
    assert InbandDtmfDetector().feed(pcm(signals)) == []


def test_random_noise_and_tone_buried_in_noise():
    rng = np.random.default_rng(410)
    for scale in (0.003, 0.1, 0.3):
        noise = np.clip(rng.normal(0, scale, 16000), -1, 1)
        assert InbandDtmfDetector().feed((noise * 32767).astype("<i2").tobytes()) == []
    tone_samples = np.frombuffer(tone("5", duration=2), dtype="<i2").astype(float)
    mixed = np.clip(
        tone_samples + rng.normal(0, 14000, len(tone_samples)), -32768, 32767
    )
    assert InbandDtmfDetector().feed(mixed.astype("<i2").tobytes()) == []


@pytest.mark.parametrize("duration", [0.02, 0.03, 0.04, 0.05, 0.055, 0.059])
def test_short_bursts_do_not_emit(duration):
    detector = InbandDtmfDetector()
    assert detector.feed(tone("8", duration=duration) + silence()) == []


def test_short_burst_and_reset():
    detector = InbandDtmfDetector()
    assert detector.feed(tone("8", duration=0.02) + silence()) == []
    assert detector.feed(tone("8")) == ["8"]
    detector.reset()
    assert detector.feed(tone("8")) == ["8"]


def test_valid_tone_with_small_noise_and_permitted_detuning():
    rng = np.random.default_rng(8)
    data = pcm([(770 * 1.01, 0.2), (1336 * 1.01, 0.18)])
    samples = np.frombuffer(data, dtype="<i2").astype(float)
    samples += rng.normal(0, 150, len(samples))
    assert InbandDtmfDetector().feed(samples.astype("<i2").tobytes()) == ["5"]


@pytest.mark.parametrize("gap", [0.04, 0.06, 0.1])
def test_same_key_rearms_after_tone_end(gap):
    detector = InbandDtmfDetector()
    assert detector.feed(tone("2") + silence(gap) + tone("2")) == ["2", "2"]


@pytest.mark.parametrize("leading_samples", [1, 79, 159, 161, 237])
def test_tone_start_crosses_analysis_boundary(leading_samples):
    detector = InbandDtmfDetector()
    data = bytes(leading_samples * 2) + tone("D") + silence()
    assert detector.feed(data) == ["D"]
