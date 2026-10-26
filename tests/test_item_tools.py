"""list_items, edit_item, split_item, delete_item and move_item against the fake REAPER."""

import pytest

from tests.fake_reaper.item_state import TakeState
from tests.fake_reaper.objects import ItemState
from tests.helpers import assert_no_track


def _item(reaper, track, position=1.0, length=4.0, **kwargs):
    state = ItemState(reaper.new_pointer("MediaItem"), position, length, midi=False, **kwargs)
    track.items.append(state)
    track.items.sort(key=lambda i: i.position)
    return state


# list_items


def test_list_items_reports_every_setting(reaper, call, tmp_path):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, source_file=str(tmp_path / "vox.wav"),
                 info={"D_FADEINLEN": 0.5, "D_FADEOUTLEN": 0.25, "C_FADEOUTSHAPE": 2.0,
                       "D_VOL": 0.5, "B_MUTE": 1.0, "C_LOCK": 1.0})
    item.take_info.update({"D_STARTOFFS": 1.5, "D_PLAYRATE": 2.0, "D_VOL": -2.0})
    item.take.name = "vox take"
    item.extra_takes = 1
    item.active = 0
    item.info["D_FADEOUTLEN_AUTO"] = 0.05
    compact = {"guid": item.guid, "track_index": 0, "item_index": 0, "position": 1.0,
               "length": 4.0, "muted": True, "locked": True, "take_count": 2,
               "active_take": "vox take", "midi": False}
    result = call("list_items")
    assert result["success"] is True and result["count"] == 1
    assert result["items"] == [compact]
    [listed] = call("list_items", detail=True)["items"]
    assert listed == {
        **compact, "fade_in": 0.5, "fade_out": 0.25, "auto_fade_in": 0.0,
        "auto_fade_out": 0.05, "volume_db": -6.02, "start_offset": 1.5, "playrate": 2.0,
        "take_volume_db": 6.02, "source_file": str(tmp_path / "vox.wav"),
    }


def test_list_items_per_track_and_empty_item(reaper, call):
    first, second = reaper.add_track("A"), reaper.add_track("B")
    _item(reaper, first)
    empty = _item(reaper, second, position=0.0, has_take=False)
    result = call("list_items", track_index=1)
    [listed] = result["items"]
    assert (listed["guid"], listed["track_index"], listed["take_count"]) == (empty.guid, 1, 0)
    assert listed["active_take"] is None
    [detailed] = call("list_items", track_index=1, detail=True)["items"]
    assert detailed["source_file"] is None
    assert call("list_items")["count"] == 2


def test_list_items_midi(reaper, call):
    track = reaper.add_track("Keys")
    track.items.append(ItemState(reaper.new_pointer("MediaItem"), 0.0, 2.0, midi=True))
    [listed] = call("list_items", detail=True)["items"]
    assert (listed["midi"], listed["source_file"]) == (True, None)


def test_list_items_reports_fixed_lanes(reaper, call):
    track = reaper.add_track("Lanes")
    track.info["I_FREEMODE"] = 2.0
    _item(reaper, track, info={"I_FIXEDLANE": 1.0, "C_LANEPLAYS": 1.0})
    [listed] = call("list_items")["items"]
    assert (listed["lane"], listed["lane_plays"]) == (1, True)


def test_list_items_bad_track(reaper, call):
    result = call("list_items", track_index=3)
    assert result == {"success": False, "error": "No track 3: there are 0 (indices start at 0)"}


# edit_item


def test_edit_item_sets_exact_values_by_guid(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, info={"D_FADEINLEN": 0.5, "D_FADEOUTLEN": 0.25,
                                      "D_FADEINLEN_AUTO": 0.02})
    item.extra_takes = 1
    item.takes[0].info["D_PLAYRATE"] = 2.0
    result = call("edit_item", item=item.guid, start=2.0, end=4.0, fade_in=0.0,
                  fade_out=1.0, volume_db=-6.0, take_volume_db=3.0, muted=True)
    assert result["success"] is True, result
    assert result["changed"] == ["start", "end", "fade_in", "fade_out", "volume_db",
                                 "take_volume_db", "muted"]
    assert (item.position, item.length) == (2.0, 2.0)
    # The audio stays where it was: every take's offset moved by one item second.
    assert [t.info["D_STARTOFFS"] for t in item.takes] == [2.0, 1.0]
    # The fade is removed, not left alone, and so is an automatic crossfade.
    assert item.info["D_FADEINLEN"] == item.info["D_FADEINLEN_AUTO"] == 0.0
    assert item.info["D_FADEOUTLEN"] == 1.0
    assert item.info["D_VOL"] == pytest.approx(0.501, abs=1e-3)
    assert item.take_info["D_VOL"] == pytest.approx(1.413, abs=1e-3)
    assert item.info["B_MUTE"] == 1.0
    assert result["item"]["fade_in"] == 0.0
    assert reaper.undo_steps[-1][0].startswith("MCP: Edit item (item=")


def test_edit_item_by_index_and_nothing_else_changes(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, info={"D_FADEINLEN": 0.5, "D_FADEOUTLEN": 0.25})
    result = call("edit_item", item=0, track_index=0, muted=False)
    assert result["changed"] == ["muted"]
    assert item.info == {"D_FADEINLEN": 0.5, "D_FADEOUTLEN": 0.25, "B_MUTE": 0.0}


def test_edit_item_keeps_take_polarity(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    item.take_info["D_VOL"] = -1.0
    call("edit_item", item=item.guid, take_volume_db=-6.0)
    assert item.take_info["D_VOL"] == pytest.approx(-0.501, abs=1e-3)


def test_edit_item_shortening_shortens_fades(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, info={"D_FADEINLEN": 3.0, "D_FADEOUTLEN": 0.5})
    call("edit_item", item=item.guid, end=3.0)
    assert item.info == {"D_FADEINLEN": 2.0, "D_FADEOUTLEN": 0.5}


@pytest.mark.parametrize(("arguments", "error"), [
    ({"start": -1.0}, "start must be 0 seconds or later"),
    ({"end": 1.0}, "end (1 s) must be after start (1 s)"),
    ({"start": 3.0, "end": 2.0}, "end (2 s) must be after start (3 s)"),
    ({"fade_in": -0.1}, "fades must be >= 0"),
    ({"fade_out": 5.0}, "a fade cannot be longer than the item (4 s)"),
    ({"end": 2.0, "fade_in": 2.0}, "a fade cannot be longer than the item (1 s)"),
    ({"volume_db": 30.0}, "volumes above +24 dB are refused (too loud to be intended)"),
])
def test_edit_item_refuses_bad_values(reaper, call, arguments, error):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    result = call("edit_item", item=item.guid, **arguments)
    assert result == {"success": False, "error": error}
    assert (item.position, item.length, item.info) == (
        1.0, 4.0, {"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 0.0})


def test_edit_item_take_volume_needs_a_take(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, has_take=False)
    result = call("edit_item", item=item.guid, take_volume_db=0.0)
    assert result == {"success": False, "error": "The item has no take, so it has no take volume"}


def test_edit_item_refuses_a_locked_item_unless_unlocking(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, info={"C_LOCK": 1.0})
    refused = call("edit_item", item=item.guid, start=3.0)
    assert refused["success"] is False and "is locked" in refused["error"]
    assert item.position == 1.0
    result = call("edit_item", item=item.guid, start=3.0, locked=False)
    assert result["success"] is True and item.position == 3.0
    assert result["changed"] == ["start", "locked"] and item.info["C_LOCK"] == 0.0


def test_edit_item_locks_after_the_other_changes(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    result = call("edit_item", item=item.guid, start=2.0, locked=True)
    assert result["item"]["locked"] is True and item.position == 2.0


def test_edit_item_unknown_guid_and_index_without_track(reaper, call):
    reaper.add_track("Vox")
    missing = "{00000000-0000-0000-0000-000000000000}"
    result = call("edit_item", item=missing, muted=True)
    assert result["success"] is False and result["error"].startswith(f"No item has GUID {missing}")
    result = call("edit_item", item=0, muted=True)
    assert result["success"] is False and "needs track_index" in result["error"]
    result = call("edit_item", item=0, track_index=0, muted=True)
    assert result == {"success": False,
                      "error": "No item 0 on track 0: it has 0 (indices start at 0)"}


def test_edit_item_bad_track(reaper, call):
    assert_no_track(call("edit_item", item=0, track_index=2, muted=True))


# split_item


def test_split_item_at_several_times(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, info={"D_FADEINLEN": 0.1, "D_FADEOUTLEN": 0.2})
    item.take_info["D_PLAYRATE"] = 2.0
    result = call("split_item", item=item.guid, times=[4.0, 2.0])
    assert result["success"] is True, result
    pieces = result["pieces"]
    assert [(p["position"], p["length"]) for p in pieces] == [(1.0, 1.0), (2.0, 2.0), (4.0, 1.0)]
    assert pieces[0]["guid"] == item.guid
    assert len({p["guid"] for p in pieces}) == 3
    assert [i.guid for i in track.items] == [p["guid"] for p in pieces]
    # Each piece plays on from where the last stopped, in source seconds (playrate 2).
    assert [i.take_info["D_STARTOFFS"] for i in track.items] == [0.0, 2.0, 6.0]
    assert len(reaper.undo_steps) == 1


@pytest.mark.parametrize("times", [[1.0], [5.0], [0.5, 2.0], [], [1.0005], [4.9995]])
def test_split_item_refuses_times_outside(reaper, call, times):
    track = reaper.add_track("Vox")
    _item(reaper, track)
    result = call("split_item", item=0, track_index=0, times=times)
    assert result["success"] is False and "inside the item (1 to 5 s)" in result["error"]
    assert len(track.items) == 1


def test_split_item_counts_nearly_equal_times_once(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    result = call("split_item", item=item.guid, times=[2.0, 2.0 + 1e-13, 3.0, 3.0005])
    assert [(p["position"], p["length"]) for p in result["pieces"]] == [
        (1.0, 1.0), (2.0, 1.0), (3.0, 2.0)]
    assert "note" not in result  # named by GUID


def test_split_item_reports_the_pieces_made_when_reaper_refuses(reaper, call, monkeypatch):
    from reapy import reascript_api

    track = reaper.add_track("Vox")
    _item(reaper, track)
    real = reascript_api.SplitMediaItem
    monkeypatch.setattr(reascript_api, "SplitMediaItem", lambda i, t: (
        "(MediaItem*)0x0000000000000000" if t == 3.0 else real(i, t)))
    result = call("split_item", item=0, track_index=0, times=[2.0, 3.0])
    assert result["success"] is False and "did not split the item at 3 s" in result["error"]
    assert [p["guid"] for p in result["pieces"]] == [i.guid for i in track.items]
    assert len(track.items) == 2


def test_split_item_refuses_locked(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, info={"C_LOCK": 1.0})
    result = call("split_item", item=item.guid, times=[2.0])
    assert result["success"] is False and "locked" in result["error"]


# delete_item


def test_delete_item(reaper, call):
    track = reaper.add_track("Vox")
    keep = _item(reaper, track, position=10.0)
    gone = _item(reaper, track, position=0.0)
    result = call("delete_item", item=gone.guid)
    assert result == {"success": True, "deleted_guid": gone.guid, "track_index": 0}
    assert track.items == [keep]
    call("undo")
    assert [i.guid for i in reaper.tracks[0].items] == [gone.guid, keep.guid]


def test_delete_item_refuses_locked(reaper, call):
    track = reaper.add_track("Vox")
    _item(reaper, track, info={"C_LOCK": 1.0})
    result = call("delete_item", item=0, track_index=0)
    assert result["success"] is False and len(track.items) == 1


def test_delete_item_by_index_reminds_that_indices_shift(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    result = call("delete_item", item=0, track_index=0)
    assert result["success"] is True and item.guid in result["note"]
    assert "prefer GUIDs" in result["note"]


# move_item


def test_move_item_in_time_and_to_another_track(reaper, call):
    source, dest = reaper.add_track("A"), reaper.add_track("B")
    item = _item(reaper, source)
    result = call("move_item", item=item.guid, position=7.5, dest_track_index=1)
    assert result["success"] is True
    assert (result["item"]["track_index"], result["item"]["position"]) == (1, 7.5)
    assert (source.items, dest.items) == ([], [item])


def test_move_item_in_time_only(reaper, call):
    track = reaper.add_track("A")
    first = _item(reaper, track, position=0.0)
    second = _item(reaper, track, position=10.0)
    result = call("move_item", item=first.guid, position=20.0)
    assert result["item"]["item_index"] == 1  # REAPER keeps items in time order
    assert track.items == [second, first]


def test_move_item_needs_something_to_do(reaper, call):
    track = reaper.add_track("A")
    item = _item(reaper, track)
    assert call("move_item", item=item.guid) == {
        "success": False, "error": "pass position and/or dest_track_index"}
    assert call("move_item", item=item.guid, position=-1.0)["success"] is False


def test_move_item_bad_destination(reaper, call):
    track = reaper.add_track("A")
    item = _item(reaper, track)
    assert_no_track(call("move_item", item=item.guid, dest_track_index=4))
    assert track.items == [item]


def test_takes_have_their_own_pointers_and_guids():
    first, second = TakeState(), TakeState()
    assert first.pointer != second.pointer and first.guid != second.guid
