"""get_context, get_project_summary and the project summary resource, against the fake REAPER."""

import json

import anyio
import pytest
from mcp.client import Client

from reaper_mcp.server import mcp
from tests.fake_reaper.objects import FXState, ItemState

# GetLastTouchedFX packs the item number in the high word of the track number.
ITEM_SHIFT = 16
INPUT_FX = 0x1000000


def test_get_context_of_a_stopped_empty_project(reaper, call):
    assert call("get_context") == {
        "success": True,
        "play_state": "stopped",
        "edit_cursor": 0.0,
        "play_position": 0.0,
        "time_selection": None,
        "loop_range": None,
        "repeat": False,
        "selected_tracks": [],
        "selected_items": [],
        "last_touched_fx": None,
        "unsaved_changes": False,
        "project_file": "",
    }


@pytest.mark.parametrize(("transport", "words"), [
    ("playing", "playing"), ("paused", "paused"), ("recording", "recording"),
])
def test_get_context_play_state(reaper, call, transport, words):
    reaper.transport = transport
    assert call("get_context")["play_state"] == words


def test_get_context_play_state_recording_paused(reaper, call, monkeypatch):
    from reapy import reascript_api

    monkeypatch.setattr(reascript_api, "GetPlayState", lambda: 6)
    assert call("get_context")["play_state"] == "recording (paused)"


def test_get_context_positions_and_ranges(reaper, call):
    reaper.cursor, reaper.play_position = 3.5, 7.25
    reaper.time_selection, reaper.loop_range = (1.0, 2.0), (4.0, 8.0)
    reaper.repeat = True
    result = call("get_context")
    assert (result["edit_cursor"], result["play_position"]) == (3.5, 7.25)
    assert result["time_selection"] == {"start": 1.0, "end": 2.0}
    assert result["loop_range"] == {"start": 4.0, "end": 8.0}
    assert result["repeat"] is True


def test_get_context_selection(reaper, call):
    reaper.add_track("Vox")
    gtr = reaper.add_track("Gtr")
    gtr.selected = True
    gtr.items += [ItemState(reaper.new_pointer("MediaItem"), 1.0, 2.0, False),
                  ItemState(reaper.new_pointer("MediaItem"), 4.0, 3.0, False, selected=True)]
    result = call("get_context")
    assert result["selected_tracks"] == [{"index": 1, "guid": gtr.guid, "name": "Gtr"}]
    assert result["selected_items"] == [{
        "guid": gtr.items[1].guid, "track_index": 1, "track_guid": gtr.guid,
        "item_index": 1, "position": 4.0, "length": 3.0,
    }]


def test_get_context_unsaved_changes_and_file(reaper, call, tmp_path):
    reaper.add_track("Vox")
    call("set_track_mute", track_index=0, muted=True)
    assert call("get_context")["unsaved_changes"] is True
    call("save_project", project_path=str(tmp_path / "song.rpp"))
    result = call("get_context")
    assert (result["unsaved_changes"], result["project_file"]) == (False, str(tmp_path / "song.rpp"))


def test_get_context_last_touched_track_fx(reaper, call):
    reaper.add_track("Vox")
    track = reaper.add_track("Gtr")
    track.fxs += [FXState("ReaEQ", [0.5] * 3), FXState("ReaComp", [0.5] * 4)]
    reaper.last_touched_fx = (2, 1, 3)  # track 2 (1-based), FX 1, parameter 3
    assert call("get_context")["last_touched_fx"] == {
        "on": "track", "track_index": 1, "fx_index": 1, "input_fx": False,
        "fx_name": "VST: ReaComp (Cockos)", "param_index": 3,
    }


def test_get_context_last_touched_master_fx(reaper, call):
    reaper.master.fxs.append(FXState("ReaLimit", [0.5] * 3))
    reaper.last_touched_fx = (0, 0, 2)
    assert call("get_context")["last_touched_fx"] == {
        "on": "master", "track_index": None, "fx_index": 0, "input_fx": False,
        "fx_name": "VST: ReaLimit (Cockos)", "param_index": 2,
    }


def test_get_context_last_touched_input_fx(reaper, call):
    reaper.add_track("Vox")
    reaper.last_touched_fx = (1, INPUT_FX | 0, 0)
    result = call("get_context")["last_touched_fx"]
    assert (result["fx_index"], result["input_fx"], result["fx_name"]) == (0, True, "")


def test_get_context_last_touched_item_fx(reaper, call):
    reaper.last_touched_fx = (2 << ITEM_SHIFT | 1, 1 << 16 | 3, 5)
    assert call("get_context")["last_touched_fx"] == {
        "on": "item", "track_index": 0, "item_index": 1, "take_index": 1, "fx_index": 3,
        "param_index": 5,
    }


def test_get_context_last_touched_fx_on_a_deleted_track(reaper, call):
    reaper.last_touched_fx = (3, 0, 0)
    assert call("get_context")["last_touched_fx"] is None


# get_project_summary


def _project(reaper):
    vox = reaper.add_track("Vox")
    vox.info.update({"D_VOL": 0.5, "I_RECARM": 1.0, "I_RECINPUT": 0.0, "I_RECMON": 1.0})
    vox.fxs.append(FXState("ReaComp", [0.5] * 4))
    gtr = reaper.add_track("Gtr")
    gtr.info.update({"I_RECINPUT": 1.0, "I_SOLO": 2.0})
    gtr.items.append(ItemState(reaper.new_pointer("MediaItem"), 0.0, 4.0, False))
    reverb = reaper.add_track("Reverb")
    reaper.CreateTrackSend(vox.pointer, reverb.pointer)
    reaper.master.fxs.append(FXState("ReaLimit", [0.5] * 3))
    reaper.markers = [(2.0, "verse")]
    reaper.regions = [(0.0, 8.0, "take 1")]
    return vox, gtr, reverb


def test_get_project_summary(reaper, call):
    vox, gtr, reverb = _project(reaper)
    result = call("get_project_summary")
    assert {k: v for k, v in result.items() if k != "tracks"} == {
        "success": True,
        "name": "Test Project",
        "project_file": "",
        "tempo": 120.0,
        "time_signature": "4/4",
        "length": 4.0,
        "master": {"volume_db": 0.0, "pan": 0.0, "muted": False, "soloed": False,
                   "fx": ["VST: ReaLimit (Cockos)"]},
        "markers": [{"index": 0, "name": "verse", "position": 2.0}],
        "regions": [{"index": 0, "name": "take 1", "start": 0.0, "end": 8.0}],
    }
    first, second, third = result["tracks"]
    assert first == {
        "index": 0, "guid": vox.guid, "name": "Vox", "volume_db": -6.02, "pan": 0.0,
        "muted": False, "soloed": False, "armed": True, "record_input": "mono input 1",
        "monitoring": "on", "fx": ["VST: ReaComp (Cockos)"],
        "sends": [{"send_index": 0, "dest_track_index": 2, "dest_track_guid": reverb.guid,
                   "dest_track_name": "Reverb", "volume_db": 0.0, "pan": 0.0, "muted": False}],
        "item_count": 0,
    }
    assert (second["guid"], second["record_input"], second["soloed"]) == (
        gtr.guid, "mono input 2", True)
    assert (second["armed"], second["monitoring"], second["item_count"]) == (False, "off", 1)
    assert (third["name"], third["sends"]) == ("Reverb", [])


def test_get_project_summary_empty(reaper, call):
    result = call("get_project_summary")
    assert (result["success"], result["tracks"]) == (True, [])


async def _read(uri: str):
    async with Client(mcp) as client:
        listed = (await client.list_resources()).resources
        read = await client.read_resource(uri)
    return listed, read


def test_project_summary_resource(reaper, call):
    _project(reaper)
    listed, read = anyio.run(_read, "reaper://project/summary")
    assert [str(r.uri) for r in listed] == ["reaper://project/summary"]
    [content] = read.contents
    assert json.loads(content.text) == call("get_project_summary")


def test_project_summary_resource_reports_failure_as_json(reaper, monkeypatch):
    from reapy import reascript_api

    def broken(*_):
        raise RuntimeError("REAPER went away")

    monkeypatch.setattr(reascript_api, "Master_GetTempo", broken)
    _, read = anyio.run(_read, "reaper://project/summary")
    assert json.loads(read.contents[0].text) == {"success": False, "error": "REAPER went away"}
