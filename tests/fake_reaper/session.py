"""The fake REAPER's undo history, transport state, selections and the
read-only getters over tracks and items.

Undo is modelled as REAPER documents it: Undo_BeginBlock2 / Undo_EndBlock2
nest, and only the outermost end records a step, under its description.
A step remembers the project as it was before the block, so undoing restores
that state and redoing restores the state the step left.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from reapy.errors import DistError

from tests.fake_reaper.objects import ItemState

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper

# The project state an undo step restores.
UNDO_STATE = ("tracks", "master", "project_markers", "tempo_markers", "bpm",
              "time_signature", "envelopes", "project_info")
PLAY_STATE = {"stopped": 0, "playing": 1, "paused": 2, "recording": 5}
NULL_TRACK = "(MediaTrack*)0x0000000000000000"


def _null_string_error() -> DistError:
    """What reapy raises when a ReaScript function returns a NULL string:
    REAPER's binding does str(r.decode()), which fails inside REAPER."""
    return DistError("AttributeError: 'NoneType' object has no attribute 'decode'")


class Session:
    """Mixed into FakeReaper; start_session() sets the state up."""

    def start_session(self: FakeReaper) -> None:
        self.undo_depth = 0
        self.ui_refresh_prevented = 0
        self.undo_steps: list[tuple[str, dict]] = []
        self.redo_steps: list[tuple[str, dict]] = []
        self._before_block: dict = {}
        self.dirty = False
        self.project_file = ""
        self.repeat = False
        self.loop_range = (0.0, 0.0)
        # REAPER's default "Loop points linked to time selection": setting one sets both.
        self.loop_linked = True
        self.play_position = 0.0
        # (track number as GetLastTouchedFX packs it, fx number, param), or None.
        self.last_touched_fx: tuple[int, int, int] | None = None

    def snapshot(self: FakeReaper) -> dict:
        return {name: copy.deepcopy(getattr(self, name)) for name in UNDO_STATE}

    def restore(self: FakeReaper, state: dict) -> None:
        for name, value in state.items():
            setattr(self, name, copy.deepcopy(value))

    # Undo

    def Undo_BeginBlock2(self: FakeReaper, project: int) -> None:
        if self.undo_depth == 0:
            self._before_block = self.snapshot()
        self.undo_depth += 1

    def Undo_EndBlock2(self: FakeReaper, project: int, description: str, flags: int) -> None:
        if self.undo_depth == 0:
            raise AssertionError("Undo_EndBlock2 without Undo_BeginBlock2")
        self.undo_depth -= 1
        if self.undo_depth == 0:
            self.undo_steps.append((description, self._before_block))
            self.redo_steps.clear()
            self.dirty = True

    def PreventUIRefresh(self: FakeReaper, count: int) -> None:
        self.ui_refresh_prevented += count
        if self.ui_refresh_prevented < 0:
            raise AssertionError("PreventUIRefresh removed more than was added")

    def Undo_CanUndo2(self: FakeReaper, project: int) -> str:
        if not self.undo_steps:
            raise _null_string_error()
        return self.undo_steps[-1][0]

    def Undo_CanRedo2(self: FakeReaper, project: int) -> str:
        if not self.redo_steps:
            raise _null_string_error()
        return self.redo_steps[-1][0]

    def Undo_DoUndo2(self: FakeReaper, project: int) -> int:
        if not self.undo_steps:
            return 0
        description, before = self.undo_steps.pop()
        self.redo_steps.append((description, self.snapshot()))
        self.restore(before)
        return 1

    def Undo_DoRedo2(self: FakeReaper, project: int) -> int:
        if not self.redo_steps:
            return 0
        description, after = self.redo_steps.pop()
        self.undo_steps.append((description, self.snapshot()))
        self.restore(after)
        return 1

    # Project and transport

    def EnumProjects(self: FakeReaper, index: int, filename: str, size: int) -> list:
        return [self.project.id, index, self.project_file, size]

    def IsProjectDirty(self: FakeReaper, project: int) -> int:
        return int(self.dirty)

    def GetPlayState(self: FakeReaper) -> int:
        return PLAY_STATE[self.transport]

    def GetCursorPosition(self: FakeReaper) -> float:
        return self.cursor

    def GetPlayPosition(self: FakeReaper) -> float:
        return self.play_position

    def GetSetRepeat(self: FakeReaper, value: int) -> int:
        if value >= 0:
            self.repeat = not self.repeat if value > 1 else bool(value)
        return int(self.repeat)

    def GetSet_LoopTimeRange2(self: FakeReaper, project, is_set, is_loop, start, end, seek):
        if is_set:
            setattr(self, "loop_range" if is_loop else "time_selection", (start, end))
            if self.loop_linked:
                setattr(self, "time_selection" if is_loop else "loop_range", (start, end))
        start, end = self.loop_range if is_loop else self.time_selection
        return [project, is_set, is_loop, start, end, seek]

    def GetLastTouchedFX(self: FakeReaper, track: int, fx: int, param: int) -> list:
        if self.last_touched_fx is None:
            return [False, 0, 0, 0]
        return [True, *self.last_touched_fx]

    # Tracks

    def GetMasterTrack(self: FakeReaper, project: int) -> str:
        return self.master.pointer

    def GetMediaTrackInfo_Value(self: FakeReaper, track: str, param: str) -> float:
        if param == "IP_TRACKNUMBER":
            if track == self.master.pointer:
                return -1.0
            pointers = [t.pointer for t in self.tracks]
            return float(pointers.index(track) + 1) if track in pointers else 0.0
        return self.track(track).value(param)

    def CountSelectedTracks(self: FakeReaper, project: int) -> int:
        return sum(t.selected for t in self.tracks)

    def GetSelectedTrack(self: FakeReaper, project: int, index: int) -> str:
        selected = [t.pointer for t in self.tracks if t.selected]
        return selected[index] if index < len(selected) else NULL_TRACK

    def TrackFX_GetFXName(self: FakeReaper, track: str, fx: int, buf: str, size: int) -> list:
        fxs = self.track(track).fxs
        if not 0 <= fx < len(fxs):
            return [False, track, fx, "", size]
        return [True, track, fx, f"VST: {fxs[fx].plugin} (Cockos)", size]

    # Items

    def _selected_items(self: FakeReaper) -> list[ItemState]:
        return [item for track in self.tracks for item in track.items if item.selected]

    def CountSelectedMediaItems(self: FakeReaper, project: int) -> int:
        return len(self._selected_items())

    def GetSelectedMediaItem(self: FakeReaper, project: int, index: int) -> str:
        return self._selected_items()[index].pointer

    def GetMediaItem_Track(self: FakeReaper, item: str) -> str:
        return self._item(item)[0].pointer
