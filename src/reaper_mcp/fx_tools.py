"""Track FX tools, and the FX helpers the master-track tools share.

reapy facts these rely on (tests/contract, REAPER 7.82): Track.add_fx returns
the FX object, not an index, and raises ValueError for an unknown plugin;
FXParam.normalized's setter is broken in reapy 0.10, so parameters are set
through TrackFX_SetParamNormalized.
"""

from reaper_mcp import fx_values
from reaper_mcp.fx_presets import current_preset
from reaper_mcp.fx_presets import preset_index as preset_index_of
from reaper_mcp.guids import resolve_track_index
from reaper_mcp.reaper import RPR, get_project
from reaper_mcp.undo import edits, read_only

INSERT_AT = -1000  # TrackFX_AddByName: instantiate -1000 - n inserts at chain position n


def add_plugin(track, fx_name: str):
    """Add fx_name to track; the new FX, or None when REAPER has no such plugin."""
    try:
        return track.add_fx(fx_name)
    except ValueError:
        return None


def set_param_normalized(track, fx_index: int, param_index: int, *, value: float):
    """Set a parameter's normalized value; returns the parameter as REAPER now has it."""
    track.fxs[fx_index].params[param_index]  # raises IndexError for a bad index first
    RPR.TrackFX_SetParamNormalized(track.id, fx_index, param_index, value)
    return track.fxs[fx_index].params[param_index]


def insert_plugin(track, fx_name: str, position: int):
    """Add fx_name to track at position in its chain (0 is first; past the end
    appends); the new FX, or None when REAPER has no such plugin."""
    position = max(0, min(position, track.n_fxs))
    # instantiate <= -1000 inserts at -1000 - instantiate (TrackFX_AddByName).
    index = RPR.TrackFX_AddByName(track.id, fx_name, False, INSERT_AT - position)
    if index < 0:
        return None
    return track.fxs[index]


@edits()
def add_fx(*, track_index: int, fx_name: str, position: int | None = None) -> dict:
    """
    Add an FX plugin to a track. Works for both instruments (VSTi) and effects (VST/AU).
    Use the plugin name as shown in REAPER's FX browser, or as list_installed_fx gives it.
    Built-in Cockos plugins: ReaEQ, ReaComp, ReaDelay, ReaVerb, ReaLimit, ReaSynth,
    ReaSamplOmatic5000, ReaTune, ReaGate, ReaFIR, ReaXcomp.
    position: where in the FX chain (0 = first, processed before the others);
    default: at the end.
    """
    track = get_project().tracks[track_index]
    if position is not None and position < 0:
        return {"success": False, "error": "position must be 0 (first) or more"}
    fx = add_plugin(track, fx_name) if position is None else insert_plugin(
        track, fx_name, position)
    if fx is None:
        return {"success": False, "error": f"Plugin not found: '{fx_name}'. "
                                           "list_installed_fx finds installed plugins by name"}
    return {
        "success": True,
        "fx_index": fx.index,
        "name": fx.name,
        "n_params": fx.n_params,
        "track_index": track_index,
    }


@edits(destructive=True)
def remove_fx(*, track_index: int | str, fx_index: int) -> dict:
    """Remove an FX plugin from a track by its index.
    track_index: the track's index, or its GUID from list_tracks (safer: a GUID
    still names the same track after tracks were added, deleted or moved)."""
    track_index, track = resolve_track_index(get_project(), track_index)
    fx_name = track.fxs[fx_index].name
    RPR.TrackFX_Delete(track.id, fx_index)
    return {"success": True, "track_index": track_index, "removed": fx_name}


@edits(idempotent=True)
def set_fx_parameter(
    *,
    track_index: int, fx_index: int, param_index: int, value: float
) -> dict:
    """
    Set a normalized parameter value (0.0–1.0) on an FX plugin.
    Use get_fx_parameters to discover available parameters and their indices.
    """
    track = get_project().tracks[track_index]
    param = set_param_normalized(track, fx_index, param_index, value=value)
    return {
        "success": True,
        "track_index": track_index,
        "fx_index": fx_index,
        "param_index": param_index,
        "param_name": param.name,
        "value": param.normalized,
        "formatted_value": fx_values.displayed(track.id, fx_index, param_index),
    }


def find_param(fx, name: str) -> int:
    """The index of fx's parameter called name: an exact match (ignoring case),
    else the only one whose name contains it; LookupError otherwise."""
    names = [param.name for param in fx.params]
    wanted = name.strip().casefold()
    exact = [i for i, n in enumerate(names) if n.strip().casefold() == wanted]
    if exact:
        return exact[0]
    partial = [i for i, n in enumerate(names) if wanted and wanted in n.casefold()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise LookupError(f"'{name}' matches several parameters of {fx.name}: "
                          f"{[names[i] for i in partial]}; use the full name")
    raise LookupError(f"{fx.name} has no parameter '{name}'. Its parameters: {names}")


def fx_index_error(track, fx_index: int) -> dict | None:
    """A failure result when track has no FX fx_index (a negative index too:
    REAPER takes -1 as no FX, where Python would count from the end)."""
    if 0 <= fx_index < track.n_fxs:
        return None
    return {"success": False, "error": f"There is no FX {fx_index} on that track: it has "
                                       f"{track.n_fxs} (numbered from 0; list_track_fx lists them)"}


def _target_normalized(track, fx_index: int, param_index: int, *, value: str) -> float:
    """The normalized value that makes the parameter display value."""
    if fx_values.ambiguous_number(value):
        raise ValueError(f"'{value}' is ambiguous: write one thousand as '1000' or '1 k' "
                         "(with the unit, e.g. '1000 Hz' or '1 kHz'), and decimals with "
                         "one or two digits ('1.5')")
    parsed = fx_values.parse_value(value)
    if parsed is None:
        found, seen = fx_values.match_text(track.id, fx_index, param_index, text=value)
        if found is None:
            raise ValueError(f"'{value}' is not a value this parameter shows; it shows: {seen}")
        return found
    number, dimension = parsed
    fx_values.check_dimension(track.id, fx_index, param_index, dimension=dimension)
    return fx_values.bisect_to(track.id, fx_index, param_index, target=number)


def _fx_owner(project, track_index: int | None, master: bool):
    """The track (or master track) an FX tool works on; ValueError when the
    choice is missing or both are given."""
    if master == (track_index is not None):
        raise ValueError("Give either track_index or master=true")
    return project.master_track if master else project.tracks[track_index]


@edits(idempotent=True)
def set_fx_parameter_by_name(
    *, param_name: str, value: str, fx_index: int, track_index: int | None = None,
    master: bool = False,
) -> dict:
    """
    Set an FX parameter by its name, with the value as the plugin displays it:
    "-3 dB", "300 Hz", "1.5 kHz", "20 ms", "2 s", "4:1", or a word such as "On".
    Give track_index for a track's FX, or master=true for the master track's FX
    (e.g. a limiter's ceiling). param_name: as get_fx_parameters lists it (case
    does not matter; part of the name works when only one parameter contains it).
    Write thousands without a separator ("1000 Hz" or "1 kHz", not "1.000 Hz").
    A value past the parameter's range lands on its end; the result says what the
    plugin now displays and whether that is the value asked for.
    """
    try:
        track = _fx_owner(get_project(), track_index, master)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    error = fx_index_error(track, fx_index)
    if error:
        return error
    fx = track.fxs[fx_index]
    try:
        param_index = find_param(fx, param_name)
        normalized = _target_normalized(track, fx_index, param_index, value=value)
    except (LookupError, ValueError) as e:
        return {"success": False, "error": str(e)}
    RPR.TrackFX_SetParamNormalized(track.id, fx_index, param_index, normalized)
    shown = fx_values.displayed(track.id, fx_index, param_index)
    parsed = fx_values.parse_value(value)
    exact = (fx_values.reached(shown, parsed[0]) if parsed
             else shown.strip().casefold() == value.strip().casefold())
    owner = {"master": True} if master else {"track_index": track_index}
    result = {"success": True, **owner, "fx_index": fx_index,
              "fx_name": fx.name, "param_index": param_index,
              "param_name": fx.params[param_index].name, "requested": value,
              "formatted_value": shown, "reached": exact}
    if not exact:
        low, high = fx_values.display_range(track.id, fx_index, param_index)
        result["note"] = (f"The parameter goes from '{low}' to '{high}'; it was set as "
                          "close as it gets")
    return result


@read_only
def get_fx_parameters(*, track_index: int, fx_index: int) -> dict:
    """Get all parameters for an FX plugin, including names, indices, and current values."""
    fx = get_project().tracks[track_index].fxs[fx_index]
    params = [
        {
            "index": i,
            "name": param.name,
            "normalized_value": param.normalized,
            "formatted_value": param.formatted,
        }
        for i, param in enumerate(fx.params)
    ]
    return {
        "success": True,
        "track_index": track_index,
        "fx_index": fx_index,
        "fx_name": fx.name,
        "parameters": params,
    }


@read_only
def list_track_fx(*, track_index: int) -> dict:
    """List all FX plugins on a track."""
    track = get_project().tracks[track_index]
    fxs = track.fxs
    fx_list = [
        {"index": i, "name": fxs[i].name, "enabled": fxs[i].is_enabled, "n_params": fxs[i].n_params}
        for i in range(track.n_fxs)
    ]
    return {"success": True, "track_index": track_index, "fx": fx_list}


@edits(idempotent=True)
def bypass_fx(*, track_index: int, fx_index: int, bypassed: bool) -> dict:
    """Enable or bypass (disable) an FX plugin on a track."""
    track = get_project().tracks[track_index]
    fx = track.fxs[fx_index]
    fx.is_enabled = not bypassed
    return {
        "success": True,
        "track_index": track_index,
        "fx_index": fx_index,
        "fx_name": fx.name,
        "bypassed": not track.fxs[fx_index].is_enabled,
    }


@edits(destructive=True, idempotent=True)
def load_fx_preset(
    *, track_index: int, fx_index: int, preset_name: str | None = None,
    preset_index: int | None = None,
) -> dict:
    """Load a preset for an FX plugin, by preset_name (as REAPER's preset menu
    shows it) or by preset_index (its place in list_fx_presets' list, 0 = first;
    use it for presets the plugin ships with if the name is not found).
    The preset replaces the plugin's current settings (undo brings them back)."""
    if (preset_name is None) == (preset_index is None):
        return {"success": False, "error": "Give either preset_name or preset_index"}
    track = get_project().tracks[track_index]
    error = fx_index_error(track, fx_index)
    if error:
        return error
    fx = track.fxs[fx_index]
    if preset_index is not None:
        _, count = preset_index_of(track.id, fx_index)
        if not 0 <= preset_index < count or not RPR.TrackFX_SetPresetByIndex(
                track.id, fx_index, preset_index):
            return {"success": False, "error": f"{fx.name} has no preset {preset_index}: it has "
                                               f"{count} (numbered from 0)"}
    elif not RPR.TrackFX_SetPreset(track.id, fx_index, preset_name):
        return {"success": False, "error": f"{fx.name} has no preset named '{preset_name}' "
                                           "(names must match exactly); list_fx_presets "
                                           "lists them with their preset_index"}
    return {
        "success": True,
        "track_index": track_index,
        "fx_index": fx_index,
        "fx_name": fx.name,
        "preset": current_preset(track.id, fx_index),
    }


TOOLS = (
    add_fx,
    remove_fx,
    set_fx_parameter,
    set_fx_parameter_by_name,
    get_fx_parameters,
    list_track_fx,
    bypass_fx,
    load_fx_preset,
)
