"""Automation tools against the fake REAPER, whose volume envelope stores
1000 * linear gain (its stand-in fader curve) and whose pan envelope runs
opposite to track pan, as REAPER's does."""

import pytest

from reaper_mcp.units import db_to_linear, linear_to_db
from tests.fake_reaper.envelopes import SQUARE, Point, shape_of
from tests.helpers import assert_no_track


def _volume(db: float) -> float:
    return 1000 * db_to_linear(db)


def _db(points):
    return [(time, round(linear_to_db(raw / 1000), 3)) for time, raw in points]


def _evaluate_db(reaper, track, time: float) -> float:
    envelope = reaper.GetTrackEnvelopeByName(track.pointer, "Volume")
    return linear_to_db(reaper.Envelope_Evaluate(envelope, time, 48000.0, 1)[5] / 1000)


@pytest.fixture
def vox(reaper):
    reaper.add_track("Guitar")
    return reaper.add_track("Vox")


def _with_volume(reaper, track, points):
    reaper.show_envelope(track, "Volume")
    reaper.envelopes[(track.pointer, "Volume")] = [(t, _volume(db)) for t, db in points]
    return reaper.envelopes[(track.pointer, "Volume")]


# get_envelope_points


def test_get_envelope_points_in_db(reaper, call, vox):
    _with_volume(reaper, vox, [(0.0, 0.0), (2.0, -6.0), (4.0, -12.0)])
    result = call("get_envelope_points", track_index=1)
    assert result == {
        "success": True, "track_index": 1, "envelope": "volume", "unit": "dB", "exists": True,
        "automation_mode": "trim_read", "count": 3,
        "points": [{"time": 0.0, "value": 0.0, "shape": 0},
                   {"time": 2.0, "value": -6.0, "shape": 0},
                   {"time": 4.0, "value": -12.0, "shape": 0}],
    }


def test_get_envelope_points_in_a_range_and_pan(reaper, call, vox):
    reaper.show_envelope(vox, "Pan")
    reaper.envelopes[(vox.pointer, "Pan")] = [(1.0, 0.5), (3.0, -1.0)]
    result = call("get_envelope_points", track_index=1, envelope="Pan", start=2.0)
    assert result["points"] == [{"time": 3.0, "value": 1.0, "shape": 0}]  # +1 = right


def test_get_envelope_points_without_an_envelope_creates_none(reaper, call, vox):
    result = call("get_envelope_points", track_index=1)
    assert (result["exists"], result["points"]) == (False, [])
    assert reaper.envelopes == {}
    assert reaper.commands == []


def test_get_envelope_points_unknown_envelope(reaper, call, vox):
    result = call("get_envelope_points", track_index=1, envelope="width")
    assert result == {"success": False, "error": "envelope must be one of ['volume', 'pan'], "
                                                 "not 'width'"}


# clear_automation


def test_clear_automation_keeps_the_curve_outside(reaper, call, vox):
    points = _with_volume(reaper, vox, [(0.0, 0.0), (2.0, -6.0), (3.0, -20.0), (6.0, -6.0)])
    result = call("clear_automation", track_index=1, start=1.0, end=4.0)
    assert result["success"] is True
    assert result["deleted_points"] == 2
    # Before 1 s and after 4 s the curve is as it was (linear between the old points).
    before, after = _db(points)[1], _db(points)[2]
    assert before[0] == 1.0 and before[1] == pytest.approx(linear_to_db((1 + db_to_linear(-6)) / 2),
                                                          abs=1e-3)
    assert after[0] == 4.0
    assert after[1] == pytest.approx(linear_to_db((2 * db_to_linear(-20) + db_to_linear(-6)) / 3),
                                     abs=1e-3)
    assert [t for t, _ in points] == [0.0, 1.0, 4.0, 6.0]


def test_clear_automation_keeps_a_square_step_after_the_range(reaper, call, vox):
    reaper.show_envelope(vox, "Volume")
    reaper.envelopes[(vox.pointer, "Volume")] = [
        Point(0.0, _volume(0.0)), Point(2.0, _volume(-6.0), SQUARE),
        Point(8.0, _volume(0.0))]
    call("clear_automation", track_index=1, start=1.0, end=3.0)
    points = reaper.envelopes[(vox.pointer, "Volume")]
    # From 3 s the curve still holds -6 dB until it steps up at 8 s.
    assert [shape_of(p) for p in points] == [0, 0, SQUARE, 0]
    assert _evaluate_db(reaper, vox, 7.9) == pytest.approx(-6.0)


def test_clear_automation_needs_an_envelope_and_a_range(reaper, call, vox):
    missing = call("clear_automation", track_index=1, start=0.0, end=1.0)
    assert missing == {"success": False, "error": "Track 1 has no volume automation; "
                                                  "nothing was changed"}
    _with_volume(reaper, vox, [(0.0, 0.0)])
    backwards = call("clear_automation", track_index=1, start=2.0, end=1.0)
    assert backwards == {"success": False, "error": "need 0 <= start < end (seconds)"}
    # Nothing changed, so REAPER records no step (seen on 7.69), not even "(failed)".
    assert reaper.undo_steps == []


def test_clear_automation_bad_track(reaper, call, vox):
    assert_no_track(call("clear_automation", track_index=5, start=0.0, end=1.0))


# set_volume_over_range


def test_set_volume_over_range_on_a_new_envelope(reaper, call, vox):
    result = call("set_volume_over_range", track_index=1, start=10.0, end=20.0, change_db=-3.0,
                  ramp_seconds=0.5)
    assert result["success"] is True
    assert reaper.commands == [40406]  # the volume envelope was shown (created)
    assert _db(reaper.envelopes[(vox.pointer, "Volume")]) == [
        (9.5, 0.0), (10.0, -3.0), (20.0, -3.0), (20.5, 0.0)]


def curve_db(t: float) -> float:
    """The fake's fade below (linear in raw value) at t, in dB."""
    if t <= 6:
        return linear_to_db(1 + (db_to_linear(-6) - 1) * t / 6)
    return linear_to_db(db_to_linear(-6) + (db_to_linear(-12) - db_to_linear(-6)) * (t - 6) / 6)


def test_set_volume_over_range_is_relative_to_the_curve(reaper, call, vox):
    # A fade from 0 dB at 0 s to -12 dB at 12 s, ridden over 4..8 s.
    points = _with_volume(reaper, vox, [(0.0, 0.0), (6.0, -6.0), (12.0, -12.0)])
    call("set_volume_over_range", track_index=1, start=4.0, end=8.0, change_db=-3.0,
         ramp_seconds=1.0)
    values = dict(_db(points))
    assert list(values) == [0.0, 3.0, 4.0, 6.0, 8.0, 9.0, 12.0]
    assert values[0.0] == 0.0 and values[12.0] == -12.0
    assert values[6.0] == pytest.approx(-9.0)  # the existing point moved by -3 dB
    assert values[4.0] == pytest.approx(curve_db(4.0) - 3, abs=1e-3)
    assert values[8.0] == pytest.approx(curve_db(8.0) - 3, abs=1e-3)
    assert values[3.0] == pytest.approx(curve_db(3.0), abs=1e-3)  # the ramp starts unchanged
    assert values[9.0] == pytest.approx(curve_db(9.0), abs=1e-3)


def test_set_volume_over_range_keeps_a_square_step_after_the_range(reaper, call, vox):
    # -10 dB held from 0 s, stepping up to 0 dB at 8 s.
    reaper.show_envelope(vox, "Volume")
    reaper.envelopes[(vox.pointer, "Volume")] = [
        Point(0.0, _volume(-10.0), SQUARE), Point(8.0, _volume(0.0))]
    call("set_volume_over_range", track_index=1, start=2.0, end=4.0, change_db=-3.0,
         ramp_seconds=0.5)
    assert _evaluate_db(reaper, vox, 3.0) == pytest.approx(-13.0)
    assert _evaluate_db(reaper, vox, 6.0) == pytest.approx(-10.0)  # not a slide to 0 dB
    assert _evaluate_db(reaper, vox, 7.99) == pytest.approx(-10.0)
    assert _evaluate_db(reaper, vox, 4.75) == pytest.approx(-10.0)  # ramped back
    assert _evaluate_db(reaper, vox, 1.75) == pytest.approx(
        linear_to_db((db_to_linear(-10) + db_to_linear(-13)) / 2), abs=1e-3)  # ramping in


def test_set_volume_over_range_scales_points_inside_a_ramp(reaper, call, vox):
    points = _with_volume(reaper, vox, [(0.0, 0.0), (9.0, 0.0), (20.0, 0.0)])
    call("set_volume_over_range", track_index=1, start=10.0, end=12.0, change_db=-6.0,
         ramp_seconds=2.0)
    assert dict(_db(points))[9.0] == pytest.approx(-3.0)  # halfway into the ramp


def test_set_volume_over_range_without_ramp_still_steps(reaper, call, vox):
    call("set_volume_over_range", track_index=1, start=1.0, end=2.0, change_db=6.0,
         ramp_seconds=0.0)
    times = [t for t, _ in reaper.envelopes[(vox.pointer, "Volume")]]
    assert times == pytest.approx([0.999, 1.0, 2.0, 2.001])


def test_set_volume_over_range_at_the_start_has_no_ramp_before(reaper, call, vox):
    call("set_volume_over_range", track_index=1, start=0.0, end=2.0, change_db=-3.0)
    assert _db(reaper.envelopes[(vox.pointer, "Volume")]) == [
        (0.0, -3.0), (2.0, -3.0), (2.05, 0.0)]


def test_set_volume_over_range_notes_points_above_the_envelope_lane(reaper, call, vox):
    """REAPER plays them as set (+20 dB heard on 7.69): a note, not a warning."""
    result = call("set_volume_over_range", track_index=1, start=1.0, end=2.0, change_db=9.0)
    assert "above +6 dB" in result["note"] and "plays them as set" in result["note"]
    assert "warning" not in result


def test_set_volume_over_range_refuses_bad_ranges(reaper, call, vox):
    result = call("set_volume_over_range", track_index=1, start=3.0, end=3.0, change_db=-3.0)
    assert result == {"success": False, "error": "need 0 <= start < end (seconds)"}
    result = call("set_volume_over_range", track_index=1, start=1.0, end=3.0, change_db=-3.0,
                  ramp_seconds=-1.0)
    assert result["success"] is False
    assert reaper.envelopes == {}


def test_set_volume_over_range_is_one_undo_step(reaper, call, vox):
    _with_volume(reaper, vox, [(0.0, 0.0)])
    call("set_volume_over_range", track_index=1, start=1.0, end=2.0, change_db=-3.0)
    assert reaper.undo_steps[-1][0].startswith("MCP: Set volume over range")
    call("undo")
    assert reaper.envelopes[(vox.pointer, "Volume")] == [(0.0, 1000.0)]


# set_pan_over_range


def test_set_pan_over_range_holds_an_absolute_pan(reaper, call, vox):
    reaper.show_envelope(vox, "Pan")
    reaper.envelopes[(vox.pointer, "Pan")] = [(0.0, -0.5), (3.0, 0.5)]  # half right to half left
    result = call("set_pan_over_range", track_index=1, start=2.0, end=4.0, pan=-1.0,
                  ramp_seconds=1.0)
    assert result["success"] is True and "note" not in result
    # The point at 3 s inside the range is replaced; the curve is kept outside.
    assert reaper.envelopes[(vox.pointer, "Pan")] == [
        (0.0, -0.5), (1.0, pytest.approx(-0.5 + 1 / 3)), (2.0, 1.0), (4.0, 1.0), (5.0, 0.5)]
    assert [p["value"] for p in result["points"]][2:] == [-1.0, -1.0, -0.5]


def test_set_pan_over_range_creates_the_pan_envelope(reaper, call, vox):
    call("set_pan_over_range", track_index=1, start=2.0, end=4.0, pan=0.5)
    assert reaper.commands == [40407]
    assert reaper.envelopes[(vox.pointer, "Pan")] == [
        (1.95, 0.0), (2.0, -0.5), (4.0, -0.5), (4.05, 0.0)]


def test_set_pan_over_range_refuses_pan_past_the_edges(reaper, call, vox):
    result = call("set_pan_over_range", track_index=1, start=2.0, end=4.0, pan=1.5)
    assert result == {"success": False, "error": "pan must be from -1 (left) to 1 (right)"}
    assert reaper.envelopes == {}


def test_set_pan_over_range_notes_an_off_center_pan_knob(reaper, call, vox):
    vox.info["D_PAN"] = 0.3
    result = call("set_pan_over_range", track_index=1, start=2.0, end=4.0, pan=-1.0)
    assert "pan knob is not centered" in result["note"]


# set_automation_mode


def test_set_automation_mode(reaper, call, vox):
    result = call("set_automation_mode", track_index=1, mode="Read")
    assert result == {"success": True, "track_index": 1, "mode": "read"}
    assert vox.info["I_AUTOMODE"] == 1
    assert call("get_envelope_points", track_index=1)["automation_mode"] == "read"
    write = call("set_automation_mode", track_index=1, mode="write")
    assert "next playback records over" in write["warning"]


def test_set_automation_mode_unknown(reaper, call, vox):
    result = call("set_automation_mode", track_index=1, mode="record")
    assert result == {"success": False, "error": "mode must be one of ['trim_read', 'read', "
                                                 "'touch', 'write', 'latch']"}
    assert "I_AUTOMODE" not in vox.info
