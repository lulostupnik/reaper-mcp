"""The fake REAPER's track FX calls: parameters, display text, presets,
adding at a position, and copying or moving between chains."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from tests.fake_reaper.objects import INSTALLED_FX, PRESETS, REAPER_PARAMS, FXState, display

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper

INSERT_AT = -1000  # TrackFX_AddByName: instantiate <= -1000 inserts at -1000 - instantiate


def preset_values(state: FXState, index: int) -> list[float]:
    """The parameter values a fake preset loads: distinct per preset."""
    return [round(0.1 * (index + 1) + 0.01 * i, 4) for i in range(len(state.values))]


class FX:
    """Mixed into FakeReaper."""

    def _fx(self: FakeReaper, track: str, fx: int) -> FXState:
        """REAPER has no FX -1: a negative index is not counted from the end."""
        fxs = self.track(track).fxs
        if not 0 <= fx < len(fxs):
            raise IndexError(f"no FX {fx}")
        return fxs[fx]

    def TrackFX_GetNumParams(self: FakeReaper, track: str, fx: int) -> int:
        return len(self._fx(track, fx).values)

    def TrackFX_GetParamNormalized(self: FakeReaper, track: str, fx: int, param: int) -> float:
        return self._fx(track, fx).values[param]

    def TrackFX_GetParamName(self: FakeReaper, track, fx, param, buf, size) -> list:
        state = self._fx(track, fx)
        return [True, track, fx, param, INSTALLED_FX[state.plugin][param], size]

    def TrackFX_GetParamFromIdent(self: FakeReaper, track: str, fx: int, ident: str) -> int:
        """":bypass", ":wet", ":delta": REAPER's own parameters at the end of
        the FX (only plugins that list them, see REAPER_PARAMS); else -1."""
        names = INSTALLED_FX[self._fx(track, fx).plugin]
        own = [":" + name.lower() for name in REAPER_PARAMS]
        if tuple(names[-len(own):]) != REAPER_PARAMS or ident not in own:
            return -1
        return len(names) - len(own) + own.index(ident)

    def TrackFX_SetParamNormalized(self: FakeReaper, track, fx, param, value) -> bool:
        self._fx(track, fx).values[param] = value
        return True

    def TrackFX_GetFormattedParamValue(self: FakeReaper, track, fx, param, buf, size) -> list:
        state = self._fx(track, fx)
        return [True, track, fx, param, display(state.plugin, param, state.values[param]), size]

    def TrackFX_FormatParamValueNormalized(self: FakeReaper, track, fx, param, value, buf, size):
        state = self._fx(track, fx)
        return [True, track, fx, param, value, display(state.plugin, param, value), size]

    def TrackFX_Delete(self: FakeReaper, track: str, fx_index: int) -> bool:
        """False for an FX that is not there (-1 included); nothing is removed."""
        fxs = self.track(track).fxs
        if not 0 <= fx_index < len(fxs):
            return False
        del fxs[fx_index]
        return True

    def TrackFX_AddByName(self: FakeReaper, track, name, record_fx, instantiate) -> int:
        """-1 for an unknown plugin; instantiate 0 only looks the plugin up."""
        fxs = self.track(track).fxs
        found = next((i for i, s in enumerate(fxs) if s.plugin == name), -1)
        if name not in INSTALLED_FX:
            return -1
        if instantiate == 0 or (instantiate > 0 and found >= 0):
            return found
        state = FXState(name, [0.5] * len(INSTALLED_FX[name]))
        at = min(INSERT_AT - instantiate, len(fxs)) if instantiate <= INSERT_AT else len(fxs)
        fxs.insert(at, state)
        return at

    def TrackFX_CopyToTrack(self: FakeReaper, source, source_fx, dest, dest_fx, move) -> None:
        """dest_fx is the FX's index in the destination chain afterwards; an
        FX index outside the source chain (negative too) does nothing."""
        source_fxs, dest_fxs = self.track(source).fxs, self.track(dest).fxs
        if not 0 <= source_fx < len(source_fxs):
            return
        state = source_fxs.pop(source_fx) if move else deepcopy(source_fxs[source_fx])
        dest_fxs.insert(min(dest_fx, len(dest_fxs)), state)

    # Presets: loading one sets every parameter (see preset_values).

    def _load_preset(self: FakeReaper, state: FXState, index: int) -> None:
        state.preset = PRESETS[state.plugin][index]
        state.values = preset_values(state, index)

    def TrackFX_SetPreset(self: FakeReaper, track: str, fx: int, preset: str) -> bool:
        state = self._fx(track, fx)
        if preset not in PRESETS[state.plugin]:
            return False
        self._load_preset(state, PRESETS[state.plugin].index(preset))
        return True

    def TrackFX_SetPresetByIndex(self: FakeReaper, track: str, fx: int, index: int) -> bool:
        state = self._fx(track, fx)
        if not 0 <= index < len(PRESETS[state.plugin]):
            return False
        self._load_preset(state, index)
        return True

    def TrackFX_GetPresetIndex(self: FakeReaper, track: str, fx: int, count: int) -> list:
        state = self._fx(track, fx)
        names = PRESETS[state.plugin]
        index = names.index(state.preset) if state.preset in names else -1
        return [index, track, fx, len(names)]

    def TrackFX_GetPreset(self: FakeReaper, track: str, fx: int, buf: str, size: int) -> list:
        state = self._fx(track, fx)
        return [bool(state.preset), track, fx, state.preset, size]
