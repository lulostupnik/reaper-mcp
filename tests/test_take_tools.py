"""list_takes, set_active_take, crop_to_active_take and suggest_best_take
against the fake REAPER."""

from tests.fake_reaper.item_state import TakeState
from tests.fake_reaper.objects import ItemState
from tests.helpers import write_wav

CROP = 40131


def _takes(reaper, tmp_path, levels, length=2.0):
    """An item with one take per (amplitude, seconds of sound) in levels,
    each its own file; the last take is active."""
    track = reaper.add_track("Vox")
    takes = []
    for index, (amplitude, sound) in enumerate(levels):
        path = write_wav(tmp_path / f"take{index}.wav", [(sound, amplitude), (length - sound, 0.0)])
        takes.append(TakeState(path, name=f"Vox take {index + 1}"))
    item = ItemState(reaper.new_pointer("MediaItem"), 0.0, length, midi=False, takes=takes)
    item.active = len(takes) - 1
    track.items.append(item)
    return track, item


def _lanes(reaper, tmp_path, levels):
    """A fixed-lane track with one item per lane, all at 0 s; lane 0 plays."""
    track = reaper.add_track("Lanes")
    track.info.update({"I_FREEMODE": 2.0, "I_NUMFIXEDLANES": float(len(levels))})
    for lane, amplitude in enumerate(levels):
        path = write_wav(tmp_path / f"lane{lane}.wav", [(2.0, amplitude)])
        track.items.append(ItemState(
            reaper.new_pointer("MediaItem"), 0.0, 2.0, midi=False, source_file=path,
            info={"I_FIXEDLANE": float(lane), "C_LANEPLAYS": float(lane == 0)}))
    return track


# list_takes


def test_list_takes(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0), (0.4, 2.0)])
    result = call("list_takes", item=item.guid)
    assert result["success"] is True and result["item_guid"] == item.guid
    assert [(t["index"], t["name"], t["active"]) for t in result["takes"]] == [
        (0, "Vox take 1", False), (1, "Vox take 2", True)]
    assert result["takes"][0]["guid"] == item.takes[0].guid
    assert result["takes"][1]["source_file"] == str(tmp_path / "take1.wav")
    assert "lanes" not in result


def test_list_takes_on_a_fixed_lane_track(reaper, call, tmp_path):
    track = _lanes(reaper, tmp_path, [0.5, 0.3])
    result = call("list_takes", item=0, track_index=0)
    assert [(lane["lane"], lane["plays"]) for lane in result["lanes"]] == [(0, True), (1, False)]
    assert result["lanes"][1]["item_guid"] == track.items[1].guid


# set_active_take


def test_set_active_take(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0), (0.4, 2.0)])
    result = call("set_active_take", item=item.guid, take_index=0)
    assert result == {"success": True, "item_guid": item.guid, "active_take_index": 0,
                      "active_take": "Vox take 1"}
    assert item.active == 0 and len(item.takes) == 2
    call("undo")
    assert reaper.tracks[0].items[0].active == 1


def test_set_active_take_refuses_bad_arguments(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0)])
    result = call("set_active_take", item=item.guid, take_index=3)
    assert result == {"success": False, "error": "No take 3: the item has 1 (indices start at 0)"}


def test_set_active_take_refuses_a_locked_item(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0), (0.4, 2.0)])
    item.info["C_LOCK"] = 1.0
    result = call("set_active_take", item=item.guid, take_index=0)
    assert result["success"] is False and "locked" in result["error"]
    assert item.active == 1


# crop_to_active_take


def test_crop_to_active_take(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0), (0.4, 2.0), (0.3, 2.0)])
    item.active = 1
    other = ItemState(reaper.new_pointer("MediaItem"), 5.0, 1.0, midi=False, selected=True)
    reaper.tracks[0].items.append(other)
    result = call("crop_to_active_take", item=item.guid)
    assert result == {"success": True, "item_guid": item.guid, "removed_takes": 2,
                      "kept_take": "Vox take 2"}
    assert [t.name for t in item.takes] == ["Vox take 2"]
    assert CROP in reaper.commands
    assert other.selected and not item.selected
    call("undo")
    assert len(reaper.tracks[0].items[0].takes) == 3


def test_crop_to_active_take_with_one_take_does_nothing(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0)])
    result = call("crop_to_active_take", item=item.guid)
    assert result["removed_takes"] == 0 and CROP not in reaper.commands


def test_crop_to_active_take_refuses_locked(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0), (0.4, 2.0)])
    item.info["C_LOCK"] = 1.0
    assert call("crop_to_active_take", item=item.guid)["success"] is False
    assert len(item.takes) == 2


# suggest_best_take


def test_suggest_best_take_avoids_clipping_quiet_and_incomplete_takes(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(1.0, 2.0), (0.01, 2.0), (0.3, 2.0), (0.4, 1.0)])
    result = call("suggest_best_take", item=item.guid)
    assert result["success"] is True
    assert result["suggested"] == {"take_index": 2, "name": "Vox take 3"}
    issues = [c["issues"] for c in result["candidates"]]
    assert "clipped" in issues[0][0]
    assert "very quiet" in issues[1][0]
    assert issues[2] == []
    assert "stops early" in issues[3][0]  # only 1 s of sound where the others have 2
    assert [c["sound_seconds"] for c in result["candidates"]][2:] == [2.0, 1.0]
    assert "listening" in result["note"]
    assert item.active == 3  # nothing changed


def test_suggest_best_take_does_not_prefer_the_louder_of_clean_takes(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.2, 2.0), (0.5, 2.0)])
    result = call("suggest_best_take", item=item.guid)
    assert result["suggested"] is None
    assert result["tied"] == [{"take_index": 0, "name": "Vox take 1"},
                              {"take_index": 1, "name": "Vox take 2"}]
    assert result["note"].startswith("2 takes are equally fine technically")


def test_suggest_best_take_reports_takes_it_cannot_read(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0), (0.4, 2.0)])
    item.takes[0].source_file = str(tmp_path / "gone.wav")
    result = call("suggest_best_take", item=item.guid)
    assert result["candidates"][0]["issues"][0].startswith("could not be analyzed")
    assert result["suggested"]["take_index"] == 1


def test_suggest_best_take_with_one_take(reaper, call, tmp_path):
    _, item = _takes(reaper, tmp_path, [(0.5, 2.0)])
    result = call("suggest_best_take", item=item.guid)
    assert result["note"].startswith("Only one take")


def test_suggest_best_take_compares_lanes(reaper, call, tmp_path):
    track = _lanes(reaper, tmp_path, [1.0, 0.4])
    result = call("suggest_best_take", item=track.items[0].guid)
    assert result["suggested"] == {"lane": 1, "item_guid": track.items[1].guid}


def test_lanes_keep_the_most_overlapping_item_per_lane(reaper, call, tmp_path):
    track = _lanes(reaper, tmp_path, [0.5, 0.3])
    neighbour = ItemState(reaper.new_pointer("MediaItem"), 1.5, 2.0, midi=False,
                          info={"I_FIXEDLANE": 1.0})
    track.items.append(neighbour)
    track.items.sort(key=lambda i: i.position)
    result = call("list_takes", item=track.items[0].guid)
    assert [lane["item_guid"] for lane in result["lanes"]] == [
        track.items[0].guid, track.items[1].guid]
