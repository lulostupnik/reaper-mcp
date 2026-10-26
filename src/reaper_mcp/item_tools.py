"""Items on the timeline: list them, set their settings, split, delete and move.

Every tool names an item by its GUID (from list_items; it survives other
edits) or by its index on track_index. Locked items are refused, except by
edit_item locked=false, which unlocks them.
"""

from reaper_mcp import items
from reaper_mcp.items import ItemRef, find_item, item_value, ref_of
from reaper_mcp.reaper import RPR
from reaper_mcp.undo import edits, read_only
from reaper_mcp.units import db_to_linear

MAX_VOLUME_DB = 24.0  # item and take volumes above this are refused (normalize_item too)
AUTO_FADES = {"fade_in": "D_FADEINLEN_AUTO", "fade_out": "D_FADEOUTLEN_AUTO"}


def resolve(item: int | str, track_index: int | None, *, unlocked: bool = True) -> ItemRef:
    """The item, or LookupError / PermissionError with a message for the user."""
    ref = find_item(item=item, track_index=track_index)
    error = items.locked_error(ref) if unlocked else None
    if error:
        raise PermissionError(error)
    return ref


def failure(error: Exception) -> dict:
    return {"success": False, "error": str(error)}


def index_note(item: int | str, ref: ItemRef) -> dict:
    """A reminder, when an item was named by index, that indices shift."""
    if isinstance(item, str) and not item.strip().isdigit():
        return {}
    return {"note": f"Item {ref.index} on track {ref.track_index} was {ref.guid}. Indices "
                    "change after splits, deletes and moves: prefer GUIDs, or call list_items "
                    "again before using an index."}


@read_only
def list_items(*, track_index: int | None = None, detail: bool = False) -> dict:
    """List the items of every track, or of one track: GUID, track, item index,
    position and length (seconds), muted, locked, take count, the active take's
    name and whether it is MIDI; on REAPER 7 fixed-lane tracks also the item's
    lane and whether that lane plays. detail=true adds fade lengths (manual and
    automatic crossfade, seconds), item volume (dB), and the active take's start
    offset, playrate, volume (dB) and source file."""
    try:
        refs = items.all_items(track_index)
    except LookupError as e:
        return failure(e)
    described = [items.describe(ref, detail=detail) for ref in refs]
    return {"success": True, "count": len(described), "items": described}


def _bounds(ref: ItemRef, changes: dict) -> tuple[float, float]:
    position = item_value(ref.pointer, "D_POSITION")
    end = position + item_value(ref.pointer, "D_LENGTH")
    return changes.get("start", position), changes.get("end", end)


def _edit_error(ref: ItemRef, settings: dict, locked: bool | None) -> str | None:
    """Why the edit cannot be made as asked, or None."""
    changes = {k: v for k, v in settings.items() if v is not None}
    if items.is_locked(ref.pointer) and changes and locked is not False:
        return items.locked_error(ref)
    start, end = _bounds(ref, changes)
    length = end - start
    checks = [
        (start < 0, "start must be 0 seconds or later"),
        (length <= 0, f"end ({end:g} s) must be after start ({start:g} s)"),
        (any(changes.get(f, 0) < 0 for f in ("fade_in", "fade_out")), "fades must be >= 0"),
        (any(changes.get(f, 0) > length for f in ("fade_in", "fade_out")),
         f"a fade cannot be longer than the item ({length:g} s)"),
        (any(changes.get(f, 0) > MAX_VOLUME_DB for f in ("volume_db", "take_volume_db")),
         f"volumes above +{MAX_VOLUME_DB:g} dB are refused (too loud to be intended)"),
    ]
    for failed, message in checks:
        if failed:
            return message
    if "take_volume_db" in changes and items.active_take(ref.pointer) is None:
        return "The item has no take, so it has no take volume"
    return None


def _apply(ref: ItemRef, settings: dict) -> list[str]:
    """Set the given settings; returns the names of those set."""
    item, take = ref.pointer, items.active_take(ref.pointer)
    fades = {"fade_in": "D_FADEINLEN", "fade_out": "D_FADEOUTLEN"}
    changed = []
    for name, value in settings.items():
        if value is None:
            continue
        changed.append(name)
        if name in fades:
            RPR.SetMediaItemInfo_Value(item, fades[name], float(value))
            # While an automatic crossfade length is set, REAPER plays it and
            # not the manual fade (heard in a render on 7.69), so it goes too.
            RPR.SetMediaItemInfo_Value(item, AUTO_FADES[name], 0.0)
        elif name == "start":
            items.set_start(item, value)
        elif name == "end":
            items.set_length(item, value - item_value(item, "D_POSITION"))
        elif name == "volume_db":
            RPR.SetMediaItemInfo_Value(item, "D_VOL", db_to_linear(value))
        elif name == "take_volume_db" and take is not None:
            sign = -1.0 if items.take_value(take, "D_VOL") < 0 else 1.0  # keep polarity
            RPR.SetMediaItemTakeInfo_Value(take, "D_VOL", sign * db_to_linear(value))
        elif name == "muted":
            RPR.SetMediaItemInfo_Value(item, "B_MUTE", float(value))
    return changed


def _set_lock(item, locked: bool) -> None:
    flags = int(item_value(item, "C_LOCK"))
    RPR.SetMediaItemInfo_Value(item, "C_LOCK", float(flags | 1 if locked else flags & ~1))


@edits(idempotent=True)
def edit_item(  # noqa: PLR0913 -- one argument per item setting
    *,
    item: int | str,
    track_index: int | None = None,
    start: float | None = None,
    end: float | None = None,
    fade_in: float | None = None,
    fade_out: float | None = None,
    volume_db: float | None = None,
    take_volume_db: float | None = None,
    muted: bool | None = None,
    locked: bool | None = None,
) -> dict:
    """Set an item's settings to exact values; anything left out stays as it is.
    item: the item's GUID from list_items (preferred), or its index on track_index.
    start/end: where the item starts/ends (project seconds); the audio stays where
    it is in time, so this trims (or uncovers) the item's edges. To move the
    whole item use move_item.
    fade_in/fade_out: fade length in seconds, 0 removes the fade. An automatic
    crossfade on that side is removed, so the fade set here is what plays.
    volume_db: the item's own volume, the one to use for "make this louder/quieter".
    take_volume_db: the active take's volume, which normalize_item sets; usually
    leave it alone. Both add up, and the track's volume comes on top.
    muted, locked: locked=false unlocks a locked item (and allows the other changes)."""
    try:
        ref = resolve(item, track_index, unlocked=False)
    except LookupError as e:
        return failure(e)
    settings = {"start": start, "end": end, "fade_in": fade_in, "fade_out": fade_out,
                "volume_db": volume_db, "take_volume_db": take_volume_db, "muted": muted}
    error = _edit_error(ref, settings, locked)
    if error:
        return {"success": False, "error": error}
    if locked is False:
        _set_lock(ref.pointer, False)
    changed = _apply(ref, settings)
    if locked is not None:
        changed.append("locked")
        if locked:
            _set_lock(ref.pointer, True)
    return {"success": True, "changed": changed, "item": items.describe(ref_of(ref.pointer))}


@edits()
def split_item(*, item: int | str, times: list[float], track_index: int | None = None) -> dict:
    """Split an item at one or more times (project seconds, each inside the item
    and at least 1 ms from its edges; times closer than 1 ms count once).
    item: the item's GUID from list_items (preferred), or its index on track_index.
    Returns the pieces left to right with their GUIDs; the first keeps the
    original GUID, the others are new items."""
    try:
        ref = resolve(item, track_index)
        cuts = items.split_times(ref.pointer, times)
    except (LookupError, PermissionError, ValueError) as e:
        return failure(e)
    try:
        pieces = items.split(ref.pointer, cuts)
    except items.SplitError as e:
        return {"success": False, "error": str(e), "pieces": items.describe_pieces(e.pieces)}
    return {"success": True, "track_index": ref.track_index,
            "pieces": items.describe_pieces(pieces), **index_note(item, ref)}


@edits(destructive=True)
def delete_item(*, item: int | str, track_index: int | None = None) -> dict:
    """Delete an item from its track (undo brings it back).
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    try:
        ref = resolve(item, track_index)
    except (LookupError, PermissionError) as e:
        return failure(e)
    if not RPR.DeleteTrackMediaItem(ref.track, ref.pointer):
        return {"success": False, "error": "REAPER did not delete the item"}
    return {"success": True, "deleted_guid": ref.guid, "track_index": ref.track_index,
            **index_note(item, ref)}


@edits()
def move_item(
    *,
    item: int | str,
    track_index: int | None = None,
    position: float | None = None,
    dest_track_index: int | None = None,
) -> dict:
    """Move a whole item (its audio with it) in time and/or to another track.
    position: the new start (seconds); dest_track_index: the track to move it to.
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    if position is None and dest_track_index is None:
        return {"success": False, "error": "pass position and/or dest_track_index"}
    if position is not None and position < 0:
        return {"success": False, "error": "position must be 0 seconds or later"}
    try:
        ref = resolve(item, track_index)
    except (LookupError, PermissionError) as e:
        return failure(e)
    if dest_track_index is not None and dest_track_index != ref.track_index:
        if not RPR.MoveMediaItemToTrack(ref.pointer, RPR.GetTrack(0, dest_track_index)):
            return {"success": False, "error": "REAPER did not move the item to that track"}
    if position is not None:
        RPR.SetMediaItemInfo_Value(ref.pointer, "D_POSITION", position)
    return {"success": True, "item": items.describe(ref_of(ref.pointer))}


TOOLS = (
    list_items,
    edit_item,
    split_item,
    delete_item,
    move_item,
)
