"""Item edits that make or reshape items: duplicate, trim to the time
selection, and glue.
"""

import re
import uuid

from reaper_mcp import actions, items
from reaper_mcp.actions import GLUE_ITEMS
from reaper_mcp.item_tools import failure, resolve
from reaper_mcp.items import item_value, ref_of
from reaper_mcp.reaper import RPR
from reaper_mcp.transport_tools import time_range
from reaper_mcp.undo import edits

CHUNK_SIZE = 4 * 1024 * 1024
# Lines of an item's state chunk that carry a GUID: the item's, each take's,
# a pooled MIDI source's (a fresh one unpools the copy), each take FX's and
# each take envelope's.
GUID_LINE = re.compile(r"^(\s*(?:IGUID|GUID|POOLEDEVTS|FXID|EGUID)\s+)\{[0-9A-Fa-f-]{36}\}",
                       re.MULTILINE)
EXTENT_TOLERANCE = 1e-6  # seconds


def fresh_guids(chunk: str) -> str:
    """chunk with every item, take, pool, take FX and envelope GUID replaced by a new one, so the
    copy never shares an identity with the original."""
    return GUID_LINE.sub(lambda m: m.group(1) + "{" + str(uuid.uuid4()).upper() + "}", chunk)


@edits()
def duplicate_item(
    *,
    item: int | str,
    track_index: int | None = None,
    position: float | None = None,
    dest_track_index: int | None = None,
) -> dict:
    """Copy an item (with all its takes, fades and settings) as a new item with
    new GUIDs. position: where the copy starts (seconds; default right after the
    original). dest_track_index: the track for the copy (default the same track).
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    if position is not None and position < 0:
        return {"success": False, "error": "position must be 0 seconds or later"}
    try:
        ref = resolve(item, track_index, unlocked=False)
    except LookupError as e:
        return failure(e)
    # (retval, item, chunk, size, undo) per REAPER's Python binding.
    ok, _, chunk, _, _ = RPR.GetItemStateChunk(ref.pointer, "", CHUNK_SIZE, False)
    if not ok or not chunk:
        return {"success": False, "error": "REAPER did not give the item's state"}
    track = ref.track if dest_track_index is None else RPR.GetTrack(0, dest_track_index)
    copy = RPR.AddMediaItemToTrack(track)
    if not RPR.SetItemStateChunk(copy, fresh_guids(chunk), False):
        RPR.DeleteTrackMediaItem(track, copy)
        return {"success": False, "error": "REAPER did not accept the item's state for the copy"}
    end = item_value(ref.pointer, "D_POSITION") + item_value(ref.pointer, "D_LENGTH")
    RPR.SetMediaItemInfo_Value(copy, "D_POSITION", end if position is None else position)
    described = items.describe(ref_of(copy))
    if described["guid"].upper() == ref.guid.upper():
        return {"success": False, "error": "The copy kept the original's GUID; undo this step"}
    return {"success": True, "original_guid": ref.guid, "item": described}


@edits()
def trim_item_to_time_selection(*, item: int | str, track_index: int | None = None) -> dict:
    """Trim an item to the time selection: the parts before and after it are
    removed and the audio that stays does not move in time.
    item: the item's GUID from list_items (preferred), or its index on track_index.
    Set the time selection first with set_time_selection."""
    selection = time_range(loop=False)
    if selection is None:
        return {"success": False, "error": "There is no time selection: set one with "
                                           "set_time_selection first"}
    try:
        ref = resolve(item, track_index)
    except (LookupError, PermissionError) as e:
        return failure(e)
    start = item_value(ref.pointer, "D_POSITION")
    end = start + item_value(ref.pointer, "D_LENGTH")
    new_start, new_end = max(start, selection["start"]), min(end, selection["end"])
    if new_end <= new_start:
        return {"success": False,
                "error": f"The item ({start:g} to {end:g} s) is outside the time selection "
                         f"({selection['start']:g} to {selection['end']:g} s)"}
    if new_start > start:
        items.set_start(ref.pointer, new_start)
    items.set_length(ref.pointer, new_end - new_start)
    return {"success": True, "item": items.describe(ref_of(ref.pointer))}


def _glue_error(refs: list) -> str | None:
    if len({ref.track for ref in refs}) > 1:
        return "Glue works on items of one track: pass items from a single track"
    locked = [ref.guid for ref in refs if items.is_locked(ref.pointer)]
    if locked:
        return f"Locked items cannot be glued: {locked}"
    several = [ref.guid for ref in refs if RPR.CountTakes(ref.pointer) > 1]
    if several:
        return (f"These items have more than one take: {several}. Gluing keeps only the "
                "active take; choose it and use crop_to_active_take first")
    return None


def _glued(track, span: tuple[float, float], *, before: set[str], glued: set[str]) -> list:
    """The items on track spanning span that are new, or one of the glued ones
    reused (REAPER may keep the first item and give it the new take)."""
    start, end = span
    return [p for p in items.track_items(track)
            if (items.item_guid_of(p) not in before or items.item_guid_of(p) in glued)
            and abs(item_value(p, "D_POSITION") - start) <= EXTENT_TOLERANCE
            and abs(item_value(p, "D_POSITION") + item_value(p, "D_LENGTH") - end)
            <= EXTENT_TOLERANCE]


def _take_guid(item) -> str | None:
    take = items.active_take(item)
    return None if take is None else items.take_string(take, "GUID")


@edits(destructive=True)
def glue_items(*, items_to_glue: list[int | str], track_index: int | None = None) -> dict:
    """Glue items of one track into a single item playing a new audio file that
    REAPER renders (into the project's recording folder). The originals are
    replaced; gaps between them become silence. What you hear is baked into the
    new file: the active take only, its take FX, item and take volume, and fades.
    Items with more than one take are refused (crop_to_active_take first). Rarely
    needed: splits, moves and remove_silence work without gluing.
    items_to_glue: the items' GUIDs from list_items (preferred), or their
    indices on track_index."""
    if not items_to_glue:
        return {"success": False, "error": "items_to_glue is empty"}
    try:
        refs = [resolve(i, track_index, unlocked=False) for i in items_to_glue]
    except LookupError as e:
        return failure(e)
    error = _glue_error(refs)
    if error:
        return {"success": False, "error": error}
    actions.check(GLUE_ITEMS)
    track = refs[0].track
    start = min(item_value(r.pointer, "D_POSITION") for r in refs)
    end = max(item_value(r.pointer, "D_POSITION") + item_value(r.pointer, "D_LENGTH")
              for r in refs)
    before = {items.item_guid_of(p) for p in items.track_items(track)}
    takes_before = {_take_guid(r.pointer) for r in refs}
    with items.only_selected([ref.pointer for ref in refs]):
        RPR.Main_OnCommand(GLUE_ITEMS.command, 0)
    found = _glued(track, (start, end), before=before, glued={ref.guid for ref in refs})
    if len(found) == 1 and _take_guid(found[0]) in takes_before:
        found = []  # an original still playing its old take: nothing was glued
        return {"success": False, "error": f"After gluing, {len(found)} items span "
                                           f"{start:g} to {end:g} s instead of one; check the "
                                           "track (undo takes the glue back)"}
    return {"success": True, "glued_guids": [ref.guid for ref in refs],
            "item": items.describe(ref_of(found[0]))}


TOOLS = (
    duplicate_item,
    trim_item_to_time_selection,
    glue_items,
)
