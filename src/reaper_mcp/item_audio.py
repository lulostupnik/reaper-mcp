"""Reading what a take plays straight from its audio file, and measuring it.

A take plays its file from D_STARTOFFS (source seconds) at D_PLAYRATE, so one
second of the item is playrate seconds of the file; a section source (a part
of a file) adds its own offset. The analysis reads the file itself, so it
measures the recording before item/take volume and before any take FX, but
hears only the channels the take plays (its I_CHANMODE: left, right, downmix).
Reversed sources are refused: where they read from in the file is not
something the ReaScript API states, so a cut there could land in the wrong place.
Stretch markers bend the item-to-file time mapping the same way: tools that cut
refuse them, the others warn.
"""

import math
import os
from dataclasses import dataclass, field

import numpy as np

from reaper_mcp import items
from reaper_mcp.reaper import RPR, Pointer
from reaper_mcp.units import linear_to_db

WINDOW_SECONDS = 0.02  # level windows, in item seconds
CLIP_LEVEL = 0.999  # a sample at or above this is counted as clipped
MIN_LUFS_SECONDS = 0.4  # integrated loudness needs at least one 400 ms block
BLOCK_FRAMES = 1 << 20  # samples are measured in blocks, so no full float64 copy is made
# I_CHANMODE: 0 normal, 1 reverse stereo, 2 downmix, 3 left, 4 right.
NORMAL, REVERSE, DOWNMIX, LEFT, RIGHT = range(5)


@dataclass
class TakeAudio:
    path: str
    start: float  # file seconds where the item starts
    playrate: float
    length: float  # item seconds
    loop: tuple[float, float] | None  # the file span a looped item repeats
    source: Pointer  # the take's own source (a section, or the file)
    source_start: float  # the take's offset in that source
    channel_mode: int = NORMAL
    warnings: list[str] = field(default_factory=list)


def take_audio(item: Pointer, take: Pointer | None = None, *,
               cutting: bool = False) -> TakeAudio:
    """How to read take (by default the item's active take); ValueError with a
    message for the user when it cannot be analyzed. cutting: the result decides
    where to cut, so a time mapping that cannot be trusted is refused."""
    take = take or items.active_take(item)
    if take is None:
        raise ValueError("The item has no take, so there is no audio to analyze")
    if items.is_midi(take):
        raise ValueError("This is a MIDI item: there is no audio to analyze")
    source = RPR.GetMediaItemTake_Source(take)
    # (retval, source, offset, length, reversed) per REAPER's Python binding.
    is_section, _, section_start, section_length, reverse = RPR.PCM_Source_GetSectionInfo(
        source, 0.0, 0.0, False)
    if is_section and reverse:
        raise ValueError("The take is reversed; this tool cannot analyze reversed audio. "
                         "Un-reverse it in REAPER (Item properties) first")
    path = items.source_file(take)
    if not path or not os.path.isfile(path):
        raise ValueError(f"The take's audio file cannot be found: {path or 'no file'}")
    offset, rate = items.take_value(take, "D_STARTOFFS"), items.take_value(take, "D_PLAYRATE")
    shift = section_start if is_section else 0.0
    looped = bool(items.item_value(item, "B_LOOPSRC"))
    loop = None
    if looped:
        length = section_length if is_section else RPR.GetMediaSourceLength(source, False)[0]
        loop = (shift, shift + length)
    audio = TakeAudio(path, offset + shift, rate, items.item_value(item, "D_LENGTH"), loop,
                      source, offset, int(items.take_value(take, "I_CHANMODE")))
    _check_stretch_markers(take, audio, cutting=cutting)
    if audio.channel_mode > RIGHT:
        audio.warnings.append("The take plays a single channel of a multichannel file; "
                              "the analysis hears all its channels")
    if RPR.TakeFX_GetCount(take):
        audio.warnings.append("The take has take FX; the analysis reads the recording "
                              "without them")
    return audio


def _check_stretch_markers(take: Pointer, audio: TakeAudio, *, cutting: bool) -> None:
    if not RPR.GetTakeNumStretchMarkers(take):
        return
    if cutting:
        raise ValueError("The take has stretch markers, so this tool cannot tell where its "
                         "audio is in time; remove them in REAPER or split by hand")
    audio.warnings.append("The take has stretch markers; the analysis reads the recording "
                          "as if it were not stretched")


def _channels(data: np.ndarray, mode: int) -> np.ndarray:
    """The channels the take plays, per its channel mode."""
    if data.shape[1] < 2 or mode == NORMAL or mode > RIGHT:
        return data
    if mode == REVERSE:
        return data[:, [1, 0, *range(2, data.shape[1])]]
    if mode == DOWNMIX:
        return data[:, :2].mean(axis=1, keepdims=True)
    return data[:, [0 if mode == LEFT else 1]]


def read(audio: TakeAudio) -> tuple[np.ndarray, int]:
    """(samples, sample rate) of what the item plays, frames x channels, as the
    file has them (one frame per file sample, so playrate seconds per item second)."""
    import soundfile as sf

    info = sf.info(audio.path)
    rate, total = info.samplerate, info.frames
    first = round(audio.start * rate)
    count = max(0, round(audio.length * audio.playrate * rate))
    if audio.loop is not None:
        a, b = (min(total, round(t * rate)) for t in audio.loop)
        if not (a <= first and first + count <= b):  # it really wraps around
            data = sf.read(audio.path, start=a, stop=b, dtype="float32", always_2d=True)[0]
            if len(data):
                return _channels(data[(np.arange(count) + first - a) % len(data)],
                                 audio.channel_mode), rate
    lo, hi = max(0, first), max(0, min(total, first + count))
    data = sf.read(audio.path, start=lo, stop=hi, dtype="float32", always_2d=True)[0] \
        if hi > lo else np.zeros((0, info.channels), dtype="float32")
    before = min(count, max(0, -first))
    after = max(0, count - before - len(data))
    return _channels(np.pad(data, ((before, after), (0, 0))), audio.channel_mode), rate


def window_levels(data: np.ndarray, rate: int, playrate: float) -> tuple[np.ndarray, float]:
    """(RMS level in dBFS of each window, window length in item seconds)."""
    frames = max(1, round(WINDOW_SECONDS * playrate * rate))
    count = math.ceil(len(data) / frames)
    power = np.empty(count)
    step = max(1, BLOCK_FRAMES // frames)  # windows per block
    for first in range(0, count, step):
        block = data[first * frames:(first + step) * frames]
        windows = math.ceil(len(block) / frames)
        block = np.pad(block, ((0, windows * frames - len(block)), (0, 0)))
        squared = np.square(block.reshape(windows, frames, -1), dtype=np.float64)
        power[first:first + windows] = squared.mean(axis=(1, 2))
    levels = 10 * np.log10(np.maximum(power, 1e-15))
    return levels, frames / (rate * playrate)


def silent_runs(audio: TakeAudio, data: np.ndarray, rate: int, *, threshold_db: float,
                min_silence: float) -> list[tuple[float, float]]:
    """(start, end) in item seconds of each stretch quieter than threshold_db
    lasting at least min_silence."""
    levels, window = window_levels(data, rate, audio.playrate)
    runs, start = [], None
    for index, quiet in enumerate([*(levels < threshold_db), False]):
        if quiet and start is None:
            start = index
        elif not quiet and start is not None:
            runs.append((start * window, min(index * window, audio.length)))
            start = None
    return [(a, b) for a, b in runs if b - a >= min_silence]


def pad_runs(runs: list[tuple[float, float]], padding: float,
             length: float) -> list[tuple[float, float]]:
    """runs shrunk by padding where they meet sound (not at the item's edges),
    dropping those left with nothing."""
    padded = []
    for a, b in runs:
        a2 = a if a <= 0 else a + padding
        b2 = b if b >= length else b - padding
        if b2 - a2 >= items.MIN_PIECE:
            padded.append((a2, b2))
    return padded


def levels(data: np.ndarray, rate: int, *, lufs: bool = True) -> dict:
    """Peak and RMS (dBFS), clipped sample count, and integrated loudness (LUFS,
    None when too short or silent, or not asked for) of samples."""
    if not data.size:
        return {"peak_db": None, "rms_db": None, "clipped_samples": 0, "lufs": None}
    peak, energy, clipped = 0.0, 0.0, 0
    for first in range(0, len(data), BLOCK_FRAMES):
        block = np.abs(data[first:first + BLOCK_FRAMES])
        peak = max(peak, float(block.max()))
        energy += float(np.square(block, dtype=np.float64).sum())
        clipped += int(np.count_nonzero(block >= CLIP_LEVEL))
    rms = math.sqrt(energy / data.size)
    return {"peak_db": round(linear_to_db(peak), 2), "rms_db": round(linear_to_db(rms), 2),
            "clipped_samples": clipped, "lufs": _lufs(data, rate) if lufs else None}


def sound_span(audio: TakeAudio, data: np.ndarray, rate: int, *,
               threshold_db: float) -> float:
    """Item seconds from the first to the last sound louder than threshold_db."""
    levels_db, window = window_levels(data, rate, audio.playrate)
    loud = np.flatnonzero(levels_db >= threshold_db)
    if not loud.size:
        return 0.0
    return min(audio.length, (loud[-1] + 1) * window) - loud[0] * window


def _lufs(data: np.ndarray, rate: int) -> float | None:
    import pyloudnorm

    if len(data) < MIN_LUFS_SECONDS * rate:
        return None
    loudness = float(pyloudnorm.Meter(rate).integrated_loudness(data.astype(np.float64)))
    return round(loudness, 2) if math.isfinite(loudness) else None
