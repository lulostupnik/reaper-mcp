"""Project markers and regions: list, add, rename, move, delete, go to.

A marker's number is the one REAPER shows on it ("1", "2"...), not its place
in the list: numbers stay with markers when others are added, deleted or
moved, can have gaps, and markers and regions are numbered separately. The
tools name a marker by that number, by its name, or by its GUID (stable,
from list_markers). Handles to markers (ProjectMarker*) are only used right
after they are looked up, since adding or deleting one can move the others.
"""

from dataclasses import dataclass

from reaper_mcp.reaper import RPR, Pointer, get_project, is_null
from reaper_mcp.undo import edits, no_undo, read_only

KINDS = ("marker", "region")
DEFAULT_COLOR = 0
# AddProjectMarker2's wantidx: let REAPER number it (the lowest free number of
# that kind, seen on 7.69). AddRegionOrMarker, which would return the new
# ProjectMarker*, is not in REAPER 7.69.
NEXT_FREE_NUMBER = -1


@dataclass(frozen=True)
class Mark:
    handle: Pointer
    is_region: bool
    number: int
    name: str
    start: float
    end: float
    guid: str

    @property
    def kind(self) -> str:
        return "region" if self.is_region else "marker"

    def describe(self) -> dict:
        place = ({"start": self.start, "end": self.end, "length": self.end - self.start}
                 if self.is_region else {"position": self.start})
        return {"kind": self.kind, "number": self.number, "name": self.name, **place,
                "guid": self.guid}


def _text(handle: Pointer, param: str) -> str:
    # (retval, proj, marker, param, value, set) per REAPER's Python binding.
    return RPR.GetSetRegionOrMarkerInfo_String(0, handle, param, "", False)[4]


def _value(handle: Pointer, param: str) -> float:
    return RPR.GetRegionOrMarkerInfo_Value(0, handle, param)


def read_mark(handle: Pointer) -> Mark:
    return Mark(handle=handle, is_region=bool(_value(handle, "B_ISREGION")),
                number=int(_value(handle, "I_NUMBER")), name=_text(handle, "P_NAME"),
                start=_value(handle, "D_STARTPOS"), end=_value(handle, "D_ENDPOS"),
                guid=_text(handle, "GUID"))


def all_marks() -> list[Mark]:
    """Every marker and region, in REAPER's (timeline) order."""
    total = RPR.CountProjectMarkers(0, 0, 0)[0]
    return [read_mark(RPR.GetRegionOrMarker(0, i, "")) for i in range(total)]


def by_guid(guid: str) -> Mark | None:
    handle = RPR.GetRegionOrMarker(0, -1, guid)
    return None if is_null(handle) else read_mark(handle)


def _kinds(kind: str | None) -> tuple[bool, ...]:
    """The is_region values kind allows: both for None; ValueError for
    anything but marker, region or None."""
    if kind is None:
        return (False, True)
    wanted = kind.strip().lower()
    if wanted not in KINDS:
        raise ValueError(f"kind must be 'marker' or 'region', not '{kind}'")
    return (wanted == "region",)


def _matches(mark: Mark, ref: int | str) -> bool:
    """Whether ref names mark: a number matches its number, digits in text
    its number or its name ("2" may be a marker named "2"), "{...}" its GUID,
    other text its name (ignoring case)."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        return mark.number == ref
    text = ref.strip()
    if text.startswith("{") and text.endswith("}"):
        return mark.guid.upper() == text.upper()
    named = mark.name.strip().casefold() == text.casefold()
    return named or (text.isdigit() and mark.number == int(text))


def _noun(kinds: tuple[bool, ...]) -> str:
    return KINDS[kinds[0]] if len(kinds) == 1 else "marker or region"


def resolve(ref: int | str, kind: str | None = None) -> Mark:
    """The one marker or region ref names (its number, name or GUID), among
    markers and regions both unless kind says which; LookupError when none
    or several match."""
    kinds = _kinds(kind)
    marks = all_marks()
    candidates = [m for m in marks if m.is_region in kinds]
    found = [m for m in candidates if _matches(m, ref)]
    if len(found) == 1:
        return found[0]
    if found:
        raise LookupError(f"Several match {ref!r}: {[m.describe() for m in found]}; "
                          "pass kind ('marker' or 'region') or the GUID of the one you mean")
    listed = [f"{m.kind} {m.number}: {m.name or '(no name)'}" for m in candidates]
    other = [m for m in marks if m.is_region not in kinds and _matches(m, ref)]
    hint = f" A {other[0].kind} matches it: pass kind='{other[0].kind}'." if other else ""
    raise LookupError(f"No {_noun(kinds)} {ref!r}.{hint} There are (kind number: name): "
                      f"{listed or 'none'}")


def _failure(error: Exception) -> dict:
    return {"success": False, "error": str(error)}


@read_only
def list_markers() -> dict:
    """List the project's markers and regions in timeline order, each with the
    number REAPER shows on it, its name, position (regions: start, end, length)
    and GUID. Other marker tools take the number, the name or the GUID."""
    marks = all_marks()
    return {"success": True,
            "markers": [m.describe() for m in marks if not m.is_region],
            "regions": [m.describe() for m in marks if m.is_region]}


def _add(*, is_region: bool, start: float, end: float, name: str) -> dict:
    """Add a marker or region and describe it. AddProjectMarker2 returns only
    the new mark's number (which another mark of that kind may share), so the
    new mark is the one whose GUID was not there before."""
    what = KINDS[is_region]
    before = {m.guid for m in all_marks()}
    number = RPR.AddProjectMarker2(0, is_region, start, end, name, NEXT_FREE_NUMBER,
                                   DEFAULT_COLOR)
    new = [m for m in all_marks() if m.guid not in before]
    if number < 0 or len(new) != 1:
        return {"success": False, "error": f"REAPER did not add the {what}"}
    return {"success": True, **new[0].describe()}


@edits()
def add_marker(*, position: float, name: str = "") -> dict:
    """Add a marker at position (seconds) with an optional name, e.g. "Chorus".
    REAPER gives it the next free number."""
    if position < 0:
        return {"success": False, "error": "position must be 0 seconds or later"}
    return _add(is_region=False, start=position, end=position, name=name)


@edits()
def add_region(*, start: float, end: float, name: str = "") -> dict:
    """Add a region from start to end (seconds) with an optional name, e.g.
    "Verse 1". Regions mark song parts and can be rendered one by one."""
    if start < 0 or end <= start:
        return {"success": False, "error": "need 0 <= start < end (seconds)"}
    return _add(is_region=True, start=start, end=end, name=name)


@edits()
def rename_marker(*, marker: int | str, name: str, kind: str | None = None) -> dict:
    """Rename a marker or region (song parts such as "Chorus" are usually regions).
    marker: its number as REAPER shows it, its current name, or its GUID.
    kind: "marker" or "region", only needed when both have that number or name."""
    try:
        mark = resolve(marker, kind)
    except (LookupError, ValueError) as e:
        return _failure(e)
    RPR.GetSetRegionOrMarkerInfo_String(0, mark.handle, "P_NAME", name, True)
    renamed = by_guid(mark.guid)
    if renamed is None or renamed.name != name:
        return {"success": False, "error": f"REAPER did not rename the {mark.kind}"}
    return {"success": True, "old_name": mark.name, **renamed.describe()}


def _set_bounds(mark: Mark, start: float, end: float) -> bool:
    """Set start (and a region's end) in an order that never makes end < start.
    The handle is looked up again by GUID before the second change, since the
    first may re-sort the markers; False when it is gone."""
    if not mark.is_region:
        RPR.SetRegionOrMarkerInfo_Value(0, mark.handle, "D_STARTPOS", start)
        return True
    first, second = (("D_ENDPOS", end), ("D_STARTPOS", start)) if start > mark.end else (
        ("D_STARTPOS", start), ("D_ENDPOS", end))
    RPR.SetRegionOrMarkerInfo_Value(0, mark.handle, *first)
    again = by_guid(mark.guid)
    if again is None:
        return False
    RPR.SetRegionOrMarkerInfo_Value(0, again.handle, *second)
    return True


def _others(marks: list[Mark], guid: str) -> dict[str, tuple]:
    """Every other mark's place, number and name, by GUID."""
    return {m.guid: (m.start, m.end, m.number, m.name) for m in marks if m.guid != guid}


@edits()
def move_marker(
    *, marker: int | str, position: float, kind: str | None = None, end: float | None = None
) -> dict:
    """Move a marker to position (seconds); for a region, position is its new
    start and it keeps its length unless end is given.
    marker: its number as REAPER shows it, its name, or its GUID. Its number
    does not change. kind: "marker" or "region", only needed when both have
    that number or name."""
    try:
        mark = resolve(marker, kind)
    except (LookupError, ValueError) as e:
        return _failure(e)
    new_end = end if end is not None else position + (mark.end - mark.start)
    if position < 0 or (mark.is_region and new_end <= position):
        return {"success": False, "error": "need 0 <= position (and position < end for a region)"}
    others = _others(all_marks(), mark.guid)
    moved = by_guid(mark.guid) if _set_bounds(mark, position, new_end) else None
    if moved is None or abs(moved.start - position) > 1e-9 or (
            mark.is_region and abs(moved.end - new_end) > 1e-9) or (
            _others(all_marks(), mark.guid) != others):
        return {"success": False, "error": f"REAPER did not move the {mark.kind} as asked; "
                                           "undo this step"}
    return {"success": True, **moved.describe()}


@edits(destructive=True)
def delete_marker(*, marker: int | str, kind: str | None = None) -> dict:
    """Delete a marker or region (the audio under a region stays).
    marker: its number as REAPER shows it, its name, or its GUID.
    kind: "marker" or "region", only needed when both have that number or name."""
    try:
        mark = resolve(marker, kind)
    except (LookupError, ValueError) as e:
        return _failure(e)
    same_number = [m for m in all_marks()
                   if m.is_region == mark.is_region and m.number == mark.number]
    if len(same_number) > 1:
        return {"success": False, "error": f"{len(same_number)} {mark.kind}s share number "
                                           f"{mark.number}, so it cannot be deleted safely; "
                                           "give them different numbers in REAPER first"}
    RPR.DeleteProjectMarker(0, mark.number, mark.is_region)
    if by_guid(mark.guid) is not None:
        return {"success": False, "error": f"REAPER did not delete the {mark.kind}"}
    return {"success": True, "deleted": mark.describe()}


@no_undo(idempotent=True)
def go_to_marker(*, marker: int | str, kind: str | None = None) -> dict:
    """Move the edit cursor to a marker, or to the start of a region (e.g. "Chorus").
    marker: its number as REAPER shows it, its name, or its GUID.
    kind: "marker" or "region", only needed when both have that number or name."""
    try:
        mark = resolve(marker, kind)
    except (LookupError, ValueError) as e:
        return _failure(e)
    project = get_project()
    project.cursor_position = mark.start
    return {"success": True, "position": project.cursor_position, **{
        k: v for k, v in mark.describe().items() if k in ("kind", "number", "name")}}


TOOLS = (
    list_markers,
    add_marker,
    add_region,
    rename_marker,
    move_marker,
    delete_marker,
    go_to_marker,
)
