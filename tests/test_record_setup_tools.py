"""record_setup_tools against the fake REAPER: the audio device, track input,
arming, monitoring, record mode and the input level check."""

import pytest

from reaper_mcp import record_setup_tools
from tests.helpers import assert_no_track

ALL_MIDI_INPUTS = 4096 + (63 << 5)


class FakeClock:
    """Time that only passes when the code under test pauses, plus a fixed cost
    per meter reading (a round trip to REAPER)."""

    def __init__(self):
        self.now = 0.0
        self.pauses: list[float] = []

    def pause(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(record_setup_tools, "_pause", fake.pause)
    monkeypatch.setattr(record_setup_tools, "_clock", lambda: fake.now)
    return fake


def _steps(reaper):
    return [description for description, _ in reaper.undo_steps]


# get_audio_setup


def test_get_audio_setup(reaper, call):
    result = call("get_audio_setup")
    assert result == {
        "success": True,
        "audio_running": True,
        "system": "JACK",
        "input_device": "Scarlett Solo USB",
        "output_device": "Scarlett Solo USB",
        "sample_rate": 48000,
        "block_size": 256,
        "sample_format": "32",
        "input_count": 2,
        "inputs": [{"number": 1, "name": "Scarlett Solo Input 1"},
                   {"number": 2, "name": "Scarlett Solo Input 2"}],
        "output_count": 2,
        "outputs": [{"number": 1, "name": "Scarlett Solo Output 1"},
                    {"number": 2, "name": "Scarlett Solo Output 2"}],
        "latency": {"input_samples": 288, "output_samples": 544,
                    "input_ms": 6.0, "output_ms": 11.3},
        "warnings": [],
        "how_to_change": record_setup_tools.DEVICE_GUIDANCE,
    }
    assert reaper.commands == []


def test_get_audio_setup_without_a_running_device_explains(reaper, call):
    reaper.audio_running = False
    result = call("get_audio_setup")
    assert (result["audio_running"], result["system"], result["sample_rate"]) == (False, None, None)
    assert (result["inputs"], result["latency"]["input_ms"]) == ([], None)
    [warning] = result["warnings"]
    assert warning.startswith("Audio is not running") and "Preferences > Audio" in warning


def test_get_audio_setup_warns_when_there_are_no_inputs(reaper, call):
    reaper.audio_inputs = []
    [warning] = call("get_audio_setup")["warnings"]
    assert warning.startswith("REAPER sees no audio inputs")


# set_track_input


@pytest.mark.parametrize(("arguments", "value", "words"), [
    ({"input": "mono", "channel": 1}, 0, "mono input 1"),
    ({"input": "mono", "channel": 2}, 1, "mono input 2"),
    ({"input": "stereo"}, 1024, "stereo input 1/2"),
    ({"input": "midi"}, ALL_MIDI_INPUTS, "all MIDI devices, all channels"),
    ({"input": "midi", "midi_channel": 10}, ALL_MIDI_INPUTS + 10, "all MIDI devices, channel 10"),
    ({"input": "none"}, -1, "none"),
])
def test_set_track_input(reaper, call, arguments, value, words):
    track = reaper.add_track("Gtr")
    result = call("set_track_input", track_index=0, **arguments)
    assert result == {"success": True, "track_index": 0, "armed": False, "record_input": words,
                      "monitoring": "off", "record_mode": "input"}
    assert track.info["I_RECINPUT"] == value


@pytest.mark.parametrize(("arguments", "message"), [
    ({"input": "mono", "channel": 3}, "the interface has 2 input(s), so a mono input starts at 1 to 2, not 3"),
    ({"input": "mono", "channel": 0}, "a mono input starts at 1 to 2, not 0"),
    ({"input": "stereo", "channel": 2}, "a stereo input starts at 1 to 1, not 2"),
    ({"input": "midi", "midi_channel": 17}, "midi_channel must be 0 (all) to 16, not 17"),
    ({"input": "line"}, "input must be mono, stereo, midi or none, not 'line'"),
])
def test_set_track_input_refuses_inputs_the_interface_lacks(reaper, call, arguments, message):
    track = reaper.add_track("Gtr")
    result = call("set_track_input", track_index=0, **arguments)
    assert result["success"] is False and message in result["error"], result
    assert result["error"].endswith("nothing was changed")
    assert "I_RECINPUT" not in track.info


def test_set_track_input_without_audio_inputs(reaper, call):
    reaper.add_track("Gtr")
    reaper.audio_running = False
    result = call("set_track_input", track_index=0, input="mono")
    assert result["success"] is False and "sees no audio inputs" in result["error"]


def test_set_track_input_is_one_undo_step(reaper, call):
    track = reaper.add_track("Gtr")
    call("set_track_input", track_index=0, input="mono", channel=2)
    assert _steps(reaper) == ["MCP: Set track input (track_index=0, input=mono, channel=2, midi_channel=0)"]
    call("undo")
    assert "I_RECINPUT" not in reaper.tracks[0].info
    assert track.name == "Gtr"


def test_set_track_input_bad_track(reaper, call):
    assert_no_track(call("set_track_input", track_index=0, input="mono"))


# arm_track


def test_arm_track(reaper, call):
    track = reaper.add_track("Vox")
    result = call("arm_track", track_index=0)
    assert result == {"success": True, "track_index": 0, "armed": True,
                      "record_input": "mono input 1", "monitoring": "off",
                      "record_mode": "input", "disarmed_tracks": [], "armed_tracks": [0],
                      "warnings": []}
    assert track.info["I_RECARM"] == 1
    assert reaper.transport == "stopped"


def test_arm_track_exclusive_disarms_the_others(reaper, call):
    first, second, third = (reaper.add_track(name) for name in ("a", "b", "c"))
    first.info["I_RECARM"] = third.info["I_RECARM"] = 1.0
    result = call("arm_track", track_index=1, exclusive=True)
    assert (result["disarmed_tracks"], result["armed_tracks"]) == ([0, 2], [1])
    assert [t.info["I_RECARM"] for t in (first, second, third)] == [0, 1, 0]


def test_arm_track_keeps_others_armed_by_default(reaper, call):
    first = reaper.add_track("a")
    reaper.add_track("b")
    first.info["I_RECARM"] = 1.0
    assert call("arm_track", track_index=1)["armed_tracks"] == [0, 1]


def test_arm_track_disarm(reaper, call):
    track = reaper.add_track("Vox")
    track.info["I_RECARM"] = 1.0
    result = call("arm_track", track_index=0, armed=False)
    assert (result["armed"], result["armed_tracks"], result["warnings"]) == (False, [], [])


def test_arm_track_warns_about_what_would_record_nothing(reaper, call):
    track = reaper.add_track("Vox")
    track.info["I_RECINPUT"] = -1.0
    assert call("arm_track", track_index=0)["warnings"] == [
        "The track has no record input: set one with set_track_input."]
    track.info["I_RECMODE"] = 2.0
    assert call("arm_track", track_index=0)["warnings"] == [
        "The record mode is none: the track will not record (set_track_record_mode input)."]


def test_arm_track_output_mode_needs_no_input(reaper, call):
    reaper.add_track("Bus").info.update(I_RECINPUT=-1.0, I_RECMODE=1.0)
    assert call("arm_track", track_index=0)["warnings"] == []


def test_arm_track_warns_about_monitoring(reaper, call):
    reaper.add_track("Vox").info["I_RECMON"] = 1.0
    [warning] = call("arm_track", track_index=0)["warnings"]
    assert "Monitoring is on" in warning and "headphones" in warning


# set_monitoring


@pytest.mark.parametrize(("mode", "value", "words"), [
    ("on", 1, "on"), ("tape", 2, "tape (off while playing)"),
])
def test_set_monitoring_on_warns_about_feedback_and_double_monitoring(reaper, call, mode, value,
                                                                      words):
    track = reaper.add_track("Vox")
    result = call("set_monitoring", track_index=0, mode=mode)
    assert (result["success"], result["monitoring"]) == (True, words)
    assert "headphones" in result["warning"] and "Direct Monitor" in result["warning"]
    assert "arm_track" in result["note"]  # the track is not armed
    assert track.info["I_RECMON"] == value


def test_set_monitoring_on_an_armed_track_has_no_note(reaper, call):
    reaper.add_track("Vox").info["I_RECARM"] = 1.0
    assert "note" not in call("set_monitoring", track_index=0, mode="on")


def test_set_monitoring_off(reaper, call):
    track = reaper.add_track("Vox")
    track.info["I_RECMON"] = 1.0
    result = call("set_monitoring", track_index=0, mode="off")
    assert (result["monitoring"], "warning" in result) == ("off", False)
    assert track.info["I_RECMON"] == 0


def test_set_monitoring_bad_mode(reaper, call):
    track = reaper.add_track("Vox")
    result = call("set_monitoring", track_index=0, mode="loud")
    assert result["success"] is False and "'loud'" in result["error"]
    assert "I_RECMON" not in track.info


# set_track_record_mode


@pytest.mark.parametrize(("mode", "value", "words"), [
    ("input", 0, "input"), ("output", 1, "output (stereo)"), ("none", 2, "none (monitoring only)"),
    ("midi_overdub", 7, "MIDI overdub"), ("midi_replace", 8, "MIDI replace"),
])
def test_set_track_record_mode(reaper, call, mode, value, words):
    track = reaper.add_track("Keys")
    track.info["I_RECINPUT"] = float(ALL_MIDI_INPUTS)
    result = call("set_track_record_mode", track_index=0, mode=mode)
    assert (result["success"], result["record_mode"]) == (True, words)
    assert track.info["I_RECMODE"] == value


def test_set_track_record_mode_midi_on_an_audio_input_warns(reaper, call):
    reaper.add_track("Gtr")
    warnings = call("set_track_record_mode", track_index=0, mode="midi_overdub")["warnings"]
    assert warnings == ["MIDI record modes need a MIDI input (set_track_input midi)."]


def test_set_track_record_mode_bad_mode(reaper, call):
    reaper.add_track("Gtr")
    result = call("set_track_record_mode", track_index=0, mode="loop")
    assert result["success"] is False and "'loop'" in result["error"]


# check_input_level


def _armed(reaper, level, name="Gtr"):
    track = reaper.add_track(name)
    track.info.update(I_RECARM=1.0, I_RECINPUT=1.0)
    track.input_level = level
    return track


@pytest.mark.parametrize(("level", "peak", "verdict", "advice"), [
    (0.0, -150.0, "no signal", "48V"),
    (0.01, -40.0, "too quiet", "gain knob up"),
    (0.1, -20.0, "good", "a little low"),
    (0.25, -12.0, "good", "Good level"),
    (0.63, -4.0, "good", "a little hot"),
    (1.0, 0.0, "clipping", "never lights red"),
])
def test_check_input_level(reaper, call, level, peak, verdict, advice):
    _armed(reaper, level)
    result = call("check_input_level", seconds=1.0)
    assert (result["success"], result["track_index"], result["track_name"]) == (True, 0, "Gtr")
    assert (result["peak_dbfs"], result["verdict"]) == (pytest.approx(peak, abs=0.1), verdict)
    assert (result["seconds"], result["warnings"]) == (1.0, [])
    assert any(advice in line for line in result["advice"]), result["advice"]
    assert result["record_input"] == "mono input 2"


def test_check_input_level_no_signal_names_the_usual_causes(reaper, call):
    _armed(reaper, 0.0)
    advice = " ".join(call("check_input_level")["advice"])
    for cause in ("cable", "48V", "INST", "gain knob"):
        assert cause in advice


def test_check_input_level_never_records_and_records_no_undo_step(reaper, call):
    _armed(reaper, 0.25)
    call("check_input_level", seconds=10.0)
    assert (reaper.commands, reaper.transport, reaper.undo_steps) == ([], "stopped", [])
    assert reaper.peak_hold_clears == 2  # both channels' hold cleared before listening


def test_check_input_level_reads_the_meter_for_the_requested_time(reaper, call, clock):
    _armed(reaper, 0.25)
    call("check_input_level", seconds=2.0)
    assert sum(clock.pauses) == pytest.approx(2.0)


def test_check_input_level_is_limited_by_time_not_readings(reaper, call, clock, monkeypatch):
    """Each reading is a round trip to REAPER (here 40 ms): the check still
    ends after the requested time and reports how long it listened."""
    from reapy import reascript_api

    track = _armed(reaper, 0.25)
    peak = reaper.Track_GetPeakInfo

    def slow_peak(pointer, channel):
        clock.now += 0.02
        return peak(pointer, channel)

    monkeypatch.setattr(reascript_api, "Track_GetPeakInfo", slow_peak)
    result = call("check_input_level", seconds=2.0)
    assert result["seconds"] == pytest.approx(2.0, abs=0.1)
    assert track.info["I_RECARM"] == 1


def test_check_input_level_warns_when_volume_or_fx_may_change_the_meter(reaper, call):
    track = _armed(reaper, 0.25)
    track.info["D_VOL"] = 2.0
    [warning] = call("check_input_level")["warnings"]
    assert "volume" in warning


def test_check_input_level_picks_the_named_track(reaper, call):
    _armed(reaper, 0.0, "Vox")
    _armed(reaper, 0.25, "Gtr")
    result = call("check_input_level", track_index=1)
    assert (result["track_name"], result["verdict"]) == ("Gtr", "good")


@pytest.mark.parametrize(("setup", "arguments", "message"), [
    (lambda r: r.add_track("Gtr"), {}, "no track is armed"),
    (lambda r: [_armed(r, 0.1), _armed(r, 0.1)], {}, "tracks [0, 1] are armed: pass track_index"),
    (lambda r: r.add_track("Gtr"), {"track_index": 0}, "track 0 is not armed"),
    (lambda r: r.add_track("Gtr"), {"track_index": 4}, "No track 4"),
    (lambda r: _armed(r, 0.1).info.update(I_RECINPUT=-1.0), {}, "records nothing"),
    (lambda r: _armed(r, 0.1).info.update(I_RECINPUT=float(ALL_MIDI_INPUTS)), {}, "records MIDI"),
    (lambda r: _armed(r, 0.1), {"seconds": 11.0}, "seconds must be 0.5 to 10.0"),
    (lambda r: _armed(r, 0.1), {"seconds": 0.1}, "seconds must be 0.5 to 10.0"),
])
def test_check_input_level_refusals(reaper, call, setup, arguments, message):
    setup(reaper)
    result = call("check_input_level", **arguments)
    assert result["success"] is False and message in result["error"], result


def test_check_input_level_without_audio(reaper, call):
    _armed(reaper, 0.5)
    reaper.audio_running = False
    result = call("check_input_level")
    assert result["success"] is False and result["error"].startswith("Audio is not running")
