"""Mix snapshots for A/B comparisons: save the mixer, try something, compare, go back.

See mix_snapshots.py for what a snapshot holds and how it is stored.
"""

from reaper_mcp.mix_snapshots import (
    CURRENT,
    MASTER,
    MAX_NAME,
    MAX_PARAMS_PER_FX,
    MAX_SNAPSHOTS,
    SnapshotError,
    by_guid,
    capture_mix,
    delete_blob,
    find,
    load,
    match_fx,
    param_changes,
    read_index,
    same_layout,
    setting_changes,
    skipped_params,
    store,
    write_index,
)
from reaper_mcp.reaper import RPR, get_project
from reaper_mcp.undo import edits, no_undo, read_only

FORMAT_BUFFER = 256
# compare_mix_snapshots lists at most this many rows in all; an FX with more
# changed parameters than MAX_PARAMS_LISTED gets one summary row instead,
# unless detail=true.
MAX_DIFFERENCES = 100
MAX_PARAMS_LISTED = 8


# Tools


def _name_error(name: str) -> str | None:
    clean = name.strip()
    if not clean:
        return "the snapshot needs a name, e.g. \"before EQ\""
    if len(clean) > MAX_NAME:
        return f"the name is longer than {MAX_NAME} characters"
    if clean.casefold() == CURRENT:
        return f"{CURRENT!r} is reserved: compare_mix_snapshots uses it for the mix as it is now"
    return None


@no_undo(destructive=True)
def save_mix_snapshot(*, name: str, overwrite: bool = False) -> dict:
    """
    Save the mixer as it is now under a name ("before EQ", "mix A"): every
    track's and the master's volume, pan, mute, solo, and each FX's on/off
    state and parameter values. Saved inside the project, so it is kept when
    the project is saved. Recall it with recall_mix_snapshot, compare with
    compare_mix_snapshots. A name already used is only replaced with
    overwrite=true. Not an undo step: saving changes nothing in the mix.
    """
    error = _name_error(name)
    if error:
        return {"success": False, "error": error}
    name = name.strip()
    try:
        index = read_index()
    except SnapshotError as e:
        return {"success": False, "error": str(e)}
    existing = find(index, name)
    if existing is not None and not overwrite:
        return {"success": False, "error": f"a snapshot called {existing['name']!r} already "
                                           "exists; choose another name (e.g. with the time), "
                                           "or pass overwrite=true to replace it"}
    if existing is None and len(index) >= MAX_SNAPSHOTS:
        return {"success": False, "error": f"there are already {MAX_SNAPSHOTS} snapshots; "
                                           "delete some with delete_mix_snapshot"}
    snapshot = capture_mix(name)
    key = store("snap", snapshot)
    index = [e for e in index if e is not existing]
    index.append({"name": name, "key": key, "saved_at": snapshot["saved_at"],
                  "tracks": len(snapshot["tracks"])})
    write_index(index)
    if existing is not None:
        delete_blob(existing["key"])
    RPR.MarkProjectDirty(0)  # so REAPER offers to save the project, snapshot included
    result = {"success": True, "name": name, "saved_at": snapshot["saved_at"],
              "tracks": len(snapshot["tracks"]), "replaced": existing is not None,
              "note": "Stored in the project: save the project to keep it."}
    skipped = skipped_params(snapshot)
    if skipped:
        result["warning"] = ("Only the on/off state was saved for plug-ins with more than "
                             f"{MAX_PARAMS_PER_FX} parameters: " + "; ".join(skipped))
    return result


@read_only
def list_mix_snapshots() -> dict:
    """List the mix snapshots saved in this project, oldest first."""
    try:
        index = read_index()
    except SnapshotError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "snapshots": [
        {"name": e["name"], "saved_at": e["saved_at"], "tracks": e["tracks"]} for e in index]}


@no_undo(destructive=True)
def delete_mix_snapshot(*, name: str) -> dict:
    """Delete a saved mix snapshot (the mix itself is not touched)."""
    try:
        index = read_index()
    except SnapshotError as e:
        return {"success": False, "error": str(e)}
    entry = find(index, name)
    if entry is None:
        return {"success": False, "error": f"No mix snapshot called {name!r}"}
    write_index([e for e in index if e is not entry])
    delete_blob(entry["key"])
    RPR.MarkProjectDirty(0)
    return {"success": True, "deleted": entry["name"]}


# Recall


# Each setting setting_changes reports -> (snapshot key, REAPER track parameter).
SETTINGS = {"volume": ("volume", "D_VOL"), "pan": ("pan", "D_PAN"),
            "muted": ("mute", "B_MUTE"), "soloed": ("solo", "I_SOLO")}


def _restore_settings(track, saved: dict, live: dict) -> list[str]:
    changed = [setting for setting, _, _ in setting_changes(live, saved)]
    for setting in changed:
        key, param = SETTINGS[setting]
        track.set_info_value(param, saved[key])
    return changed


def _restore_fx(track, saved: dict, live: dict, *, label: str) -> tuple[list[str], list[str]]:
    """Put the track's FX back as saved; (what changed, what could not be)."""
    changed, skipped = [], []
    fxs = track.fxs
    for fx, match in match_fx(saved["fx"], live["fx"]):
        if match is None:
            skipped.append(f"{label}: {fx['name']} is no longer on the track")
            continue
        if bool(fxs[match].is_enabled) != fx["enabled"]:
            fxs[match].is_enabled = fx["enabled"]
            changed.append(f"{fx['name']} {'on' if fx['enabled'] else 'bypassed'}")
        params = fx["params"]
        if params is None:
            continue
        now = live["fx"][match]["params"] or []
        if not same_layout(params, now):
            skipped.append(f"{label}: {fx['name']} has different parameters now; its "
                           "settings were not restored")
            continue
        moved = param_changes(params, now)
        for i in moved:
            RPR.TrackFX_SetParamNormalized(track.id, match, i, params[i][1])
        if moved:
            changed.append(f"{fx['name']}: {len(moved)} parameter(s)")
    return changed, skipped


@edits()
def recall_mix_snapshot(*, name: str, include_fx: bool = True) -> dict:
    """
    Put the mixer back the way a saved snapshot has it: volume, pan, mute, solo,
    and (include_fx=true) the on/off state and parameters of each FX that was
    there when it was saved. Only that: FX, sends and tracks added since stay as
    they are (bypass_fx/remove_fx, or undo, take those back), and are listed in
    the reply. Tracks are found by GUID, so moved tracks are still matched;
    tracks deleted since are reported. The snapshot is checked first and nothing
    is changed if any value in it is out of range. One undo step: undo takes the
    whole recall back.
    """
    try:
        snapshot = load(name)
    except SnapshotError as e:
        return {"success": False, "error": str(e)}
    project = get_project()
    live = capture_mix(CURRENT, names=False)
    pointers = {entry["guid"]: track
                for entry, track in zip(live["tracks"], project.tracks, strict=True)}
    if snapshot["tracks"] and not any(t["guid"] in pointers for t in snapshot["tracks"]):
        return {"success": False, "error": "None of the snapshot's tracks are in this project "
                                           "(is it from another project?); nothing was changed"}
    pointers[MASTER] = project.master_track
    live_tracks = by_guid(live)
    changed, skipped = [], []
    for saved in [*snapshot["tracks"], snapshot["master"]]:
        track = pointers.get(saved["guid"])
        if track is None:
            skipped.append(f"{saved['name']}: the track is no longer in the project")
            continue
        current = live_tracks[saved["guid"]]
        label = current["name"] or saved["name"]
        done = _restore_settings(track, saved, current)
        if include_fx:
            fx_done, fx_skipped = _restore_fx(track, saved, current, label=label)
            done += fx_done
            skipped += fx_skipped
        if done:
            changed.append({"track": label, "track_index": current["index"], "changed": done})
    saved_guids = by_guid(snapshot)
    return {"success": True, "name": snapshot["name"], "saved_at": snapshot["saved_at"],
            "changed": changed, "already_matching": not changed, "skipped": skipped,
            "tracks_not_in_snapshot": [e["name"] for e in live["tracks"]
                                       if e["guid"] not in saved_guids],
            "fx_not_in_snapshot": _added_fx(snapshot, live) if include_fx else [],
            "note": "Sends are not part of a snapshot; FX and tracks added since it was "
                    "saved were left as they are."}


def _added_fx(snapshot: dict, live: dict) -> list[str]:
    """"Track: FX" for each FX on a track now that the snapshot does not have."""
    saved = by_guid(snapshot)
    added = []
    for guid, track in by_guid(live).items():
        if guid not in saved:
            continue
        matched = {index for _, index in match_fx(saved[guid]["fx"], track["fx"])}
        added += [f"{track['name']}: {fx['name']}" for i, fx in enumerate(track["fx"])
                  if i not in matched]
    return added


# Compare


def _format(pointer: str, fx: int, param: int, *, value: float) -> str | None:
    """value as the plug-in would display it, or None when it cannot say."""
    # (retval, track, fx, param, value, text, size) per REAPER's Python binding.
    result = RPR.TrackFX_FormatParamValueNormalized(pointer, fx, param, value, "", FORMAT_BUFFER)
    return str(result[5]) if result[0] and result[5] else None


def _param_differences(a_fx: dict, b_fx: dict, live, *, detail: bool) -> list[dict]:
    """Parameters that differ between two saves of one FX, with the values as
    the plug-in displays them when the FX is still on the track (live: (pointer,
    index) or None); one summary row when there are many, unless detail."""
    a_params, b_params = a_fx["params"], b_fx["params"]
    if a_params is None or b_params is None or not same_layout(a_params, b_params):
        return []
    moved = param_changes(a_params, b_params)
    if len(moved) > MAX_PARAMS_LISTED and not detail:
        return [{"setting": f"{a_fx['name']}: parameters", "a": "as saved",
                 "b": f"{len(moved)} parameters differ (detail=true lists them)"}]
    differences = []
    for i in moved:
        p, q = a_params[i], b_params[i]
        a_value, b_value = round(p[1], 4), round(q[1], 4)
        if live is not None:
            a_value = _format(live[0], live[1], i, value=p[1]) or a_value
            b_value = _format(live[0], live[1], i, value=q[1]) or b_value
        differences.append({"setting": f"{a_fx['name']}: {p[0] or q[0]}", "a": a_value,
                            "b": b_value})
    return differences


def _fx_differences(a: dict, b: dict, live: tuple[str, str] | None, *,
                    detail: bool) -> list[dict]:
    """live: (the side, "a" or "b", that is the mix as it is now, and its track
    pointer), so values can be shown in the plug-in's own units; or None."""
    differences = []
    pairs = match_fx(a["fx"], b["fx"])
    matched = {index for _, index in pairs if index is not None}
    for position, (fx, index) in enumerate(pairs):
        if index is None:
            differences.append({"setting": f"FX {fx['name']}", "a": "present", "b": "absent"})
            continue
        other = b["fx"][index]
        if fx["enabled"] != other["enabled"]:
            differences.append({"setting": f"{fx['name']} on/off",
                                "a": "on" if fx["enabled"] else "bypassed",
                                "b": "on" if other["enabled"] else "bypassed"})
        shown = None if live is None else (live[1], index if live[0] == "b" else position)
        differences += _param_differences(fx, other, shown, detail=detail)
    for index, fx in enumerate(b["fx"]):
        if index not in matched:
            differences.append({"setting": f"FX {fx['name']}", "a": "absent", "b": "present"})
    return differences


def _track_differences(a: dict, b: dict, live: tuple[str, str] | None, *,
                       detail: bool) -> list[dict]:
    rows = [{"setting": setting, "a": x, "b": y} for setting, x, y in setting_changes(a, b)]
    return rows + _fx_differences(a, b, live, detail=detail)


def _live_pointers(project, live: dict) -> dict[str, str]:
    """Each track's GUID in the live capture -> its track pointer."""
    pointers = {entry["guid"]: track.id
                for entry, track in zip(live["tracks"], project.tracks, strict=True)}
    pointers[MASTER] = project.master_track.id
    return pointers


@read_only
def compare_mix_snapshots(*, a: str, b: str = CURRENT, detail: bool = False) -> dict:
    """
    What differs between two mix snapshots, track by track: volume, pan, mute,
    solo, FX on/off and FX parameters (shown as the plug-in displays them when
    the FX is still there). b defaults to "current", the mix as it is now, so
    compare_mix_snapshots(a="before EQ") shows what changed since that save.
    An FX with many changed parameters (a preset change) is summed up in one
    row; detail=true lists every parameter. At most 100 rows are listed.
    """
    names = (a.strip().casefold(), b.strip().casefold())
    project = get_project()
    live = capture_mix(CURRENT, names=False) if CURRENT in names else None
    try:
        first, second = (live if name == CURRENT else load(name) for name in names)
    except SnapshotError as e:
        return {"success": False, "error": str(e)}
    pointers = _live_pointers(project, live) if live is not None else {}
    side = "b" if names[1] == CURRENT else "a"
    a_tracks, b_tracks = by_guid(first), by_guid(second)
    tracks, total, listed = [], 0, 0
    for guid, a_track in a_tracks.items():
        if guid not in b_tracks:
            tracks.append({"track": a_track["name"], "only_in": "a"})
            continue
        shown = (side, pointers[guid]) if guid in pointers else None
        rows = _track_differences(a_track, b_tracks[guid], shown, detail=detail)
        total += len(rows)
        rows = rows[:max(0, MAX_DIFFERENCES - listed)]
        listed += len(rows)
        if rows:
            tracks.append({"track": b_tracks[guid]["name"] or a_track["name"],
                           "track_index": b_tracks[guid]["index"], "differences": rows})
    tracks += [{"track": t["name"], "only_in": "b"} for g, t in b_tracks.items()
               if g not in a_tracks]
    result = {"success": True, "a": first["name"], "b": second["name"],
              "identical": not tracks, "tracks": tracks}
    if total > listed:
        result["note"] = (f"{total} differences; only the first {listed} are listed. Compare "
                          "fewer changes at a time, or save a new snapshot to start from.")
    return result


TOOLS = (
    save_mix_snapshot,
    list_mix_snapshots,
    recall_mix_snapshot,
    compare_mix_snapshots,
    delete_mix_snapshot,
)
