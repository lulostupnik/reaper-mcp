"""Takes: an item recorded over several times holds one take per pass and
plays the active one.

REAPER 7 can also keep passes as fixed item lanes: each pass is its own item,
on its own lane of the track, and the lane that plays is chosen per track.
Here the lanes overlapping an item are listed and compared like takes (one
item per lane, the one overlapping most), read-only: which lane plays, and
tidying lanes, is done in REAPER. A comp lane cannot be told apart from a
recorded one through the API, so it is listed like any other lane.
"""

from reaper_mcp import actions, item_audio, items
from reaper_mcp.actions import CROP_TO_ACTIVE_TAKE
from reaper_mcp.item_tools import failure, resolve
from reaper_mcp.items import FIXED_LANES, ItemRef, item_value
from reaper_mcp.reaper import RPR, Pointer
from reaper_mcp.undo import edits, read_only

QUIET_PEAK_DB = -24.0
SHORT_FRACTION = 0.9
SOUND_DB = -45.0  # RMS dBFS above which a window counts as sound (remove_silence's default)


def _take_info(index: int, take: Pointer, active: Pointer | None) -> dict:
    summary = items.take_summary(take)
    return {"index": index, "name": summary.pop("active_take"),
            "guid": items.take_string(take, "GUID"), "active": take == active, **summary}


def _has_lanes(track: Pointer) -> bool:
    return int(RPR.GetMediaTrackInfo_Value(track, "I_FREEMODE")) == FIXED_LANES


def _overlap(pointer: Pointer, start: float, end: float) -> float:
    position = item_value(pointer, "D_POSITION")
    return min(end, position + item_value(pointer, "D_LENGTH")) - max(start, position)


def _lane_items(ref: ItemRef) -> list[Pointer]:
    """On a fixed-lane track, per lane the item overlapping ref the most (ref
    itself on its own lane), in lane order."""
    if not _has_lanes(ref.track):
        return []
    start = item_value(ref.pointer, "D_POSITION")
    end = start + item_value(ref.pointer, "D_LENGTH")
    best: dict[int, Pointer] = {}
    for p in items.track_items(ref.track):
        lane = int(item_value(p, "I_FIXEDLANE"))
        if _overlap(p, start, end) <= 0 or best.get(lane) == ref.pointer:
            continue
        if p == ref.pointer or lane not in best or (
                _overlap(p, start, end) > _overlap(best[lane], start, end)):
            best[lane] = p
    return [best[lane] for lane in sorted(best)]


def _lane_info(pointer: Pointer) -> dict:
    return {"lane": int(item_value(pointer, "I_FIXEDLANE")),
            "plays": int(item_value(pointer, "C_LANEPLAYS")) > 0,
            "item_guid": items.item_guid_of(pointer),
            "position": item_value(pointer, "D_POSITION"),
            "length": item_value(pointer, "D_LENGTH")}


@read_only
def list_takes(*, item: int | str, track_index: int | None = None) -> dict:
    """List an item's takes: index, name, GUID, which one is active (plays),
    source file, start offset, playrate and take volume (dB). On a REAPER 7
    fixed-lane track also the overlapping lanes (lane number, whether it plays,
    the lane item's GUID).
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    try:
        ref = resolve(item, track_index, unlocked=False)
    except LookupError as e:
        return failure(e)
    active = items.active_take(ref.pointer)
    result = {"success": True, "item_guid": ref.guid, "track_index": ref.track_index,
              "takes": [_take_info(i, t, active) for i, t in enumerate(items.takes(ref.pointer))]}
    lanes = _lane_items(ref)
    if lanes:
        result["lanes"] = [_lane_info(p) for p in lanes]
    return result


@edits(idempotent=True)
def set_active_take(*, item: int | str, take_index: int, track_index: int | None = None) -> dict:
    """Choose which take of an item plays (take_index, from list_takes); the
    other takes are kept. (On a REAPER 7 fixed-lane track, choose the playing
    lane in REAPER itself.)
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    try:
        ref = resolve(item, track_index)
    except (LookupError, PermissionError) as e:
        return failure(e)
    return _switch_take(ref, take_index)


def _switch_take(ref: ItemRef, index: int) -> dict:
    takes = items.takes(ref.pointer)
    if not 0 <= index < len(takes):
        return {"success": False, "error": f"No take {index}: the item has {len(takes)} "
                                           "(indices start at 0)"}
    RPR.SetActiveTake(takes[index])
    if items.active_take(ref.pointer) != takes[index]:
        return {"success": False, "error": "REAPER did not switch the take"}
    return {"success": True, "item_guid": ref.guid, "active_take_index": index,
            "active_take": items.take_string(takes[index], "P_NAME")}


@edits(destructive=True)
def crop_to_active_take(*, item: int | str, track_index: int | None = None) -> dict:
    """Delete every take of an item except the active one (undo brings them
    back; the files stay on disk). Choose the take first with set_active_take.
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    try:
        ref = resolve(item, track_index)
    except (LookupError, PermissionError) as e:
        return failure(e)
    count = RPR.CountTakes(ref.pointer)
    if count <= 1:
        return {"success": True, "item_guid": ref.guid, "removed_takes": 0,
                "message": "The item has only one take; nothing to crop"}
    take = items.active_take(ref.pointer)
    kept = items.take_string(take, "P_NAME") if take else ""
    actions.check(CROP_TO_ACTIVE_TAKE)
    with items.only_selected([ref.pointer]):
        RPR.Main_OnCommand(CROP_TO_ACTIVE_TAKE.command, 0)
    left = RPR.CountTakes(ref.pointer)
    if left != 1:
        return {"success": False, "error": f"The item still has {left} takes after cropping"}
    return {"success": True, "item_guid": ref.guid, "removed_takes": count - 1,
            "kept_take": kept}


def _measure(item: Pointer, take: Pointer | None) -> dict:
    try:
        audio = item_audio.take_audio(item, take)
        data, rate = item_audio.read(audio)
    except (ValueError, OSError, RuntimeError) as e:
        return {"error": str(e)}
    return {"sound_seconds": round(item_audio.sound_span(audio, data, rate,
                                                         threshold_db=SOUND_DB), 3),
            **item_audio.levels(data, rate)}


def _issues(candidate: dict, longest: float) -> list[str]:
    if "error" in candidate:
        return [f"could not be analyzed: {candidate['error']}"]
    issues = []
    if candidate["clipped_samples"]:
        issues.append(f"clipped ({candidate['clipped_samples']} samples at 0 dBFS)")
    if candidate["peak_db"] is None or candidate["peak_db"] < QUIET_PEAK_DB:
        issues.append(f"very quiet (peak below {QUIET_PEAK_DB:g} dBFS)")
    if candidate["sound_seconds"] < SHORT_FRACTION * longest:
        issues.append("its sound stops early or starts late compared with the longest "
                      "take (incomplete?)")
    return issues


def _candidates(ref: ItemRef) -> list[dict]:
    """Each take of the item, or each overlapping lane item on a fixed-lane track."""
    lanes = _lane_items(ref)
    if len(lanes) > 1:
        return [{"lane": int(item_value(p, "I_FIXEDLANE")), "item_guid": items.item_guid_of(p),
                 **_measure(p, None)} for p in lanes]
    return [{"take_index": i, "name": items.take_string(t, "P_NAME"), **_measure(ref.pointer, t)}
            for i, t in enumerate(items.takes(ref.pointer))]


def _label(candidate: dict) -> dict:
    return {k: candidate[k] for k in ("take_index", "lane", "item_guid", "name")
            if k in candidate}


def _verdict(candidates: list[dict]) -> tuple[dict | None, list[dict], str]:
    """(the suggestion or None, the candidates tied for fewest issues, a note)."""
    fewest = min(len(c["issues"]) for c in candidates)
    best = [c for c in candidates if len(c["issues"]) == fewest]
    if len(candidates) == 1:
        return _label(best[0]), [], "Only one take: nothing to compare."
    note = ("A technical check only (clipping, level, completeness); the best performance "
            "is for you to judge by listening.")
    if fewest:
        note += " Every take has a problem; the suggestion has the fewest."
    if len(best) > 1:
        return None, [_label(c) for c in best], (
            f"{len(best)} takes are equally fine technically: listen to them and choose. "
            + note)
    return _label(best[0]), [], note


@read_only
def suggest_best_take(*, item: int | str, track_index: int | None = None) -> dict:
    """Check an item's takes (or, on a REAPER 7 fixed-lane track, its lanes) for
    technical problems: clipping, a very low level, and sound that stops early
    or starts late compared with the others. Suggests a take only when exactly
    one has the fewest problems; otherwise lists the tied ones ("tied"). Louder
    is not better: it cannot judge the performance, so listen before choosing,
    then use set_active_take. Nothing is changed.
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    try:
        ref = resolve(item, track_index, unlocked=False)
    except LookupError as e:
        return failure(e)
    candidates = _candidates(ref)
    if not candidates:
        return {"success": False, "error": "The item has no takes"}
    longest = max(c.get("sound_seconds", 0.0) for c in candidates)
    for candidate in candidates:
        candidate["issues"] = _issues(candidate, longest)
    suggested, tied, note = _verdict(candidates)
    result = {"success": True, "item_guid": ref.guid, "candidates": candidates,
              "suggested": suggested, "note": note}
    if tied:
        result["tied"] = tied
    return result


TOOLS = (
    list_takes,
    set_active_take,
    crop_to_active_take,
    suggest_best_take,
)
