"""Media items and takes as the item tools see them.

Items are named by GUID (stable) or by their 0-based index on a track (which
changes whenever an item is added, split or moved). Everything here works on
REAPER's pointers through RPR, so splitting, moving and deleting never leave
a stale reapy object behind.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from reaper_mcp.guids import _is_guid
from reaper_mcp.reaper import RPR, Pointer, is_null
from reaper_mcp.units import linear_to_db

LOCKED = 1  # C_LOCK bit
FIXED_LANES = 2  # a track's I_FREEMODE with fixed item lanes (REAPER 7)
STRING_SIZE = 4096
ITEM_TYPE = "MediaItem*"
MIN_PIECE = 0.001  # seconds: split times closer than this to each other or an edge are refused


@dataclass(frozen=True)
class ItemRef:
    pointer: Pointer
    guid: str
    track: Pointer
    track_index: int
    index: int


def item_guid_of(pointer: Pointer) -> str:
    # (retval, item, parmname, value, set) per REAPER's Python binding.
    return RPR.GetSetMediaItemInfo_String(pointer, "GUID", "", False)[3]


def track_items(track: Pointer) -> list[Pointer]:
    return [RPR.GetTrackMediaItem(track, i) for i in range(RPR.CountTrackMediaItems(track))]


def _track(index: int) -> Pointer:
    count = RPR.CountTracks(0)
    if not 0 <= index < count:
        raise LookupError(f"No track {index}: there are {count} (indices start at 0)")
    return RPR.GetTrack(0, index)


def ref_of(pointer: Pointer) -> ItemRef:
    """Where an item is now (its track and index change as items are edited)."""
    track = RPR.GetMediaItem_Track(pointer)
    return ItemRef(pointer, item_guid_of(pointer), track,
                   int(RPR.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER")) - 1,
                   int(RPR.GetMediaItemInfo_Value(pointer, "IP_ITEMNUMBER")))


def all_items(track_index: int | None = None) -> list[ItemRef]:
    """Every item in the project, or on one track, in track then time order."""
    indices = [track_index] if track_index is not None else range(RPR.CountTracks(0))
    refs = []
    for t in indices:
        track = _track(t)
        refs += [ItemRef(p, item_guid_of(p), track, t, i) for i, p in enumerate(track_items(track))]
    return refs


def find_item(*, item: int | str, track_index: int | None = None) -> ItemRef:
    """The item named by a GUID ("{...}", anywhere in the project) or by its
    index on track_index. LookupError when it names nothing."""
    if isinstance(item, str) and item.strip().isdigit():
        item = int(item)
    if isinstance(item, int) and not isinstance(item, bool):
        if track_index is None:
            raise LookupError("An item index needs track_index; or pass the item's GUID "
                              "(list_items shows them)")
        track = _track(track_index)
        pointers = track_items(track)
        if not 0 <= item < len(pointers):
            raise LookupError(f"No item {item} on track {track_index}: it has {len(pointers)} "
                              "(indices start at 0)")
        return ItemRef(pointers[item], item_guid_of(pointers[item]), track, track_index, item)
    if not isinstance(item, str) or not _is_guid(item.strip()):
        raise LookupError(f"{item!r} is neither an item index nor an item GUID")
    wanted = item.strip().upper()
    for ref in all_items(track_index):
        if ref.guid.upper() == wanted:
            return ref
    raise LookupError(f"No item has GUID {item}: it was deleted (a split or glue makes new "
                      "items), or belongs to another project. Call list_items for the current "
                      "items.")


def item_value(pointer: Pointer, param: str) -> float:
    return RPR.GetMediaItemInfo_Value(pointer, param)


def is_locked(pointer: Pointer) -> bool:
    return bool(int(item_value(pointer, "C_LOCK")) & LOCKED)


def locked_error(ref: ItemRef) -> str | None:
    if not is_locked(ref.pointer):
        return None
    return (f"Item {ref.guid} is locked, so it was not changed. Unlock it first with "
            "edit_item locked=false.")


# Takes


def takes(item: Pointer) -> list[Pointer]:
    return [RPR.GetTake(item, i) for i in range(RPR.CountTakes(item))]


def active_take(item: Pointer) -> Pointer | None:
    take = RPR.GetActiveTake(item)
    return None if is_null(take) else take


def take_value(take: Pointer, param: str) -> float:
    return RPR.GetMediaItemTakeInfo_Value(take, param)


def take_string(take: Pointer, param: str) -> str:
    # (retval, take, parmname, value, set) per REAPER's Python binding.
    return str(RPR.GetSetMediaItemTakeInfo_String(take, param, "", False)[3])


def source_type(source: Pointer) -> str:
    # (source, typebuf, size) per REAPER's Python binding.
    return str(RPR.GetMediaSourceType(source, "", 64)[1])


def is_midi(take: Pointer) -> bool:
    return source_type(RPR.GetMediaItemTake_Source(take)).upper().startswith("MIDI")


def file_source(take: Pointer) -> Pointer:
    """The source holding the take's file: the take's own, or the parent of a
    section (a reversed or cut part of a file)."""
    source = RPR.GetMediaItemTake_Source(take)
    parent = RPR.GetMediaSourceParent(source)
    return source if is_null(parent) else parent


def source_file(take: Pointer) -> str | None:
    """The absolute path of the file the take plays, or None (MIDI, no file)."""
    from reaper_mcp.recording_tools import record_path

    name = str(RPR.GetMediaSourceFileName(file_source(take), "", STRING_SIZE)[1])
    if not name:
        return None
    return name if os.path.isabs(name) else os.path.join(record_path(), name)


# Changing an item's bounds


def set_start(item: Pointer, start: float) -> None:
    """Move the item's start to start (seconds) keeping its audio where it was
    in time: every take's source offset moves too, in source seconds (scaled by
    its playrate), so the takes stay aligned. A fade-in longer than what is left
    is shortened."""
    position, length = item_value(item, "D_POSITION"), item_value(item, "D_LENGTH")
    shift = start - position
    for take in takes(item):
        offset = take_value(take, "D_STARTOFFS") + shift * take_value(take, "D_PLAYRATE")
        RPR.SetMediaItemTakeInfo_Value(take, "D_STARTOFFS", offset)
    RPR.SetMediaItemInfo_Value(item, "D_POSITION", start)
    set_length(item, length - shift)


def set_length(item: Pointer, length: float) -> None:
    """Set the length, shortening fades that would no longer fit."""
    RPR.SetMediaItemInfo_Value(item, "D_LENGTH", length)
    for fade in ("D_FADEINLEN", "D_FADEOUTLEN"):
        if item_value(item, fade) > length:
            RPR.SetMediaItemInfo_Value(item, fade, length)


class SplitError(RuntimeError):
    """REAPER refused a split part way; pieces are those already made."""

    def __init__(self, time: float, pieces: list[Pointer]) -> None:
        super().__init__(f"REAPER did not split the item at {time:g} s; the splits before "
                         "it were made (undo takes them back)")
        self.pieces = pieces


def split_times(item: Pointer, times: list[float]) -> list[float]:
    """times sorted, with times closer than MIN_PIECE to the one before dropped;
    ValueError when one is outside the item or closer than MIN_PIECE to its edge."""
    start = item_value(item, "D_POSITION")
    end = start + item_value(item, "D_LENGTH")
    outside = [t for t in times if not start + MIN_PIECE <= t <= end - MIN_PIECE]
    if not times or outside:
        raise ValueError(f"split times must be inside the item ({start:g} to {end:g} s) and "
                         f"at least {MIN_PIECE * 1000:g} ms from its edges; not so: "
                         f"{outside or 'no times given'}")
    kept: list[float] = []
    for time in sorted(times):
        if not kept or time - kept[-1] >= MIN_PIECE:
            kept.append(time)
    return kept


def split(item: Pointer, times: list[float]) -> list[Pointer]:
    """Split item at each time (project seconds, from split_times); the pieces,
    left to right. The first piece is the original item (same GUID). SplitError
    when REAPER refuses one."""
    pieces = [item]
    for time in times:
        right = RPR.SplitMediaItem(pieces[-1], time)
        if is_null(right):
            raise SplitError(time, pieces)
        pieces.append(right)
    return pieces


# Describing items


def _lane(track: Pointer, item: Pointer) -> dict:
    if int(RPR.GetMediaTrackInfo_Value(track, "I_FREEMODE")) != FIXED_LANES:
        return {}
    return {"lane": int(item_value(item, "I_FIXEDLANE")),
            "lane_plays": int(item_value(item, "C_LANEPLAYS")) > 0}


def _db(linear: float) -> float:
    return round(linear_to_db(abs(linear)), 2)


def take_summary(take: Pointer | None) -> dict:
    if take is None:
        return {"active_take": None, "start_offset": None, "playrate": None,
                "take_volume_db": None, "source_file": None, "midi": False}
    return {
        "active_take": take_string(take, "P_NAME"),
        "start_offset": take_value(take, "D_STARTOFFS"),
        "playrate": take_value(take, "D_PLAYRATE"),
        "take_volume_db": _db(take_value(take, "D_VOL")),
        "source_file": source_file(take),
        "midi": is_midi(take),
    }


def describe(ref: ItemRef, *, detail: bool = True) -> dict:
    """What list_items reports for one item; detail adds fades, volumes and the
    active take's offset, playrate and file."""
    item = ref.pointer
    take = active_take(item)
    summary = take_summary(take)
    compact = {
        "guid": ref.guid,
        "track_index": ref.track_index,
        "item_index": ref.index,
        "position": item_value(item, "D_POSITION"),
        "length": item_value(item, "D_LENGTH"),
        "muted": bool(item_value(item, "B_MUTE")),
        "locked": is_locked(item),
        "take_count": RPR.CountTakes(item),
        "active_take": summary.pop("active_take"),
        "midi": summary.pop("midi"),
        **_lane(ref.track, item),
    }
    if not detail:
        return compact
    return {
        **compact,
        "fade_in": item_value(item, "D_FADEINLEN"),
        "fade_out": item_value(item, "D_FADEOUTLEN"),
        "auto_fade_in": item_value(item, "D_FADEINLEN_AUTO"),
        "auto_fade_out": item_value(item, "D_FADEOUTLEN_AUTO"),
        "volume_db": _db(item_value(item, "D_VOL")),
        **summary,
    }


def describe_pieces(pointers: list[Pointer]) -> list[dict]:
    return [{"guid": item_guid_of(p), "position": item_value(p, "D_POSITION"),
             "length": item_value(p, "D_LENGTH")} for p in pointers]


@contextmanager
def only_selected(items: list[Pointer]) -> Iterator[None]:
    """Select exactly items while the body runs (for an action that works on
    the selected items), then put the user's selection back: the items that
    still exist; ValidatePtr2 checks, since a dangling pointer crashes REAPER."""
    before = [RPR.GetSelectedMediaItem(0, i) for i in range(RPR.CountSelectedMediaItems(0))]
    RPR.SelectAllMediaItems(0, False)
    for item in items:
        RPR.SetMediaItemSelected(item, True)
    try:
        yield
    finally:
        RPR.SelectAllMediaItems(0, False)
        for item in before:
            if RPR.ValidatePtr2(0, item, ITEM_TYPE):
                RPR.SetMediaItemSelected(item, True)
