"""Measuring one track, and checking a mix change against what was expected.

measure_track renders the track soloed to a temp file (render_tools), so the
numbers are what the track sounds like after its FX, fader and pan, through
the master track's volume. The master's FX are bypassed for the render unless
asked for: a limiter there would hide a fader move. Solo, mute, master FX,
the render settings and the time selection are put back afterwards.

compare_measurements is the honest half of "make the vocal 3 dB louder":
measure before, change, measure after, then compare; it says verified,
not_verified or cannot_verify, never guesses.
"""

import math
import os
from collections.abc import Iterator
from contextlib import contextmanager

from reaper_mcp.guids import resolve_track_index, track_guid
from reaper_mcp.reaper import get_project, lost_call, owe_on_reconnect
from reaper_mcp.render_tools import SOLO_IN_PLACE, render_to_temp_file, transport_error
from reaper_mcp.undo import read_only

# Integrated loudness needs at least one 400 ms gating block.
MIN_LOUDNESS_SECONDS = 0.4
CLIP_LINEAR = 0.9999
SILENCE_LINEAR = 1e-6  # -120 dBFS: below this a render counts as silent
METRICS = ("integrated_lufs", "rms_db", "sample_peak_dbfs")
DEFAULT_TOLERANCE_DB = 1.0
# The reply keys that say what was measured; compare_measurements needs them.
IDENTITY = (("track_guid", "tracks"), ("start", "start times"), ("end", "end times"),
            ("include_master_fx", "master FX settings"))
WHAT_IS_MEASURED = (
    "The track as heard when soloed: after its FX, volume and pan, plus what it sends "
    "to (e.g. a reverb track), through the master track's volume{master}.")


def _db(linear: float) -> float | None:
    return round(20 * math.log10(linear), 2) if linear > SILENCE_LINEAR else None


def measure_file(path: str) -> dict:
    """Loudness (LUFS, ITU-R BS.1770), RMS and sample peak of an audio file.
    Levels are None for silence; integrated_lufs also for less than 0.4 s, or
    when everything is below the loudness gate."""
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf

    data, rate = sf.read(path, always_2d=True)
    seconds = len(data) / rate
    peak = float(np.max(np.abs(data))) if len(data) else 0.0
    rms = float(np.sqrt(np.mean(data ** 2))) if len(data) else 0.0
    lufs = None
    if seconds >= MIN_LOUDNESS_SECONDS and peak > SILENCE_LINEAR:
        loudness = pyln.Meter(rate).integrated_loudness(data)
        lufs = round(float(loudness), 2) if math.isfinite(loudness) else None
    peak_db, rms_db = _db(peak), _db(rms)
    return {
        "integrated_lufs": lufs,
        "rms_db": rms_db,
        "sample_peak_dbfs": peak_db,
        "crest_factor_db": round(peak_db - rms_db, 2) if peak_db is not None
                           and rms_db is not None else None,
        "clipping": peak >= CLIP_LINEAR,
        "silent": peak <= SILENCE_LINEAR,
        "seconds": round(seconds, 3),
    }


def _restore(tracks, solos: list[float], *, track, mute: float) -> None:
    for each, solo in zip(tracks, solos, strict=True):
        each.set_info_value("I_SOLO", solo)
    track.set_info_value("B_MUTE", mute)


@contextmanager
def soloed_alone(tracks, index: int) -> Iterator[bool]:
    """tracks[index] soloed (and unmuted) with every other solo off for the body;
    every solo and the track's mute put back afterwards, or owed to the next
    tool call when a call to REAPER was lost. Yields whether it was muted."""
    track = tracks[index]
    solos = [each.get_info_value("I_SOLO") for each in tracks]
    mute = track.get_info_value("B_MUTE")
    try:
        for i, each in enumerate(tracks):
            each.set_info_value("I_SOLO", SOLO_IN_PLACE if i == index else 0)
        if mute:
            track.set_info_value("B_MUTE", 0)
        yield bool(mute)
    finally:
        if lost_call() is not None:
            owe_on_reconnect(lambda: _restore(tracks, solos, track=track, mute=mute))
        else:
            _restore(tracks, solos, track=track, mute=mute)


@contextmanager
def master_fx_bypassed(project, *, bypass: bool) -> Iterator[list[str]]:
    """The master track's FX chain bypassed for the body when bypass (its
    I_FXEN, the chain's bypass button), then put back as it was (or owed to
    the next tool call when a call to REAPER was lost). Yields the names of
    the master FX that are on. Not each FX's own switch: TrackFX_SetEnabled
    records an "FX bypass toggle" undo step and marks the project dirty
    (seen on 7.69); I_FXEN does neither."""
    master = project.master_track
    chain_on = master.get_info_value("I_FXEN")
    active = [fx.name for fx in master.fxs if fx.is_enabled] if chain_on else []
    if not bypass or not active:
        yield active
        return
    try:
        master.set_info_value("I_FXEN", 0)
        yield active
    finally:
        if lost_call() is not None:
            owe_on_reconnect(lambda: master.set_info_value("I_FXEN", chain_on))
        else:
            master.set_info_value("I_FXEN", chain_on)


def _span(start: float | None, end: float | None) -> tuple[float, float] | None:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError("give both start and end (seconds), or neither for the whole project")
    if not 0 <= start < end:
        raise ValueError(f"need 0 <= start < end; got start={start}, end={end}")
    return (start, end)


def _within(span: tuple[float, float] | None,
            length: float) -> tuple[tuple[float, float] | None, str | None]:
    """span cut to the end of the project, with a note when it was cut."""
    if span is None:
        return None, None
    start, end = span
    if start >= length:
        raise ValueError(f"start={start} is past the end of the project ({length:.2f} s); "
                         "nothing plays there")
    if end > length:
        return (start, length), (f"end={end} is past the end of the project; measured up to "
                                 f"{length:.2f} s")
    return span, None


@read_only
def measure_track(*, track_index: int | str, start: float | None = None,
                  end: float | None = None, include_master_fx: bool = False) -> dict:
    """
    Measure how loud one track is, after its FX and fader: integrated loudness
    (LUFS), RMS level and sample peak (dBFS), by rendering it soloed to a temp
    file. Whole project by default, or start/end in seconds (a 20-30 s stretch,
    e.g. the chorus, is much quicker than a whole song). The solo and mute
    buttons are left exactly as they were. Anything the track sends to (a reverb
    track) and the master volume are included. The master track's FX (a limiter)
    are bypassed for the measurement and switched back on afterwards, so a fader
    move shows up 1:1; include_master_fx=true measures through them.
    track_index: the track's index, or its GUID from list_tracks.
    Keep the whole reply: give it to compare_measurements with a later one to
    check a change ("is the vocal 3 dB louder now?"). Refused while REAPER is
    playing or recording.
    """
    try:
        span = _span(start, end)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    error = transport_error()
    if error is not None:
        return {"success": False, "error": error}
    project = get_project()
    try:
        span, clipped = _within(span, project.length)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    index, track = resolve_track_index(project, track_index)
    with (soloed_alone(project.tracks, index) as was_muted,
          master_fx_bypassed(project, bypass=not include_master_fx) as master_fx):
        tmp = render_to_temp_file(span=span)
    try:
        measurement = measure_file(tmp)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    notes = [clipped] if clipped else []
    always = [t.name or f"track {i}" for i, t in enumerate(project.tracks)
              if i != index and t.get_info_value("B_SOLO_DEFEAT") and not t.get_info_value("B_MUTE")]
    if always:
        notes.append("These tracks ignore solo (solo defeat), so they are heard in every "
                     f"measurement, this one too: {always}")
    return {
        "success": True,
        "track_index": index,
        "track_guid": track_guid(track),
        "track_name": track.name,
        "start": span[0] if span else None,
        "end": span[1] if span else None,
        "include_master_fx": include_master_fx,
        "master_fx": master_fx,
        "measurement": measurement,
        "what_is_measured": WHAT_IS_MEASURED.format(
            master=_master_text(master_fx, included=include_master_fx)),
        "notes": notes + _notes(measurement, master_fx=master_fx,
                                include_master_fx=include_master_fx, was_muted=was_muted),
    }


def _master_text(master_fx: list[str], *, included: bool) -> str:
    if not master_fx:
        return ""
    names = ", ".join(master_fx)
    return f" and its FX ({names})" if included else f"; its FX ({names}) were bypassed for this"


def _notes(measurement: dict, *, master_fx: list[str], include_master_fx: bool,
           was_muted: bool) -> list[str]:
    notes = []
    if master_fx and include_master_fx:
        notes.append("The master track's FX were included: a limiter or compressor there "
                     "can hide a fader change, so it may not show up 1:1.")
    if was_muted:
        notes.append("The track is muted; it was measured as if unmuted (and is still muted).")
    if measurement["silent"]:
        notes.append("The track is silent here: no items in this range, or its volume is "
                     "all the way down.")
    elif measurement["integrated_lufs"] is None:
        notes.append("Too short or too quiet for a loudness (LUFS) reading; compare rms_db "
                     "instead.")
    return notes


def _levels(measured: dict) -> dict:
    """The measurement inside a measure_track reply, or the dict itself."""
    inner = measured.get("measurement", measured)
    return inner if isinstance(inner, dict) else {}


def _not_comparable(before: dict, after: dict) -> str | None:
    """Why two measure_track replies cannot be compared, or None."""
    for key, what in IDENTITY:
        if key not in before or key not in after:
            return (f"{key!r} is missing: pass the whole measure_track replies, not only "
                    "their measurement, so it is certain they measure the same thing")
        if before[key] != after[key]:
            return (f"the two measurements are of different {what} ({before[key]} vs "
                    f"{after[key]}); measure the same track and range before and after")
    return None


def _change(before: dict, after: dict, metric: str) -> float | None:
    a, b = _levels(before).get(metric), _levels(after).get(metric)
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    return round(b - a, 2)


def _verdict(change: float | None, expected: float | None, tolerance: float) -> str | None:
    if expected is None:
        return None
    if change is None:
        return "cannot_verify"
    return "verified" if abs(change - expected) <= tolerance else "not_verified"


def _message(metric: str, change: float | None, *, expected: float | None, tolerance: float,
             verdict: str | None) -> str:
    if change is None:
        return (f"No {metric} in one of the measurements (silent, or too short for LUFS): "
                "nothing to compare. Try metric=\"rms_db\".")
    moved = "louder" if change > 0 else "quieter" if change < 0 else "no louder or quieter"
    text = f"{metric} changed by {change:+.2f} dB ({moved})"
    if expected is None:
        return text + "."
    return (f"{text}; expected {expected:+.2f} dB, give or take {tolerance:g} dB: "
            f"{verdict.replace('_', ' ')}.")


@read_only
def compare_measurements(*, before: dict, after: dict, expected_change_db: float | None = None,
                         metric: str = "integrated_lufs",
                         tolerance_db: float = DEFAULT_TOLERANCE_DB) -> dict:
    """
    Compare two measure_track replies (before and after a mix change) and say
    honestly whether the change did what was intended. before/after: the whole
    measure_track replies, unchanged. expected_change_db: e.g.
    3 for "3 dB louder", -2 for "2 dB quieter"; leave it out to just report the
    changes. metric: integrated_lufs (default; how loud it sounds), rms_db or
    sample_peak_dbfs. verdict: "verified" (within tolerance_db of the expected
    change), "not_verified", or "cannot_verify" (different tracks or ranges, or no
    reading, e.g. silence). Changes nothing in REAPER.
    """
    if metric not in METRICS:
        return {"success": False, "error": f"metric must be one of {list(METRICS)}, not {metric!r}"}
    if not tolerance_db > 0:
        return {"success": False, "error": f"tolerance_db must be more than 0, not {tolerance_db}"}
    problem = _not_comparable(before, after)
    if problem is not None:
        return {"success": True, "verdict": "cannot_verify" if expected_change_db is not None
                else None, "verified": None, "comparable": False, "message": problem,
                "changes_db": None}
    changes = {name: _change(before, after, name) for name in METRICS}
    change = changes[metric]
    verdict = _verdict(change, expected_change_db, tolerance_db)
    result = {
        "success": True,
        "metric": metric,
        "change_db": change,
        "expected_change_db": expected_change_db,
        "tolerance_db": tolerance_db,
        "verdict": verdict,
        "verified": {"verified": True, "not_verified": False}.get(verdict),
        "comparable": True,
        "message": _message(metric, change, expected=expected_change_db,
                            tolerance=tolerance_db, verdict=verdict),
        "changes_db": changes,
    }
    if verdict == "not_verified" and after.get("include_master_fx") and after.get("master_fx"):
        result["hint"] = ("These were measured through the master track's FX "
                          f"({', '.join(after['master_fx'])}); a limiter there hides fader "
                          "changes. Measure both again with include_master_fx=false before "
                          "changing anything else.")
    if _levels(after).get("clipping"):
        result["warning"] = "The after measurement clips (reaches 0 dBFS): turn something down."
    return result


TOOLS = (
    measure_track,
    compare_measurements,
)
