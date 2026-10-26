"""Mix snapshots (save, list, recall, compare, delete) against the fake REAPER,
whose SetProjExtState/GetProjExtState follow the API reference
(tests/fake_reaper/ext_state.py)."""

import pytest

from reaper_mcp import mix_snapshots
from reaper_mcp.mix_snapshots import EXT_NAME, INDEX_KEY, read_blob, write_blob
from reaper_mcp.units import db_to_linear
from tests.fake_reaper.objects import FXState


@pytest.fixture
def mix(reaper, call):
    reaper.add_track("Vocal")
    reaper.add_track("Guitar")
    call("add_fx", track_index=0, fx_name="ReaEQ")
    call("add_fx", track_index=0, fx_name="ReaComp")
    call("add_master_fx", fx_name="ReaLimit")
    reaper.dirty = False
    return reaper


def _change_everything(reaper):
    vocal = reaper.tracks[0]
    vocal.info.update({"D_VOL": db_to_linear(-6.0), "D_PAN": -0.5, "B_MUTE": 1.0, "I_SOLO": 2.0})
    vocal.fxs[1].values[0] = 0.2
    vocal.fxs[0].enabled = False
    reaper.master.fxs[0].values[1] = 0.9
    reaper.master.info["D_VOL"] = db_to_linear(-3.0)


def _state(reaper):
    return [(dict(t.info), [(f.plugin, f.enabled, list(f.values)) for f in t.fxs])
            for t in [*reaper.tracks, reaper.master]]


def test_save_stores_the_mix_in_the_project_and_marks_it_unsaved(mix, call):
    steps = len(mix.undo_steps)
    result = call("save_mix_snapshot", name="  before EQ ")
    assert result["success"] is True
    assert (result["name"], result["tracks"], result["replaced"]) == ("before EQ", 2, False)
    assert "save the project" in result["note"]
    assert mix.dirty is True
    assert len(mix.undo_steps) == steps  # not an undo step
    assert all(ext == EXT_NAME for ext, _ in mix.ext_state)
    assert call("list_mix_snapshots")["snapshots"] == [
        {"name": "before EQ", "saved_at": result["saved_at"], "tracks": 2}]


def test_recall_puts_volume_pan_mute_solo_and_fx_back(mix, call):
    before = _state(mix)
    call("save_mix_snapshot", name="A")
    _change_everything(mix)

    result = call("recall_mix_snapshot", name="a")  # names are matched case-insensitively
    assert result["success"] is True
    assert _state(mix) == before
    changed = {entry["track"]: entry["changed"] for entry in result["changed"]}
    assert changed["Vocal"][:4] == ["volume", "pan", "muted", "soloed"]
    assert "VST: ReaEQ (Cockos) on" in changed["Vocal"]
    assert "VST: ReaComp (Cockos): 1 parameter(s)" in changed["Vocal"]
    assert changed["master"] == ["volume", "VST: ReaLimit (Cockos): 1 parameter(s)"]
    assert "Guitar" not in changed
    assert result["skipped"] == [] and result["tracks_not_in_snapshot"] == []


def test_recall_is_one_undo_step_that_undo_takes_back(mix, call):
    call("save_mix_snapshot", name="A")
    _change_everything(mix)
    changed = _state(mix)
    call("recall_mix_snapshot", name="A")
    assert mix.undo_steps[-1][0] == "MCP: Recall mix snapshot (name=A, include_fx=True)"
    assert call("undo")["success"] is True
    assert _state(mix) == changed


def test_recall_without_fx_leaves_the_plugins_alone(mix, call):
    call("save_mix_snapshot", name="A")
    _change_everything(mix)
    call("recall_mix_snapshot", name="A", include_fx=False)
    assert mix.tracks[0].info["D_VOL"] == 1.0
    assert mix.tracks[0].fxs[1].values[0] == 0.2
    assert mix.tracks[0].fxs[0].enabled is False


def test_recall_finds_moved_tracks_and_fx_by_guid_and_name(mix, call):
    call("save_mix_snapshot", name="A")
    vocal = mix.tracks[0]
    vocal.info["D_VOL"] = 0.25
    vocal.fxs[1].values[2] = 0.9
    mix.tracks.reverse()  # the vocal is track 1 now
    vocal.fxs.insert(0, FXState("ReaGate", [0.5, 0.5]))  # and its FX moved down one

    result = call("recall_mix_snapshot", name="A")
    assert vocal.info["D_VOL"] == 1.0
    assert vocal.fxs[2].values[2] == 0.5
    assert vocal.fxs[0].values == [0.5, 0.5]  # the new FX is left alone
    assert result["changed"][0]["track_index"] == 1


def test_recall_reports_what_it_could_not_restore(mix, call, reaper):
    call("save_mix_snapshot", name="A")
    del mix.tracks[1].fxs[:]  # nothing to do there
    del mix.tracks[0].fxs[1]  # the compressor is gone
    mix.tracks.pop(1)  # and so is the guitar
    reaper.add_track("Keys")
    result = call("recall_mix_snapshot", name="A")
    assert result["success"] is True
    assert "Guitar: the track is no longer in the project" in result["skipped"]
    assert "Vocal: VST: ReaComp (Cockos) is no longer on the track" in result["skipped"]
    assert result["tracks_not_in_snapshot"] == ["Keys"]
    assert result["already_matching"] is True


def test_recall_of_another_projects_snapshot_changes_nothing(mix, call):
    call("save_mix_snapshot", name="A")
    mix.tracks.clear()
    mix.add_track("Other")
    mix.master.info["D_VOL"] = 0.5
    steps = len(mix.undo_steps)
    result = call("recall_mix_snapshot", name="A")
    assert result["success"] is False and "another project" in result["error"]
    assert mix.master.info["D_VOL"] == 0.5
    assert mix.undo_steps[-1][0].endswith("(failed)") or len(mix.undo_steps) == steps


def test_recall_unknown_snapshot(mix, call):
    call("save_mix_snapshot", name="A")
    result = call("recall_mix_snapshot", name="B")
    assert result["success"] is False
    assert "No mix snapshot called 'B'" in result["error"] and "['A']" in result["error"]


def test_save_refuses_an_existing_name_unless_overwrite(mix, call):
    call("save_mix_snapshot", name="A")
    mix.tracks[0].info["D_VOL"] = 0.5
    refused = call("save_mix_snapshot", name="a")
    assert refused["success"] is False and "overwrite=true" in refused["error"]
    replaced = call("save_mix_snapshot", name="a", overwrite=True)
    assert replaced["replaced"] is True
    assert [s["name"] for s in call("list_mix_snapshots")["snapshots"]] == ["a"]
    mix.tracks[0].info["D_VOL"] = 1.0
    call("recall_mix_snapshot", name="A")
    assert mix.tracks[0].info["D_VOL"] == 0.5


@pytest.mark.parametrize(("name", "error"), [
    ("  ", "needs a name"),
    ("x" * 65, "longer than 64"),
    ("Current", "reserved"),
])
def test_save_refuses_bad_names(mix, call, name, error):
    result = call("save_mix_snapshot", name=name)
    assert result["success"] is False and error in result["error"]
    assert mix.ext_state == {}


def test_save_refuses_past_the_limit(mix, call, monkeypatch):
    monkeypatch.setattr(mix_snapshots, "MAX_SNAPSHOTS", 1)
    monkeypatch.setattr("reaper_mcp.snapshot_tools.MAX_SNAPSHOTS", 1)
    call("save_mix_snapshot", name="A")
    result = call("save_mix_snapshot", name="B")
    assert result["success"] is False and "delete_mix_snapshot" in result["error"]


def test_delete(mix, call):
    call("save_mix_snapshot", name="A")
    call("save_mix_snapshot", name="B")
    mix.dirty = False
    assert call("delete_mix_snapshot", name="a") == {"success": True, "deleted": "A"}
    assert [s["name"] for s in call("list_mix_snapshots")["snapshots"]] == ["B"]
    assert mix.dirty is True
    assert call("recall_mix_snapshot", name="A")["success"] is False
    assert call("delete_mix_snapshot", name="A")["success"] is False
    call("delete_mix_snapshot", name="B")
    # Only the (empty) index is left, and what points at it.
    left = {key.split("#")[0] for _, key in mix.ext_state}
    assert left == {INDEX_KEY, mix.ext_state[(EXT_NAME, INDEX_KEY)]}
    assert read_blob(mix.ext_state[(EXT_NAME, INDEX_KEY)]) == []


def test_list_without_snapshots(reaper, call):
    assert call("list_mix_snapshots") == {"success": True, "snapshots": []}


def test_big_plugins_keep_only_their_on_off_state(mix, call, monkeypatch):
    monkeypatch.setattr(mix_snapshots, "MAX_PARAMS_PER_FX", 3)
    result = call("save_mix_snapshot", name="A")
    assert "ReaEQ" not in result["warning"]  # 3 parameters: saved
    assert "Vocal: VST: ReaComp (Cockos) (4 parameters)" in result["warning"]
    mix.tracks[0].fxs[1].values[0] = 0.1
    mix.tracks[0].fxs[1].enabled = False
    call("recall_mix_snapshot", name="A")
    assert mix.tracks[0].fxs[1].enabled is True
    assert mix.tracks[0].fxs[1].values[0] == 0.1


def test_long_values_are_split_into_chunks(reaper, monkeypatch):
    monkeypatch.setattr(mix_snapshots, "CHUNK", 10)
    monkeypatch.setattr(mix_snapshots, "READ_BUFFER", 11)
    value = {"text": "quote \" and ' and ` and\nnewline", "n": list(range(20))}
    write_blob("k", value)
    assert read_blob("k") == value
    assert all(len(v) <= 10 for v in reaper.ext_state.values())


def test_an_unreadable_snapshot_is_reported(mix, call):
    call("save_mix_snapshot", name="A")
    key = next(k for _, k in mix.ext_state if k.startswith("snap_") and k.endswith("#0"))
    mix.ext_state[(EXT_NAME, key)] = "not base64 json"
    result = call("recall_mix_snapshot", name="A")
    assert result["success"] is False and "could not be read back" in result["error"]


# Compare


def test_compare_with_the_current_mix_shows_values_as_the_plugin_displays_them(mix, call):
    call("save_mix_snapshot", name="A")
    _change_everything(mix)
    result = call("compare_mix_snapshots", a="A")
    assert (result["a"], result["b"], result["identical"]) == ("A", "current", False)
    rows = {t["track"]: t["differences"] for t in result["tracks"]}
    assert rows["Vocal"][:4] == [
        {"setting": "volume", "a": "+0.00 dB", "b": "-6.00 dB"},
        {"setting": "pan", "a": 0.0, "b": -0.5},
        {"setting": "muted", "a": False, "b": True},
        {"setting": "soloed", "a": False, "b": True},
    ]
    assert {"setting": "VST: ReaEQ (Cockos) on/off", "a": "on", "b": "bypassed"} in rows["Vocal"]
    assert {"setting": "VST: ReaComp (Cockos): Thresh", "a": "0.50", "b": "0.20"} in rows["Vocal"]
    # ReaLimit's ceiling has no unit in the fake's display.
    assert {"setting": "VST: ReaLimit (Cockos): Ceiling", "a": "0.50", "b": "0.90"} in rows["master"]
    assert "Guitar" not in rows


def test_compare_two_saved_snapshots_and_either_order(mix, call):
    call("save_mix_snapshot", name="A")
    mix.tracks[0].fxs[0].values[0] = 1.0  # ReaEQ Gain-Low: +24 dB in the fake
    call("save_mix_snapshot", name="B")
    mix.tracks[0].fxs[0].values[0] = 0.0
    saved = call("compare_mix_snapshots", a="A", b="B")
    [row] = saved["tracks"][0]["differences"]
    assert row == {"setting": "VST: ReaEQ (Cockos): Gain-Low", "a": 0.5, "b": 1.0}
    live_first = call("compare_mix_snapshots", a="current", b="A")
    [row] = live_first["tracks"][0]["differences"]
    assert row == {"setting": "VST: ReaEQ (Cockos): Gain-Low", "a": "-inf", "b": "+0.0 dB"}


def test_compare_reports_added_and_removed_tracks_and_fx(mix, call, reaper):
    call("save_mix_snapshot", name="A")
    mix.tracks.pop(1)
    reaper.add_track("Keys")
    del mix.tracks[0].fxs[0]
    mix.tracks[0].fxs.append(FXState("ReaGate", [0.5, 0.5]))
    result = call("compare_mix_snapshots", a="A")
    assert {"track": "Guitar", "only_in": "a"} in result["tracks"]
    assert {"track": "Keys", "only_in": "b"} in result["tracks"]
    vocal = next(t for t in result["tracks"] if t["track"] == "Vocal")["differences"]
    assert {"setting": "FX VST: ReaEQ (Cockos)", "a": "present", "b": "absent"} in vocal
    assert {"setting": "FX VST: ReaGate (Cockos)", "a": "absent", "b": "present"} in vocal


def test_compare_identical(mix, call):
    call("save_mix_snapshot", name="A")
    assert call("compare_mix_snapshots", a="A")["identical"] is True


def test_compare_unknown_snapshot(mix, call):
    result = call("compare_mix_snapshots", a="nope")
    assert result["success"] is False and "No mix snapshot" in result["error"]


def _snapshot_key(reaper, name):
    index = read_blob(reaper.ext_state[(EXT_NAME, INDEX_KEY)])
    return next(e["key"] for e in index if e["name"] == name)


def test_replacing_a_snapshot_writes_new_keys_and_removes_the_old_ones(mix, call):
    call("save_mix_snapshot", name="A")
    old_index, old_snap = mix.ext_state[(EXT_NAME, INDEX_KEY)], _snapshot_key(mix, "A")
    call("save_mix_snapshot", name="A", overwrite=True)
    assert mix.ext_state[(EXT_NAME, INDEX_KEY)] != old_index
    assert _snapshot_key(mix, "A") != old_snap
    gone = (old_index, old_snap)
    assert not any(key.split("#")[0] in gone for _, key in mix.ext_state)


def test_a_save_lost_part_way_keeps_the_earlier_snapshots(mix, call):
    call("save_mix_snapshot", name="A")
    mix.lose_request("SetProjExtState", applied=False)
    assert call("save_mix_snapshot", name="B")["success"] is False
    call("get_context")
    assert [s["name"] for s in call("list_mix_snapshots")["snapshots"]] == ["A"]


def test_an_unreadable_snapshot_list_is_an_error_not_an_empty_list(mix, call):
    call("save_mix_snapshot", name="A")
    index = mix.ext_state[(EXT_NAME, INDEX_KEY)]
    mix.ext_state[(EXT_NAME, f"{index}#0")] = "garbage"
    listed = call("list_mix_snapshots")
    assert listed["success"] is False and "could not be read back" in listed["error"]
    saved = call("save_mix_snapshot", name="B")
    assert saved["success"] is False
    assert mix.ext_state[(EXT_NAME, INDEX_KEY)] == index  # the list was not replaced


@pytest.mark.parametrize(("where", "value"), [
    ("volume", 1e6),  # +120 dB
    ("pan", 3.0),
    ("volume", "loud"),
])
def test_recall_refuses_a_snapshot_with_values_out_of_range(mix, call, where, value):
    call("save_mix_snapshot", name="A")
    key = _snapshot_key(mix, "A")
    snapshot = read_blob(key)
    snapshot["tracks"][0][where] = value
    write_blob(key, snapshot)
    mix.tracks[1].info["D_VOL"] = 0.5
    result = call("recall_mix_snapshot", name="A")
    assert result["success"] is False and "nothing was changed" in result["error"]
    assert mix.tracks[1].info["D_VOL"] == 0.5


def test_recall_refuses_a_parameter_out_of_range(mix, call):
    call("save_mix_snapshot", name="A")
    key = _snapshot_key(mix, "A")
    snapshot = read_blob(key)
    snapshot["tracks"][0]["fx"][0]["params"][0][1] = 7.0
    write_blob(key, snapshot)
    result = call("recall_mix_snapshot", name="A")
    assert result["success"] is False and "outside 0..1" in result["error"]


def test_recall_lists_fx_added_since_and_says_sends_are_not_restored(mix, call):
    call("save_mix_snapshot", name="before vocal chain")
    call("add_fx", track_index=1, fx_name="ReaGate")
    result = call("recall_mix_snapshot", name="before vocal chain")
    assert result["fx_not_in_snapshot"] == ["Guitar: VST: ReaGate (Cockos)"]
    assert "Sends are not part of a snapshot" in result["note"]
    assert len(mix.tracks[1].fxs) == 1  # left in place


def test_reapers_own_bypass_and_delta_parameters_are_not_settings(reaper, call):
    reaper.add_track("Vocal")
    call("add_fx", track_index=0, fx_name="ReaDelay")
    call("save_mix_snapshot", name="A")
    delay = reaper.tracks[0].fxs[0]
    delay.enabled = False
    delay.values[3] = 1.0  # what REAPER shows as the Bypass parameter
    delay.values[5] = 1.0  # Delta
    delay.values[4] = 0.2  # Wet is a real setting
    rows = call("compare_mix_snapshots", a="A")["tracks"][0]["differences"]
    assert [r["setting"] for r in rows] == ["VST: ReaDelay (Cockos) on/off",
                                           "VST: ReaDelay (Cockos): Wet"]
    result = call("recall_mix_snapshot", name="A")
    assert result["changed"][0]["changed"] == ["VST: ReaDelay (Cockos) on",
                                               "VST: ReaDelay (Cockos): 1 parameter(s)"]
    assert delay.values[5] == 1.0


def test_compare_sums_up_an_fx_with_many_changes_unless_detail(mix, call, monkeypatch):
    monkeypatch.setattr("reaper_mcp.snapshot_tools.MAX_PARAMS_LISTED", 2)
    call("save_mix_snapshot", name="A")
    mix.tracks[0].fxs[1].values[:] = [0.1, 0.2, 0.3, 0.4]
    [row] = call("compare_mix_snapshots", a="A")["tracks"][0]["differences"]
    assert row["b"].startswith("4 parameters differ")
    detailed = call("compare_mix_snapshots", a="A", detail=True)
    assert len(detailed["tracks"][0]["differences"]) == 4


def test_compare_lists_at_most_max_differences_rows(mix, call, monkeypatch):
    monkeypatch.setattr("reaper_mcp.snapshot_tools.MAX_DIFFERENCES", 3)
    call("save_mix_snapshot", name="A")
    _change_everything(mix)
    result = call("compare_mix_snapshots", a="A", detail=True)
    assert sum(len(t.get("differences", [])) for t in result["tracks"]) == 3
    assert "only the first 3 are listed" in result["note"]


def test_a_loud_fader_up_to_24_db_is_saved_and_recalled(mix, call):
    assert call("set_track_volume", track_index=0, volume_db=15)["success"] is True
    assert call("save_mix_snapshot", name="A")["success"] is True
    assert call("compare_mix_snapshots", a="A")["success"] is True
    mix.tracks[0].info["D_VOL"] = 1.0
    assert call("recall_mix_snapshot", name="A")["success"] is True
    assert mix.tracks[0].info["D_VOL"] == pytest.approx(db_to_linear(15.0))


def test_save_refuses_a_mix_it_could_never_recall(mix, call):
    call("set_track_volume", track_index=0, volume_db=30)
    mix.dirty = False
    result = call("save_mix_snapshot", name="A")
    assert result["success"] is False
    assert "Vocal: volume +30.0 dB is above +24 dB" in result["error"]
    assert "Nothing was saved" in result["error"]
    assert call("list_mix_snapshots")["snapshots"] == []
    assert mix.dirty is False
