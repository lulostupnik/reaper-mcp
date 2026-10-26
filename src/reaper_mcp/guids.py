"""GUIDs: names for tracks and items that survive reordering.

An index changes whenever a track is added, deleted or moved; a GUID does
not. Tools report both, and tools that take a track can accept either
through resolve_track. A GUID that matches nothing is an error, never a
fallback to some other track.
"""

from collections.abc import Sequence

from reaper_mcp.reaper import RPR, Pointer


def pointer_guid(pointer: Pointer) -> str:
    """The "{...}" GUID of the track at pointer.

    Not GetTrackGUID: REAPER's Python binding returns that GUID* as a pointer
    string ("(GUID*)0x..."), a memory address that changes when undo recreates
    the track. The string getter fills a buffer with the GUID's text instead.
    """
    # (retval, track, parmname, value, set) per REAPER's Python binding.
    return RPR.GetSetMediaTrackInfo_String(pointer, "GUID", "", False)[3]


def track_guid(track) -> str:
    return pointer_guid(track.id)


def item_guid(item) -> str:
    # (retval, item, parmname, value, set) per REAPER's Python binding.
    return RPR.GetSetMediaItemInfo_String(item.id, "GUID", "", False)[3]


TRACK_HINT = "Call list_tracks for the current tracks and their GUIDs."


def _is_guid(text: str) -> bool:
    return text.startswith("{") and text.endswith("}") and len(text) == 38


def resolve_indexed(objects: Sequence, ref: int | str, guid_of, *, noun: str, hint: str):
    """(index, object) for an index (an int or a string of digits), or for the
    object whose GUID is ref; LookupError otherwise."""
    if isinstance(ref, str) and ref.strip().isdigit():
        ref = int(ref)
    if isinstance(ref, int) and not isinstance(ref, bool):
        if not 0 <= ref < len(objects):
            raise LookupError(f"No {noun} {ref}: there are {len(objects)} (indices start at 0)")
        return ref, objects[ref]
    if not isinstance(ref, str) or not _is_guid(ref.strip()):
        raise LookupError(f"{ref!r} is neither a {noun} index nor a {noun} GUID")
    wanted = ref.strip().upper()
    for index, candidate in enumerate(objects):
        if guid_of(candidate).upper() == wanted:
            return index, candidate
    raise LookupError(f"No {noun} has GUID {ref}: it was deleted, or belongs to another "
                      f"project. {hint}")


def resolve_track_index(project, track: int | str) -> tuple[int, object]:
    """(index, track) for the track named by a 0-based index or a GUID such as
    "{0A1B...}"."""
    return resolve_indexed(list(project.tracks), track, track_guid, noun="track",
                            hint=TRACK_HINT)


def resolve_track(project, track: int | str):
    """The project's track named by a 0-based index or a GUID such as "{0A1B...}"."""
    return resolve_track_index(project, track)[1]


def resolve_item(track, item: int | str):
    """The item on track named by a 0-based index or an item GUID."""
    return resolve_indexed(list(track.items), item, item_guid, noun="item",
                            hint="Call get_track_info for the current items and their GUIDs.")[1]
