"""item_audio's measurements, checked against reference implementations."""

import numpy as np
import pyloudnorm
import pytest

from reaper_mcp import item_audio


def _signal(seconds: float, rate: int, channels: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    frames = int(seconds * rate)
    t = np.arange(frames) / rate
    data = np.stack([0.2 * np.sin(2 * np.pi * (220 + 110 * c) * t) + 0.05 * rng.standard_normal(frames)
                     for c in range(channels)], axis=1)
    data[frames // 3: frames // 2] *= 0.001  # a quiet stretch the gates drop
    return data.astype(np.float32)


@pytest.mark.parametrize(("seconds", "rate", "channels"), [
    (0.4, 48000, 2), (1.37, 44100, 1), (3.0, 48000, 2), (7.3, 96000, 2), (2.5, 48000, 5)])
def test_lufs_matches_pyloudnorm(seconds, rate, channels, monkeypatch):
    # Small blocks, so the streaming crosses many block edges.
    monkeypatch.setattr(item_audio, "BLOCK_FRAMES", 12345)
    data = _signal(seconds, rate, channels, seed=channels)
    expected = round(float(pyloudnorm.Meter(rate).integrated_loudness(data.astype(np.float64))), 2)
    assert item_audio._lufs(data, rate) == expected


def test_lufs_of_silence_and_of_too_little_is_none():
    assert item_audio._lufs(np.zeros((48000, 2), dtype=np.float32), 48000) is None
    assert item_audio._lufs(np.full((48000, 2), 1e-6, dtype=np.float32), 48000) is None
    assert item_audio._lufs(np.ones((1000, 2), dtype=np.float32), 48000) is None


def test_lufs_keeps_memory_bounded(monkeypatch):
    """pyloudnorm copied the take to float64 (and more) at once; the stream
    only ever holds a few blocks of float64."""
    import tracemalloc

    monkeypatch.setattr(item_audio, "BLOCK_FRAMES", 1 << 16)
    data = _signal(60.0, 48000, 2, seed=1)  # 23 MB of float32
    tracemalloc.start()
    item_audio._lufs(data, 48000)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 0.5 * data.nbytes
