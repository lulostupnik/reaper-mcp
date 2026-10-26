"""The recording workflow against a running REAPER with an audio interface.

Takes are a couple of seconds from input 1 of whatever device REAPER has open
(nothing needs to be plugged in: a silent take is still a take). Each test
records into a project saved in its tmp_path, with monitoring off, and puts
REAPER's global options (punch mode, metronome) back as they were.
"""

import base64
import os
import time
from pathlib import Path

import pytest
import soundfile as sf

from reaper_mcp import actions
from reaper_mcp.reaper import string_or_none
from tests.live.rpr import RPR
from tests.live.test_project_tools_live import close_current_tab_without_prompt, open_projects

pytestmark = pytest.mark.live

TAKE_SECONDS = 2.0
PAUSE = 1008
NEW_TAB_IGNORING_TEMPLATE = 41929
RECORD_MODES = {40252: "normal", 40076: "time_selection", 40253: "selected_items"}
LOOP_LINKED = 40621  # Options: Toggle loop points linked to time selection
WAV_24 = "ZXZhdxgAAQ=="


def next_undo() -> str | None:
    return string_or_none(RPR.Undo_CanUndo2, 0)


@pytest.fixture
def reaper_options(live_project):
    """Puts the punch mode, metronome and loop-link option back afterwards."""
    mode = next(c for c in RECORD_MODES if RPR.GetToggleCommandState(c) == 1)
    metronome = RPR.GetToggleCommandState(40364)
    linked = RPR.GetToggleCommandState(LOOP_LINKED)
    yield
    if RPR.GetPlayState():
        RPR.Main_OnCommand(40667, 0)
    RPR.Main_OnCommand(mode, 0)
    if RPR.GetToggleCommandState(40364) != metronome:
        RPR.Main_OnCommand(40364, 0)
    if RPR.GetToggleCommandState(LOOP_LINKED) != linked:
        RPR.Main_OnCommand(LOOP_LINKED, 0)
    assert RPR.GetToggleCommandState(mode) == 1
    assert RPR.GetToggleCommandState(40364) == metronome


@pytest.fixture
def studio(live_project, call, tmp_path, reaper_options):
    """A project saved in tmp_path with one track, "vox", recording mono input 1
    with monitoring off, as WAV 24-bit next to the project file."""
    if not RPR.Audio_IsRunning():
        pytest.fail("REAPER's audio device is not running")
    RPR.GetSetProjectInfo_String(0, "RECORD_PATH", "", True)
    RPR.GetSetProjectInfo_String(0, "RECORD_FORMAT", WAV_24, True)
    live_project.add_track(0, "vox")
    assert call("save_project", project_path=str(tmp_path / "studio.rpp"))["success"]
    assert call("set_track_input", track_index=0, input="mono", channel=1)["success"]
    assert call("set_monitoring", track_index=0, mode="off")["success"]
    RPR.Undo_OnStateChangeEx2(0, "live test setup", -1, -1)
    yield live_project
    RPR.GetSetProjectInfo_String(0, "RECORD_PATH", "", True)
    RPR.GetSetProjectInfo_String(0, "RECORD_FORMAT", WAV_24, True)


def record(call, seconds: float = TAKE_SECONDS, **arguments) -> dict:
    started = call("start_recording", **{"track_index": 0, **arguments})
    assert started["success"] is True, started
    time.sleep(seconds)
    stopped = call("stop_recording")
    assert stopped["success"] is True, stopped
    return stopped


# Actions


ACTION_NAMES = {
    1013: "Transport: Record",
    1016: "Transport: Stop",
    40667: "Transport: Stop (save all recorded media)",
    40364: "Options: Toggle metronome",
    40252: "Record: Set record mode to normal",
    40076: "Record: Set record mode to time selection auto-punch",
    40253: "Record: Set record mode to selected item auto-punch",
}


def test_reaper_names_the_actions_the_tools_run(live_project):
    assert {c: actions.action_name(c) for c in ACTION_NAMES} == ACTION_NAMES
    # An unknown ID: REAPER's binding returns "" (not NULL), read as no name.
    assert RPR.kbd_getTextFromCmd(999999, 0) == ""
    assert actions.action_name(999999) is None


def test_the_record_modes_report_which_one_is_on(reaper_options):
    states = [RPR.GetToggleCommandState(c) for c in RECORD_MODES]
    assert sorted(states) == [0, 0, 1]
    assert RPR.GetToggleCommandState(40364) in (0, 1)


def test_set_punch_mode_is_confirmed_and_kept_per_project(live_project, call, tmp_path,
                                                          reaper_options):
    """The record mode belongs to the project: saved in the .rpp as RECMODE,
    each tab keeps its own, and a new tab starts at normal."""
    assert call("set_time_selection", start=1.0, end=2.0)["success"] is True
    result = call("set_punch_mode", mode="time_selection")
    assert (result["success"], result["confirmed"]) == (True, True), result
    assert RPR.GetToggleCommandState(40076) == 1
    assert call("save_project", project_path=str(tmp_path / "punch.rpp"))["success"]
    saved = [line.split() for line in (tmp_path / "punch.rpp").read_text().splitlines()]
    recmode = [line[1:] for line in saved if line and line[0] == "RECMODE"]
    assert recmode == [["2"]]  # time selection auto-punch
    tabs = len(open_projects())
    RPR.Main_OnCommand(NEW_TAB_IGNORING_TEMPLATE, 0)
    try:
        assert call("get_context")["punch_mode"] == "normal"
    finally:
        close_current_tab_without_prompt(tmp_path)
    assert len(open_projects()) == tabs
    assert call("get_context")["punch_mode"] == "time_selection"
    result = call("set_punch_mode", mode="normal")
    assert (result["success"], result["confirmed"]) == (True, True), result
    call("set_time_selection", start=0.0, end=0.0)


def test_set_metronome_is_confirmed_and_its_flags_follow(call, reaper_options):
    for enabled in (True, False, True):
        result = call("set_metronome", enabled=enabled)
        assert result["metronome"] is enabled
        flags = int(RPR.get_config_var_string("projmetroen", "", 64)[2])
        assert bool(flags & 1) is enabled
        assert result["count_in"] is not None  # the flags agree with the toggle


def test_with_loop_points_linked_setting_one_range_moves_the_other(call, reaper_options):
    assert actions.action_name(LOOP_LINKED) == "Options: Toggle loop points linked to time selection"
    if RPR.GetToggleCommandState(LOOP_LINKED) != 1:
        RPR.Main_OnCommand(LOOP_LINKED, 0)
    call("set_time_selection", start=0.0, end=0.0)
    assert call("get_context").get("loop_range") is None
    result = call("set_time_selection", start=1.0, end=2.0)
    assert result["loop_range"] == {"start": pytest.approx(1.0), "end": pytest.approx(2.0)}
    assert "warning" in result
    RPR.Main_OnCommand(LOOP_LINKED, 0)  # unlinked
    result = call("set_loop", enabled=True, start=4.0, end=6.0)
    assert result["repeat"] is True and "warning" not in result
    assert result["time_selection"] == {"start": pytest.approx(1.0), "end": pytest.approx(2.0)}
    assert result["loop_range"] == {"start": pytest.approx(4.0), "end": pytest.approx(6.0)}
    assert call("set_loop", enabled=False)["repeat"] is False
    call("set_time_selection", start=0.0, end=0.0)


# The audio device


def test_get_audio_setup_reads_the_interface(live_project, call):
    setup = call("get_audio_setup")
    assert setup["audio_running"] is True
    assert setup["input_count"] == RPR.GetNumAudioInputs() >= 1
    assert [c["number"] for c in setup["inputs"]] == list(range(1, setup["input_count"] + 1))
    assert all(c["name"] for c in setup["inputs"] + setup["outputs"])
    assert setup["sample_rate"] in (44100, 48000, 88200, 96000)
    assert setup["block_size"] > 0
    assert setup["sample_format"].isdigit()  # REAPER words BPS as the bit count, e.g. "32"
    latency = setup["latency"]
    assert latency["input_samples"] > 0 and latency["output_samples"] > 0
    assert latency["input_ms"] == round(latency["input_samples"] / setup["sample_rate"] * 1000, 1)
    print(setup)


def test_set_track_input_numbers_inputs_from_one(live_project, call):
    live_project.add_track(0, "a")
    count = RPR.GetNumAudioInputs()
    for channel in range(1, count + 1):
        assert call("set_track_input", track_index=0, input="mono", channel=channel)["success"]
        assert live_project.tracks[0].get_info_value("I_RECINPUT") == channel - 1
    assert call("set_track_input", track_index=0, input="stereo", channel=1)["success"]
    assert live_project.tracks[0].get_info_value("I_RECINPUT") == 1024
    refused = call("set_track_input", track_index=0, input="mono", channel=count + 1)
    assert refused["success"] is False


@pytest.mark.parametrize("monitoring", ["off", "on"])
def test_check_input_level_listens_for_the_time_asked(studio, call, monitoring):
    call("set_monitoring", track_index=0, mode=monitoring)
    call("arm_track", track_index=0)
    started = time.monotonic()
    result = call("check_input_level", seconds=2.0)
    took = time.monotonic() - started
    assert result["success"] is True, result
    assert 2.0 <= result["seconds"] <= 2.3
    assert took < 3.0
    assert result["peak_dbfs"] < 0
    print(monitoring, result["peak_dbfs"], result["verdict"])


# Recording


def test_a_take_is_recorded_kept_and_reported(studio, call, tmp_path):
    started = call("start_recording", track_index=0)
    assert started["success"] is True, started
    assert next_undo() == "MCP: Arm tracks for recording"
    assert started["record_path"] == str(tmp_path)
    time.sleep(1.0)
    assert (RPR.GetPlayState(), call("get_context")["play_state"]) == (5, "recording")
    RPR.Main_OnCommand(PAUSE, 0)
    time.sleep(0.3)
    assert (RPR.GetPlayState(), call("get_context")["play_state"]) == (6, "recording (paused)")
    RPR.Main_OnCommand(PAUSE, 0)
    time.sleep(1.0)
    stopped = call("stop_recording")
    assert stopped["success"] is True, stopped
    [take] = stopped["takes"]
    assert take["track_index"] == 0 and take["length"] > 1.5
    assert Path(take["file"]).is_absolute() and Path(take["file"]).parent == tmp_path
    assert take["peak_dbfs"] is not None  # closed and readable at once
    assert sf.info(take["file"]).subtype == "PCM_24"
    assert take["item_guid"] == RPR.GetSetMediaItemInfo_String(
        RPR.GetTrackMediaItem(studio.tracks[0].id, 0), "GUID", "", False)[3]

    assert next_undo() == "Recorded media"  # REAPER's own step
    refused = call("undo")
    assert refused["success"] is False and "include_manual_steps" in refused["error"]
    assert call("undo", include_manual_steps=True)["success"] is True
    assert studio.tracks[0].n_items == 0


def test_start_recording_stops_playback_first_and_records_from_the_position(studio, call):
    call("play_project")
    time.sleep(0.5)
    started = call("start_recording", track_index=0, position=3.0)
    assert started["success"] is True, started
    assert started.get("stopped_playback") is True
    time.sleep(TAKE_SECONDS)
    [take] = call("stop_recording")["takes"]
    assert take["position"] == pytest.approx(3.0)


@pytest.mark.parametrize(("arguments", "subtype", "suffix"), [
    ({"bit_depth": 16}, "PCM_16", ".wav"),
    ({"bit_depth": 32}, "FLOAT", ".wav"),
    ({"format": "flac"}, None, ".flac"),
])
def test_recordings_use_the_recording_format(studio, call, arguments, subtype, suffix):
    assert call("set_recording_format", **arguments)["success"] is True
    stored = base64.b64decode(RPR.GetSetProjectInfo_String(0, "RECORD_FORMAT", "", False)[3])
    if suffix == ".wav":
        assert (stored[:4], stored[4]) == (b"evaw", arguments["bit_depth"])
    [take] = record(call)["takes"]
    assert Path(take["file"]).suffix == suffix
    if subtype:
        assert sf.info(take["file"]).subtype == subtype


def test_set_record_path_takes_effect_for_the_next_recording(studio, call, tmp_path):
    result = call("set_record_path", subfolder="Takes")
    assert result["record_path"] == str(tmp_path / "Takes")
    # Reading the recording folder back (GetProjectPathEx) already creates it.
    assert (tmp_path / "Takes").is_dir()
    [take] = record(call)["takes"]
    assert Path(take["file"]).parent == tmp_path / "Takes"
    call("set_record_path", subfolder="")
    assert RPR.GetProjectPathEx(0, "", 4096)[1] == str(tmp_path)


def test_recording_over_an_item_reports_only_the_parts_playing_the_new_file(studio, call):
    """REAPER's default ("add takes") splits the old item at the take's edges:
    the part before keeps the old file (plus an empty take) and is not reported."""
    [first] = record(call, position=0.0, seconds=3.0)["takes"]
    takes = record(call, position=1.0)["takes"]
    assert takes and {t["file"] for t in takes} != {first["file"]}
    assert len({t["file"] for t in takes}) == 1
    assert takes[0]["position"] == pytest.approx(1.0)
    assert first["item_guid"] not in {t["item_guid"] for t in takes}
    left = studio.tracks[0].items[0]
    assert (left.position, left.length, left.n_takes) == (0.0, pytest.approx(1.0), 2)
    print("items after overdub:", [(round(i.position, 2), round(i.length, 2), i.n_takes)
                                   for i in studio.tracks[0].items])


def test_time_selection_auto_punch_records_only_inside_it(studio, call):
    [first] = record(call, position=0.0, seconds=3.5)["takes"]
    call("set_time_selection", start=1.0, end=2.0)
    assert call("set_punch_mode", mode="time_selection")["confirmed"] is True
    stopped = record(call, position=0.0, seconds=3.0)
    call("set_punch_mode", mode="normal")
    call("set_time_selection", start=0.0, end=0.0)
    [take] = stopped["takes"]
    assert take["position"] == pytest.approx(1.0, abs=0.05)
    assert take["length"] == pytest.approx(1.0, abs=0.05)
    assert take["file"] != first["file"]
    files = [os.path.basename(str(RPR.GetMediaSourceFileName(RPR.GetMediaItemTake_Source(
        RPR.GetActiveTake(i.id)), "", 4096)[1])) for i in studio.tracks[0].items]
    print("items after punch:", [(round(i.position, 2), round(i.length, 2)) for i in
                                 studio.tracks[0].items], files)
