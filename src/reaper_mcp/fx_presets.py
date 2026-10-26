"""FX presets: listing them by name and loading one by name.

REAPER has no call that names a preset without loading it, so listing loads
each preset in turn (TrackFX_SetPresetByIndex, then TrackFX_GetPreset) and
then puts the plugin back: the preset that was showing, then each parameter
whose value changed (the user may have tweaked it after loading a preset). The
listing runs as an undo step too, so one undo also brings back anything a
plugin keeps outside its parameters.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from reaper_mcp.reaper import RPR, Pointer

NAME_BUFFER = 2048
MAX_PRESETS = 300
# Parameter values read back this close count as restored.
RESTORE_TOLERANCE = 1e-6


def current_preset(track: Pointer, fx: int) -> str:
    """The preset REAPER shows for the FX ("" for none). A VST3 factory preset
    comes back as a .vstpreset file path; its file name is the preset's name."""
    name = RPR.TrackFX_GetPreset(track, fx, "", NAME_BUFFER)[3]
    if name.lower().endswith(".vstpreset"):
        return os.path.splitext(os.path.basename(name))[0]
    return name


def preset_index(track: Pointer, fx: int) -> tuple[int, int]:
    """(index of the preset showing or -1, number of presets)."""
    # (retval, track, fx, numberOfPresetsOut) per REAPER's Python binding.
    result = RPR.TrackFX_GetPresetIndex(track, fx, 0)
    return int(result[0]), int(result[3])


def _param_values(track: Pointer, fx: int) -> list[float]:
    return [RPR.TrackFX_GetParamNormalized(track, fx, i)
            for i in range(RPR.TrackFX_GetNumParams(track, fx))]


@contextmanager
def restoring_params(track: Pointer, fx: int) -> Iterator[dict]:
    """Put the FX's preset selection and parameter values back afterwards.
    Yields a dict that says, afterwards, whether every value read back as before
    ("restored")."""
    index, _ = preset_index(track, fx)
    before = _param_values(track, fx)
    outcome: dict = {}
    try:
        yield outcome
    finally:
        if index >= 0:
            RPR.TrackFX_SetPresetByIndex(track, fx, index)
        changed = _param_values(track, fx)
        # Only what differs is written back: one bridge call per changed
        # parameter, and none to parameters the preset did not touch.
        for param, (value, now) in enumerate(zip(before, changed, strict=False)):
            if abs(now - value) > RESTORE_TOLERANCE:
                RPR.TrackFX_SetParamNormalized(track, fx, param, value)
        after = _param_values(track, fx)
        outcome["restored"] = len(after) == len(before) and all(
            abs(a - b) <= RESTORE_TOLERANCE for a, b in zip(after, before, strict=True))


def preset_names(track: Pointer, fx: int) -> tuple[list[str], int]:
    """(names of the FX's first MAX_PRESETS presets in REAPER's order, total
    count). Loads each one: run inside restoring_params."""
    _, count = preset_index(track, fx)
    names = []
    for index in range(min(count, MAX_PRESETS)):
        names.append(current_preset(track, fx) if RPR.TrackFX_SetPresetByIndex(track, fx, index)
                     else "")
    return names, count

