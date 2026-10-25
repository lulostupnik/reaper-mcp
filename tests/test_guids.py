"""The shared GUID-or-index resolver, against the fake REAPER."""

import pytest

from reaper_mcp.guids import item_guid, resolve_item, resolve_track, track_guid
from reaper_mcp.reaper import get_project
from tests.fake_reaper.objects import ItemState


def _tracks(reaper):
    return reaper.add_track("Vox"), reaper.add_track("Gtr")


def test_track_by_index(reaper):
    _, gtr = _tracks(reaper)
    assert resolve_track(get_project(), 1).id == gtr.pointer
    assert resolve_track(get_project(), " 1 ").id == gtr.pointer


def test_track_by_guid_in_any_case(reaper):
    _, gtr = _tracks(reaper)
    assert resolve_track(get_project(), gtr.guid).id == gtr.pointer
    assert resolve_track(get_project(), gtr.guid.lower()).id == gtr.pointer
    assert track_guid(resolve_track(get_project(), gtr.guid)) == gtr.guid


def test_track_guid_follows_the_track_when_it_moves(reaper):
    vox, gtr = _tracks(reaper)
    reaper.tracks.reverse()
    assert resolve_track(get_project(), gtr.guid).id == gtr.pointer
    assert resolve_track(get_project(), 1).id == vox.pointer


def test_stale_track_guid_is_an_error_not_another_track(reaper):
    vox, _ = _tracks(reaper)
    guid = vox.guid
    reaper.DeleteTrack(vox.pointer)
    with pytest.raises(LookupError, match=r"No track has GUID .*Call list_tracks"):
        resolve_track(get_project(), guid)


@pytest.mark.parametrize("ref", [2, -1, "7"])
def test_track_index_out_of_range(reaper, ref):
    _tracks(reaper)
    with pytest.raises(LookupError, match=r"there are 2 \(indices start at 0\)"):
        resolve_track(get_project(), ref)


@pytest.mark.parametrize("ref", ["Vox", "{not-a-guid}", True, 1.5])
def test_neither_index_nor_guid(reaper, ref):
    _tracks(reaper)
    with pytest.raises(LookupError, match="is neither a track index nor a track GUID"):
        resolve_track(get_project(), ref)


def test_item_by_index_and_guid(reaper):
    vox, _ = _tracks(reaper)
    vox.items += [ItemState(reaper.new_pointer("MediaItem"), t, 1.0, False) for t in (0.0, 2.0)]
    track = get_project().tracks[0]
    second = vox.items[1]
    assert resolve_item(track, 1).id == second.pointer
    assert resolve_item(track, second.guid).id == second.pointer
    assert item_guid(resolve_item(track, 0)) == vox.items[0].guid


def test_stale_item_guid(reaper):
    vox, gtr = _tracks(reaper)
    gtr.items.append(ItemState(reaper.new_pointer("MediaItem"), 0.0, 1.0, False))
    vox.items.append(ItemState(reaper.new_pointer("MediaItem"), 0.0, 1.0, False))
    # Another track's item is not this track's.
    with pytest.raises(LookupError, match=r"No item has GUID .*get_track_info"):
        resolve_item(get_project().tracks[0], gtr.items[0].guid)


def test_guids_the_tools_report_resolve_back(reaper, call):
    """A GUID from list_tracks names its track again: REAPER's "{...}" text, not
    the "(GUID*)0x..." address GetTrackGUID gives through the Python binding."""
    _tracks(reaper)
    listed = call("list_tracks")["tracks"]
    assert listed[1]["guid"].startswith("{")
    assert not reaper.GetTrackGUID(reaper.tracks[1].pointer).startswith("{")
    assert resolve_track(get_project(), listed[1]["guid"]).id == reaper.tracks[1].pointer


def test_destructive_tools_take_a_guid(reaper, call):
    vox, gtr = _tracks(reaper)
    gtr_guid = call("list_tracks")["tracks"][1]["guid"]
    call("create_track", name="Bass")  # the GUID still names Gtr after changes
    assert call("delete_track", track_index=gtr_guid) == {"success": True, "deleted_index": 1}
    assert [t.name for t in reaper.tracks] == ["Vox", "Bass"]
    result = call("delete_track", track_index=gtr_guid)
    assert result["success"] is False and "No track has GUID" in result["error"]
    assert [t.name for t in reaper.tracks] == ["Vox", "Bass"]


def test_remove_fx_and_send_take_a_guid(reaper, call):
    vox, gtr = _tracks(reaper)
    call("add_fx", track_index=0, fx_name="ReaEQ")
    call("create_send", source_track_index=0, dest_track_index=1)
    guid = call("list_tracks")["tracks"][0]["guid"]
    assert call("remove_fx", track_index=guid, fx_index=0)["track_index"] == 0
    assert call("remove_send", source_track_index=guid, send_index=0)["source_track_index"] == 0
    assert (vox.fxs, vox.sends) == ([], [])
