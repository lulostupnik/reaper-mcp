"""A tool whose request to REAPER timed out or lost its connection says the
outcome is uncertain and what to do next; it never reports success. An undo
block or render settings it left changed are put back at the next tool call
that reaches the same REAPER. Against the fake REAPER, which can lose a
request (tests/fake_reaper/transport.py)."""

import pytest
from reapy.errors import DistError

from reaper_mcp import reaper as reaper_module
from reaper_mcp import server
from reaper_mcp.reaper import RPR
from reaper_mcp.undo import FAILED_SUFFIX

BUS_STEP = "MCP: Create bus (name=Drums, track_indices=[0, 1])"
CLOSING = ["PreventUIRefresh", "Undo_EndBlock2", "TrackList_AdjustWindows", "UpdateArrange"]


@pytest.fixture
def two_tracks(reaper):
    reaper.add_track("Kick")
    reaper.add_track("Snare")
    return reaper


def _assert_uncertain(result: dict, function: str) -> None:
    assert result["success"] is False
    assert result["outcome"] == "uncertain"
    assert f"during {function}." in result["error"]
    assert "unknown whether this tool's change was made" in result["next_step"]
    assert "get_context" in result["next_step"]


def _settle(call) -> None:
    """The next tool call, which closes the undo block the lost one left open."""
    assert call("get_context")["success"] is True


def _create_bus(call) -> dict:
    return call("create_bus", name="Drums", track_indices=[0, 1])


@pytest.mark.parametrize("applied", [False, True])
def test_a_request_with_no_answer_is_uncertain_whether_or_not_it_ran(two_tracks, call, applied):
    two_tracks.lose_request("CreateTrackSend", applied=applied)
    result = _create_bus(call)

    _assert_uncertain(result, "CreateTrackSend")
    assert "did not answer within 60 s" in result["error"]
    assert "dialog" in result["next_step"]
    assert "may still finish it" in result["next_step"]
    assert len(two_tracks.tracks[0].sends) == (1 if applied else 0)
    # Nothing more was sent once the request was lost.
    assert two_tracks.requests[-1] == "CreateTrackSend"
    _settle(call)


def test_a_dropped_connection_is_uncertain(two_tracks, call):
    two_tracks.lose_request("CreateTrackSend", applied=True, error=ConnectionResetError)
    result = _create_bus(call)
    _assert_uncertain(result, "CreateTrackSend")
    assert "lost the connection" in result["error"]
    assert "closed or crashed" in result["next_step"]
    _settle(call)


def test_the_next_tool_call_reconnects_and_closes_the_open_undo_block(two_tracks, call):
    two_tracks.lose_request("CreateTrackSend", applied=True)
    _create_bus(call)
    assert two_tracks.undo_depth == 1  # REAPER never heard the end of the block

    context = call("get_context")
    assert context["success"] is True
    assert two_tracks.undo_depth == 0
    assert two_tracks.ui_refresh_prevented == 0
    assert [name for name, _ in two_tracks.undo_steps] == [BUS_STEP + FAILED_SUFFIX]
    # The step is still undoable as one.
    assert call("undo")["success"] is True
    assert all(t.sends == [] for t in two_tracks.tracks)


@pytest.mark.parametrize(("function", "skip", "applied"), [
    ("Undo_BeginBlock2", 0, True),
    ("PreventUIRefresh", 0, True),   # PreventUIRefresh(1), opening
    ("PreventUIRefresh", 1, True),   # PreventUIRefresh(-1), closing
    ("Undo_EndBlock2", 0, True),
    ("TrackList_AdjustWindows", 0, False),
])
def test_a_call_lost_while_opening_or_closing_the_block_never_leaves_it_open(
        two_tracks, call, function, skip, applied):
    two_tracks.lose_request(function, applied=applied, skip=skip)
    _assert_uncertain(_create_bus(call), function)
    _settle(call)
    assert two_tracks.undo_depth == 0
    assert two_tracks.ui_refresh_prevented == 0


def test_a_restarted_reaper_is_not_sent_the_old_ones_closing_calls(two_tracks, call):
    two_tracks.lose_request("CreateTrackSend", applied=True, error=ConnectionResetError)
    _create_bus(call)
    two_tracks.bridge_instance = "fake-reaper-2"  # REAPER quit and was started again
    sent = len(two_tracks.requests)

    _settle(call)
    assert not set(two_tracks.requests[sent:]) & {"Undo_EndBlock2", "PreventUIRefresh"}
    assert reaper_module._owed == []
    # A real restart would have discarded the open block with the old REAPER.
    two_tracks.PreventUIRefresh(-1)
    two_tracks.Undo_EndBlock2(0, "restart", -1)


def test_an_owed_call_that_fails_does_not_make_the_next_tool_uncertain(two_tracks, call):
    two_tracks.lose_request("CreateTrackSend", applied=True)
    _create_bus(call)
    two_tracks.lose_request("PreventUIRefresh", applied=True)  # the first owed call

    result = call("get_context")
    assert result["success"] is False
    assert "outcome" not in result
    assert "Nothing was sent for this tool" in result["error"]
    assert "dialog" in result["error"]
    # The calls still owed run at the next tool call.
    _settle(call)
    assert two_tracks.undo_depth == 0
    assert two_tracks.ui_refresh_prevented == 0


def test_a_read_only_tool_that_loses_a_request_says_it_changed_nothing(reaper, call):
    reaper.lose_request("GetPlayState", applied=True)
    result = call("get_context")
    assert result["outcome"] == "uncertain"
    assert result["next_step"].startswith("This tool changes nothing.")
    assert "unknown whether" not in result["next_step"]
    assert call("get_context")["success"] is True


def test_an_analysis_whose_render_is_lost_puts_the_render_settings_back_later(reaper, call):
    mine = {"RENDER_FILE": "/home/me/mixes", "RENDER_PATTERN": "$project-final",
            "RENDER_FORMAT": "bDNwbQ==", "RENDER_SRATE": 44100.0, "RENDER_CHANNELS": 1.0,
            "RENDER_BOUNDSFLAG": 2.0, "RENDER_ADDTOPROJ": 1.0,
            "RENDER_SETTINGS": 130.0, "RENDER_NORMALIZE": 65.0, "RENDER_TAILFLAG": 6.0}
    reaper.project_info.update(mine)
    reaper.lose_request("Main_OnCommand", applied=True)

    assert call("analyze_loudness")["outcome"] == "uncertain"
    assert reaper.project_info != mine  # nothing could be sent after the loss
    _settle(call)
    assert reaper.project_info == mine


def test_an_owed_restore_whose_call_is_lost_again_waits_for_the_next_tool_call(reaper, call):
    """The owed restore catches its own lost call and owes itself again:
    paying must stop there instead of retrying it forever."""
    mine = {"RENDER_FILE": "/home/me/mixes", "RENDER_PATTERN": "$project-final"}
    reaper.project_info.update(mine)
    reaper.lose_request("Main_OnCommand", applied=True)
    assert call("analyze_loudness")["outcome"] == "uncertain"
    reaper.lose_request("GetSetProjectInfo_String", applied=False)

    result = call("get_context")  # must return, not hang
    assert result["success"] is False
    assert "outcome" not in result
    assert "Nothing was sent for this tool" in result["error"]
    _settle(call)
    assert {key: reaper.project_info[key] for key in mine} == mine


def test_a_tool_that_swallows_the_lost_call_still_cannot_report_success(reaper):
    def careless() -> dict:
        try:
            RPR.GetPlayState()
        except Exception:
            pass
        return {"success": True, "track_index": 3}

    reaper.lose_request("GetPlayState", applied=False)
    result = server._serialized(careless)()
    _assert_uncertain(result, "GetPlayState")
    assert result["partial_result"] == {"success": True, "track_index": 3}


def test_a_lost_call_does_not_leak_into_the_next_tool_call(reaper):
    def reads() -> dict:
        return {"success": True, "state": RPR.GetPlayState()}

    reaper.lose_request("GetPlayState", applied=False)
    assert server._serialized(reads)()["outcome"] == "uncertain"
    assert server._serialized(reads)() == {"success": True, "state": 0}
    assert reaper_module.lost_call() is None


def test_an_error_inside_the_fake_is_a_reaper_error_not_a_lost_request(reaper):
    """The fake raising OSError (say, a missing file) is a bug to see, not 'uncertain'."""
    def broken() -> None:
        raise FileNotFoundError("no such template")

    with pytest.raises(DistError, match="no such template"):
        reaper.deliver("Main_openProject", broken, ())
