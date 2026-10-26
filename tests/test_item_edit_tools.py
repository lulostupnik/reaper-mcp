"""duplicate_item, trim_item_to_time_selection and glue_items against the fake REAPER."""

import re

from reaper_mcp.item_edit_tools import fresh_guids
from tests.fake_reaper.objects import ItemState
from tests.helpers import assert_no_track

GLUE = 40362


def _item(reaper, track, position=1.0, length=4.0, **kwargs):
    state = ItemState(reaper.new_pointer("MediaItem"), position, length, midi=False, **kwargs)
    track.items.append(state)
    track.items.sort(key=lambda i: i.position)
    return state


# duplicate_item


def test_duplicate_item_copies_everything_with_new_guids(reaper, call, tmp_path):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, source_file=str(tmp_path / "a.wav"),
                 info={"D_FADEINLEN": 0.5, "D_FADEOUTLEN": 0.0, "D_VOL": 0.5})
    item.take_info["D_STARTOFFS"] = 1.25
    item.extra_takes = 1
    result = call("duplicate_item", item=item.guid)
    assert result["success"] is True, result
    [original, copy] = track.items
    assert original is item
    assert result["item"]["guid"] == copy.guid != item.guid
    assert (copy.position, copy.length) == (5.0, 4.0)  # right after the original
    assert copy.info == item.info
    assert [t.info for t in copy.takes] == [t.info for t in item.takes]
    assert [t.source_file for t in copy.takes] == [t.source_file for t in item.takes]
    assert copy.active == item.active == 1
    assert not {t.guid for t in copy.takes} & {t.guid for t in item.takes}
    assert len(reaper.undo_steps) == 1


def test_duplicate_item_to_a_position_on_another_track(reaper, call):
    source, dest = reaper.add_track("A"), reaper.add_track("B")
    item = _item(reaper, source)
    result = call("duplicate_item", item=0, track_index=0, position=12.0, dest_track_index=1)
    assert (result["item"]["track_index"], result["item"]["position"]) == (1, 12.0)
    assert len(source.items) == 1 and len(dest.items) == 1
    assert dest.items[0].guid != item.guid


def test_duplicate_item_refuses_negative_position_and_bad_track(reaper, call):
    track = reaper.add_track("A")
    item = _item(reaper, track)
    assert call("duplicate_item", item=item.guid, position=-1.0)["success"] is False
    assert_no_track(call("duplicate_item", item=item.guid, dest_track_index=5))
    assert len(track.items) == 1


def test_fresh_guids_replaces_item_take_pool_fx_and_envelope_guids():
    old = "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"
    chunk = (f"<ITEM\n  IGUID {old}\n  GUID {old}\n  POOLEDEVTS {old}\n  FXID {old}\n"
             f"  EGUID {old}\n  NAME \"{old}\"\n>\n")
    new = fresh_guids(chunk)
    guids = re.findall(r"\{[0-9A-F-]{36}\}", new)
    assert guids.count(old) == 1  # only the name is left alone
    assert len(set(guids)) == 6


def test_duplicate_item_gives_take_fx_new_guids(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    item.take.fx_count = 2
    original = [item.take.fx_guid(i) for i in range(2)]
    assert call("duplicate_item", item=item.guid)["success"] is True
    copy = track.items[1]
    assert len(copy.take.fx_guids) == 2 and not set(copy.take.fx_guids) & set(original)


# trim_item_to_time_selection


def test_trim_item_to_time_selection(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track, info={"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 3.0})
    item.extra_takes = 1
    item.takes[0].info["D_PLAYRATE"] = 0.5
    reaper.time_selection = (2.0, 4.0)
    result = call("trim_item_to_time_selection", item=item.guid)
    assert result["success"] is True, result
    assert (item.position, item.length) == (2.0, 2.0)
    # The audio stays put: every take's offset moved by one item second.
    assert [t.info["D_STARTOFFS"] for t in item.takes] == [0.5, 1.0]
    assert item.info["D_FADEOUTLEN"] == 2.0  # a fade longer than the item is shortened


def test_trim_item_to_a_selection_covering_one_end(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    reaper.time_selection = (0.0, 3.0)
    call("trim_item_to_time_selection", item=0, track_index=0)
    assert (item.position, item.length, item.take_info["D_STARTOFFS"]) == (1.0, 2.0, 0.0)


def test_trim_item_to_time_selection_needs_an_overlapping_selection(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    result = call("trim_item_to_time_selection", item=item.guid)
    assert result["success"] is False and "no time selection" in result["error"]
    reaper.time_selection = (6.0, 8.0)
    result = call("trim_item_to_time_selection", item=item.guid)
    assert result["success"] is False and "outside the time selection" in result["error"]
    assert (item.position, item.length) == (1.0, 4.0)


# glue_items


def test_glue_items_restores_the_selection(reaper, call):
    track = reaper.add_track("Vox")
    first = _item(reaper, track, position=0.0, length=1.0)
    second = _item(reaper, track, position=2.0, length=1.0)
    other = _item(reaper, reaper.add_track("Other"), position=0.0, selected=True)
    result = call("glue_items", items_to_glue=[first.guid, second.guid])
    assert result["success"] is True, result
    assert result["glued_guids"] == [first.guid, second.guid]
    [glued] = track.items
    assert result["item"]["guid"] == glued.guid
    assert (glued.position, glued.length) == (0.0, 3.0)
    assert GLUE in reaper.commands
    assert other.selected and not glued.selected  # the user's selection is back
    call("undo")
    assert [i.guid for i in reaper.tracks[0].items] == [first.guid, second.guid]


def test_glue_items_does_not_select_the_glued_item_in_place_of_a_selected_original(reaper, call):
    """REAPER gives the glued item the address of a glued-away one (7.69), so the
    selection is put back by GUID, not by checking the old pointers."""
    track = reaper.add_track("Vox")
    first = _item(reaper, track, position=0.0, length=1.0)
    second = _item(reaper, track, position=2.0, length=1.0, selected=True)
    other = _item(reaper, reaper.add_track("Other"), position=0.0, selected=True)
    assert call("glue_items", items_to_glue=[first.guid, second.guid])["success"] is True
    [glued] = track.items
    assert glued.pointer == second.pointer and glued.guid != second.guid
    assert other.selected and not glued.selected


def test_glue_items_into_a_folder_reaper_cannot_write_is_refused_first(reaper, call, tmp_path):
    """The glued file goes into the recording folder; REAPER would answer a
    file it cannot write with a modal dialog."""
    folder = tmp_path / "read-only"
    folder.mkdir()
    reaper.project_file = str(folder / "song.rpp")
    track = reaper.add_track("Vox")
    first = _item(reaper, track, position=0.0, length=1.0)
    folder.chmod(0o555)
    try:
        result = call("glue_items", items_to_glue=[first.guid])
    finally:
        folder.chmod(0o755)
    assert result["success"] is False
    assert "cannot write in" in result["error"] and "nothing was glued" in result["error"]
    assert GLUE not in reaper.commands


def test_glue_items_finds_the_result_when_reaper_reuses_the_first_item(reaper, call):
    reaper.glue_reuses_first_item = True
    track = reaper.add_track("Vox")
    first = _item(reaper, track, position=0.0, length=1.0)
    second = _item(reaper, track, position=2.0, length=1.0)
    result = call("glue_items", items_to_glue=[first.guid, second.guid])
    assert result["success"] is True, result
    assert result["item"]["guid"] == first.guid and track.items == [first]
    assert (first.position, first.length) == (0.0, 3.0)


def test_glue_items_reports_when_nothing_was_glued(reaper, call, monkeypatch):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    monkeypatch.setattr(type(reaper), "_glue", lambda self, track: None)
    result = call("glue_items", items_to_glue=[item.guid])
    assert result["success"] is False and "instead of one" in result["error"]


def test_glue_items_accepts_a_length_rounded_to_samples(reaper, call):
    reaper.glue_length_error = 2e-5  # about one sample at 48 kHz
    track = reaper.add_track("Vox")
    first = _item(reaper, track, position=0.0, length=1.0)
    second = _item(reaper, track, position=2.0, length=1.0)
    result = call("glue_items", items_to_glue=[first.guid, second.guid])
    assert result["success"] is True, result
    [glued] = track.items
    assert result["item"]["guid"] == glued.guid


def test_glue_items_reports_when_no_item_spans_the_extent(reaper, call):
    reaper.glue_length_error = 0.5
    track = reaper.add_track("Vox")
    first = _item(reaper, track, position=0.0, length=1.0)
    second = _item(reaper, track, position=2.0, length=1.0)
    result = call("glue_items", items_to_glue=[first.guid, second.guid])
    assert result["success"] is False and "0 items span" in result["error"]


def test_glue_items_reports_when_several_items_span_the_extent(reaper, call, monkeypatch):
    track = reaper.add_track("Vox")
    first = _item(reaper, track, position=0.0, length=1.0)
    second = _item(reaper, track, position=2.0, length=1.0)
    glue = type(reaper)._glue

    def glue_twice(self, track):
        glue(self, track)
        [glued] = [i for i in track.items if i.selected]
        _item(self, track, position=glued.position, length=glued.length)

    monkeypatch.setattr(type(reaper), "_glue", glue_twice)
    result = call("glue_items", items_to_glue=[first.guid, second.guid])
    assert result["success"] is False and "2 items span" in result["error"]


def test_glue_items_refuses_items_with_several_takes(reaper, call):
    track = reaper.add_track("Vox")
    item = _item(reaper, track)
    item.extra_takes = 1
    result = call("glue_items", items_to_glue=[item.guid])
    assert result["success"] is False and "crop_to_active_take" in result["error"]
    assert GLUE not in reaper.commands and len(item.takes) == 2


def test_glue_items_refuses_items_on_two_tracks_or_locked(reaper, call):
    a, b = reaper.add_track("A"), reaper.add_track("B")
    first, second = _item(reaper, a), _item(reaper, b)
    result = call("glue_items", items_to_glue=[first.guid, second.guid])
    assert result["success"] is False and "one track" in result["error"]
    locked = _item(reaper, a, position=8.0, info={"C_LOCK": 1.0})
    result = call("glue_items", items_to_glue=[first.guid, locked.guid])
    assert result["success"] is False and "Locked" in result["error"]
    assert call("glue_items", items_to_glue=[])["success"] is False
    assert GLUE not in reaper.commands


def test_glue_items_refuses_when_reaper_names_the_action_otherwise(reaper, call, monkeypatch):
    from tests.fake_reaper import recording

    monkeypatch.setitem(recording.ACTION_NAMES, GLUE, "Item: Something else")
    track = reaper.add_track("A")
    item = _item(reaper, track)
    result = call("glue_items", items_to_glue=[item.guid])
    assert result["success"] is False and "was not run" in result["error"]
    assert GLUE not in reaper.commands and track.items == [item]
