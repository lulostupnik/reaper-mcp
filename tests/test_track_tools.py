"""Characterization tests for track_tools, run end to end against the fake REAPER."""

import anyio
import pytest

from reaper_mcp import track_tools
from tests.conftest import _call
from tests.fake_reaper.objects import FXState, ItemState
from tests.helpers import assert_no_track

DEFAULT_INFO = {"D_VOL": 1.0, "D_PAN": 0.0, "B_MUTE": 0.0, "I_SOLO": 0.0}
OUT_OF_RANGE = {"success": False, "error": "list index out of range"}


def call_with_name(tool, **arguments):
    """`call` reserves `name` for the tool name, so tools taking `name` go through here."""
    return anyio.run(_call, tool, arguments)


def test_create_track_audio(reaper):
    reaper.add_track("first")
    result = call_with_name("create_track", name="Gtr")
    assert result == {"success": True, "track_index": 1, "guid": reaper.tracks[1].guid,
                      "name": "Gtr", "type": "audio"}
    assert [t.name for t in reaper.tracks] == ["first", "Gtr"]
    assert reaper.tracks[1].info == DEFAULT_INFO


def test_create_track_from_python_defaults_to_audio(reaper):
    assert track_tools.create_track(name="Gtr") == {
        "success": True, "track_index": 0, "guid": reaper.tracks[0].guid, "name": "Gtr",
        "type": "audio",
    }
    assert reaper.tracks[0].info == DEFAULT_INFO


# I_RECINPUT: 4096 (MIDI) + 63 << 5 (all devices) + 0 (all channels).
ALL_MIDI_INPUTS = 4096 + (63 << 5)


def test_create_track_midi_records_every_device_and_channel(reaper):
    result = call_with_name("create_track", name="Keys", track_type="midi")
    assert result["type"] == "midi"
    assert reaper.tracks[0].info == {**DEFAULT_INFO, "I_RECINPUT": ALL_MIDI_INPUTS}
    assert track_tools.describe_input(ALL_MIDI_INPUTS) == "all MIDI devices, all channels"


def test_create_track_instrument_monitors_its_input(reaper):
    result = call_with_name("create_track", name="Piano", track_type="instrument")
    assert result["type"] == "instrument"
    assert reaper.tracks[0].info == {**DEFAULT_INFO, "I_RECINPUT": ALL_MIDI_INPUTS, "I_RECMON": 1}


@pytest.mark.parametrize(("value", "words"), [
    (-1, "none"),
    (0, "mono input 1"),
    (1, "mono input 2"),
    (1024, "stereo input 1/2"),
    (2048 + 2, "multichannel from input 3"),
    (4096, "MIDI device 0, all channels"),
    (4096 + (62 << 5) + 10, "virtual MIDI keyboard, channel 10"),
])
def test_describe_input(value, words):
    assert track_tools.describe_input(value) == words


def test_create_track_folder(reaper):
    result = call_with_name("create_track", name="Bus", track_type="folder")
    assert result["success"] is True
    assert reaper.tracks[0].info == {**DEFAULT_INFO, "I_FOLDERDEPTH": 1}


def test_create_track_unknown_type_is_plain(reaper):
    result = call_with_name("create_track", name="X", track_type="weird")
    assert result == {"success": True, "track_index": 0, "guid": reaper.tracks[0].guid,
                      "name": "X", "type": "weird"}
    assert reaper.tracks[0].info == DEFAULT_INFO


def test_delete_track(reaper, call):
    reaper.add_track("a")
    keep = reaper.add_track("b")
    assert call("delete_track", track_index=0) == {"success": True, "deleted_index": 0}
    assert reaper.tracks == [keep]


def test_delete_track_out_of_range(reaper, call):
    reaper.add_track("a")
    assert_no_track(call("delete_track", track_index=3))
    assert len(reaper.tracks) == 1


def test_rename_track(reaper):
    state = reaper.add_track("old")
    assert call_with_name("rename_track", track_index=0, name="new") == {
        "success": True,
        "track_index": 0,
        "name": "new",
    }
    assert state.name == "new"


def test_rename_track_out_of_range(reaper):
    assert_no_track(call_with_name("rename_track", track_index=0, name="x"))


def test_set_track_volume(reaper, call):
    state = reaper.add_track("a")
    result = call("set_track_volume", track_index=0, volume_db=-6.0)
    assert result == {"success": True, "track_index": 0, "volume_db": -6.0}
    assert state.info["D_VOL"] == pytest.approx(10 ** (-6 / 20))


def test_set_track_volume_floor_is_silence(reaper, call):
    state = reaper.add_track("a")
    result = call("set_track_volume", track_index=0, volume_db=-200.0)
    assert result["volume_db"] == -150.0
    assert state.info["D_VOL"] == 0.0


def test_set_track_volume_out_of_range(reaper, call):
    assert_no_track(call("set_track_volume", track_index=0, volume_db=0.0))


def test_set_track_pan(reaper, call):
    state = reaper.add_track("a")
    result = call("set_track_pan", track_index=0, pan=-0.5)
    assert result == {"success": True, "track_index": 0, "pan": -0.5}
    assert state.info["D_PAN"] == -0.5


def test_set_track_pan_out_of_range(reaper, call):
    assert_no_track(call("set_track_pan", track_index=0, pan=0.0))


def test_set_track_mute_and_unmute(reaper, call):
    state = reaper.add_track("a")
    reaper.add_track("b")
    assert call("set_track_mute", track_index=1, muted=True) == {
        "success": True, "track_index": 1, "muted": True,
    }
    assert reaper.tracks[1].info["B_MUTE"] == 1
    assert call("set_track_mute", track_index=0, muted=True)["muted"] is True
    assert state.info["B_MUTE"] == 1
    assert call("set_track_mute", track_index=0, muted=False) == {
        "success": True, "track_index": 0, "muted": False,
    }
    assert state.info["B_MUTE"] == 0


def test_set_track_mute_out_of_range(reaper, call):
    assert_no_track(call("set_track_mute", track_index=0, muted=True))


def test_set_track_solo_and_unsolo(reaper, call):
    state = reaper.add_track("a")
    reaper.add_track("b")
    assert call("set_track_solo", track_index=1, soloed=True) == {
        "success": True, "track_index": 1, "soloed": True,
    }
    assert call("set_track_solo", track_index=0, soloed=True)["soloed"] is True
    assert state.info["I_SOLO"] == 2  # solo in place
    assert call("set_track_solo", track_index=0, soloed=False) == {
        "success": True, "track_index": 0, "soloed": False,
    }
    assert state.info["I_SOLO"] == 0


def test_set_track_solo_out_of_range(reaper, call):
    assert_no_track(call("set_track_solo", track_index=0, soloed=True))


def test_get_track_info(reaper, call):
    state = reaper.add_track("a")
    state.info.update({"D_VOL": 0.5, "D_PAN": 0.25, "B_MUTE": 1.0})
    state.items.append(ItemState("(MediaItem*)0x1", 1.0, 2.0, midi=False))
    result = call("get_track_info", track_index=0)
    assert result == {
        "success": True,
        "track_index": 0,
        "guid": state.guid,
        "name": "a",
        "volume_db": -6.02,
        "pan": 0.25,
        "muted": True,
        "soloed": False,
        "armed": False,
        "record_input": "mono input 1",
        "monitoring": "off",
        "record_mode": "input",
        "fx_count": 0,
        "fx": [],
        "item_count": 1,
        "items": [{"index": 0, "guid": state.items[0].guid, "position": 1.0, "length": 2.0,
                   "name": "take"}],
    }


def test_get_track_info_lists_fx(reaper, call):
    state = reaper.add_track("a")
    state.fxs += [FXState("ReaEQ", [0.5] * 3), FXState("ReaComp", [0.5] * 4, enabled=False)]
    result = call("get_track_info", track_index=0)
    assert (result["fx_count"], result["fx"]) == (2, [
        {"index": 0, "name": "VST: ReaEQ (Cockos)", "enabled": True},
        {"index": 1, "name": "VST: ReaComp (Cockos)", "enabled": False},
    ])


def test_get_track_info_out_of_range(reaper, call):
    assert call("get_track_info", track_index=0) == OUT_OF_RANGE


def test_list_tracks_empty(reaper, call):
    assert call("list_tracks") == {"success": True, "count": 0, "tracks": []}


def test_list_tracks(reaper, call):
    reaper.add_track("a")
    b = reaper.add_track("b")
    b.info["I_SOLO"] = 2.0
    result = call("list_tracks")
    assert result["count"] == 2
    assert result["tracks"][1] == {
        "index": 1, "guid": b.guid, "name": "b", "volume_db": 0.0, "pan": 0.0,
        "muted": False, "soloed": True, "armed": False, "record_input": "mono input 1",
        "monitoring": "off", "record_mode": "input", "fx_count": 0, "item_count": 0,
    }


def test_list_tracks_shows_the_recording_setup(reaper, call):
    gtr = reaper.add_track("Gtr")
    gtr.info.update(I_RECARM=1.0, I_RECINPUT=1.0, I_RECMON=2.0, I_RECMODE=7.0)
    [track] = call("list_tracks")["tracks"]
    assert (track["armed"], track["record_input"], track["monitoring"], track["record_mode"]) == (
        True, "mono input 2", "tape (off while playing)", "MIDI overdub")
    info = call("get_track_info", track_index=0)
    assert (info["armed"], info["record_mode"]) == (True, "MIDI overdub")


def test_set_track_color(reaper, call):
    state = reaper.add_track("a")
    assert call("set_track_color", track_index=0, r=255, g=128, b=1) == {
        "success": True,
        "track_index": 0,
        "r": 255,
        "g": 128,
        "b": 1,
    }
    assert state.info["I_CUSTOMCOLOR"] == 255 | (128 << 8) | (1 << 16) | 0x1000000


def test_set_track_color_keeps_even_channels(reaper, call):
    state = reaper.add_track("a")
    call("set_track_color", track_index=0, r=16, g=0, b=2)
    assert state.info["I_CUSTOMCOLOR"] == 16 | (2 << 16) | 0x1000000


def test_set_track_color_out_of_range(reaper, call):
    assert_no_track(call("set_track_color", track_index=0, r=0, g=0, b=0))


def test_get_track_info_with_an_empty_item(reaper, call):
    state = reaper.add_track("a")
    state.items.append(ItemState("(MediaItem*)0x1", 0.0, 2.0, midi=False, has_take=False))
    [item] = call("get_track_info", track_index=0)["items"]
    assert item == {"index": 0, "guid": state.items[0].guid, "position": 0.0, "length": 2.0,
                    "name": ""}
