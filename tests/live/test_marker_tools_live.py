"""Marker and region tools against a running REAPER, read back through
REAPER's own marker API."""

import pytest

from tests.live.rpr import RPR

pytestmark = pytest.mark.live


def marks() -> list[tuple]:
    """(kind, number, name, start, end, guid) of every marker and region, in timeline order."""
    out = []
    for i in range(RPR.CountProjectMarkers(0, 0, 0)[0]):
        handle = RPR.GetRegionOrMarker(0, i, "")
        value = lambda param, h=handle: RPR.GetRegionOrMarkerInfo_Value(0, h, param)  # noqa: E731
        text = lambda param, h=handle: RPR.GetSetRegionOrMarkerInfo_String(  # noqa: E731
            0, h, param, "", False)[4]
        out.append(("region" if value("B_ISREGION") else "marker", int(value("I_NUMBER")),
                    text("P_NAME"), value("D_STARTPOS"), value("D_ENDPOS"), text("GUID")))
    return out


def test_add_marker_and_region_get_the_lowest_free_number(live_project, call):
    """REAPER 7.69 has no AddRegionOrMarker; AddProjectMarker2 numbers a new
    mark with the lowest free number of its kind."""
    first = call("add_marker", position=5.0, name="a")
    assert (first["success"], first["number"], first["position"]) == (True, 1, 5.0), first
    second = call("add_marker", position=1.0, name="b")
    assert second["number"] == 2
    third = call("add_marker", position=9.0, name="c")
    assert call("delete_marker", marker=2, kind="marker")["success"] is True
    again = call("add_marker", position=3.0, name="d")
    assert again["number"] == 2  # the gap, not 4
    region = call("add_region", start=2.0, end=4.0, name="Verse")
    assert (region["kind"], region["number"], region["start"], region["end"]) == (
        "region", 1, 2.0, 4.0)
    assert first["guid"].startswith("{") and first["guid"].endswith("}")
    assert [(m[0], m[1], m[2]) for m in marks()] == [
        ("region", 1, "Verse"), ("marker", 2, "d"), ("marker", 1, "a"), ("marker", 3, "c")]
    assert third["guid"] in [m[5] for m in marks()]


def test_list_markers_numbers_and_guids_match_reaper(live_project, call):
    RPR.AddProjectMarker2(0, False, 4.0, 0.0, "x", 7, 0)
    RPR.AddProjectMarker2(0, True, 1.0, 3.0, "r", 2, 0)
    listed = call("list_markers")
    assert listed["markers"][0]["number"] == 7
    assert listed["regions"][0]["number"] == 2
    assert {listed["markers"][0]["guid"], listed["regions"][0]["guid"]} == {m[5] for m in marks()}


def test_move_marker_keeps_number_and_guid_and_finds_it_by_guid(live_project, call):
    marker = call("add_marker", position=1.0, name="m")
    call("add_marker", position=6.0, name="other")
    moved = call("move_marker", marker=marker["guid"], position=8.0)
    assert moved["success"] is True, moved
    assert (moved["number"], moved["guid"], moved["position"]) == (1, marker["guid"], 8.0)
    assert [m[2] for m in marks()] == ["other", "m"]


def test_move_region_past_its_end_and_past_other_marks(live_project, call):
    """REAPER swaps a region's start and end when the start is set past the end,
    so the tool sets the end first; the handle is found again by GUID."""
    region = call("add_region", start=2.0, end=4.0, name="r")
    call("add_marker", position=5.0, name="between")
    moved = call("move_marker", marker="r", kind="region", position=6.0)
    assert moved["success"] is True, moved
    assert (moved["start"], moved["end"], moved["number"], moved["guid"]) == (
        6.0, 8.0, 1, region["guid"])
    back = call("move_marker", marker="r", kind="region", position=0.5, end=1.0)
    assert (back["start"], back["end"]) == (0.5, 1.0), back


def test_region_start_past_its_end_is_swapped_by_reaper(live_project):
    """The observation behind _set_bounds' order."""
    RPR.AddProjectMarker2(0, True, 2.0, 4.0, "r", -1, 0)
    handle = RPR.GetRegionOrMarker(0, 0, "")
    RPR.SetRegionOrMarkerInfo_Value(0, handle, "D_STARTPOS", 6.0)
    assert [(m[3], m[4]) for m in marks()] == [(4.0, 6.0)]


def test_delete_marker_by_number_keeps_the_region_of_that_number(live_project, call):
    call("add_marker", position=1.0, name="m1")
    call("add_marker", position=2.0, name="m2")
    call("add_region", start=0.0, end=1.0, name="r1")
    region2 = call("add_region", start=3.0, end=4.0, name="r2")
    marker2 = [m for m in marks() if m[:2] == ("marker", 2)][0]
    deleted = call("delete_marker", marker=2, kind="marker")
    assert deleted["success"] is True, deleted
    assert sorted((m[0], m[1]) for m in marks()) == [("marker", 1), ("region", 1), ("region", 2)]
    assert region2["guid"] in [m[5] for m in marks()]
    assert call("undo")["success"] is True
    assert marker2 in marks()  # same number, name, place and GUID


def test_each_marker_tool_is_one_undo_step(live_project, call):
    call("add_marker", position=1.0, name="a")
    region = call("add_region", start=2.0, end=3.0, name="r")
    before = marks()
    for tool, args in (("rename_marker", {"marker": "a", "name": "b"}),
                       ("move_marker", {"marker": region["guid"], "position": 5.0}),
                       ("delete_marker", {"marker": "r"}),
                       ("add_marker", {"position": 7.0})):
        assert call(tool, **args)["success"] is True, tool
        assert call("undo")["success"] is True
        assert marks() == before, tool


def test_go_to_marker_moves_the_cursor(live_project, call):
    call("add_region", start=2.5, end=3.0, name="Chorus")
    result = call("go_to_marker", marker="Chorus")
    assert result["position"] == pytest.approx(2.5)
    assert live_project.cursor_position == pytest.approx(2.5)
