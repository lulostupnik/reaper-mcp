"""recording_tools against the fake REAPER: start_recording's checks,
stop_recording's takes, and the recording format and folder."""

import base64
from pathlib import Path

import pytest
from reapy import reascript_api

from reaper_mcp import recording_tools
from tests.fake_reaper import recording as fake_recording

RECORD, STOP_SAVE = 1013, 40667


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(recording_tools, "_pause", lambda seconds: None)


@pytest.fixture
def saved(reaper, tmp_path):
    """The fake project saved as Song/Song.rpp; returns its folder."""
    folder = tmp_path / "Song"
    folder.mkdir()
    (folder / "Song.rpp").write_text("<REAPER_PROJECT\n>\n")
    reaper.project_file = str(folder / "Song.rpp")
    return folder


def _steps(reaper):
    return [description for description, _ in reaper.undo_steps]


# start_recording


def test_start_recording_arms_the_track_and_disarms_the_others(reaper, call, saved):
    other = reaper.add_track("Old take")
    other.info["I_RECARM"] = 1.0
    vox = reaper.add_track("Vox")
    result = call("start_recording", track_index=1)
    assert result == {
        "success": True,
        "recording_tracks": [{"index": 1, "name": "Vox", "armed": True,
                              "record_input": "mono input 1", "monitoring": "off",
                              "record_mode": "input"}],
        "disarmed_tracks": [0],
        "position": 0.0,
        "count_in": False,
        "record_path": str(saved),
        "message": "Recording. Call stop_recording to stop and keep the take.",
    }
    assert (reaper.commands, reaper.transport) == ([RECORD], "recording")
    assert (vox.info["I_RECARM"], other.info["I_RECARM"]) == (1, 0)
    assert _steps(reaper) == ["MCP: Arm tracks for recording"]


def test_start_recording_keeps_others_armed_when_asked(reaper, call, saved):
    other = reaper.add_track("Gtr")
    other.info["I_RECARM"] = 1.0
    reaper.add_track("Vox")
    result = call("start_recording", track_index=1, disarm_others=False)
    assert result["disarmed_tracks"] == []
    assert other.info["I_RECARM"] == 1
    # Both record, so both are reported.
    assert [t["index"] for t in result["recording_tracks"]] == [0, 1]


def test_start_recording_checks_the_other_armed_tracks_it_keeps(reaper, call, saved):
    reaper.add_track("Gtr").info.update(I_RECARM=1.0, I_RECMODE=2.0)
    reaper.add_track("Vox")
    result = call("start_recording", track_index=1, disarm_others=False)
    assert result["success"] is False and "Track 0 (Gtr)" in result["error"]
    assert reaper.commands == []


def test_start_recording_an_output_mode_needs_no_input(reaper, call, saved):
    reaper.add_track("Bus").info.update(I_RECINPUT=-1.0, I_RECMODE=1.0)
    assert call("start_recording", track_index=0)["success"] is True


def test_start_recording_stops_playback_so_it_starts_at_the_position(reaper, call, saved):
    reaper.add_track("Vox")
    reaper.Main_OnCommand(1007, 0)  # Transport: Play
    reaper.play_position = 30.0
    reaper.commands.clear()
    result = call("start_recording", track_index=0, position=10.0)
    assert (result["success"], result["stopped_playback"]) == (True, True)
    assert reaper.commands == [1016, RECORD]
    assert reaper.record_start == 10.0


def test_start_recording_warns_about_monitoring_punch_and_repeat(reaper, call, saved):
    reaper.add_track("Vox").info["I_RECMON"] = 1.0
    reaper.punch_mode, reaper.repeat = "time_selection", True
    reaper.time_selection = (1.0, 2.0)
    warnings = " ".join(call("start_recording", track_index=0)["warnings"])
    for words in ("Monitoring is on for track(s) [0]", "headphones", "punch mode is time_selection",
                  "Repeat is on"):
        assert words in warnings


def test_start_recording_on_the_armed_tracks_changes_no_arming(reaper, call, saved):
    for name in ("Vox", "Gtr"):
        reaper.add_track(name).info["I_RECARM"] = 1.0
    result = call("start_recording")
    assert [t["index"] for t in result["recording_tracks"]] == [0, 1]
    assert _steps(reaper) == []  # nothing to arm: no empty undo step


def test_start_recording_from_a_position(reaper, call, saved):
    reaper.add_track("Vox")
    assert call("start_recording", track_index=0, position=12.5)["position"] == 12.5
    assert reaper.record_start == 12.5


def test_start_recording_reports_count_in(reaper, call, saved):
    reaper.add_track("Vox")
    reaper.config["projmetroen"] = str(6 | 16)
    assert call("start_recording", track_index=0)["count_in"] is True


def test_start_recording_refuses_a_never_saved_project(reaper, call):
    track = reaper.add_track("Vox")
    result = call("start_recording", track_index=0)
    assert result["success"] is False
    assert "never been saved" in result["error"] and "save_first=true" in result["error"]
    assert (reaper.commands, reaper.transport, "I_RECARM" in track.info) == ([], "stopped", False)


def test_start_recording_can_save_first(reaper, call, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    reaper.project_name = "Demo"
    reaper.add_track("Vox")
    result = call("start_recording", track_index=0, save_first=True)
    assert result["success"] is True, result
    assert reaper.project_file == str(tmp_path / "Documents" / "REAPER Projects" / "Demo.rpp")
    assert result["record_path"] == str(Path(reaper.project_file).parent)
    assert reaper.transport == "recording"


@pytest.mark.parametrize(("setup", "message"), [
    (lambda r: r.Main_OnCommand(RECORD, 0), "already recording"),
    (lambda r: setattr(r, "audio_running", False), "Audio is not running"),
    (lambda r: r.tracks[0].info.update(I_RECINPUT=-1.0), "Track 0 (Vox) cannot record: The track "
                                                         "has no record input"),
    (lambda r: r.tracks[0].info.update(I_RECMODE=2.0), "The record mode is none"),
])
def test_start_recording_preconditions(reaper, call, saved, setup, message):
    reaper.add_track("Vox")
    setup(reaper)
    reaper.commands.clear()
    result = call("start_recording", track_index=0)
    assert result["success"] is False and message in result["error"], result
    assert reaper.commands == []


def test_start_recording_needs_a_track(reaper, call, saved):
    reaper.add_track("Vox")
    result = call("start_recording")
    assert result == {"success": False,
                      "error": "No track to record on: pass track_index, or arm one with arm_track"}


def test_start_recording_bad_track(reaper, call, saved):
    result = call("start_recording", track_index=0)
    assert result["success"] is False and result["error"].startswith("No track 0")
    assert reaper.commands == []


def test_start_recording_negative_position(reaper, call, saved):
    reaper.add_track("Vox")
    result = call("start_recording", track_index=0, position=-1.0)
    assert result["success"] is False and "position" in result["error"]


def test_start_recording_reports_when_reaper_does_not_record(reaper, call, saved, monkeypatch):
    reaper.add_track("Old").info["I_RECARM"] = 1.0
    reaper.add_track("Vox")
    monkeypatch.setattr(reascript_api, "GetPlayState", lambda: 0)
    result = call("start_recording", track_index=1)
    assert result["success"] is False and "did not start recording" in result["error"]
    assert (result["play_state"], result["disarmed_tracks"]) == (0, [0])


def test_start_recording_waits_through_a_count_in(reaper, call, saved, monkeypatch):
    """REAPER plays (bit 1) while it counts in, then records."""
    reaper.add_track("Vox")
    states = iter([1] * 40 + [5])
    monkeypatch.setattr(reascript_api, "GetPlayState",
                        lambda: 0 if reaper.transport == "stopped" else next(states, 5))
    assert call("start_recording", track_index=0)["success"] is True


def test_start_recording_says_when_it_may_still_be_counting_in(reaper, call, saved, monkeypatch):
    reaper.add_track("Vox")
    monkeypatch.setattr(reascript_api, "GetPlayState",
                        lambda: 0 if reaper.transport == "stopped" else 1)
    result = call("start_recording", track_index=0)
    assert result["success"] is False and "counting in" in result["error"]


def test_start_recording_runs_record_under_a_translated_name(reaper, call, saved, monkeypatch):
    """A LangPack translates action names; Transport: Record's ID is well known."""
    reaper.add_track("Vox")
    monkeypatch.setitem(fake_recording.ACTION_NAMES, RECORD, "Transporte: Grabar")
    assert call("start_recording", track_index=0)["success"] is True
    assert reaper.commands == [RECORD]


# stop_recording


def _record(reaper, call, level=0.5, name="Vox"):
    track = reaper.add_track(name)
    track.input_level = level
    reaper.cursor = 2.0
    assert call("start_recording", track_index=len(reaper.tracks) - 1)["success"]
    reaper.commands.clear()
    return track


def test_stop_recording_keeps_the_take_and_reports_it(reaper, call, saved):
    track = _record(reaper, call)
    result = call("stop_recording")
    [item] = track.items
    assert result == {
        "success": True,
        "takes": [{"track_index": 0, "track_name": "Vox", "item_index": 0,
                   "item_guid": item.guid, "position": 2.0, "length": 3.0,
                   "file": str(saved / "Vox-001.wav"), "peak_dbfs": -6.0, "clipped": False}],
        "record_path": str(saved),
    }
    assert Path(result["takes"][0]["file"]).is_file()
    assert (reaper.commands, reaper.transport) == ([STOP_SAVE], "stopped")
    assert _steps(reaper)[-1] == "Recorded media"  # REAPER's own undo point


def test_stop_recording_reports_only_new_items(reaper, call, saved):
    from tests.fake_reaper.objects import ItemState

    track = reaper.add_track("Vox")
    track.items.append(ItemState(reaper.new_pointer("MediaItem"), 10.0, 1.0, midi=False))
    track.input_level = 0.5
    call("start_recording", track_index=0)
    [take] = call("stop_recording")["takes"]
    assert (take["item_index"], take["position"]) == (0, 0.0)


def test_stop_recording_reports_a_take_added_to_an_existing_item(reaper, call, saved):
    """Loop/lane recording or "add takes to existing items": same item, one more take."""
    from tests.fake_reaper.objects import ItemState

    track = reaper.add_track("Vox")
    track.items.append(ItemState(reaper.new_pointer("MediaItem"), 0.0, 5.0, midi=False,
                                 source_file=str(saved / "old.wav")))
    track.input_level, reaper.record_overlap = 0.5, "add_take"
    call("start_recording", track_index=0)
    result = call("stop_recording")
    [take] = result["takes"]
    assert (take["item_guid"], take["file"]) == (track.items[0].guid, str(saved / "Vox-001.wav"))
    assert "warning" not in result


def test_stop_recording_ignores_the_rest_of_an_item_it_cut(reaper, call, saved):
    """Auto-punch over an item splits it: the right-hand part is a new item
    playing the old file, not a take."""
    from tests.fake_reaper.objects import ItemState

    track = reaper.add_track("Vox")
    track.items.append(ItemState(reaper.new_pointer("MediaItem"), 0.0, 10.0, midi=False,
                                 source_file=str(saved / "old.wav")))
    track.input_level, reaper.record_overlap = 0.5, "split"
    reaper.cursor = 2.0
    call("start_recording", track_index=0)
    [take] = call("stop_recording")["takes"]
    assert (take["position"], take["file"]) == (2.0, str(saved / "Vox-001.wav"))
    assert len(track.items) == 3


def test_stop_recording_warns_about_clipping(reaper, call, saved):
    _record(reaper, call, level=1.0)
    result = call("stop_recording")
    assert (result["takes"][0]["clipped"], result["takes"][0]["peak_dbfs"]) == (True, 0.0)
    assert "clipped" in result["warning"]


def test_stop_recording_without_takes_warns(reaper, call, saved):
    track = _record(reaper, call)
    track.info["I_RECMODE"] = 2.0
    result = call("stop_recording")
    assert result["takes"] == [] and "No new takes" in result["warning"]


def test_stop_recording_midi_take_has_no_file(reaper, call, saved):
    track = reaper.add_track("Keys")
    track.info["I_RECINPUT"] = float(4096 + (63 << 5))
    call("start_recording", track_index=0)
    [take] = call("stop_recording")["takes"]
    assert (take["file"], take["peak_dbfs"], take["clipped"]) == (None, None, False)


def test_stop_recording_when_not_recording(reaper, call):
    result = call("stop_recording")
    assert result["success"] is False and "not recording" in result["error"]
    assert reaper.commands == []


def test_stop_recording_records_into_the_record_path(reaper, call, saved):
    call("set_record_path", subfolder="Audio")
    _record(reaper, call)
    [take] = call("stop_recording")["takes"]
    assert take["file"] == str(saved / "Audio" / "Vox-001.wav")


def test_file_peak_db_of_a_missing_file():
    assert recording_tools.file_peak_db("/nonexistent/take.wav") is None
    assert recording_tools.file_peak_db(None) is None


# set_recording_format


@pytest.mark.parametrize(("arguments", "config", "bit_depth"), [
    ({}, b"evaw\x18\x00\x01", 24),
    ({"bit_depth": 16}, b"evaw\x10\x00\x01", 16),
    ({"bit_depth": 32}, b"evaw\x20\x00\x01", 32),
    ({"format": "flac"}, b"calf", None),
])
def test_set_recording_format(reaper, call, arguments, config, bit_depth):
    result = call("set_recording_format", **arguments)
    assert result == {"success": True, "format": arguments.get("format", "wav"),
                      "bit_depth": bit_depth, "previous": None}
    # REAPER stores its whole sink config, so it reads back longer than written.
    stored = base64.b64decode(reaper.project_info["RECORD_FORMAT"])
    assert stored.startswith(config) and len(stored) > len(config)


def test_set_recording_format_reports_a_format_reaper_did_not_take(reaper, call, monkeypatch):
    from reaper_mcp import recording_tools as tools

    monkeypatch.setattr(tools, "get_project_string", lambda key: base64.b64encode(
        b"evaw\x10\x00\x01").decode())
    result = call("set_recording_format", bit_depth=24)
    assert result == {"success": False, "error": "REAPER did not take the recording format"}


@pytest.mark.parametrize(("arguments", "message"), [
    ({"format": "mp3"}, "format must be one of"),
    ({"bit_depth": 20}, "unsupported WAV bit depth 20"),
])
def test_set_recording_format_refusals(reaper, call, arguments, message):
    result = call("set_recording_format", **arguments)
    assert result["success"] is False and message in result["error"]
    assert "RECORD_FORMAT" not in reaper.project_info


def test_set_recording_format_is_not_an_undo_step_and_names_the_previous_one(reaper, call):
    """REAPER keeps the recording format out of its undo history."""
    assert call("set_recording_format", bit_depth=16)["previous"] is None
    assert reaper.undo_steps == []
    assert call("set_recording_format", format="flac")["previous"] == {
        "format": "wav", "bit_depth": 16}
    assert call("set_recording_format")["previous"] == {"format": "flac", "bit_depth": None}


def test_recordings_use_the_recording_format(reaper, call, saved):
    import soundfile as sf

    call("set_recording_format", bit_depth=16)
    _record(reaper, call)
    [take] = call("stop_recording")["takes"]
    assert sf.info(take["file"]).subtype == "PCM_16"


# set_record_path


def test_set_record_path_relative(reaper, call, saved):
    result = call("set_record_path", subfolder="Audio/")
    assert result == {"success": True, "record_path_setting": "Audio",
                      "record_path": str(saved / "Audio"), "previous_record_path_setting": ""}


@pytest.mark.parametrize("subfolder", ["/tmp/takes", "~/takes", "../elsewhere", "Audio/../../x",
                                       "/"])
def test_set_record_path_refuses_folders_outside_the_project(reaper, call, saved, subfolder):
    result = call("set_record_path", subfolder=subfolder)
    assert result["success"] is False and "inside the project's folder" in result["error"]
    assert "RECORD_PATH" not in reaper.project_info


def test_set_record_path_empty_records_next_to_the_project(reaper, call, saved):
    call("set_record_path", subfolder="Audio")
    assert call("set_record_path", subfolder="")["record_path"] == str(saved)


def test_set_record_path_is_not_an_undo_step_and_names_the_previous_one(reaper, call, saved):
    """REAPER keeps the recording folder out of its undo history."""
    assert call("set_record_path", subfolder="Audio")["previous_record_path_setting"] == ""
    assert reaper.undo_steps == []
    assert call("set_record_path", subfolder="")["previous_record_path_setting"] == "Audio"


def test_start_recording_bad_track_is_not_an_undo_step(reaper, call, saved):
    call("start_recording", track_index=0)
    assert reaper.undo_steps == []
