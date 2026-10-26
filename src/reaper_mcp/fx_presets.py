"""FX presets: listing them by name and loading one by name.

REAPER has no call that names a preset without loading it, so listing loads
each preset in turn (TrackFX_SetPresetByIndex, then TrackFX_GetPreset) into a
bypassed copy of the plugin at the end of its chain, which is removed
afterwards. The plugin itself is never touched: putting it back after loading
presets into it was not enough, since a preset can change more than parameter
values (ReaEQ's presets change its number of bands, so its parameters could
not be set back; seen on 7.69). The listing runs as an undo step too.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from reaper_mcp.reaper import RPR, Pointer

NAME_BUFFER = 2048
MAX_PRESETS = 300
# Parameter values read back this close count as unchanged.
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


def param_values(track: Pointer, fx: int) -> list[float]:
    return [RPR.TrackFX_GetParamNormalized(track, fx, i)
            for i in range(RPR.TrackFX_GetNumParams(track, fx))]


def same_settings(track: Pointer, fx: int, before: list[float]) -> bool:
    """Whether the FX's parameters read back as before."""
    after = param_values(track, fx)
    return len(after) == len(before) and all(
        abs(a - b) <= RESTORE_TOLERANCE for a, b in zip(after, before, strict=True))


@contextmanager
def scratch_copy(track: Pointer, fx: int) -> Iterator[int]:
    """The index of a bypassed copy of the FX, at the end of the track's
    chain, for the body; the copy is removed afterwards."""
    copy = RPR.TrackFX_GetCount(track)
    RPR.TrackFX_CopyToTrack(track, fx, track, copy, False)
    if RPR.TrackFX_GetCount(track) != copy + 1:
        raise RuntimeError("REAPER did not make a copy of the plugin to list its presets on; "
                           "nothing was changed")
    try:
        RPR.TrackFX_SetEnabled(track, copy, False)  # not heard while presets load
        yield copy
    finally:
        RPR.TrackFX_Delete(track, copy)


def preset_names(track: Pointer, fx: int) -> tuple[list[str], int]:
    """(names of the FX's first MAX_PRESETS presets in REAPER's order, total
    count). Loads each one: run it on a scratch_copy."""
    _, count = preset_index(track, fx)
    names = []
    for index in range(min(count, MAX_PRESETS)):
        names.append(current_preset(track, fx) if RPR.TrackFX_SetPresetByIndex(track, fx, index)
                     else "")
    return names, count

