"""GUIDs, get_context's readers, buses and folders, new tracks' inputs and
the analysis tools' render settings, against a running REAPER."""

import re
import time

import numpy as np
import pytest
import soundfile as sf

from tests.live.fixtures import write_tone
from tests.live.rpr import RPR

pytestmark = pytest.mark.live

GUID = re.compile(r"^\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}$")
PAUSE = 1008  # Transport: Pause
TRANSPORT_STOP = 1016
INPUT_FX = 0x1000000
ALL_MIDI_INPUTS = 4096 | 63 << 5


# GUIDs


def test_track_guids_are_guid_strings_that_survive_undoing_a_delete(live_project, call):
    live_project.add_track(0, "a")
    live_project.add_track(1, "b")
    RPR.Undo_OnStateChangeEx2(0, "live test setup", -1, -1)
    guids = [t["guid"] for t in call("list_tracks")["tracks"]]
    assert all(GUID.match(g) for g in guids), guids
    # The pointer getter is not the GUID: it is an address, new after undo.
    assert RPR.GetTrackGUID(live_project.tracks[1].id).startswith("(GUID*)0x")

    assert call("delete_track", track_index=guids[1])["success"] is True
    assert call("undo")["success"] is True
    assert [t["guid"] for t in call("list_tracks")["tracks"]] == guids
    assert call("set_track_volume", track_index=1, volume_db=-3.0)["success"] is True


def test_import_audio_file_picks_the_new_item_before_an_existing_one(live_project, call,
                                                                     tmp_path):
    """REAPER keeps a track's items in time order, so the item imported at 0 s
    becomes item 0, ahead of the one already at 5 s."""
    wav = tmp_path / "tone.wav"
    write_tone(wav, 440)
    live_project.add_track(0, "a")
    later = call("import_audio_file", file_path=str(wav), track_index=0, position=5.0)
    earlier = call("import_audio_file", file_path=str(wav), track_index=0, position=0.0)
    assert (later["item_index"], earlier["item_index"]) == (0, 0)
    assert GUID.match(earlier["item_guid"]) and earlier["item_guid"] != later["item_guid"]
    items = list(live_project.tracks[0].items)
    assert [round(i.position, 6) for i in items] == [0.0, 5.0]
    first = RPR.GetTrackMediaItem(live_project.tracks[0].id, 0)
    assert RPR.GetSetMediaItemInfo_String(first, "GUID", "", False)[3] == earlier["item_guid"]


# get_context


def test_the_selected_item_reports_its_index_on_the_track(live_project, call, tmp_path):
    wav = tmp_path / "tone.wav"
    write_tone(wav, 440)
    live_project.add_track(0, "a")
    call("import_audio_file", file_path=str(wav), track_index=0, position=0.0)
    second = call("import_audio_file", file_path=str(wav), track_index=0, position=5.0)
    RPR.SelectAllMediaItems(0, False)  # InsertMedia selects what it inserts
    item = RPR.GetTrackMediaItem(live_project.tracks[0].id, 1)
    RPR.SetMediaItemSelected(item, True)
    assert RPR.GetMediaItemInfo_Value(item, "IP_ITEMNUMBER") == 1.0  # 0-based
    [selected] = call("get_context")["selected_items"]
    assert (selected["item_index"], selected["guid"]) == (1, second["item_guid"])
    RPR.SetMediaItemSelected(item, False)


def test_time_selection_and_loop_range_read_back(live_project, call):
    result = RPR.GetSet_LoopTimeRange2(0, False, False, 0.0, 0.0, False)
    assert len(result) == 6  # (project, is_set, is_loop, start, end, allow_autoseek)
    call("set_time_selection", start=1.0, end=3.0)
    context = call("get_context")
    assert context["time_selection"] == {"start": pytest.approx(1.0), "end": pytest.approx(3.0)}
    assert RPR.GetSet_LoopTimeRange2(0, False, False, 0.0, 0.0, False)[3:5] == \
        [pytest.approx(1.0), pytest.approx(3.0)]
    call("set_time_selection", start=0.0, end=0.0)
    assert call("get_context")["time_selection"] is None


def test_last_touched_fx_on_a_track_the_master_and_an_input_fx(live_project, call):
    track = live_project.add_track(0, "a")
    track.add_fx("ReaEQ")
    RPR.TrackFX_SetParam(track.id, 0, 2, 0.3)
    touched = call("get_context")["last_touched_fx"]
    assert (touched["on"], touched["track_index"], touched["fx_index"], touched["param_index"],
            touched["input_fx"]) == ("track", 0, 0, 2, False)
    assert "ReaEQ" in touched["fx_name"]

    master = RPR.GetMasterTrack(0)
    RPR.TrackFX_AddByName(master, "ReaComp", False, -1)
    RPR.TrackFX_SetParam(master, 0, 1, 0.5)
    touched = call("get_context")["last_touched_fx"]
    assert (touched["on"], touched["track_index"], touched["fx_index"]) == ("master", None, 0)
    assert "ReaComp" in touched["fx_name"]

    RPR.TrackFX_AddByName(track.id, "ReaComp", True, -1)
    RPR.TrackFX_SetParam(track.id, INPUT_FX, 1, 0.5)
    assert RPR.GetLastTouchedFX(0, 0, 0) == [1, 1, INPUT_FX, 1]
    touched = call("get_context")["last_touched_fx"]
    assert (touched["on"], touched["fx_index"], touched["input_fx"]) == ("track", 0, True)
    assert "ReaComp" in touched["fx_name"]


def test_play_states(add_tone, call, live_project):
    add_tone("a", 440)
    live_project.cursor_position = 0.0  # InsertMedia left it at the item's end
    repeat = RPR.GetSetRepeat(-1)
    assert RPR.GetSetRepeat(-1) == repeat  # -1 only asks
    try:
        call("play_project")
        time.sleep(0.3)
        assert (RPR.GetPlayState(), call("get_context")["play_state"]) == (1, "playing")
        RPR.Main_OnCommand(PAUSE, 0)
        time.sleep(0.3)
        assert (RPR.GetPlayState(), call("get_context")["play_state"]) == (2, "paused")
    finally:
        RPR.Main_OnCommand(TRANSPORT_STOP, 0)
    assert call("get_context")["play_state"] == "stopped"


# Tracks, buses and folders


@pytest.mark.parametrize("track_type", ["midi", "instrument"])
def test_midi_and_instrument_tracks_record_every_midi_input(live_project, call, track_type):
    assert call("create_track", name="keys", track_type=track_type)["success"] is True
    track = live_project.tracks[0]
    assert track.get_info_value("I_RECINPUT") == ALL_MIDI_INPUTS == 6112
    if track_type == "instrument":  # other new tracks follow REAPER's preference
        assert track.get_info_value("I_RECMON") == 1
    state = call("get_track_info", track_index=0)
    assert "MIDI" in str(state)


def render_peak_db(call, tmp_path, name: str) -> float:
    out = tmp_path / f"{name}.wav"
    assert call("render_project", output_path=str(out))["success"] is True
    data, _ = sf.read(str(out))
    return float(20 * np.log10(np.max(np.abs(data))))


def test_a_track_routed_to_a_bus_is_heard_once(add_tone, call, tmp_path):
    """B_MAINSEND=0 removes the direct master send: through the bus the level
    is the source's, not +6 dB (heard twice)."""
    add_tone("a", 440)
    direct = render_peak_db(call, tmp_path, "direct")
    assert call("create_bus", name="Bus", track_indices=[0])["success"] is True
    assert render_peak_db(call, tmp_path, "bus") == pytest.approx(direct, abs=0.1)


def test_create_bus_closes_a_folder_left_open_and_adds_the_bus_at_top_level(live_project, call):
    folder = live_project.add_track(0, "folder")
    child = live_project.add_track(1, "child")
    folder.set_info_value("I_FOLDERDEPTH", 1)
    child.set_info_value("I_FOLDERDEPTH", 0)  # the folder is left open at the end
    assert call("create_bus", name="Bus", track_indices=[1])["success"] is True
    bus = live_project.tracks[2]
    assert child.get_info_value("I_FOLDERDEPTH") == -1
    assert RPR.GetParentTrack(bus.id).endswith("0x0000000000000000")
    assert RPR.GetParentTrack(child.id) == folder.id


def test_list_sends_names_the_destination_track(live_project, call):
    live_project.add_track(0, "src")
    live_project.add_track(1, "dst")
    call("create_send", source_track_index=0, dest_track_index=1)
    value = RPR.GetTrackSendInfo_Value(live_project.tracks[0].id, 0, 0, "P_DESTTRACK")
    assert isinstance(value, float)
    [send] = call("list_sends", track_index=0)["sends"]
    assert (send["dest_track_index"], send["dest_track_name"]) == (1, "dst")


# Analysis renders


USER_RENDER_STRINGS = {"RENDER_FILE": "/tmp/user-render-folder", "RENDER_PATTERN": "my mix"}
USER_RENDER_NUMBERS = {"RENDER_SRATE": 44100.0, "RENDER_CHANNELS": 1.0,
                       "RENDER_BOUNDSFLAG": 2.0, "RENDER_ADDTOPROJ": 1.0}


def render_settings_text(tmp_path) -> list[str]:
    copy = tmp_path / "settings.rpp"
    RPR.Main_SaveProjectEx(0, str(copy), 0)
    lines = copy.read_text(errors="replace").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("RENDER_FILE"))
    end = next(i for i, line in enumerate(lines) if line.strip() == "<RENDER_CFG")
    return [line.strip() for line in lines[start:end + 3]]


@pytest.mark.parametrize("tool", ["analyze_loudness", "detect_clipping"])
def test_analysis_tools_leave_the_render_settings_as_they_were(add_tone, call, tmp_path, tool):
    add_tone("a", 440)
    for key, value in USER_RENDER_STRINGS.items():
        RPR.GetSetProjectInfo_String(0, key, value, True)
    for key, number in USER_RENDER_NUMBERS.items():
        RPR.GetSetProjectInfo(0, key, number, True)
    call("render_project", output_path=str(tmp_path / "mp3.mp3"), format="mp3")
    RPR.GetSetProjectInfo_String(0, "RENDER_PATTERN", "my mix", True)
    RPR.GetSetProjectInfo_String(0, "RENDER_FILE", "/tmp/user-render-folder", True)
    for key, number in USER_RENDER_NUMBERS.items():
        RPR.GetSetProjectInfo(0, key, number, True)
    before = render_settings_text(tmp_path)
    fmt = RPR.GetSetProjectInfo_String(0, "RENDER_FORMAT", "", False)[3]

    assert call(tool)["success"] is True
    assert render_settings_text(tmp_path) == before
    assert RPR.GetSetProjectInfo_String(0, "RENDER_FORMAT", "", False)[3] == fmt
    for key, number in USER_RENDER_NUMBERS.items():
        assert RPR.GetSetProjectInfo(0, key, 0.0, False) == number
    RPR.GetSetProjectInfo(0, "RENDER_ADDTOPROJ", 0.0, True)
