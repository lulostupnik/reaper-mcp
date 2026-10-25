"""The undo layer (every project change is one named undo step), the undo,
redo and get_undo_state tools, and the tool annotations, against the fake REAPER."""

import anyio
import pytest
from mcp.client import Client

from reaper_mcp import undo_tools
from reaper_mcp.server import mcp
from reaper_mcp.undo import kind_of, step_name

UNDO_STEP = "MCP: Set track volume (track_index=0, volume_db=-6)"


MUTE_STEP = "MCP: Set track mute (track_index=0, muted=True)"
PAN_STEP = "MCP: Set track pan (track_index=0, pan=0.5)"


def _steps(reaper) -> list[str]:
    return [name for name, _ in reaper.undo_steps]


def test_a_project_change_is_one_named_undo_step(reaper, call):
    track = reaper.add_track("Vox")
    call("set_track_volume", track_index=0, volume_db=-6.0)
    assert _steps(reaper) == [UNDO_STEP]
    assert track.info["D_VOL"] == pytest.approx(0.501, abs=1e-3)


def test_a_tool_with_several_changes_is_still_one_step(reaper, call):
    reaper.add_track("Kick")
    reaper.add_track("Snare")
    call("create_bus", name="Drums", track_indices=[0, 1])
    bus_step = "MCP: Create bus (name=Drums, track_indices=[0, 1])"
    assert _steps(reaper) == [bus_step]
    assert call("undo")["undone"] == bus_step
    assert [t.name for t in reaper.tracks] == ["Kick", "Snare"]
    assert all(t.sends == [] and "B_MAINSEND" not in t.info for t in reaper.tracks)


def test_a_read_only_tool_records_no_step(reaper, call):
    reaper.add_track("Vox")
    call("list_tracks")
    call("get_context")
    call("get_project_summary")
    assert _steps(reaper) == []
    assert reaper.dirty is False


def test_transport_records_no_step(reaper, call):
    call("play_project")
    call("set_cursor_position", position=2.0)
    call("stop_transport")
    assert _steps(reaper) == []


def test_a_bad_track_index_is_refused_before_any_undo_step(reaper, call):
    reaper.add_track("Vox")
    call("set_track_pan", track_index=0, pan=0.5)
    call("undo")
    result = call("set_track_volume", track_index=3, volume_db=-6.0)
    assert result == {"success": False,
                      "error": "No track 3: there are 1 (indices start at 0); nothing was changed"}
    assert _steps(reaper) == []
    assert reaper.redo_steps  # redo survives a call that could not do anything


def test_a_tool_failing_inside_its_block_names_the_step_failed(reaper, call):
    reaper.add_track("Vox")
    result = call("remove_fx", track_index=0, fx_index=2)
    assert result["success"] is False
    # The reaper fixture also checks the block and the UI-refresh hold were closed.
    assert _steps(reaper) == ["MCP: Remove fx (track_index=0, fx_index=2) (failed)"]
    undone = call("undo")
    assert undone["undone"] == "MCP: Remove fx (track_index=0, fx_index=2) (failed)"
    assert "failed" in undone["note"]


def test_a_tool_reporting_failure_names_the_step_failed(reaper, call):
    reaper.add_track("Vox")
    result = call("create_bus", name="Bus", track_indices=[0, 0])
    assert result["success"] is False
    assert _steps(reaper) == ["MCP: Create bus (name=Bus, track_indices=[0, 0]) (failed)"]


def test_long_arguments_are_shortened_in_the_step_name(reaper, call, tmp_path):
    reaper.add_track("Vox")
    long_name = "x" * 100
    call("rename_track", track_index=0, name=long_name)
    [step] = _steps(reaper)
    assert step == f"MCP: Rename track (track_index=0, name=...{'x' * 37})"


def test_the_undo_layer_connects_before_its_first_call(reaper, monkeypatch):
    from reaper_mcp import reaper as reaper_module

    monkeypatch.setattr(reaper_module, "_connected", False)
    reaper.add_track("Vox")
    from reaper_mcp.track_tools import set_track_volume
    from reaper_mcp.undo import as_undo_step

    assert as_undo_step(set_track_volume)(track_index=0, volume_db=0.0)["success"] is True
    assert reaper_module._connected is True


def test_step_names():
    assert step_name(undo_tools.get_undo_state) == "Get undo state"


# undo, redo, get_undo_state


def test_undo_reverts_the_last_step_and_says_which(reaper, call):
    track = reaper.add_track("Vox")
    call("set_track_mute", track_index=0, muted=True)
    result = call("undo")
    assert result == {"success": True, "undone": MUTE_STEP,
                      "next_undo": None, "next_redo": MUTE_STEP}
    assert reaper.tracks[0].info["B_MUTE"] == 0.0
    assert reaper.tracks[0].pointer == track.pointer


def test_redo_reapplies_it(reaper, call):
    reaper.add_track("Vox")
    call("set_track_mute", track_index=0, muted=True)
    call("undo")
    result = call("redo")
    assert result == {"success": True, "redone": MUTE_STEP,
                      "next_undo": MUTE_STEP, "next_redo": None}
    assert reaper.tracks[0].info["B_MUTE"] == 1.0


def test_undo_takes_back_a_deleted_track(reaper, call):
    reaper.add_track("Vox")
    reaper.add_track("Gtr")
    call("delete_track", track_index=0)
    call("undo")
    assert [t.name for t in reaper.tracks] == ["Vox", "Gtr"]


def test_undo_with_nothing_to_undo(reaper, call):
    assert call("undo") == {"success": False, "error": "Nothing to undo"}
    assert call("redo") == {"success": False, "error": "Nothing to redo"}


def test_undo_reports_a_refusal(reaper, call, monkeypatch):
    from reapy import reascript_api

    reaper.add_track("Vox")
    call("set_track_pan", track_index=0, pan=0.5)
    monkeypatch.setattr(reascript_api, "Undo_DoUndo2", lambda project: 0)
    assert call("undo") == {"success": False, "error": f"REAPER could not undo '{PAN_STEP}'"}


def test_redo_reports_a_refusal(reaper, call, monkeypatch):
    from reapy import reascript_api

    reaper.add_track("Vox")
    call("set_track_pan", track_index=0, pan=0.5)
    call("undo")
    monkeypatch.setattr(reascript_api, "Undo_DoRedo2", lambda project: 0)
    assert call("redo") == {"success": False, "error": f"REAPER could not redo '{PAN_STEP}'"}


def test_a_new_change_clears_redo(reaper, call):
    reaper.add_track("Vox")
    call("set_track_pan", track_index=0, pan=0.5)
    call("undo")
    call("set_track_pan", track_index=0, pan=-0.5)
    assert call("get_undo_state") == {"success": True,
                                      "next_undo": "MCP: Set track pan (track_index=0, pan=-0.5)",
                                      "next_redo": None}


def test_get_undo_state_with_an_empty_history(reaper, call):
    """REAPER's binding raises on the NULL "no step" answer (str(None.decode()));
    reapy hands that back as DistError, and it reads as null."""
    assert call("get_undo_state") == {"success": True, "next_undo": None, "next_redo": None}


def test_get_undo_state_reads_an_empty_string_as_null(reaper, call, monkeypatch):
    from reapy import reascript_api

    monkeypatch.setattr(reascript_api, "Undo_CanUndo2", lambda project: "")
    assert call("get_undo_state") == {"success": True, "next_undo": None, "next_redo": None}


def test_undo_refuses_a_step_made_by_hand(reaper, call):
    """REAPER's own steps (a recording, a hand edit) are only undone when asked."""
    reaper.add_track("Vox")
    reaper.Undo_BeginBlock2(0)
    reaper.tracks[0].info["B_MUTE"] = 1.0
    reaper.Undo_EndBlock2(0, "Recorded media", -1)
    result = call("undo")
    assert result["success"] is False
    assert "'Recorded media'" in result["error"] and "include_manual_steps" in result["error"]
    assert _steps(reaper) == ["Recorded media"]
    assert call("undo", include_manual_steps=True)["undone"] == "Recorded media"
    assert reaper.tracks[0].info["B_MUTE"] == 0.0
    assert call("redo")["success"] is False
    assert call("redo", include_manual_steps=True)["redone"] == "Recorded media"


def test_undo_refuses_while_recording(reaper, call):
    reaper.add_track("Vox")
    call("set_track_pan", track_index=0, pan=0.5)
    reaper.transport = "recording"
    result = call("undo")
    assert result["success"] is False
    assert "recording" in result["error"]
    assert _steps(reaper) == [PAN_STEP]


def test_undo_tools_are_not_wrapped_in_an_undo_step(reaper, call):
    reaper.add_track("Vox")
    call("set_track_pan", track_index=0, pan=0.5)
    call("undo")
    call("redo")
    assert _steps(reaper) == [PAN_STEP]


# Annotations


async def _tools() -> dict:
    async with Client(mcp) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}


def test_every_tool_is_annotated():
    tools = anyio.run(_tools)
    unannotated = [name for name, tool in tools.items() if tool.annotations is None]
    assert unannotated == []
    assert all(tool.annotations.open_world_hint is False for tool in tools.values())


@pytest.mark.parametrize(("name", "read_only", "destructive", "idempotent"), [
    ("list_tracks", True, None, None),
    ("get_context", True, None, None),
    ("get_project_summary", True, None, None),
    ("analyze_loudness", True, None, None),
    ("set_track_volume", False, False, True),
    ("create_track", False, False, False),
    ("delete_track", False, True, False),
    ("render_project", False, True, False),
    ("undo", False, True, False),
    ("play_project", False, False, True),
])
def test_annotations(name, read_only, destructive, idempotent):
    annotations = anyio.run(_tools)[name].annotations
    assert annotations.read_only_hint is read_only
    assert annotations.destructive_hint is destructive
    assert annotations.idempotent_hint is idempotent


def test_an_unmarked_tool_is_refused():
    def tool() -> dict:
        return {"success": True}

    with pytest.raises(TypeError, match="tool is not marked"):
        kind_of(tool)
