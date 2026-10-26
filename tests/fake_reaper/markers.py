"""The fake REAPER's project markers and regions.

As in REAPER, each marker and region has a displayed number (I_NUMBER),
markers and regions numbered separately, and a GUID; they are enumerated in
timeline order, so a marker's enumeration index changes when others are
added or moved, while its number and GUID do not. ``markers`` and ``regions``
are (position, name) and (start, end, name) views; assigning them replaces
that kind, numbering it 1, 2, ... in the order given.

Handles (ProjectMarker*) are stable per marker here; real REAPER's may not
survive adding or deleting markers, which is why the tools look them up again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper

NULL_MARKER = "(ProjectMarker*)0x0000000000000000"


@dataclass
class MarkerState:
    pointer: str
    is_region: bool
    start: float
    end: float
    name: str
    number: int

    @property
    def guid(self) -> str:
        return "{" + self.pointer[-8:] + "-1111-1111-1111-111111111111}"


class Markers:
    """Mixed into FakeReaper; start_markers() sets the state up."""

    def start_markers(self: FakeReaper) -> None:
        self.project_markers: list[MarkerState] = []

    def _ordered(self: FakeReaper) -> list[MarkerState]:
        return sorted(self.project_markers, key=lambda m: (m.start, m.is_region, m.number))

    def _replace_kind(self: FakeReaper, is_region: bool, entries: list[tuple]) -> None:
        kept = [m for m in self.project_markers if m.is_region != is_region]
        for number, entry in enumerate(entries, start=1):
            start, end, name = entry if is_region else (entry[0], entry[0], entry[1])
            kept.append(MarkerState(self.new_pointer("ProjectMarker"), is_region, start, end,
                                    name, number))
        self.project_markers = kept

    @property
    def markers(self: FakeReaper) -> list[tuple[float, str]]:
        return [(m.start, m.name) for m in self._ordered() if not m.is_region]

    @markers.setter
    def markers(self: FakeReaper, entries: list[tuple[float, str]]) -> None:
        self._replace_kind(False, entries)

    @property
    def regions(self: FakeReaper) -> list[tuple[float, float, str]]:
        return [(m.start, m.end, m.name) for m in self._ordered() if m.is_region]

    @regions.setter
    def regions(self: FakeReaper, entries: list[tuple[float, float, str]]) -> None:
        self._replace_kind(True, entries)

    def marker_state(self: FakeReaper, handle: str) -> MarkerState:
        for mark in self.project_markers:
            if mark.pointer == handle:
                return mark
        raise ValueError(f"no marker {handle}")

    # ReaScript

    def CountProjectMarkers(self: FakeReaper, project: int, _markers: int, _regions: int) -> list:
        regions = sum(m.is_region for m in self.project_markers)
        markers = len(self.project_markers) - regions
        return [len(self.project_markers), project, markers, regions]

    def GetRegionOrMarker(self: FakeReaper, project: int, index: int, guid: str) -> str:
        """By enumeration index, or by GUID when index < 0; a null pointer for none."""
        if index < 0:
            found = [m for m in self.project_markers if m.guid.upper() == guid.upper()]
            return found[0].pointer if found else NULL_MARKER
        ordered = self._ordered()
        return ordered[index].pointer if index < len(ordered) else NULL_MARKER

    def GetRegionOrMarkerInfo_Value(self: FakeReaper, project: int, handle: str, param: str):
        mark = self.marker_state(handle)
        return {"B_ISREGION": float(mark.is_region), "D_STARTPOS": mark.start,
                "D_ENDPOS": mark.end if mark.is_region else mark.start,
                "I_NUMBER": float(mark.number),
                "I_INDEX": float(self._ordered().index(mark))}[param]

    def SetRegionOrMarkerInfo_Value(self: FakeReaper, project, handle, param, value) -> float:
        """A marker's end follows its start; a region refuses an end before its start."""
        mark = self.marker_state(handle)
        if param == "D_STARTPOS":
            mark.start = value
            if not mark.is_region:
                mark.end = value
        elif param == "D_ENDPOS" and mark.is_region and value >= mark.start:
            mark.end = value
        elif param == "I_NUMBER":
            mark.number = int(value)
        return 1.0

    def GetSetRegionOrMarkerInfo_String(self: FakeReaper, project, handle, param, value, is_set):
        mark = self.marker_state(handle)
        if param == "GUID" and not is_set:
            return [True, project, handle, param, mark.guid, is_set]
        if param != "P_NAME":
            return [False, project, handle, param, "", is_set]
        if is_set:
            mark.name = value
        return [True, project, handle, param, mark.name, is_set]

    def AddRegionOrMarker(self: FakeReaper, project, is_region, position, end, name, want, color):
        """The lowest free number of that kind, or want when it is free."""
        used = {m.number for m in self.project_markers if m.is_region == is_region}
        number = want if want >= 0 and want not in used else next(
            n for n in range(1, len(used) + 2) if n not in used)
        mark = MarkerState(self.new_pointer("ProjectMarker"), is_region, position,
                           end if is_region else position, name, number)
        self.project_markers.append(mark)
        return mark.pointer

    def DeleteProjectMarker(self: FakeReaper, project: int, number: int, is_region: bool) -> bool:
        for mark in self.project_markers:
            if mark.number == number and mark.is_region == is_region:
                self.project_markers.remove(mark)
                return True
        return False
