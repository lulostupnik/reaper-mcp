"""Removing silence from an item, and normalizing it.

The take's own audio file is analyzed here (see item_audio), never through
REAPER's Dynamic split dialog: a modal window would hang the bridge.
"""

from reaper_mcp import item_audio, items
from reaper_mcp.item_tools import MAX_VOLUME_DB, failure, index_note, resolve
from reaper_mcp.items import item_value, ref_of
from reaper_mcp.reaper import RPR
from reaper_mcp.undo import edits
from reaper_mcp.units import db_to_linear, linear_to_db

EDGE = 1e-6  # seconds: a piece edge this close to the item's edge is that edge
DEFAULT_TARGETS = {"peak": -1.0, "lufs": -18.0}
OTHER_TAKES_CUT = ("Only the active take was analyzed; the item's other takes are cut at "
                   "the same places (use crop_to_active_take first to keep just this one)")


def _analyze(item, track_index, *, threshold_db: float, min_silence: float, padding: float):
    """(ref, audio, silent stretches in item seconds, already padded)."""
    if min_silence < 0 or padding < 0:
        raise ValueError("min_silence and padding must be >= 0 seconds")
    ref = resolve(item, track_index)
    audio = item_audio.take_audio(ref.pointer, cutting=True)
    data, rate = item_audio.read(audio)
    runs = item_audio.silent_runs(audio, data, rate, threshold_db=threshold_db,
                                  min_silence=min_silence)
    if RPR.CountTakes(ref.pointer) > 1:
        audio.warnings.append(OTHER_TAKES_CUT)
    return ref, audio, item_audio.pad_runs(runs, padding, audio.length)


def _plan(position: float, runs: list[tuple[float, float]]) -> list[dict]:
    return [{"start": round(position + a, 6), "end": round(position + b, 6)} for a, b in runs]


def _cut(ref, runs: list[tuple[float, float]], fade: float) -> tuple[list, int]:
    """Split the item around runs and delete the silent pieces, with a short
    fade where sound was cut; (kept pieces, number deleted)."""
    position = item_value(ref.pointer, "D_POSITION")
    length = item_value(ref.pointer, "D_LENGTH")
    cuts = [position + t for a, b in runs for t in (a, b) if EDGE < t < length - EDGE]
    kept, deleted = [], 0
    for piece in items.split(ref.pointer, cuts):
        start = item_value(piece, "D_POSITION") - position
        end = start + item_value(piece, "D_LENGTH")
        if any(a - EDGE <= (start + end) / 2 <= b + EDGE for a, b in runs):
            RPR.DeleteTrackMediaItem(ref.track, piece)
            deleted += 1
            continue
        kept.append(piece)
        size = min(fade, (end - start) / 2)
        if start > EDGE:
            RPR.SetMediaItemInfo_Value(piece, "D_FADEINLEN", size)
        if end < length - EDGE:
            RPR.SetMediaItemInfo_Value(piece, "D_FADEOUTLEN", size)
    return kept, deleted


def _whole_item_silent(runs, length: float) -> bool:
    return sum(b - a for a, b in runs) >= length - EDGE


def _pieces_after(runs, length: float) -> int:
    """How many pieces stay once runs are removed from an item of length."""
    edges = [t for a, b in runs for t in (a, b)]
    sound = list(zip([0.0, *edges], [*edges, length], strict=True))[::2]
    return sum(1 for a, b in sound if b - a > EDGE)


@edits(destructive=True)
def remove_silence(
    *,
    item: int | str,
    track_index: int | None = None,
    threshold_db: float = -45.0,
    min_silence: float = 0.4,
    padding: float = 0.1,
    fade: float = 0.01,
    dry_run: bool = False,
) -> dict:
    """Remove the silent parts of an audio item: it is split around each stretch
    quieter than threshold_db (RMS dBFS of the recording, before item volume)
    lasting at least min_silence seconds, and those pieces are deleted.
    padding: seconds of quiet kept next to the sound, so breaths and note tails
    are not cut. fade: a short fade (seconds) where sound was cut, against clicks.
    dry_run=true only reports what would be removed (ranges, total seconds and how
    many pieces would stay). Raise threshold_db (e.g. -35) for a noisy recording;
    lower it (e.g. -55) if quiet notes get cut. Only the active take is analyzed.
    item: the item's GUID from list_items (preferred), or its index on track_index.
    Returns the removed time ranges and the GUIDs of the pieces kept."""
    try:
        ref, audio, runs = _analyze(item, track_index, threshold_db=threshold_db,
                                       min_silence=min_silence, padding=padding)
    except (LookupError, PermissionError, ValueError) as e:
        return failure(e)
    position = item_value(ref.pointer, "D_POSITION")
    result = {"success": True, "removed": _plan(position, runs), "dry_run": dry_run,
              "removed_seconds": round(sum(b - a for a, b in runs), 6),
              "pieces_after": _pieces_after(runs, audio.length), **index_note(item, ref)}
    if audio.warnings:
        result["warnings"] = audio.warnings
    if not runs:
        return {**result, "message": "No silence found with these settings; nothing changed",
                "kept": items.describe_pieces([ref.pointer])}
    if _whole_item_silent(runs, audio.length):
        return {"success": False, "error": f"The whole item is quieter than {threshold_db:g} "
                                           "dBFS; nothing was removed. Lower threshold_db."}
    if dry_run:
        return result
    try:
        kept, deleted = _cut(ref, runs, fade)
    except items.SplitError as e:
        # The splits come before any delete, so nothing was deleted yet.
        return {"success": False, "error": str(e), "pieces": items.describe_pieces(e.pieces)}
    return {**result, "deleted_pieces": deleted, "kept": items.describe_pieces(kept)}


@edits()
def trim_silence(
    *,
    item: int | str,
    track_index: int | None = None,
    threshold_db: float = -45.0,
    padding: float = 0.1,
    dry_run: bool = False,
) -> dict:
    """Trim the silence at the start and end of an audio item (nothing in the
    middle is touched); the audio that stays does not move in time.
    threshold_db: RMS dBFS of the recording below which it counts as silence.
    padding: seconds of quiet kept before the first and after the last sound.
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    try:
        ref, audio, runs = _analyze(item, track_index, threshold_db=threshold_db,
                                       min_silence=0.0, padding=padding)
    except (LookupError, PermissionError, ValueError) as e:
        return failure(e)
    if _whole_item_silent(runs, audio.length):
        return {"success": False, "error": f"The whole item is quieter than {threshold_db:g} "
                                           "dBFS; nothing was trimmed. Lower threshold_db."}
    head = next((b for a, b in runs if a <= EDGE), 0.0)
    tail = next((a for a, b in runs if b >= audio.length - EDGE), audio.length)
    position = item_value(ref.pointer, "D_POSITION")
    result = {"success": True, "dry_run": dry_run, "trimmed_start_seconds": round(head, 6),
              "trimmed_end_seconds": round(audio.length - tail, 6), **(
                  {"warnings": audio.warnings} if audio.warnings else {})}
    if dry_run or (head <= 0 and tail >= audio.length):
        return result
    if head > 0:
        items.set_start(ref.pointer, position + head)
    items.set_length(ref.pointer, tail - head)
    return {**result, "item": items.describe(ref_of(ref.pointer))}


def _gain_db(audio, mode: str, target: float) -> float | None:
    """The gain (dB) that brings the take's recording to target, or None."""
    measured = item_audio.levels(*item_audio.read(audio), lufs=mode == "lufs")
    level = measured["peak_db" if mode == "peak" else "lufs"]
    return None if level is None else target - level


@edits(idempotent=True)
def normalize_item(
    *,
    item: int | str,
    track_index: int | None = None,
    mode: str = "peak",
    target_db: float | None = None,
) -> dict:
    """Normalize an audio item by setting its active take's volume so the
    recording, together with the item's own volume, reaches target_db.
    mode "peak": loudest sample at target_db dBFS (default -1). mode "lufs":
    integrated loudness at target_db LUFS (default -18, a comfortable level for
    mixing). Take FX and the track's volume still apply on top. Only the take
    volume changes; the file is untouched.
    item: the item's GUID from list_items (preferred), or its index on track_index."""
    mode = mode.lower()
    if mode not in DEFAULT_TARGETS:
        return {"success": False, "error": f"mode must be one of {sorted(DEFAULT_TARGETS)}"}
    target = DEFAULT_TARGETS[mode] if target_db is None else target_db
    try:
        ref = resolve(item, track_index)
        audio = item_audio.take_audio(ref.pointer)
    except (LookupError, PermissionError, ValueError) as e:
        return failure(e)
    item_volume = abs(item_value(ref.pointer, "D_VOL"))
    if item_volume <= 0:
        return {"success": False, "error": "The item's volume is -inf dB (silent); set it with "
                                           "edit_item volume_db first"}
    gain_db = _gain_db(audio, mode, target)
    if gain_db is None:
        return {"success": False, "error": "The item is silent or too short to measure"}
    item_db = linear_to_db(item_volume)
    gain_db -= item_db  # the item's volume applies on top of the take's
    if gain_db > MAX_VOLUME_DB:
        return {"success": False, "error": f"Reaching the target needs {gain_db:+.1f} dB of take "
                                           "volume: the item is nearly silent (or its volume "
                                           "is turned down), so nothing was changed"}
    take = items.active_take(ref.pointer)
    old = items.take_value(take, "D_VOL")
    sign = -1.0 if old < 0 else 1.0  # keep the take's polarity
    RPR.SetMediaItemTakeInfo_Value(take, "D_VOL", sign * db_to_linear(gain_db))
    result = {"success": True, "mode": mode, "target_db": target,
              "previous_take_volume_db": round(linear_to_db(abs(old)), 2),
              "take_volume_db": round(gain_db, 2), "item_volume_db": round(item_db, 2),
              "item": items.describe(ref_of(ref.pointer))}
    if abs(item_db) > 0.005:
        audio.warnings.append(f"The item's own volume ({item_db:+.2f} dB) was taken into "
                              "account: the take volume makes up the rest")
    if audio.warnings:
        result["warnings"] = audio.warnings
    return result


TOOLS = (
    remove_silence,
    trim_silence,
    normalize_item,
)
