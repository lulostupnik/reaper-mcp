"""fx_values: reading values in display units, and setting FX parameters by
name with them (set_fx_parameter_by_name) against the fake REAPER's stand-in curves."""

import math

import pytest

from reaper_mcp.fx_values import ambiguous_number, parse_value, reached
from tests.fake_reaper.objects import FXState


@pytest.mark.parametrize(("text", "expected"), [
    ("-3 dB", (-3.0, "db")),
    ("-3.00dB", (-3.0, "db")),
    ("300 Hz", (300.0, "hz")),
    ("1.5 kHz", (1500.0, "hz")),
    ("1.2k", (1200.0, "")),
    ("20 ms", (0.02, "s")),
    ("2 s", (2.0, "s")),
    ("4.0:1", (4.0, "")),
    ("12.0 dB/sec", (12.0, "db/s")),
    ("1,5 s", (1.5, "s")),
    ("−6 dB", (-6.0, "db")),
    ("50%", (50.0, "%")),
])
def test_parse_value(text, expected):
    number, dimension = parse_value(text)
    assert (pytest.approx(number), dimension) == expected


def test_parse_value_infinities_and_words():
    assert parse_value("-inf dB") == (-math.inf, "db")
    assert parse_value("inf") == (math.inf, "")
    assert parse_value("Off") is None
    assert parse_value("") is None


def test_reached_uses_the_display_precision():
    assert reached("-3.00 dB", -3.0)
    assert reached("-3.0 dB", -3.04)
    assert not reached("-2.5 dB", -3.0)
    assert reached("1.20 kHz", 1200.0)
    assert reached("-inf", -math.inf)
    assert not reached("Off", 0.0)


def test_reached_scales_the_precision_by_the_shown_unit():
    # "50.0 ms" is good to 0.1 ms (0.0001 s), not to 0.1 s.
    assert reached("50.0 ms", 0.05)
    assert not reached("50.0 ms", 0.02)
    assert not reached("500.0 ms", 0.6)
    assert not reached("1.20 kHz", 1250.0)


@pytest.mark.parametrize("text", ["1.000 Hz", "1,000 Hz", "10.000", "-1.000 dB"])
def test_thousands_lookalikes_are_ambiguous(text):
    assert ambiguous_number(text)


@pytest.mark.parametrize("text", ["1.5 kHz", "1,5 s", "1000 Hz", "0.125", "-3.00 dB", "12.0"])
def test_plain_numbers_are_not_ambiguous(text):
    assert not ambiguous_number(text)


def _eq_track(reaper):
    track = reaper.add_track("Vox")
    track.fxs.append(FXState("ReaEQ", [0.5, 0.5, 0.5]))
    track.fxs.append(FXState("ReaComp", [0.5, 0.5, 0.5, 0.5]))
    track.fxs.append(FXState("ReaGate", [0.5, 0.0]))
    return track


def test_set_fx_parameter_by_name_in_db(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="gain-low",
                  value="-3 dB")
    assert result == {
        "success": True, "track_index": 0, "fx_index": 0, "fx_name": "VST: ReaEQ (Cockos)",
        "param_index": 0, "param_name": "Gain-Low", "requested": "-3 dB",
        "formatted_value": "-3.0 dB", "reached": True,
    }
    assert track.fxs[0].values[0] == pytest.approx(21 / 48, abs=2e-3)
    assert track.fxs[0].values[1:] == [0.5, 0.5]


def test_set_fx_parameter_by_name_converts_khz_to_hz(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="Freq-Low",
                  value="1.5 kHz")
    assert result["formatted_value"] == "1.50 kHz"
    assert result["reached"] is True
    low_hz = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="freq",
                  value="300 Hz")
    assert low_hz["formatted_value"] == "300.0 Hz"
    assert 20 * 1000 ** track.fxs[0].values[1] == pytest.approx(300, abs=0.1)


def test_set_fx_parameter_by_name_converts_seconds_to_ms(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=1, param_name="Attack",
                  value="0.1 s")
    assert result["formatted_value"] == "100.0 ms"
    assert track.fxs[1].values[2] == pytest.approx(0.2, abs=1e-3)


def test_set_fx_parameter_by_name_past_the_range_lands_on_its_end(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="Gain-High",
                  value="+40 dB")
    assert result["success"] is True
    assert result["reached"] is False
    assert result["formatted_value"] == "+24.0 dB"
    assert "'-inf' to '+24.0 dB'" in result["note"]
    assert track.fxs[0].values[2] == pytest.approx(1.0)


def test_set_fx_parameter_by_name_matches_words(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=2, param_name="bypass",
                  value="on")
    assert (result["formatted_value"], result["reached"]) == ("On", True)
    assert track.fxs[2].values[1] >= 0.5


def test_set_fx_parameter_by_name_refuses_a_word_it_does_not_show(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=2, param_name="Bypass",
                  value="maybe")
    assert result == {"success": False, "error": "'maybe' is not a value this parameter "
                                                 "shows; it shows: ['Off', 'On']"}
    assert track.fxs[2].values == [0.5, 0.0]


def test_set_fx_parameter_by_name_refuses_the_wrong_unit(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="Gain-Low",
                  value="300 Hz")
    assert result["success"] is False
    assert "not in hz" in result["error"]
    assert track.fxs[0].values == [0.5, 0.5, 0.5]


def test_set_fx_parameter_by_name_unknown_and_ambiguous_names(reaper, call):
    _eq_track(reaper)
    unknown = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="Width",
                   value="1")
    assert unknown == {"success": False, "error": "VST: ReaEQ (Cockos) has no parameter 'Width'. "
                                                  "Its parameters: ['Gain-Low', 'Freq-Low', "
                                                  "'Gain-High']"}
    ambiguous = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="gain",
                     value="0 dB")
    assert ambiguous["success"] is False
    assert "matches several parameters" in ambiguous["error"]


def test_set_fx_parameter_by_name_is_one_undo_step(reaper, call):
    _eq_track(reaper)
    call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="Gain-Low", value="-6 dB")
    assert reaper.undo_steps[-1][0].startswith("MCP: Set fx parameter by name")
    assert call("undo")["success"] is True
    assert reaper.tracks[0].fxs[0].values[0] == 0.5


def test_get_fx_parameters_shows_formatted_values(reaper, call):
    _eq_track(reaper)
    params = call("get_fx_parameters", track_index=0, fx_index=0)["parameters"]
    assert [p["formatted_value"] for p in params] == ["+0.0 dB", "632.5 Hz", "+0.0 dB"]


def test_set_fx_parameter_by_name_past_a_ms_range_is_not_reached(reaper, call):
    _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=1, param_name="Attack",
                  value="600 ms")
    assert (result["formatted_value"], result["reached"]) == ("500.0 ms", False)
    assert "'0.0 ms' to '500.0 ms'" in result["note"]


def test_set_fx_parameter_by_name_refuses_a_thousands_separator(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="Freq-Low",
                  value="1.000 Hz")
    assert result["success"] is False
    assert "ambiguous" in result["error"] and "'1 kHz'" in result["error"]
    assert track.fxs[0].values == [0.5, 0.5, 0.5]


def test_set_fx_parameter_by_name_reaches_minus_inf(reaper, call):
    track = _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="Gain-Low",
                  value="-inf dB")
    assert (result["formatted_value"], result["reached"]) == ("-inf", True)
    assert track.fxs[0].values[0] == 0.0


def test_set_fx_parameter_by_name_on_the_master(reaper, call):
    reaper.master.fxs.append(FXState("ReaLimit", [0.5, 0.5, 0.5]))
    result = call("set_fx_parameter_by_name", master=True, fx_index=0, param_name="Threshold",
                  value="-6 dB")
    assert (result["success"], result["master"], result["formatted_value"]) == (
        True, True, "-6.00 dB")
    assert "track_index" not in result
    neither = call("set_fx_parameter_by_name", fx_index=0, param_name="Threshold", value="0 dB")
    assert neither == {"success": False, "error": "Give either track_index or master=true"}


def test_set_fx_parameter_by_name_bad_fx_index(reaper, call):
    _eq_track(reaper)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=-1, param_name="Gain-Low",
                  value="0 dB")
    assert result["success"] is False
    assert "There is no FX -1" in result["error"]
