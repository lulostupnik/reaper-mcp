"""add_fx at a position, move_fx, copy_fx and list_fx_presets against the fake REAPER."""

from reapy import reascript_api

from tests.fake_reaper.fx import preset_values
from tests.fake_reaper.objects import FXState
from tests.helpers import assert_no_track


def _chain_track(reaper, name="Vox"):
    track = reaper.add_track(name)
    track.fxs.append(FXState("ReaEQ", [0.1, 0.2, 0.3]))
    track.fxs.append(FXState("ReaComp", [0.4, 0.5, 0.6, 0.7]))
    track.fxs.append(FXState("ReaLimit", [0.8, 0.9, 1.0]))
    return track


def _plugins(track):
    return [fx.plugin for fx in track.fxs]


# add_fx with a position


def test_add_fx_at_a_position(reaper, call):
    track = _chain_track(reaper)
    result = call("add_fx", track_index=0, fx_name="ReaGate", position=0)
    assert result == {"success": True, "fx_index": 0, "name": "VST: ReaGate (Cockos)",
                      "n_params": 2, "track_index": 0}
    assert _plugins(track) == ["ReaGate", "ReaEQ", "ReaComp", "ReaLimit"]


def test_add_fx_past_the_end_appends(reaper, call):
    track = _chain_track(reaper)
    assert call("add_fx", track_index=0, fx_name="ReaGate", position=99)["fx_index"] == 3
    assert _plugins(track)[-1] == "ReaGate"


def test_add_fx_negative_position_is_refused(reaper, call):
    track = _chain_track(reaper)
    result = call("add_fx", track_index=0, fx_name="ReaGate", position=-1)
    assert result["success"] is False
    assert len(track.fxs) == 3


def test_add_fx_at_a_position_missing_plugin(reaper, call):
    track = _chain_track(reaper)
    assert call("add_fx", track_index=0, fx_name="Nope", position=1)["success"] is False
    assert len(track.fxs) == 3


# move_fx


def test_move_fx_within_the_chain(reaper, call):
    track = _chain_track(reaper)
    result = call("move_fx", track_index=0, fx_index=2, to_position=0)
    assert result == {"success": True, "fx_name": "VST: ReaLimit (Cockos)", "fx_index": 0,
                      "chain": ["VST: ReaLimit (Cockos)", "VST: ReaEQ (Cockos)",
                                "VST: ReaComp (Cockos)"],
                      "source_track_index": 0, "to_track_index": 0}
    assert _plugins(track) == ["ReaLimit", "ReaEQ", "ReaComp"]


def test_move_fx_later_in_the_chain(reaper, call):
    track = _chain_track(reaper)
    assert call("move_fx", track_index=0, fx_index=0, to_position=2)["fx_index"] == 2
    assert _plugins(track) == ["ReaComp", "ReaLimit", "ReaEQ"]


def test_move_fx_to_another_track_keeps_its_settings(reaper, call):
    source = _chain_track(reaper)
    dest = reaper.add_track("Bus")
    result = call("move_fx", track_index=0, fx_index=1, to_track_index=1)
    assert (result["success"], result["source_track_index"], result["to_track_index"],
            result["fx_index"]) == (True, 0, 1, 0)
    assert _plugins(source) == ["ReaEQ", "ReaLimit"]
    assert [(fx.plugin, fx.values) for fx in dest.fxs] == [("ReaComp", [0.4, 0.5, 0.6, 0.7])]


def test_move_fx_needs_a_destination(reaper, call):
    _chain_track(reaper)
    result = call("move_fx", track_index=0, fx_index=1)
    assert result == {"success": False, "error": "Give to_position, to_track_index or both"}


def test_move_fx_bad_fx_and_bad_track(reaper, call):
    track = _chain_track(reaper)
    assert call("move_fx", track_index=0, fx_index=5, to_position=0)["success"] is False
    assert_no_track(call("move_fx", track_index=0, fx_index=0, to_track_index=4))
    assert _plugins(track) == ["ReaEQ", "ReaComp", "ReaLimit"]


def test_move_and_copy_fx_refuse_a_negative_fx_index(reaper, call):
    track = _chain_track(reaper)
    reaper.add_track("Bus")
    for tool, extra in (("move_fx", {"to_position": 0}), ("copy_fx", {"to_track_index": 1})):
        result = call(tool, track_index=0, fx_index=-1, **extra)
        assert (result["success"], result["error"]) == (
            False, "There is no FX -1 on that track: it has 3 (numbered from 0; "
                   "list_track_fx lists them)")
    assert _plugins(track) == ["ReaEQ", "ReaComp", "ReaLimit"]
    assert reaper.tracks[1].fxs == []


def test_move_fx_reports_a_chain_that_does_not_match(reaper, call, monkeypatch):
    track = _chain_track(reaper)
    monkeypatch.setattr(reascript_api, "TrackFX_CopyToTrack", lambda *a: None)
    result = call("move_fx", track_index=0, fx_index=2, to_position=0)
    assert result["success"] is False
    assert "undo this step" in result["error"]
    assert reaper.undo_steps[-1][0].endswith("(failed)")
    assert _plugins(track) == ["ReaEQ", "ReaComp", "ReaLimit"]


# copy_fx


def test_copy_fx_to_another_track(reaper, call):
    source = _chain_track(reaper)
    dest = _chain_track(reaper, "Bus")
    result = call("copy_fx", track_index=0, fx_index=0, to_track_index=1, to_position=1)
    assert (result["success"], result["fx_index"]) == (True, 1)
    assert _plugins(source) == ["ReaEQ", "ReaComp", "ReaLimit"]
    assert _plugins(dest) == ["ReaEQ", "ReaEQ", "ReaComp", "ReaLimit"]
    dest.fxs[1].values[0] = 0.99  # a copy, not the same plugin instance
    assert source.fxs[0].values[0] == 0.1


def test_copy_fx_on_the_same_track_duplicates_it(reaper, call):
    track = _chain_track(reaper)
    assert call("copy_fx", track_index=0, fx_index=1, to_track_index=0)["fx_index"] == 3
    assert _plugins(track) == ["ReaEQ", "ReaComp", "ReaLimit", "ReaComp"]


def test_copy_fx_is_one_undo_step(reaper, call):
    _chain_track(reaper)
    reaper.add_track("Bus")
    call("copy_fx", track_index=0, fx_index=0, to_track_index=1)
    assert call("undo")["success"] is True
    assert reaper.tracks[1].fxs == []


# list_fx_presets


def test_list_fx_presets_names_them_and_puts_the_plugin_back(reaper, call):
    track = _chain_track(reaper)
    result = call("list_fx_presets", track_index=0, fx_index=0)
    assert result == {"success": True, "track_index": 0, "fx_index": 0,
                      "fx_name": "VST: ReaEQ (Cockos)", "current_preset": "", "count": 2,
                      "presets": ["Bright", "Warm"], "settings_restored": True}
    assert track.fxs[0].values == [0.1, 0.2, 0.3]


def test_list_fx_presets_keeps_the_loaded_preset_and_tweaks(reaper, call):
    track = _chain_track(reaper)
    call("load_fx_preset", track_index=0, fx_index=0, preset_name="Bright")
    reaper.tracks[0].fxs[0].values[0] = 0.77  # tweaked after loading
    result = call("list_fx_presets", track_index=0, fx_index=0)
    assert result["current_preset"] == "Bright"
    state = reaper.tracks[0].fxs[0]
    assert state.preset == "Bright"
    assert state.values == [0.77, *preset_values(state, 0)[1:]]
    assert track.name == "Vox"


def test_list_fx_presets_reports_settings_it_could_not_restore(reaper, call, monkeypatch):
    _chain_track(reaper)
    monkeypatch.setattr(reascript_api, "TrackFX_SetParamNormalized", lambda *a: True)
    result = call("list_fx_presets", track_index=0, fx_index=0)
    assert result["settings_restored"] is False
    assert "use undo" in result["warning"]


def test_list_fx_presets_of_a_plugin_without_any(reaper, call):
    _chain_track(reaper)
    result = call("list_fx_presets", track_index=0, fx_index=2)
    assert (result["count"], result["presets"]) == (0, [])


# load_fx_preset by its place in the list


def test_load_fx_preset_by_index(reaper, call):
    _chain_track(reaper)
    result = call("load_fx_preset", track_index=0, fx_index=0, preset_index=1)
    assert (result["success"], result["preset"]) == (True, "Warm")
    state = reaper.tracks[0].fxs[0]
    assert state.values == preset_values(state, 1)


def test_load_fx_preset_by_a_bad_index_or_both(reaper, call):
    track = _chain_track(reaper)
    result = call("load_fx_preset", track_index=0, fx_index=0, preset_index=2)
    assert result == {"success": False, "error": "VST: ReaEQ (Cockos) has no preset 2: it has 2 "
                                                 "(numbered from 0)"}
    both = call("load_fx_preset", track_index=0, fx_index=0, preset_name="Warm", preset_index=0)
    assert both == {"success": False, "error": "Give either preset_name or preset_index"}
    assert track.fxs[0].values == [0.1, 0.2, 0.3]


def test_list_fx_presets_bad_fx(reaper, call):
    _chain_track(reaper)
    assert call("list_fx_presets", track_index=0, fx_index=-1)["success"] is False
