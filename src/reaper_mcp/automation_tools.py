"""Track volume and pan automation: reading the points, clearing a time range,
changing a stretch relative to the curve already there (volume) or holding a
value over it (pan), and the track's automation mode.

Volume envelope values are stored in the envelope's own scale (fader scaling,
see GetEnvelopeScalingMode) and shown here in dB; pan envelope values run
opposite to track pan (+1 is full left there) and are shown here as track pan
(-1 left, +1 right). Automation items are not read or changed.
"""

from reaper_mcp.mixing_tools import shown_envelope
from reaper_mcp.reaper import RPR, Pointer, get_project, is_null
from reaper_mcp.undo import edits, read_only
from reaper_mcp.units import db_to_linear, linear_to_db

ENVELOPES = {"volume": "Volume", "pan": "Pan"}
AUTOMATION_MODES = {"trim_read": 0, "read": 1, "touch": 2, "write": 3, "latch": 4}
# Modes in which playback records over automation.
RECORDING_MODES = ("touch", "write", "latch")
EVALUATE_RATE = 48000.0
SAME_TIME = 1e-6  # seconds
# A change needs some ramp: points at one time would make a slope from the
# previous point instead of a step.
MIN_RAMP = 0.001  # seconds
# REAPER's volume envelopes go up to +6 dB by default (Preferences > Editing
# behavior > Envelope display), so a point above that may be clipped there.
VOLUME_ENVELOPE_MAX_DB = 6.0


class Curve:
    """One track envelope and the conversions between its raw values and the
    units the tools speak (dB for volume, track pan for pan)."""

    def __init__(self, pointer: Pointer, kind: str) -> None:
        self.pointer = pointer
        self.kind = kind
        self.mode = RPR.GetEnvelopeScalingMode(pointer)

    def to_user(self, raw: float) -> float:
        if self.kind == "pan":
            return -raw
        return linear_to_db(RPR.ScaleFromEnvelopeMode(self.mode, raw))

    def to_raw(self, value: float) -> float:
        if self.kind == "pan":
            return -max(-1.0, min(1.0, value))
        return RPR.ScaleToEnvelopeMode(self.mode, db_to_linear(value))

    def evaluate(self, time: float) -> float:
        """The curve's value at time, in the tool's units."""
        # (retval, env, time, rate, samples, value, dVdS, ddVdS, dddVdS).
        result = RPR.Envelope_Evaluate(self.pointer, time, EVALUATE_RATE, 1, 0.0, 0.0, 0.0, 0.0)
        return self.to_user(result[5])

    def points(self) -> list[dict]:
        found = []
        for index in range(RPR.CountEnvelopePoints(self.pointer)):
            # (retval, env, index, time, value, shape, tension, selected).
            point = RPR.GetEnvelopePoint(self.pointer, index, 0.0, 0.0, 0, 0.0, False)
            found.append({"index": index, "time": point[3], "value": self.to_user(point[4]),
                          "shape": point[5], "tension": point[6]})
        return found


def reported(points: list[dict]) -> list[dict]:
    """points as the tools report them: values rounded, no internal index."""
    return [{"time": p["time"], "value": round(p["value"], 3), "shape": p["shape"]}
            for p in points]


def _kind(envelope: str) -> str:
    kind = envelope.strip().lower()
    if kind not in ENVELOPES:
        raise ValueError(f"envelope must be one of {list(ENVELOPES)}, not '{envelope}'")
    return kind


def _existing(track, kind: str) -> Curve | None:
    pointer = RPR.GetTrackEnvelopeByName(track.id, ENVELOPES[kind])
    return None if is_null(pointer) else Curve(pointer, kind)


def _range_error(start: float, end: float) -> dict | None:
    if start < 0 or end <= start:
        return {"success": False, "error": "need 0 <= start < end (seconds)"}
    return None


def _unit(kind: str) -> str:
    return "dB" if kind == "volume" else "pan (-1 left .. +1 right)"


@read_only
def get_envelope_points(
    *, track_index: int, envelope: str = "volume",
    start: float | None = None, end: float | None = None,
) -> dict:
    """
    List a track's volume or pan automation points: time (seconds), value (dB for
    volume; -1 left .. +1 right for pan) and shape. start/end: only points in that
    time range. Also says the track's automation mode.
    """
    track = get_project().tracks[track_index]
    try:
        kind = _kind(envelope)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    curve = _existing(track, kind)
    points = curve.points() if curve else []
    points = [p for p in points if (start is None or p["time"] >= start)
              and (end is None or p["time"] <= end)]
    return {"success": True, "track_index": track_index, "envelope": kind,
            "unit": _unit(kind), "exists": curve is not None,
            "automation_mode": _mode_name(track), "count": len(points),
            "points": reported(points)}


def _delete_points(curve: Curve, start: float, end: float) -> int:
    """Delete the points from start to end (both included), last first so the
    indices of the ones still to delete do not move."""
    doomed = [p["index"] for p in curve.points()
              if start - SAME_TIME <= p["time"] <= end + SAME_TIME]
    for index in reversed(doomed):
        RPR.DeleteEnvelopePointEx(curve.pointer, -1, index)
    return len(doomed)


LINEAR = (0, 0.0)  # (shape, tension) of a straight line to the next point


def governing_shape(points: list[dict], time: float) -> tuple[int, float]:
    """(shape, tension) of the point that shapes the curve at time (the last
    one at or before it); linear before the first point."""
    before = [p for p in points if p["time"] <= time + SAME_TIME]
    return (before[-1]["shape"], before[-1]["tension"]) if before else LINEAR


def _insert(curve: Curve, time: float, value: float, *,
            shape: tuple[int, float] = LINEAR) -> None:
    """Add a point; shape is how the curve goes from it to the next point. A
    point that splits an existing stretch takes that stretch's shape, so the
    curve after it keeps its square step, slow start, bezier and so on."""
    RPR.InsertEnvelopePoint(curve.pointer, time, curve.to_raw(value), shape[0], shape[1],
                            False, True)


@edits(destructive=True)
def clear_automation(
    *, track_index: int, start: float, end: float, envelope: str = "volume",
) -> dict:
    """
    Delete a track's volume or pan automation points between start and end (seconds).
    Points are put at start and end holding the values the curve had there, so
    the automation before and after the range does not change; the range then
    goes in a straight line between those two values.
    """
    track = get_project().tracks[track_index]
    try:
        kind = _kind(envelope)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    error = _range_error(start, end)
    curve = _existing(track, kind)
    if error or curve is None:
        return error or {"success": False, "error": f"Track {track_index} has no {kind} "
                                                    "automation; nothing was changed"}
    before = curve.points()
    edges = [(start, curve.evaluate(start), LINEAR),
             (end, curve.evaluate(end), governing_shape(before, end))]
    deleted = _delete_points(curve, start, end)
    if deleted:
        for time, value, shape in edges:
            _insert(curve, time, value, shape=shape)
    RPR.Envelope_SortPoints(curve.pointer)
    return {"success": True, "track_index": track_index, "envelope": kind,
            "deleted_points": deleted, "points": reported(curve.points())}


def _ramp_weight(time: float, *, start: float, end: float, ramp: float) -> float:
    """How much of the change applies at time: all of it from start to end,
    fading linearly to none over ramp seconds before start and after end."""
    if start <= time <= end:
        return 1.0
    distance = start - time if time < start else time - end
    return max(0.0, round(1.0 - distance / ramp, 9)) if ramp > 0 else 0.0


def _shift(curve: Curve, change: float, *, start: float, end: float, ramp: float) -> list[float]:
    """Shift every point inside the range (and its ramps) by change, weighted;
    returns the new values."""
    values = []
    for point in curve.points():
        weight = _ramp_weight(point["time"], start=start, end=end, ramp=ramp)
        if weight <= 0:
            continue
        value = point["value"] + change * weight
        values.append(value)
        RPR.SetEnvelopePoint(curve.pointer, point["index"], point["time"], curve.to_raw(value),
                             point["shape"], point["tension"], False, True)
    return values


def _change_range(curve: Curve, *, start: float, end: float, change: float,
                  ramp: float) -> list[float]:
    """Apply change over start..end relative to the curve, with ramps; points
    are added where the curve needs them to bend. Returns the new values."""
    ramp = max(ramp, MIN_RAMP)
    ramp_start = max(0.0, start - ramp)
    # Each new point's time and how much of the change it gets: none at the
    # outer ends of the ramps, all of it at start and end.
    # The ramps are straight lines; start and end + ramp split stretches of
    # the existing curve, so they take its shape there.
    points = curve.points()
    anchors = {start: (1.0, governing_shape(points, start)), end: (1.0, LINEAR),
               end + ramp: (0.0, governing_shape(points, end + ramp))}
    if ramp_start < start:
        anchors[ramp_start] = (0.0, LINEAR)
    existing = [p["time"] for p in points]
    new = [(t, curve.evaluate(t) + change * weight, shape)
           for t, (weight, shape) in sorted(anchors.items())
           if not any(abs(t - e) <= SAME_TIME for e in existing)]
    values = _shift(curve, change, start=start, end=end, ramp=ramp)
    for time, value, shape in new:
        values.append(value)
        _insert(curve, time, value, shape=shape)
    RPR.Envelope_SortPoints(curve.pointer)
    return values


def _hold_range(curve: Curve, *, start: float, end: float, value: float,
                ramp: float) -> list[float]:
    """Make the curve hold value from start to end, ramping in over ramp
    seconds before start and back to the curve as it was over ramp seconds
    after end; the points in between are replaced. Returns the new values."""
    ramp = max(ramp, MIN_RAMP)
    ramp_start = max(0.0, start - ramp)
    after = end + ramp
    points = curve.points()
    new = [(start, value, LINEAR), (end, value, LINEAR),
           (after, curve.evaluate(after), governing_shape(points, after))]
    if ramp_start < start:
        new.insert(0, (ramp_start, curve.evaluate(ramp_start), LINEAR))
    _delete_points(curve, new[0][0], after)
    for time, held, shape in new:
        _insert(curve, time, held, shape=shape)
    RPR.Envelope_SortPoints(curve.pointer)
    return [value]


def _over_range(*, track_index: int, kind: str, start: float, end: float, ramp_seconds: float,
                apply) -> dict:
    """Run apply(curve) -> new values on the track's kind envelope (created if
    needed) after checking the range, and report the points."""
    error = _range_error(start, end)
    if error:
        return error
    if ramp_seconds < 0:
        return {"success": False, "error": "ramp_seconds must be 0 or more"}
    project = get_project()
    track = project.tracks[track_index]
    curve = Curve(shown_envelope(project, track, ENVELOPES[kind]), kind)
    values = apply(curve)
    result = {"success": True, "track_index": track_index, "envelope": kind,
              "start": start, "end": end, "points": reported(curve.points())}
    if kind == "volume" and any(v > VOLUME_ENVELOPE_MAX_DB for v in values):
        result["warning"] = (f"Some points are above +{VOLUME_ENVELOPE_MAX_DB:g} dB, where "
                             "REAPER's volume envelope may stop; raise the track fader instead")
    if kind == "pan" and abs(track.get_info_value("D_PAN")) > SAME_TIME:
        result["note"] = ("The track's pan knob is not centered, and REAPER combines it "
                          "with the pan automation, so the heard pan differs; center the "
                          "knob (set_track_pan 0) to hear exactly these values")
    return result


@edits()
def set_volume_over_range(
    *, track_index: int, start: float, end: float, change_db: float,
    ramp_seconds: float = 0.05,
) -> dict:
    """
    Raise or lower a track's volume between start and end (seconds) by change_db,
    relative to the volume automation already there (a fade or ride inside the
    range is kept, just shifted). ramp_seconds: how long the change takes to come
    in before start and to go away after end, so it does not click (default 0.05;
    0 means as fast as possible, 1 ms).
    Creates the volume envelope if the track has none (0 dB elsewhere).
    Example: change_db=-3 makes a loud chorus 3 dB quieter.
    (add_volume_automation instead adds one point at an absolute level.)
    """
    return _over_range(
        track_index=track_index, kind="volume", start=start, end=end,
        ramp_seconds=ramp_seconds,
        apply=lambda curve: _change_range(curve, start=start, end=end, change=change_db,
                                          ramp=ramp_seconds))


@edits()
def set_pan_over_range(
    *, track_index: int, start: float, end: float, pan: float,
    ramp_seconds: float = 0.05,
) -> dict:
    """
    Pan a track to pan (-1 full left, 0 center, +1 full right) from start to end
    (seconds), e.g. pan=-1 puts a guitar hard left in the solo. Any pan
    automation inside the range is replaced; outside it is kept. ramp_seconds:
    how long the move takes before start and back after end (default 0.05).
    Creates the pan envelope if needed (center elsewhere).
    """
    if not -1.0 <= pan <= 1.0:
        return {"success": False, "error": "pan must be from -1 (left) to 1 (right)"}
    return _over_range(
        track_index=track_index, kind="pan", start=start, end=end, ramp_seconds=ramp_seconds,
        apply=lambda curve: _hold_range(curve, start=start, end=end, value=pan,
                                        ramp=ramp_seconds))


def _mode_name(track) -> str:
    mode = int(track.get_info_value("I_AUTOMODE"))
    return next((name for name, value in AUTOMATION_MODES.items() if value == mode), str(mode))


@edits(idempotent=True)
def set_automation_mode(*, track_index: int, mode: str) -> dict:
    """
    Set a track's automation mode: trim_read (default: automation plays, the fader
    adds on top) or read (automation plays and moves the fader). Use these to play
    back automation written with these tools.
    touch, latch and write make the next playback record fader moves over the
    automation (write overwrites all of it); only set them when the user asks for
    that mode by name.
    """
    track = get_project().tracks[track_index]
    wanted = mode.strip().lower()
    if wanted not in AUTOMATION_MODES:
        return {"success": False, "error": f"mode must be one of {list(AUTOMATION_MODES)}"}
    RPR.SetMediaTrackInfo_Value(track.id, "I_AUTOMODE", AUTOMATION_MODES[wanted])
    result = {"success": True, "track_index": track_index, "mode": _mode_name(track)}
    if wanted in RECORDING_MODES:
        result["warning"] = (f"In {wanted} mode the next playback records over this track's "
                             "automation; set it back to trim_read or read afterwards")
    return result


TOOLS = (
    get_envelope_points,
    clear_automation,
    set_volume_over_range,
    set_pan_over_range,
    set_automation_mode,
)
