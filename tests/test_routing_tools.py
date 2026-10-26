"""Send settings and folder tools against the fake REAPER."""

import pytest

from reaper_mcp.routing_tools import depths_for, parents
from tests.fake_reaper.objects import Send
from tests.helpers import assert_no_track

# edit_send


@pytest.fixture
def sent(reaper):
    vox = reaper.add_track("Vox")
    verb = reaper.add_track("Verb")
    vox.sends.append(Send(verb.pointer))
    return vox


def test_edit_send_pan_mute_and_mode(reaper, call, sent):
    result = call("edit_send", source_track_index=0, send_index=0, pan=-0.5, muted=True,
                  mode="pre_fader")
    assert result == {"success": True, "source_track_index": 0, "send_index": 0,
                      "dest_track_index": 1, "dest_track_guid": reaper.tracks[1].guid,
                      "dest_track_name": "Verb", "volume_db": 0.0, "pan": -0.5, "muted": True,
                      "mode": "pre_fader"}
    send = sent.sends[0]
    assert (send.pan, send.muted, send.mode) == (-0.5, True, 3)


def test_edit_send_changes_only_what_is_given(reaper, call, sent):
    sent.sends[0].pan = 0.25
    result = call("edit_send", source_track_index=0, send_index=0, volume_db=-6.0)
    assert (result["volume_db"], result["pan"], result["mode"]) == (-6.0, 0.25, "post_fader")


@pytest.mark.parametrize(("arguments", "error"), [
    ({}, "Nothing to change: give volume_db, pan, muted or mode"),
    ({"pan": 2.0}, "pan must be from -1 (left) to 1 (right)"),
    ({"mode": "sideways"}, "mode must be one of ['post_fader', 'pre_fx', 'pre_fader']"),
    ({"send_index": 3, "muted": True}, "track 0 has no send 3"),
])
def test_edit_send_refusals(reaper, call, sent, arguments, error):
    result = call("edit_send", **{"source_track_index": 0, "send_index": 0, **arguments})
    assert result == {"success": False, "error": error}
    assert sent.sends == [Send(reaper.tracks[1].pointer)]


def test_list_sends_still_reports_mute(reaper, call, sent):
    call("edit_send", source_track_index=0, send_index=0, muted=True)
    assert call("list_sends", track_index=0)["sends"][0]["muted"] is True


# folder structure helpers


def test_parents_and_depths_round_trip():
    depths = [1, 0, 1, -2, 0]  # A( B C(D) ) E
    assert parents(depths) == [None, 0, 0, 2, None]
    assert depths_for([0, 1, 1, 2, 0]) == depths
    assert sum(depths_for([0, 1, 2, 3])) == 0


def test_parents_tolerates_too_many_closes():
    assert parents([0, -3, 1, 0]) == [None, None, None, 2]


# folders


def _tracks(reaper, *spec):
    """spec: (name, depth) per track."""
    for name, depth in spec:
        reaper.add_track(name).info["I_FOLDERDEPTH"] = float(depth)


def _layout(reaper):
    return [(t.name, int(t.value("I_FOLDERDEPTH"))) for t in reaper.tracks]


def test_move_track_into_a_folder_at_the_end(reaper, call):
    _tracks(reaper, ("Drums", 1), ("Kick", 0), ("Snare", -1), ("Vox", 0), ("Bass", 0))
    result = call("move_track_into_folder", track_index=4, folder_track_index=0)
    assert result["success"] is True
    assert _layout(reaper) == [("Drums", 1), ("Kick", 0), ("Snare", 0), ("Bass", -1), ("Vox", 0)]
    assert result["track"] == {"index": 3, "name": "Bass", "guid": reaper.tracks[3].guid,
                               "level": 1, "parent_index": 0, "is_folder": False}


def test_move_track_into_a_normal_track_needs_allow_new_folder(reaper, call):
    _tracks(reaper, ("Gtr", 0), ("Vox", 0), ("Vox double", 0))
    refused = call("move_track_into_folder", track_index=2, folder_track_index=1)
    assert refused["success"] is False
    assert "Track 1 ('Vox') is not a folder" in refused["error"]
    assert _layout(reaper) == [("Gtr", 0), ("Vox", 0), ("Vox double", 0)]
    result = call("move_track_into_folder", track_index=2, folder_track_index=1,
                  allow_new_folder=True)
    assert result["note"] == "Track 1 is now a folder"
    assert _layout(reaper) == [("Gtr", 0), ("Vox", 1), ("Vox double", -1)]
    assert sum(d for _, d in _layout(reaper)) == 0


def test_move_track_earlier_into_a_folder_moves_it(reaper, call):
    _tracks(reaper, ("Click", 0), ("Gtrs", 1), ("Gtr L", -1))
    call("move_track_into_folder", track_index=0, folder_track_index=1)
    assert _layout(reaper) == [("Gtrs", 1), ("Gtr L", 0), ("Click", -1)]


def test_move_a_folder_with_its_contents_into_another(reaper, call):
    _tracks(reaper, ("Band", 1), ("Bass", -1), ("Drums", 1), ("Kick", -1), ("Vox", 0))
    call("move_track_into_folder", track_index=2, folder_track_index=0)
    assert _layout(reaper) == [("Band", 1), ("Bass", 0), ("Drums", 1), ("Kick", -2), ("Vox", 0)]


def test_move_track_into_itself_or_its_child_is_refused(reaper, call):
    _tracks(reaper, ("Drums", 1), ("Kick", -1))
    assert call("move_track_into_folder", track_index=0, folder_track_index=1)["success"] is False
    assert call("move_track_into_folder", track_index=0, folder_track_index=0)["success"] is False
    assert _layout(reaper) == [("Drums", 1), ("Kick", -1)]


def test_move_track_into_folder_bad_track(reaper, call):
    _tracks(reaper, ("Drums", 0))
    assert_no_track(call("move_track_into_folder", track_index=0, folder_track_index=3))


def test_move_track_out_of_folder_goes_after_it(reaper, call):
    _tracks(reaper, ("Drums", 1), ("Kick", 0), ("Snare", -1), ("Vox", 0))
    result = call("move_track_out_of_folder", track_index=1)
    assert result["success"] is True
    assert _layout(reaper) == [("Drums", 1), ("Snare", -1), ("Kick", 0), ("Vox", 0)]
    assert result["track"]["parent_index"] is None


def test_move_last_track_out_of_folder_empties_it(reaper, call):
    _tracks(reaper, ("Drums", 1), ("Kick", -1), ("Vox", 0))
    call("move_track_out_of_folder", track_index=1)
    assert _layout(reaper) == [("Drums", 0), ("Kick", 0), ("Vox", 0)]


def test_move_out_of_a_nested_folder_goes_up_one_level(reaper, call):
    _tracks(reaper, ("Band", 1), ("Drums", 1), ("Kick", 0), ("Snare", -2))
    call("move_track_out_of_folder", track_index=2)
    assert _layout(reaper) == [("Band", 1), ("Drums", 1), ("Snare", -1), ("Kick", -1)]


def test_move_out_of_folder_refuses_a_top_level_track(reaper, call):
    _tracks(reaper, ("Vox", 0))
    result = call("move_track_out_of_folder", track_index=0)
    assert result == {"success": False, "error": "Track 0 is not in a folder"}


def test_folder_moves_keep_the_selection_and_undo(reaper, call):
    _tracks(reaper, ("Drums", 1), ("Kick", -1), ("Vox", 0))
    reaper.tracks[1].selected = True
    call("move_track_into_folder", track_index=2, folder_track_index=0)
    assert [t.name for t in reaper.tracks if t.selected] == ["Kick"]
    call("undo")
    assert _layout(reaper) == [("Drums", 1), ("Kick", -1), ("Vox", 0)]


def test_folder_move_reports_a_result_reaper_did_not_produce(reaper, call, monkeypatch):
    from reapy import reascript_api

    _tracks(reaper, ("Drums", 1), ("Kick", -1), ("Vox", 0), ("Bass", 0))
    monkeypatch.setattr(reascript_api, "ReorderSelectedTracks", lambda *a: True)
    result = call("move_track_into_folder", track_index=3, folder_track_index=0)
    assert result["success"] is False
    assert "undo this step" in result["error"]


def test_get_folder_structure(reaper, call):
    _tracks(reaper, ("Drums", 1), ("Kick", -1), ("Vox", 0))
    result = call("get_folder_structure")
    rows = [(t["name"], t["level"], t["parent_index"], t["is_folder"]) for t in result["tracks"]]
    assert rows == [("Drums", 0, None, True), ("Kick", 1, 0, False), ("Vox", 0, None, False)]
