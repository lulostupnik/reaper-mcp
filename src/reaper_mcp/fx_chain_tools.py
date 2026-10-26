"""Moving and copying FX within and between tracks, and listing an FX's presets."""

from reaper_mcp.fx_presets import (
    MAX_PRESETS,
    current_preset,
    param_values,
    preset_names,
    same_settings,
    scratch_copy,
)
from reaper_mcp.fx_tools import fx_index_error
from reaper_mcp.reaper import RPR, get_project
from reaper_mcp.undo import edits


def _chain(track) -> list[str]:
    return [fx.name for fx in track.fxs]


def _transfer(*, source, fx_index: int, dest, position: int | None, move: bool) -> dict:
    """Copy or move source's FX fx_index to position in dest's chain (default
    the end), then check the chains read back as expected."""
    error = fx_index_error(source, fx_index)
    if error:
        return error
    source_chain, dest_chain = _chain(source), _chain(dest)
    name = source_chain[fx_index]
    same = source.id == dest.id
    if same and move:
        dest_chain.pop(fx_index)
    position = len(dest_chain) if position is None else max(0, min(position, len(dest_chain)))
    dest_chain.insert(position, name)
    if move and not same:
        source_chain.pop(fx_index)
    RPR.TrackFX_CopyToTrack(source.id, fx_index, dest.id, position, move)
    if _chain(dest) != dest_chain or (not same and _chain(source) != source_chain):
        return {"success": False,
                "error": "REAPER's FX chains do not look as expected afterwards; undo this "
                         "step and check the chains with list_track_fx",
                "chain": _chain(dest)}
    return {"success": True, "fx_name": name, "fx_index": position, "chain": dest_chain}


@edits()
def move_fx(
    *, track_index: int, fx_index: int, to_position: int | None = None,
    to_track_index: int | None = None,
) -> dict:
    """
    Move an FX to another place in the chain, or to another track (with its settings).
    to_position: its new place in the chain, 0 = first (processed first); default:
    the end of the destination chain. to_track_index: the track to move it to
    (default: the same track; then to_position is required).
    """
    if to_track_index is None and to_position is None:
        return {"success": False, "error": "Give to_position, to_track_index or both"}
    if to_position is not None and to_position < 0:
        return {"success": False, "error": "to_position must be 0 (first) or more"}
    tracks = get_project().tracks
    dest_index = track_index if to_track_index is None else to_track_index
    result = _transfer(source=tracks[track_index], fx_index=fx_index, dest=tracks[dest_index],
                       position=to_position, move=True)
    return {**result, "source_track_index": track_index, "to_track_index": dest_index}


@edits()
def copy_fx(
    *, track_index: int, fx_index: int, to_track_index: int, to_position: int | None = None
) -> dict:
    """
    Copy an FX, with its current settings, to a track (the same track works too).
    to_position: the copy's place in that chain, 0 = first; default: the end.
    """
    if to_position is not None and to_position < 0:
        return {"success": False, "error": "to_position must be 0 (first) or more"}
    tracks = get_project().tracks
    result = _transfer(source=tracks[track_index], fx_index=fx_index,
                       dest=tracks[to_track_index], position=to_position, move=False)
    return {**result, "source_track_index": track_index, "to_track_index": to_track_index}


@edits(idempotent=True)
def list_fx_presets(*, track_index: int, fx_index: int) -> dict:
    """
    List the presets of an FX plugin by name (in order: a preset's place in the
    list, from 0, is the preset_index load_fx_preset takes), and the one showing now.
    Only call it when the user wants to pick a preset: REAPER can only name a
    preset by loading it, so each preset is loaded in turn into a bypassed copy
    of the plugin, which is then removed; the plugin itself is not touched. It
    is one undo step.
    """
    track = get_project().tracks[track_index]
    error = fx_index_error(track, fx_index)
    if error:
        return error
    fx_name = track.fxs[fx_index].name
    current = current_preset(track.id, fx_index)
    before = param_values(track.id, fx_index)
    try:
        with scratch_copy(track.id, fx_index) as copy:
            names, count = preset_names(track.id, copy)
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    restored = same_settings(track.id, fx_index, before)
    result = {"success": True, "track_index": track_index, "fx_index": fx_index,
              "fx_name": fx_name, "current_preset": current, "count": count,
              "presets": names, "settings_restored": restored}
    if count > MAX_PRESETS:
        result["note"] = f"Only the first {MAX_PRESETS} of {count} presets are listed"
    if not restored:
        result["warning"] = ("The plugin's settings did not read back exactly as before; "
                             "use undo to put them back")
    return result


TOOLS = (move_fx, copy_fx, list_fx_presets)
