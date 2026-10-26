"""Media items and their takes, as plain state.

An item holds zero or more takes and plays its active one (I_CURTAKE). A take
plays its source file from D_STARTOFFS (source seconds) at D_PLAYRATE. Item and
take settings a test never set read back at REAPER's defaults, so ``info`` and
``take_info`` only hold what was set (tests compare them as such).

Pointers: every take has its own "(MediaItem_Take*)0x..." pointer; a take's
source is "(PCM_source*)0x<same digits>", and the parent of a section source
(a reversed or cut source) is "(PCM_source*)0x<digits with an F in front>".
GUIDs are made from the pointer digits until a state chunk sets them.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

# REAPER's values for item and take settings left at their defaults.
ITEM_DEFAULTS = {"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 0.0, "D_FADEINLEN_AUTO": 0.0,
                 "D_FADEOUTLEN_AUTO": 0.0, "D_VOL": 1.0, "B_MUTE": 0.0, "C_LOCK": 0.0,
                 "C_FADEINSHAPE": 0.0, "C_FADEOUTSHAPE": 0.0, "B_LOOPSRC": 0.0,
                 "I_FIXEDLANE": 0.0, "C_LANEPLAYS": 0.0, "D_SNAPOFFSET": 0.0, "B_UISEL": 0.0}
TAKE_DEFAULTS = {"D_VOL": 1.0, "D_PAN": 0.0, "I_CHANMODE": 0.0}
NULL_TAKE = "(MediaItem_Take*)0x0000000000000000"
# Take pointers count from here, away from FakeReaper.new_pointer's numbers.
_take_numbers = itertools.count(0x7000_0000_0000)


@dataclass
class Note:
    start: float
    end: float
    pitch: int
    velocity: int
    channel: int


@dataclass(eq=False)
class TakeState:
    source_file: str = ""  # the take's audio file; "" for MIDI and imported stand-ins
    name: str = "take"
    info: dict[str, float] = field(
        default_factory=lambda: {"D_STARTOFFS": 0.0, "D_PITCH": 0.0, "D_PLAYRATE": 1.0}
    )
    fx_count: int = 0
    stretch_markers: int = 0
    # (offset, length, reversed) when the source is a section of its file.
    section: tuple[float, float, bool] | None = None
    pointer: str = field(default_factory=lambda: f"(MediaItem_Take*)0x{next(_take_numbers):016X}")
    guid: str = ""
    fx_guids: list[str] = field(default_factory=list)  # one per take FX, as FXID lines

    def __post_init__(self) -> None:
        if not self.guid:
            self.guid = "{" + self.pointer[-8:] + "-2222-2222-2222-222222222222}"

    @property
    def digits(self) -> str:
        return self.pointer.split("0x")[-1]

    def value(self, param: str) -> float:
        return self.info[param] if param in self.info else TAKE_DEFAULTS[param]

    def fx_guid(self, index: int) -> str:
        """The GUID of take FX index (made up until a state chunk sets it)."""
        while len(self.fx_guids) <= index:
            self.fx_guids.append("{" + self.pointer[-8:]
                                 + f"-3333-3333-3333-{len(self.fx_guids):012d}" + "}")
        return self.fx_guids[index]


class ItemState:
    """A media item. The constructor keeps the one-take shorthand the tests use:
    source_file, take_info, has_take and extra_takes describe the takes."""

    def __init__(
        self, pointer: str, position: float, length: float, midi: bool, *,
        notes: list[Note] | None = None, has_take: bool = True, extra_takes: int = 0,
        info: dict[str, float] | None = None, take_info: dict[str, float] | None = None,
        selected: bool = False, source_file: str = "", takes: list[TakeState] | None = None,
    ) -> None:
        self.pointer = pointer
        self.position = position
        self.length = length
        self.midi = midi
        self.notes = notes if notes is not None else []
        self.info = info if info is not None else {"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 0.0}
        self.selected = selected
        self.guid = "{" + pointer[-8:] + "-1111-1111-1111-111111111111}"
        if takes is None:
            takes = [TakeState(source_file)] if has_take else []
            if take_info is not None and takes:
                takes[0].info = take_info
        self.takes = takes
        self.active = 0
        self.extra_takes = extra_takes

    # The active take, as the one-take shorthand sees it.

    @property
    def has_take(self) -> bool:
        return bool(self.takes)

    @property
    def take(self) -> TakeState:
        return self.takes[self.active]

    @property
    def take_info(self) -> dict[str, float]:
        return self.take.info

    @property
    def source_file(self) -> str:
        return self.take.source_file if self.takes else ""

    @source_file.setter
    def source_file(self, value: str) -> None:
        self.take.source_file = value

    @property
    def extra_takes(self) -> int:
        return max(0, len(self.takes) - 1)

    @extra_takes.setter
    def extra_takes(self, count: int) -> None:
        """More takes: like a recording into the item, each new one becomes active."""
        while self.takes and len(self.takes) - 1 < count:
            self.takes.append(TakeState(self.take.source_file,
                                        name=f"take {len(self.takes) + 1}"))
            self.active = len(self.takes) - 1

    @property
    def end(self) -> float:
        return self.position + self.length

    def value(self, param: str) -> float:
        return self.info[param] if param in self.info else ITEM_DEFAULTS[param]


def new_take_pointer() -> str:
    return f"(MediaItem_Take*)0x{next(_take_numbers):016X}"


def copy_item(state: ItemState, pointer: str) -> ItemState:
    """A copy of state under a new pointer, its takes under new pointers and
    GUIDs, as REAPER makes the right-hand part of a split."""
    takes = [TakeState(t.source_file, t.name, dict(t.info), t.fx_count, t.stretch_markers,
                       t.section, new_take_pointer(), fx_guids=list(t.fx_guids))
             for t in state.takes]
    copy = ItemState(pointer, state.position, state.length, state.midi,
                     notes=list(state.notes), info=dict(state.info), takes=takes)
    copy.active = state.active
    return copy
