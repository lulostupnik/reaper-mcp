"""reapy's object model, faked over plain state.

Each ``Fake*`` class mirrors one reapy class and exposes only API that class
has (tests/test_fake_reaper.py enforces it). Like reapy, every access through
``project.tracks``, ``track.fxs``, ``track.items`` or ``fx.params`` builds a
fresh proxy over shared state, so an attribute a tool assigns that reapy does
not have lands on a throwaway object and REAPER never sees it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.fake_reaper.item_state import NULL_TAKE, ItemState, Note, TakeState

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper

# Presets each fake plugin ships with.
PRESETS = {"ReaEQ": ["Bright", "Warm"], "ReaComp": ["Gentle"], "ReaLimit": [], "ReaSynth": [],
           "ReaGate": [], "ReaDelay": []}

# REAPER (6.37+) adds these three parameters at the end of every FX. The fake's
# plugins leave them out to keep their lists short, except ReaDelay.
REAPER_PARAMS = ("Bypass", "Wet", "Delta")

# REAPER's values for track settings a new track's info dict leaves out.
TRACK_DEFAULTS = {"I_RECARM": 0.0, "I_RECINPUT": 0.0, "I_RECMON": 0.0, "I_RECMODE": 0.0,
                  "B_MAINSEND": 1.0, "I_FOLDERDEPTH": 0.0, "I_FREEMODE": 0.0,
                  "I_NUMFIXEDLANES": 0.0, "I_AUTOMODE": 0.0, "I_FXEN": 1.0,
                  "B_SOLO_DEFEAT": 0.0}

# The plugins the fake REAPER has installed; anything else is "not found".
INSTALLED_FX = {
    "ReaEQ": ["Gain-Low", "Freq-Low", "Gain-High"],
    "ReaComp": ["Thresh", "Ratio", "Attack", "Release"],
    "ReaLimit": ["Threshold", "Ceiling", "Release"],
    "ReaSynth": ["Volume", "Tuning"],
    "ReaGate": ["Gain", "Bypass"],
    "ReaDelay": ["Wet", "Dry", "Length", *REAPER_PARAMS],
}


def display(plugin: str, param: int, value: float) -> str:
    """What a fake plugin displays for a normalized value. Stand-in curves,
    in the units REAPER's own plugins show; tests/live exercises the real ones."""
    name = INSTALLED_FX[plugin][param]
    if plugin == "ReaLimit" and name == "Threshold":
        return f"{value * 72 - 60:+.2f} dB"  # ReaLimit's threshold: -60..+12 dB
    if plugin == "ReaLimit" and name == "Release":
        # ReaLimit's release falls from "inf" as the control rises.
        return "inf" if value <= 0 else f"{6 / value**0.5:.1f} dB/sec"
    if name.startswith("Gain"):
        return "-inf" if value <= 0 else f"{value * 48 - 24:+.1f} dB"
    if name.startswith("Freq"):
        hertz = 20 * 1000**value
        return f"{hertz / 1000:.2f} kHz" if hertz >= 1000 else f"{hertz:.1f} Hz"
    if name in ("Attack", "Release"):
        return f"{value * 500:.1f} ms"
    if name == "Bypass":
        return "On" if value >= 0.5 else "Off"
    return f"{value:.2f}"


@dataclass
class FXState:
    plugin: str
    values: list[float]
    enabled: bool = True
    preset: str = ""


@dataclass
class Send:
    dest: str
    volume: float = 1.0
    pan: float = 0.0
    muted: bool = False
    mode: int = 0  # I_SENDMODE: post-fader


@dataclass
class TrackState:
    pointer: str
    name: str = ""
    fxs: list[FXState] = field(default_factory=list)
    items: list[ItemState] = field(default_factory=list)
    sends: list[Send] = field(default_factory=list)
    hardware_outputs: int = 0
    info: dict[str, float] = field(
        default_factory=lambda: {"D_VOL": 1.0, "D_PAN": 0.0, "B_MUTE": 0.0, "I_SOLO": 0.0}
    )
    selected: bool = False
    input_level: float = 0.0  # linear peak of what the track's input receives
    # Successive meter readings (linear) instead of a steady input_level; the
    # last one repeats. Each Track_GetPeakInfo of channel 1 moves to the next.
    input_levels: list[float] = field(default_factory=list)
    meter_reads: int = 0
    hold_from: int = 0  # the first reading the peak hold covers (reset by clearing it)

    @property
    def guid(self) -> str:
        return "{" + self.pointer[-8:] + "-0000-0000-0000-000000000000}"

    def value(self, param: str) -> float:
        """A setting as REAPER reads it: set ones from info, the rest at REAPER's default."""
        return self.info[param] if param in self.info else TRACK_DEFAULTS[param]


class FakeFXParam(float):
    """reapy.FXParam is a float subclass whose value is the normalized one."""

    def __new__(cls, state: FXState, index: int) -> FakeFXParam:
        return float.__new__(cls, state.values[index])

    def __init__(self, state: FXState, index: int) -> None:
        float.__init__(state.values[index])
        self._state = state
        self._index = index

    @property
    def name(self) -> str:
        return INSTALLED_FX[self._state.plugin][self._index]

    @property
    def normalized(self) -> float:
        return self._state.values[self._index]

    @normalized.setter
    def normalized(self, value: float) -> None:
        """Observed on REAPER 7.82 with reapy 0.10: the setter reads
        ``parent_fx.id``, which reapy's FX does not have, and raises."""
        raise AttributeError("'FX' object has no attribute 'id'")

    @property
    def formatted(self) -> str:
        """reapy reads it with TrackFX_GetFormattedParamValue."""
        return display(self._state.plugin, self._index, self.normalized)


class FakeFX:
    def __init__(self, state: FXState, index: int, reaper: FakeReaper | None = None) -> None:
        self._state = state
        self.index = index  # an instance attribute in reapy too
        self._reaper = reaper

    @property
    def name(self) -> str:
        return f"VST: {self._state.plugin} (Cockos)"

    @property
    def n_params(self) -> int:
        return len(self._state.values)

    @property
    def params(self) -> list[FakeFXParam]:
        return [FakeFXParam(self._state, i) for i in range(len(self._state.values))]

    @property
    def is_enabled(self) -> bool:
        return self._state.enabled

    @is_enabled.setter
    def is_enabled(self, value: bool) -> None:
        """reapy's TrackFX_SetEnabled: outside an undo block REAPER records its
        own "FX bypass toggle" step and marks the project dirty (seen on 7.69)."""
        reaper = self._reaper
        if reaper is not None and not reaper.undo_depth and bool(value) != self._state.enabled:
            reaper.undo_steps.append(("FX bypass toggle", reaper.snapshot()))
            reaper.redo_steps.clear()
            reaper.dirty = True
        self._state.enabled = value

    @property
    def preset(self) -> str:
        return self._state.preset

    @preset.setter
    def preset(self, value: str) -> None:
        self._state.preset = value


class FakeTake:
    def __init__(self, state: TakeState, item: ItemState) -> None:
        self._state = state
        self._item = item

    @property
    def is_midi(self) -> bool:
        return self._item.midi

    @property
    def name(self) -> str:
        return self._state.name

    @property
    def start_offset(self) -> float:
        return self._state.info["D_STARTOFFS"]

    def get_info_value(self, param: str) -> float:
        return self._state.value(param)

    def set_info_value(self, param: str, value: float) -> None:
        self._state.info[param] = value

    def add_note(
        self, start, end, pitch, velocity=100, channel=0, selected=False,
        muted=False, unit="seconds", sort=True,
    ) -> None:
        self._item.notes.append(Note(start, end, pitch, velocity, channel))


class FakeItem:
    def __init__(self, state: ItemState) -> None:
        self._state = state
        self.id = state.pointer

    @property
    def position(self) -> float:
        return self._state.position

    @position.setter
    def position(self, value: float) -> None:
        self._state.position = value

    @property
    def length(self) -> float:
        return self._state.length

    @length.setter
    def length(self, value: float) -> None:
        self._state.length = value

    @property
    def active_take(self) -> FakeTake:
        """reapy returns a Take even for an item without one (a null take)."""
        if not self._state.takes:
            return FakeTake(TakeState(pointer=NULL_TAKE), self._state)
        return FakeTake(self._state.take, self._state)

    @property
    def n_takes(self) -> int:
        return len(self._state.takes)

    def get_info_value(self, param: str) -> float:
        return self._state.value(param)

    def set_info_value(self, param: str, value: float) -> None:
        self._state.info[param] = value


class FakeTrack:
    def __init__(self, reaper: FakeReaper, state: TrackState) -> None:
        self._reaper = reaper
        self._state = state
        self.id = state.pointer

    @property
    def name(self) -> str:
        return self._state.name

    @name.setter
    def name(self, value: str) -> None:
        self._state.name = value

    @property
    def n_fxs(self) -> int:
        return len(self._state.fxs)

    @property
    def fxs(self) -> list[FakeFX]:
        return [FakeFX(s, i, self._reaper) for i, s in enumerate(self._state.fxs)]

    @property
    def n_items(self) -> int:
        return len(self._state.items)

    @property
    def items(self) -> list[FakeItem]:
        return [FakeItem(s) for s in self._state.items]

    def add_fx(self, name, input_fx=False, even_if_exists=True) -> FakeFX:
        """reapy returns the FX object, and raises ValueError when not found."""
        if name not in INSTALLED_FX:
            raise ValueError(f"Can't find FX named {name}")
        state = FXState(name, [0.5] * len(INSTALLED_FX[name]))
        self._state.fxs.append(state)
        return FakeFX(state, len(self._state.fxs) - 1, self._reaper)

    def add_midi_item(self, start=0, end=1, quantize=False) -> FakeItem:
        state = ItemState(self._reaper.new_pointer("MediaItem"), start, end - start, midi=True)
        self._state.items.append(state)
        return FakeItem(state)

    @property
    def is_muted(self) -> bool:
        return bool(self._state.info["B_MUTE"])

    @is_muted.setter
    def is_muted(self, value: bool) -> None:
        self._state.info["B_MUTE"] = float(value)

    @property
    def is_solo(self) -> bool:
        return bool(self._state.info["I_SOLO"])

    @is_solo.setter
    def is_solo(self, value: bool) -> None:
        # reapy's setter calls solo()/unsolo(); see solo().
        if value:
            self.solo()
        else:
            self.unsolo()

    @property
    def is_selected(self) -> bool:
        return self._state.selected

    @is_selected.setter
    def is_selected(self, value: bool) -> None:
        self._state.selected = value

    def mute(self) -> None:
        self.is_muted = True

    def unmute(self) -> None:
        self.is_muted = False

    def solo(self) -> None:
        """Observed on REAPER 7.82: reapy toggles solo through action 7 on the
        selected tracks, which leaves the solo state unchanged. Only I_SOLO
        (set_info_value / SetMediaTrackInfo_Value) changes it."""

    def unsolo(self) -> None:
        """See solo()."""

    def get_info_value(self, param: str) -> float:
        return self._state.value(param)

    def set_info_value(self, param: str, value: float) -> None:
        self._state.info[param] = value


class FakeTimeSelection:
    """reapy.TimeSelection: a live view with settable start, end and length."""

    def __init__(self, reaper: FakeReaper) -> None:
        self._reaper = reaper

    @property
    def start(self) -> float:
        return self._reaper.time_selection[0]

    @start.setter
    def start(self, value: float) -> None:
        self._reaper.time_selection = (value, self._reaper.time_selection[1])

    @property
    def end(self) -> float:
        return self._reaper.time_selection[1]

    @end.setter
    def end(self, value: float) -> None:
        self._reaper.time_selection = (self._reaper.time_selection[0], value)

    @property
    def length(self) -> float:
        return self.end - self.start

    @length.setter
    def length(self, value: float) -> None:
        self.end = self.start + value


class FakeMarker:
    """reapy.Marker has a position and no name."""

    def __init__(self, position: float) -> None:
        self._position = position

    @property
    def position(self) -> float:
        return self._position

    @position.setter
    def position(self, value: float) -> None:
        self._position = value


class FakeRegion:
    """reapy.Region has start and end and no name."""

    def __init__(self, start: float, end: float) -> None:
        self._start, self._end = start, end

    @property
    def start(self) -> float:
        return self._start

    @start.setter
    def start(self, value: float) -> None:
        self._start = value

    @property
    def end(self) -> float:
        return self._end

    @end.setter
    def end(self, value: float) -> None:
        self._end = value
