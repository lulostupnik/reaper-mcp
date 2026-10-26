"""Marker and region tools against the fake REAPER, where (as in REAPER) a
marker's number is not its place in the list."""

import pytest


@pytest.fixture
def song(reaper):
    """Markers numbered out of timeline order (2 before 1, a gap at 3), and regions."""
    reaper.markers = [(1.0, "count-in"), (8.0, "Chorus")]
    reaper.AddRegionOrMarker(0, False, 0.5, 0.5, "start", 4, 0)  # number 4, first in time
    reaper.regions = [(0.0, 8.0, "Verse"), (8.0, 16.0, "Chorus")]
    return reaper


def _marker_names(reaper):
    return [(m.number, m.name, m.start) for m in reaper._ordered() if not m.is_region]


def test_list_markers(song, call):
    result = call("list_markers")
    assert [(m["number"], m["name"], m["position"]) for m in result["markers"]] == [
        (4, "start", 0.5), (1, "count-in", 1.0), (2, "Chorus", 8.0)]
    assert result["regions"][0] == {
        "kind": "region", "number": 1, "name": "Verse", "start": 0.0, "end": 8.0,
        "length": 8.0, "guid": song.project_markers[3].guid}
    assert song.undo_steps == []


def test_add_marker_gets_the_next_free_number(song, call):
    result = call("add_marker", position=12.0, name="Solo")
    assert result["success"] is True
    assert (result["kind"], result["number"], result["name"], result["position"]) == (
        "marker", 3, "Solo", 12.0)
    assert result["guid"].startswith("{")
    assert song.undo_steps[-1][0].startswith("MCP: Add marker")


def test_add_region(reaper, call):
    result = call("add_region", start=2.0, end=6.5, name="Bridge")
    assert {k: result[k] for k in ("kind", "number", "start", "end", "length")} == {
        "kind": "region", "number": 1, "start": 2.0, "end": 6.5, "length": 4.5}
    assert reaper.regions == [(2.0, 6.5, "Bridge")]


def test_add_region_and_marker_refuse_bad_times(reaper, call):
    assert call("add_region", start=5.0, end=5.0)["success"] is False
    assert call("add_marker", position=-1.0)["success"] is False
    assert reaper.project_markers == []


def test_rename_marker_by_number_is_not_by_position(song, call):
    result = call("rename_marker", marker=1, kind="marker", name="Intro")
    assert (result["success"], result["old_name"], result["name"]) == (True, "count-in", "Intro")
    assert _marker_names(song) == [(4, "start", 0.5), (1, "Intro", 1.0), (2, "Chorus", 8.0)]


def test_rename_marker_by_name_and_by_guid(song, call):
    assert call("rename_marker", marker="CHORUS", kind="marker", name="Hook")["number"] == 2
    guid = song.project_markers[2].guid
    assert call("rename_marker", marker=guid, name="Top")["number"] == 4
    assert [n for _, n, _ in _marker_names(song)] == ["Top", "count-in", "Hook"]


def test_rename_region_does_not_touch_the_marker_of_the_same_name(song, call):
    call("rename_marker", marker="Chorus", name="Chorus 1", kind="region")
    assert song.regions[1][2] == "Chorus 1"
    assert _marker_names(song)[2][1] == "Chorus"


def test_unknown_marker_lists_the_ones_there(song, call):
    result = call("rename_marker", marker=3, kind="marker", name="x")
    assert result == {"success": False, "error": "No marker 3. There are (kind number: name): "
                                                 "['marker 4: start', 'marker 1: count-in', "
                                                 "'marker 2: Chorus']"}
    # Nothing changed, so REAPER records no step (seen on 7.69), not even "(failed)".
    assert not any(name.endswith("(failed)") for name, _ in song.undo_steps)


def test_ambiguous_name_asks_for_the_guid(song, call):
    song.AddRegionOrMarker(0, False, 20.0, 20.0, "chorus", -1, 0)
    result = call("delete_marker", marker="Chorus", kind="marker")
    assert result["success"] is False
    assert "the GUID" in result["error"]
    assert len(_marker_names(song)) == 4


def test_a_region_is_found_without_kind(song, call):
    result = call("move_marker", marker="Verse", position=2.0)
    assert (result["kind"], result["start"], result["end"]) == ("region", 2.0, 10.0)


def test_a_name_on_a_marker_and_a_region_asks_for_kind(song, call):
    result = call("delete_marker", marker="Chorus")
    assert result["success"] is False
    assert "pass kind" in result["error"]
    assert len(song.project_markers) == 5


def test_the_wrong_kind_hints_at_the_other(song, call):
    result = call("go_to_marker", marker="Verse", kind="marker")
    assert result["success"] is False
    assert "A region matches it: pass kind='region'." in result["error"]


def test_digits_that_are_a_number_and_a_name_are_ambiguous(song, call):
    song.AddRegionOrMarker(0, False, 20.0, 20.0, "2", 5, 0)  # marker 5 named "2"
    result = call("delete_marker", marker="2", kind="marker")
    assert result["success"] is False
    assert "Several match '2'" in result["error"]
    assert len(_marker_names(song)) == 4
    assert call("rename_marker", marker=5, kind="marker", name="Solo")["name"] == "Solo"


def test_bad_kind(song, call):
    result = call("go_to_marker", marker=1, kind="item")
    assert result == {"success": False, "error": "kind must be 'marker' or 'region', not 'item'"}


def test_move_marker_keeps_its_number(song, call):
    result = call("move_marker", marker=4, position=10.0)  # no region 4: kind not needed
    assert (result["number"], result["position"]) == (4, 10.0)
    assert _marker_names(song) == [(1, "count-in", 1.0), (2, "Chorus", 8.0), (4, "start", 10.0)]


def test_move_region_keeps_its_length_or_takes_an_end(song, call):
    moved = call("move_marker", marker="Chorus", kind="region", position=20.0)
    assert (moved["start"], moved["end"]) == (20.0, 28.0)
    resized = call("move_marker", marker="Verse", kind="region", position=1.0, end=4.0)
    assert (resized["start"], resized["end"]) == (1.0, 4.0)
    assert song.regions == [(1.0, 4.0, "Verse"), (20.0, 28.0, "Chorus")]


def test_move_region_backwards_far(song, call):
    song.regions = [(30.0, 32.0, "Outro")]
    result = call("move_marker", marker=1, kind="region", position=2.0)
    assert (result["start"], result["end"]) == (2.0, 4.0)


def test_move_region_refuses_an_end_before_start(song, call):
    result = call("move_marker", marker="Verse", kind="region", position=5.0, end=4.0)
    assert result["success"] is False
    assert song.regions[0] == (0.0, 8.0, "Verse")


def test_move_marker_reports_a_neighbour_that_changed(song, call, monkeypatch):
    from reapy import reascript_api
    real = reascript_api.SetRegionOrMarkerInfo_Value
    verse = song.project_markers[3]

    def also_moves_the_verse(project, handle, param, value):
        verse.start = 0.25  # a stale handle hit another region too
        return real(project, handle, param, value)

    monkeypatch.setattr(reascript_api, "SetRegionOrMarkerInfo_Value", also_moves_the_verse)
    result = call("move_marker", marker="Chorus", kind="region", position=20.0)
    assert result["success"] is False
    assert "undo this step" in result["error"]


def test_delete_marker(song, call):
    result = call("delete_marker", marker=2, kind="marker")
    assert result["success"] is True
    assert result["deleted"]["name"] == "Chorus"
    assert _marker_names(song) == [(4, "start", 0.5), (1, "count-in", 1.0)]
    assert len(song.regions) == 2  # region 2 "Chorus" is not marker 2


def test_delete_region_and_undo(song, call):
    call("delete_marker", marker="Verse", kind="region")
    assert [r[2] for r in song.regions] == ["Chorus"]
    call("undo")
    assert [r[2] for r in song.regions] == ["Verse", "Chorus"]


def test_delete_refuses_shared_numbers(song, call):
    song.AddRegionOrMarker(0, False, 20.0, 20.0, "dup", 9, 0)
    song.project_markers[-1].number = 1
    guid = song.project_markers[-1].guid
    result = call("delete_marker", marker=guid)
    assert result["success"] is False
    assert "share number 1" in result["error"]
    assert len(_marker_names(song)) == 4


def test_go_to_marker_moves_the_cursor_without_an_undo_step(song, call):
    result = call("go_to_marker", marker="chorus", kind="marker")
    assert result == {"success": True, "position": 8.0, "kind": "marker", "number": 2,
                      "name": "Chorus"}
    assert song.cursor == 8.0
    assert call("go_to_marker", marker=2, kind="region")["position"] == 8.0
    assert song.undo_steps == []
