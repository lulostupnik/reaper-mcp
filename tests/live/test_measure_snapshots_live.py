"""Per-track measurement, A/B mix snapshots and the guidance prompts' plug-in
assumptions against a running REAPER."""

import time

import pytest

from reaper_mcp.reaper import string_or_none
from tests.live.rpr import RPR

pytestmark = pytest.mark.live

SAVE_AS_PROJECT_FILE = 8


def next_undo() -> str | None:
    return string_or_none(RPR.Undo_CanUndo2, 0)


def checkpoint() -> None:
    RPR.Undo_OnStateChangeEx2(0, "live test setup", -1, -1)


def saved_clean(tmp_path) -> None:
    """The project saved as its own file, so IsProjectDirty starts at 0."""
    RPR.Main_SaveProjectEx(0, str(tmp_path / "m.rpp"), SAVE_AS_PROJECT_FILE)
    RPR.Main_SaveProject(0, False)
    assert RPR.IsProjectDirty(0) == 0


def lufs(result: dict) -> float:
    assert result["success"] is True, result
    assert result["measurement"]["integrated_lufs"] is not None, result
    return result["measurement"]["integrated_lufs"]


# measure_track


def test_measuring_changes_nothing_and_leaves_no_undo_point(add_tone, call, tmp_path,
                                                            live_project):
    add_tone("a", 440)
    add_tone("b", 660)
    live_project.tracks[1].set_info_value("B_MUTE", 1)
    live_project.tracks[0].set_info_value("I_SOLO", 1)
    live_project.master_track.add_fx("ReaLimit")
    live_project.time_selection = (0.3, 0.6)
    checkpoint()  # an undo point marks the project dirty: before saving
    saved_clean(tmp_path)
    assert call("measure_track", track_index=1, start=0.0, end=1.0)["success"] is True
    assert next_undo() == "live test setup"
    assert RPR.IsProjectDirty(0) == 0
    assert [t.get_info_value("I_SOLO") for t in live_project.tracks] == [1, 0]
    assert live_project.tracks[1].get_info_value("B_MUTE") == 1
    assert live_project.master_track.fxs[0].is_enabled
    selection = live_project.time_selection
    assert (selection.start, selection.end) == pytest.approx((0.3, 0.6))


def test_a_muted_track_is_measured_as_if_unmuted(add_tone, call, live_project):
    add_tone("a", 440)
    track = live_project.tracks[0]
    track.set_info_value("B_MUTE", 1)
    result = call("measure_track", track_index=0)
    assert result["measurement"]["silent"] is False
    assert any("muted" in note for note in result["notes"])
    assert track.get_info_value("B_MUTE") == 1


def test_a_soloed_muted_track_is_heard(add_tone, call, tmp_path, live_project):
    """On 7.69 solo overrides mute in a render (mute alone silences the
    track), so measure_track's unmuting is not needed, only harmless."""
    import soundfile as sf

    add_tone("a", 440)
    track = live_project.tracks[0]
    track.set_info_value("B_MUTE", 1)
    out = tmp_path / "x.wav"
    call("render_time_selection", output_path=str(out), start=0.0, end=1.0, bit_depth=32)
    assert abs(sf.read(str(out))[0]).max() < 1e-6
    track.set_info_value("I_SOLO", 2)
    call("render_time_selection", output_path=str(out), start=0.0, end=1.0, bit_depth=32,
         overwrite=True)
    assert abs(sf.read(str(out))[0]).max() == pytest.approx(0.1, abs=0.01)


def test_a_3_db_fader_move_measures_3_db(add_tone, call, live_project):
    add_tone("a", 440)
    add_tone("b", 220)
    before = call("measure_track", track_index=0)
    assert call("set_track_volume", track_index=0, volume_db=3.0)["success"] is True
    after = call("measure_track", track_index=0)
    compared = call("compare_measurements", before=before, after=after, expected_change_db=3.0)
    assert compared["verdict"] == "verified", compared
    assert lufs(after) - lufs(before) == pytest.approx(3.0, abs=0.1)


def test_a_limiter_on_the_master_is_bypassed_unless_asked(add_tone, call, live_project):
    add_tone("a", 440)
    live_project.master_track.add_fx("ReaLimit")
    assert call("set_fx_parameter_by_name", master=True, fx_index=0, param_name="Threshold",
                value="-40 dB")["reached"] is True
    checkpoint()
    plain = [lufs(call("measure_track", track_index=0))]
    through = [lufs(call("measure_track", track_index=0, include_master_fx=True))]
    call("set_track_volume", track_index=0, volume_db=3.0)
    plain.append(lufs(call("measure_track", track_index=0)))
    through.append(lufs(call("measure_track", track_index=0, include_master_fx=True)))
    print("limiter bypassed:", plain, "through the limiter:", through)
    assert plain[1] - plain[0] == pytest.approx(3.0, abs=0.1)
    assert through[1] - through[0] < 2.5
    assert next_undo() == "MCP: Set track volume (track_index=0, volume_db=3)"


def test_what_a_track_sends_to_and_a_folders_children_are_measured(add_tone, call, tmp_path,
                                                                   live_project):
    add_tone("vox", 440)
    verb = live_project.add_track(1, "verb")
    assert call("create_send", source_track_index=0, dest_track_index=1)["success"] is True
    alone = lufs(call("measure_track", track_index=0))
    verb.set_info_value("D_VOL", 2.0)  # the send's return, +6 dB
    with_verb = lufs(call("measure_track", track_index=0))
    print("vox alone / with the reverb return louder:", alone, with_verb)
    assert with_verb > alone + 1  # what it sends to is heard
    bus = call("measure_track", track_index=1)
    print("the reverb bus measured alone:", bus["measurement"])
    assert bus["measurement"]["silent"] is False  # its source is heard through it


def test_a_folder_track_is_measured_with_its_children(add_tone, call, live_project):
    folder = live_project.add_track(0, "folder")
    add_tone("child", 440)
    folder.set_info_value("I_FOLDERDEPTH", 1)
    live_project.tracks[1].set_info_value("I_FOLDERDEPTH", -1)
    result = call("measure_track", track_index=0)
    assert result["measurement"]["silent"] is False


def test_a_solo_defeat_track_is_heard_in_every_measurement(add_tone, call, live_project):
    add_tone("a", 440)
    add_tone("always", 220)
    live_project.tracks[1].set_info_value("B_SOLO_DEFEAT", 1)
    live_project.tracks[1].set_info_value("D_VOL", 0.0)
    quiet = lufs(call("measure_track", track_index=0))
    live_project.tracks[1].set_info_value("D_VOL", 1.0)
    result = call("measure_track", track_index=0)
    print("with a solo-defeat track:", lufs(result), "vs", quiet, "notes", result["notes"])
    assert lufs(result) > quiet + 1
    assert any("solo defeat" in note and "always" in note for note in result["notes"])


def test_a_range_past_the_last_item_is_cut_to_the_project(add_tone, call, live_project):
    add_tone("a", 440)  # 2 s
    print("project length:", live_project.length)
    assert live_project.length == pytest.approx(2.0, abs=1e-3)
    result = call("measure_track", track_index=0, start=1.0, end=5.0)
    assert result["end"] == pytest.approx(2.0, abs=1e-3)
    assert result["measurement"]["seconds"] == pytest.approx(1.0, abs=0.01)


def test_the_temp_render_ignores_the_render_dialogs_settings(add_tone, call, live_project):
    """Stems, selected tracks via master, normalization and a tail in the
    render dialog do not leak into analysis renders, and are put back."""
    add_tone("a", 440)
    add_tone("b", 220)
    settings = {"RENDER_SETTINGS": 2 | 128, "RENDER_NORMALIZE": 65, "RENDER_TAILFLAG": 4,
                "RENDER_TAILMS": 1000}
    saved = {k: RPR.GetSetProjectInfo(0, k, 0, False) for k in settings}
    try:
        for key, value in settings.items():
            RPR.GetSetProjectInfo(0, key, value, True)
        live_project.tracks[1].select()
        result = call("measure_track", track_index=0, start=0.5, end=1.5)
        assert result["measurement"]["seconds"] == pytest.approx(1.0, abs=0.01)
        # The plain tone (-20.7 LUFS), not normalized to -14
        assert result["measurement"]["integrated_lufs"] == pytest.approx(-20.7, abs=0.3)
        assert {k: RPR.GetSetProjectInfo(0, k, 0, False) for k in settings} == settings
    finally:
        for key, value in saved.items():
            RPR.GetSetProjectInfo(0, key, value, True)


# Mix snapshots


def test_ext_state_is_readable_in_the_same_session_and_an_empty_value_removes_it(live_project):
    RPR.SetProjExtState(0, "reaper-mcp-live", "k", "x" * 4000)
    value = RPR.GetProjExtState(0, "reaper-mcp-live", "k", "", 4100)
    assert value[0] and len(value[4]) == 4000
    RPR.SetProjExtState(0, "reaper-mcp-live", "k", "")
    assert RPR.GetProjExtState(0, "reaper-mcp-live", "k", "", 4100)[0] == 0


def test_snapshots_survive_saving_and_reopening_and_leave_no_stale_keys(
        add_tone, call, tmp_path, live_project):
    add_tone("a", 440)
    live_project.tracks[0].add_fx("ReaEQ")
    for name in ("A", "B", "C"):
        assert call("save_mix_snapshot", name=name)["success"] is True
    assert call("save_mix_snapshot", name="A", overwrite=True)["success"] is True
    assert call("delete_mix_snapshot", name="B")["success"] is True
    path = tmp_path / "snap.rpp"
    RPR.Main_SaveProjectEx(0, str(path), SAVE_AS_PROJECT_FILE)
    text = path.read_text()
    # REAPER writes ext state as <EXTSTATE <SECTION KEY value>>, upper-casing
    # section and key names (the values are kept; lookups ignore case).
    block = text[text.index("<EXTSTATE"):]
    block = block[block.index("<REAPER-MCP"):]
    block = block[:block.index("\n    >")]
    lines = [line.split(" ", 1) for line in (raw.strip() for raw in block.splitlines()[1:])]
    keys = {key for key, *_ in lines}
    print("ext keys in the .rpp:", sorted(keys))
    # Two snapshots, one list, the index key: no leftovers from B or old lists.
    assert len([k for k in keys if k.startswith("SNAP_") and k.endswith("#N")]) == 2
    assert len([k for k in keys if k.startswith("INDEX_") and k.endswith("#N")]) == 1
    assert "MIX_SNAPSHOTS" in keys
    assert max(len(value) for _, value in lines) <= 4000
    RPR.Main_openProject("noprompt:" + str(path))
    listed = call("list_mix_snapshots")
    assert [s["name"] for s in listed["snapshots"]] == ["C", "A"]


def test_saving_a_snapshot_marks_the_project_dirty_without_an_undo_point(
        add_tone, call, tmp_path, live_project):
    add_tone("a", 440)
    saved_clean(tmp_path)
    RPR.SetProjExtState(0, "reaper-mcp-live", "k", "v")
    print("dirty after SetProjExtState alone:", RPR.IsProjectDirty(0))
    RPR.SetProjExtState(0, "reaper-mcp-live", "k", "")
    saved_clean(tmp_path)
    step = next_undo()
    assert call("save_mix_snapshot", name="A")["success"] is True
    assert RPR.IsProjectDirty(0) == 1  # MarkProjectDirty: REAPER offers to save it
    assert next_undo() == step


def test_undo_of_a_recall_keeps_the_snapshots_and_restores_the_plugins(add_tone, call,
                                                                       live_project):
    add_tone("a", 440)
    track = live_project.tracks[0]
    for name in ("ReaEQ", "ReaComp"):
        call("add_fx", track_index=0, fx_name=name)
    assert call("save_mix_snapshot", name="A")["success"] is True
    track.set_info_value("D_VOL", 0.5)
    RPR.TrackFX_SetParamNormalized(track.id, 0, 0, 0.9)
    RPR.TrackFX_SetParamNormalized(track.id, 1, 0, 0.1)
    checkpoint()
    changed = [RPR.TrackFX_GetParamNormalized(track.id, f, 0) for f in (0, 1)]
    recalled = call("recall_mix_snapshot", name="A")
    assert recalled["success"] is True, recalled
    assert track.get_info_value("D_VOL") == pytest.approx(1.0)
    again = call("recall_mix_snapshot", name="A")
    print("second recall:", {k: v for k, v in again.items() if k != "tracks"})
    assert call("undo")["success"] is True  # the second recall (if it was a step)
    if next_undo() != "live test setup":
        assert call("undo")["success"] is True
    assert track.get_info_value("D_VOL") == pytest.approx(0.5)
    assert [RPR.TrackFX_GetParamNormalized(track.id, f, 0) for f in (0, 1)] == pytest.approx(
        changed)
    assert [s["name"] for s in call("list_mix_snapshots")["snapshots"]] == ["A"]


def test_a_compressor_working_on_a_tone_recalls_without_changes(add_tone, call, live_project):
    """Meter-like outputs (ReaComp's gain reduction) must not show up as changes."""
    add_tone("a", 440)
    call("add_fx", track_index=0, fx_name="ReaComp")
    call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name="Threshold",
         value="-40 dB")
    assert call("save_mix_snapshot", name="A")["success"] is True
    result = call("recall_mix_snapshot", name="A")
    print("recall right after saving:", result)
    compared = call("compare_mix_snapshots", a="A")
    assert compared["success"] is True
    assert all(not t.get("differences") for t in compared["tracks"]), compared


def test_snapshot_timing_with_a_big_cockos_plugin(add_tone, call, live_project):
    add_tone("a", 440)
    for _ in range(3):
        call("add_fx", track_index=0, fx_name="ReaXcomp")  # 51 parameters each
    times = {}
    for tool, args in (("save_mix_snapshot", {"name": "A"}), ("recall_mix_snapshot", {"name": "A"}),
                       ("compare_mix_snapshots", {"a": "A"})):
        start = time.monotonic()
        assert call(tool, **args)["success"] is True
        times[tool] = round(time.monotonic() - start, 2)
    print("3 x ReaXcomp (153 parameters):", times)


def test_solo_values_reaper_accepts(live_project):
    track = live_project.add_track(0, "s")
    accepted = {}
    for value in range(10):
        track.set_info_value("I_SOLO", value)
        accepted[value] = track.get_info_value("I_SOLO")
    track.set_info_value("I_SOLO", 0)
    print("I_SOLO set -> read:", accepted)


# The prompts' assumptions about REAPER's plug-ins


def test_the_vocal_chain_prompt_parameters_exist(live_project, call):
    track = live_project.add_track(0, "vox")
    call("add_fx", track_index=0, fx_name="ReaEQ")
    names = [RPR.TrackFX_GetParamName(track.id, 0, i, "", 256)[4]
             for i in range(RPR.TrackFX_GetNumParams(track.id, 0))]
    assert names[:2] == ["Freq-Low Shelf", "Gain-Low Shelf"]  # band 1 is a low shelf
    assert not any("type" in n.lower() for n in names)
    verb = call("add_fx", track_index=0, fx_name="ReaVerbate")
    assert verb["success"] is True, verb
    dry = call("set_fx_parameter_by_name", track_index=0, fx_index=1, param_name="Dry",
               value="-inf")
    print("ReaVerbate Dry at -inf:", dry)
    assert dry["success"] is True and dry["formatted_value"].startswith("-inf")


def test_delete_track_and_remove_fx_by_guid(live_project, call):
    track = live_project.add_track(0, "g")
    track.add_fx("ReaEQ")
    guid = RPR.GetSetMediaTrackInfo_String(track.id, "GUID", "", False)[3]
    removed = call("remove_fx", track_index=guid, fx_index=0)
    assert removed["success"] is True, removed
    assert track.n_fxs == 0
    deleted = call("delete_track", track_index=guid)
    assert deleted["success"] is True, deleted
    assert live_project.n_tracks == 0
