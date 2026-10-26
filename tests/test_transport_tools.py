"""transport_tools and actions against the fake REAPER: metronome and count-in,
time selection, loop and punch recording, and actions checked by name."""

import pytest
from reapy import reascript_api

from reaper_mcp import actions
from tests.fake_reaper import recording as fake_recording

TOGGLE_METRONOME = 40364


def _flags(reaper) -> int:
    return int(reaper.config["projmetroen"])


# set_metronome


def test_set_metronome_on_reads_first_so_a_toggle_never_inverts(reaper, call):
    result = call("set_metronome", enabled=True)
    assert (result["success"], result["metronome"], result["count_in"]) == (True, True, False)
    assert "Count-in before recording" in result["count_in_how_to_change"]
    assert call("set_metronome", enabled=True)["metronome"] is True
    assert reaper.commands == [TOGGLE_METRONOME]  # toggled once, not twice
    assert _flags(reaper) & 1


def test_set_metronome_off(reaper, call):
    reaper.config["projmetroen"] = "7"
    assert call("set_metronome", enabled=False)["metronome"] is False
    assert call("set_metronome", enabled=False)["metronome"] is False
    assert reaper.commands == [TOGGLE_METRONOME]


def test_set_metronome_without_arguments_only_reports(reaper, call):
    reaper.config["projmetroen"] = str(6 | 16)
    result = call("set_metronome")
    assert (result["metronome"], result["count_in"]) == (False, True)
    assert reaper.commands == []


def test_count_in_that_cannot_be_read_is_reported_as_unknown(reaper, call, monkeypatch):
    monkeypatch.setattr(reascript_api, "get_config_var_string",
                        lambda name, buf, size: [False, name, "", size])
    assert call("set_metronome")["count_in"] is None


def test_set_metronome_runs_under_a_translated_name(reaper, call, monkeypatch):
    """A LangPack translates action names; the metronome's ID is well known."""
    monkeypatch.setitem(fake_recording.ACTION_NAMES, TOGGLE_METRONOME,
                        "Opciones: Alternar metrónomo")
    assert call("set_metronome", enabled=True)["metronome"] is True
    assert reaper.commands == [TOGGLE_METRONOME]


def test_set_punch_mode_refuses_an_action_with_another_name(reaper, call, monkeypatch):
    monkeypatch.setitem(fake_recording.ACTION_NAMES, 40252,
                        "Options: Show metronome/pre-roll settings")
    reaper.punch_mode = "time_selection"
    result = call("set_punch_mode", mode="normal")
    assert result["success"] is False
    assert 'is "Options: Show metronome/pre-roll settings" in this REAPER' in result["error"]
    assert reaper.commands == []


def test_set_metronome_records_no_undo_step(reaper, call):
    call("set_metronome", enabled=True)
    assert reaper.undo_steps == []


# actions


def test_an_unnamed_or_renamed_action_runs_only_if_its_id_is_well_known(reaper, monkeypatch):
    monkeypatch.delitem(fake_recording.ACTION_NAMES, TOGGLE_METRONOME)
    actions.run(actions.TOGGLE_METRONOME)  # verified: REAPER could not name it, still run
    assert reaper.commands == [TOGGLE_METRONOME]
    unknown = actions.Action(TOGGLE_METRONOME, "Options: Toggle metronome", verified=False)
    with pytest.raises(RuntimeError, match="could not confirm"):
        actions.run(unknown)
    assert reaper.commands == [TOGGLE_METRONOME]


def test_is_on_refuses_an_action_without_a_state(reaper):
    with pytest.raises(RuntimeError, match="no on/off state"):
        actions.is_on(actions.TRANSPORT_RECORD)


# set_time_selection


def test_set_time_selection(reaper, call):
    reaper.loop_linked = False
    result = call("set_time_selection", start=4.0, end=12.0)
    assert result == {"success": True, "time_selection": {"start": 4.0, "end": 12.0},
                      "loop_range": None}
    assert reaper.time_selection == (4.0, 12.0)
    assert reaper.undo_steps == []


def test_set_time_selection_warns_when_it_moves_the_linked_loop(reaper, call):
    reaper.loop_range = (1.0, 2.0)
    result = call("set_time_selection", start=4.0, end=12.0)
    assert result["loop_range"] == {"start": 4.0, "end": 12.0}
    assert "also changed the loop range" in result["warning"]


def test_set_time_selection_equal_ends_clear_it(reaper, call):
    reaper.time_selection = (1.0, 2.0)
    assert call("set_time_selection", start=0.0, end=0.0)["time_selection"] is None


@pytest.mark.parametrize(("start", "end"), [(5.0, 2.0), (-1.0, 2.0)])
def test_set_time_selection_bad_range(reaper, call, start, end):
    result = call("set_time_selection", start=start, end=end)
    assert result["success"] is False and "need 0 <= start <= end" in result["error"]
    assert reaper.time_selection == (0.0, 0.0)


# set_loop


def test_set_loop_with_a_range(reaper, call):
    reaper.loop_linked = False
    result = call("set_loop", enabled=True, start=8.0, end=16.0)
    assert result == {"success": True, "repeat": True, "time_selection": None,
                      "loop_range": {"start": 8.0, "end": 16.0}}
    assert (reaper.repeat, reaper.loop_range) == (True, (8.0, 16.0))


def test_set_loop_warns_when_it_moves_the_linked_time_selection(reaper, call):
    """A punch time selection silently moving would change what gets recorded."""
    reaper.time_selection = reaper.loop_range = (4.0, 8.0)
    result = call("set_loop", enabled=True, start=8.0, end=16.0)
    assert result["time_selection"] == {"start": 8.0, "end": 16.0}
    assert "also changed the time selection" in result["warning"]


def test_set_loop_off_keeps_the_range(reaper, call):
    reaper.repeat, reaper.loop_range = True, (1.0, 3.0)
    result = call("set_loop", enabled=False)
    assert result == {"success": True, "repeat": False, "time_selection": None,
                      "loop_range": {"start": 1.0, "end": 3.0}}


def test_set_loop_on_needs_a_range(reaper, call):
    result = call("set_loop", enabled=True)
    assert result["success"] is False and "no loop range" in result["error"]
    assert reaper.repeat is False


@pytest.mark.parametrize("arguments", [{"start": 1.0}, {"end": 2.0}, {"start": 3.0, "end": 1.0},
                                       {"start": 3.0, "end": 3.0}])
def test_set_loop_bad_range_changes_nothing(reaper, call, arguments):
    reaper.loop_range = (1.0, 5.0)
    result = call("set_loop", enabled=True, **arguments)
    assert result["success"] is False
    assert (reaper.loop_range, reaper.repeat) == ((1.0, 5.0), False)


# set_punch_mode


def test_set_punch_mode_time_selection(reaper, call):
    reaper.time_selection = (4.0, 8.0)
    result = call("set_punch_mode", mode="time_selection")
    assert result == {"success": True, "mode": "time_selection", "confirmed": True,
                      "time_selection": {"start": 4.0, "end": 8.0}}
    assert (reaper.commands, reaper.punch_mode) == ([40076], "time_selection")


def test_set_punch_mode_time_selection_needs_one(reaper, call):
    result = call("set_punch_mode", mode="time_selection")
    assert result["success"] is False and "no time selection" in result["error"]
    assert reaper.commands == []


@pytest.mark.parametrize("previous", ["time_selection", "selected_items"])
def test_set_punch_mode_normal(reaper, call, previous):
    reaper.punch_mode = previous
    result = call("set_punch_mode", mode="normal")
    assert (result["success"], result["confirmed"]) == (True, True)
    assert (reaper.commands, reaper.punch_mode) == ([40252], "normal")


@pytest.mark.parametrize("mode", ["loop", "selected_items"])
def test_set_punch_mode_bad_mode(reaper, call, mode):
    result = call("set_punch_mode", mode=mode)
    assert result["success"] is False and repr(mode) in result["error"]
    assert reaper.commands == []


def test_set_punch_mode_reports_a_mode_that_did_not_change(reaper, call, monkeypatch):
    """The action ran, but REAPER still reports the old mode checked."""
    monkeypatch.setattr(reascript_api, "GetToggleCommandState", lambda command: int(command == 40253))
    result = call("set_punch_mode", mode="normal")
    assert result == {"success": False, "error": "REAPER's punch mode is still selected_items"}
