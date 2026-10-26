"""Mix snapshots: storing them in the project, capturing the mixer, matching them up.

The tools are in snapshot_tools.py.

A snapshot holds every track's (and the master's) volume, pan, mute, solo, and
each FX's enabled state and parameter values. Sends, and FX or tracks added
later, are not part of it. Snapshots are stored inside the project with
SetProjExtState, so they are saved with the project file and go wherever it
goes (until it is saved, they live only in the open project).

Storage: each value is compact JSON, base64-encoded (so no quote or newline can
upset the .rpp file) and split into CHUNK-sized pieces under "<key>#0", "<key>#1"...,
with the piece count under "<key>#n". Every value is written under a new key
and never rewritten in place: INDEX_KEY holds only the key of the current list
of snapshots, a single short value switched once the new list is fully
written. So a call to REAPER lost part-way leaves at worst an unused value,
never a half-written list that would make the earlier snapshots disappear.

Tracks are matched by GUID, so a snapshot still finds them after tracks are
moved; FX by position and name, then by name.
"""

import base64
import json
import math
import time
import uuid

from reaper_mcp.guids import track_guid
from reaper_mcp.reaper import RPR, get_project
from reaper_mcp.units import db_to_linear, linear_to_db

EXT_NAME = "reaper-mcp"
INDEX_KEY = "mix_snapshots"
CHUNK = 4000
READ_BUFFER = CHUNK + 64
MAX_NAME = 64
# Each snapshot is stored in the .rpp file: a few for A/B comparisons, not a history.
MAX_SNAPSHOTS = 20
# A plug-in with more parameters (a big EQ or synth) has only its enabled state
# saved: each parameter is two calls to REAPER, and a slow capture holds every
# other tool call up.
MAX_PARAMS_PER_FX = 128
# Parameters REAPER adds to every FX that are not settings: bypass (the FX's
# on/off state, saved as "enabled") and delta solo (a listening aid).
NOT_SETTINGS = (":bypass", ":delta")
PARAM_DIGITS = 6  # normalized values are stored rounded to this many decimals
CURRENT = "current"  # compare_mix_snapshots: the mix as it is now
MASTER = "master"
PARAM_EPSILON = 1e-5
SETTING_EPSILON = 1e-4  # pan, volume: smaller differences are not worth reporting
MAX_VOLUME = db_to_linear(12.0)  # the top of REAPER's fader
NAME_BUFFER = 256
VERSION = 1


class SnapshotError(LookupError):
    """A snapshot, or the list of them, cannot be found or used."""


# Storage


def _get(key: str) -> str:
    # (retval, project, extname, key, value, size) per REAPER's Python binding.
    return str(RPR.GetProjExtState(0, EXT_NAME, key, "", READ_BUFFER)[4])


def read_blob(key: str):
    count = _get(f"{key}#n")
    if not count.isdigit():
        return None
    text = "".join(_get(f"{key}#{i}") for i in range(int(count)))
    try:
        return json.loads(base64.b64decode(text.encode()).decode())
    except ValueError:
        return None


def delete_blob(key: str) -> None:
    count = _get(f"{key}#n")
    RPR.SetProjExtState(0, EXT_NAME, f"{key}#n", "")  # unreadable from here on
    for i in range(int(count) if count.isdigit() else 0):
        RPR.SetProjExtState(0, EXT_NAME, f"{key}#{i}", "")


def write_blob(key: str, value) -> None:
    """value under key; the piece count last, so it is only readable once whole."""
    text = base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()
    pieces = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    for i, piece in enumerate(pieces):
        RPR.SetProjExtState(0, EXT_NAME, f"{key}#{i}", piece)
    RPR.SetProjExtState(0, EXT_NAME, f"{key}#n", str(len(pieces)))


def store(prefix: str, value) -> str:
    """value written under a new key, which is returned."""
    key = f"{prefix}_{uuid.uuid4().hex[:12]}"
    write_blob(key, value)
    return key


def read_index() -> list[dict]:
    """The saved snapshots, oldest first. Raises SnapshotError when the list is
    there but cannot be read, rather than passing it off as empty (a save
    would then replace it and lose every earlier snapshot)."""
    key = _get(INDEX_KEY)
    if not key:
        return []
    index = read_blob(key)
    if not isinstance(index, list) or not all(isinstance(e, dict) and isinstance(
            e.get("name"), str) and isinstance(e.get("key"), str) for e in index):
        raise SnapshotError("The list of mix snapshots in this project could not be read back; "
                            "nothing was changed")
    return index


def write_index(index: list[dict]) -> None:
    """Switch to a new list of snapshots, then delete the old one."""
    old = _get(INDEX_KEY)
    RPR.SetProjExtState(0, EXT_NAME, INDEX_KEY, store("index", index))
    if old:
        delete_blob(old)


def find(index: list[dict], name: str) -> dict | None:
    wanted = name.strip().casefold()
    return next((entry for entry in index if entry["name"].casefold() == wanted), None)


def load(name: str) -> dict:
    index = read_index()
    entry = find(index, name)
    if entry is None:
        names = [e["name"] for e in index]
        raise SnapshotError(f"No mix snapshot called {name!r}. Saved: {names or 'none'} "
                            "(list_mix_snapshots lists them)")
    snapshot = read_blob(entry["key"])
    problem = invalid(snapshot)
    if problem is not None:
        raise SnapshotError(f"The mix snapshot {entry['name']!r} could not be read back "
                            f"({problem}); nothing was changed")
    return snapshot


# Checking a stored snapshot before anything is set from it


def _number(value, low: float, high: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def _invalid_fx(fx) -> str | None:
    if not isinstance(fx, dict) or not isinstance(fx.get("name"), str):
        return "an FX without a name"
    if not isinstance(fx.get("enabled"), bool):
        return f"{fx['name']}: no on/off state"
    params = fx.get("params")
    if params is None:
        return None
    if not isinstance(params, list) or not all(
            isinstance(p, list) and len(p) == 2 and isinstance(p[0], (str, type(None)))
            and (p[1] is None or _number(p[1], 0.0, 1.0)) for p in params):
        return f"{fx['name']}: a parameter value outside 0..1"
    return None


def _invalid_track(track) -> str | None:
    if not isinstance(track, dict) or not isinstance(track.get("guid"), str):
        return "a track without a GUID"
    name = track.get("name")
    checks = (("volume", 0.0, MAX_VOLUME), ("pan", -1.0, 1.0), ("mute", 0.0, 1.0),
              ("solo", 0.0, 6.0))
    for key, low, high in checks:
        if not _number(track.get(key), low, high):
            return f"{name}: {key} {track.get(key)!r} is not between {low:g} and {high:g}"
    if not isinstance(track.get("fx"), list):
        return f"{name}: no FX list"
    return next((f"{name}: {p}" for p in map(_invalid_fx, track["fx"]) if p), None)


def invalid(snapshot) -> str | None:
    """What is wrong with a stored snapshot, or None when it is safe to recall."""
    if not isinstance(snapshot, dict) or snapshot.get("version") != VERSION:
        return "not a snapshot this version of the server wrote"
    if not isinstance(snapshot.get("tracks"), list) or not isinstance(snapshot.get("name"), str):
        return "no track list"
    for track in [*snapshot["tracks"], snapshot.get("master")]:
        problem = _invalid_track(track)
        if problem:
            return problem
    return None


# Capturing the mixer


def _param_name(pointer: str, fx: int, param: int) -> str:
    # (retval, track, fx, param, name, size) per REAPER's Python binding.
    return str(RPR.TrackFX_GetParamName(pointer, fx, param, "", NAME_BUFFER)[4])


def _not_settings(pointer: str, fx: int) -> set[int]:
    """Indices of REAPER's own bypass and delta parameters on this FX."""
    found = (RPR.TrackFX_GetParamFromIdent(pointer, fx, ident) for ident in NOT_SETTINGS)
    return {index for index in found if index >= 0}


def _capture_params(pointer: str, fx: int, count: int, *, names: bool) -> list[list]:
    """[name, value] per parameter; None for REAPER's bypass and delta, and
    for the names when names=False (half the calls, for a comparison)."""
    skip = _not_settings(pointer, fx)
    return [[_param_name(pointer, fx, p) if names else None,
             None if p in skip else round(RPR.TrackFX_GetParamNormalized(pointer, fx, p),
                                          PARAM_DIGITS)]
            for p in range(count)]


def _capture_fx(track, *, names: bool) -> list[dict]:
    chain = []
    for fx in track.fxs:
        count = RPR.TrackFX_GetNumParams(track.id, fx.index)
        entry = {"name": fx.name, "enabled": bool(fx.is_enabled), "params": None,
                 "n_params": count}
        if count <= MAX_PARAMS_PER_FX:
            entry["params"] = _capture_params(track.id, fx.index, count, names=names)
        chain.append(entry)
    return chain


def _capture_track(track, index: int | None, *, names: bool) -> dict:
    return {
        "guid": MASTER if index is None else track_guid(track),
        "index": index,
        "name": MASTER if index is None else track.name,
        "volume": track.get_info_value("D_VOL"),
        "pan": track.get_info_value("D_PAN"),
        "mute": track.get_info_value("B_MUTE"),
        "solo": 0.0 if index is None else track.get_info_value("I_SOLO"),
        "fx": _capture_fx(track, names=names),
    }


def capture_mix(name: str, *, names: bool = True) -> dict:
    """The mixer as it is now, as a snapshot named name. names=False leaves the
    parameter names out: enough to compare with or recall a saved snapshot."""
    project = get_project()
    tracks = [_capture_track(track, i, names=names) for i, track in enumerate(project.tracks)]
    return {"version": VERSION, "name": name, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tracks": tracks, "master": _capture_track(project.master_track, None, names=names)}


def skipped_params(snapshot: dict) -> list[str]:
    return [f"{t['name']}: {fx['name']} ({fx['n_params']} parameters)"
            for t in [*snapshot["tracks"], snapshot["master"]]
            for fx in t["fx"] if fx["params"] is None]


def same_layout(a: list[list], b: list[list]) -> bool:
    """Whether two parameter lists of one FX line up index by index (a name
    left out, None, matches any)."""
    return len(a) == len(b) and all(
        p[0] is None or q[0] is None or p[0] == q[0] for p, q in zip(a, b, strict=True))


def param_changes(a: list[list], b: list[list]) -> list[int]:
    """Indices of the parameters whose value differs (bypass and delta never do)."""
    return [i for i, (p, q) in enumerate(zip(a, b, strict=True))
            if p[1] is not None and q[1] is not None and abs(p[1] - q[1]) > PARAM_EPSILON]


# Matching a snapshot to the project


def match_fx(saved: list[dict], live: list[dict]) -> list[tuple[dict, int | None]]:
    """Each saved FX with the index of its live FX: the same position when the
    name matches there, else the first unused one of that name; None if gone."""
    used: set[int] = set()
    pairs = []
    for position, fx in enumerate(saved):
        if position < len(live) and live[position]["name"] == fx["name"] and position not in used:
            match = position
        else:
            match = next((i for i, other in enumerate(live)
                          if other["name"] == fx["name"] and i not in used), None)
        if match is not None:
            used.add(match)
        pairs.append((fx, match))
    return pairs


def by_guid(snapshot: dict) -> dict[str, dict]:
    return {t["guid"]: t for t in [*snapshot["tracks"], snapshot["master"]]}


def _volume_text(linear: float) -> str:
    return f"{linear_to_db(linear):+.2f} dB"


def setting_changes(a: dict, b: dict) -> list[tuple[str, object, object]]:
    """(setting, a's value, b's value) for each mixer setting that differs."""
    changes = []
    if abs(linear_to_db(a["volume"]) - linear_to_db(b["volume"])) > SETTING_EPSILON:
        changes.append(("volume", _volume_text(a["volume"]), _volume_text(b["volume"])))
    if abs(a["pan"] - b["pan"]) > SETTING_EPSILON:
        changes.append(("pan", round(a["pan"], 3), round(b["pan"], 3)))
    if bool(a["mute"]) != bool(b["mute"]):
        changes.append(("muted", bool(a["mute"]), bool(b["mute"])))
    if bool(a["solo"]) != bool(b["solo"]):
        changes.append(("soloed", bool(a["solo"]), bool(b["solo"])))
    return changes
