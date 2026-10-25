"""create_bus against the fake REAPER: one bus, each source heard once through it."""

from tests.helpers import assert_no_track

OUT_OF_RANGE = "list index out of range"


def test_create_bus_appends_track_and_routes(reaper, call):
    kick = reaper.add_track("Kick")
    snare = reaper.add_track("Snare")
    vox = reaper.add_track("Vox")
    result = call("create_bus", name="Drums", track_indices=[0, 1])
    bus = reaper.tracks[3]
    assert result == {
        "success": True,
        "bus_index": 3,
        "bus_guid": bus.guid,
        "bus_name": "Drums",
        "sends": [{"track_index": 0, "send_index": 0}, {"track_index": 1, "send_index": 0}],
    }
    assert [t.name for t in reaper.tracks] == ["Kick", "Snare", "Vox", "Drums"]
    assert [s.dest for s in kick.sends] == [bus.pointer]
    assert [s.dest for s in snare.sends] == [bus.pointer]
    assert vox.sends == []
    assert bus.sends == []


def test_create_bus_routed_tracks_stop_sending_to_the_master(reaper, call):
    """Otherwise each routed track is heard twice: directly and through the bus."""
    kick = reaper.add_track("Kick")
    vox = reaper.add_track("Vox")
    call("create_bus", name="Drums", track_indices=[0])
    bus = reaper.tracks[2]
    assert kick.value("B_MAINSEND") == 0
    assert (vox.value("B_MAINSEND"), bus.value("B_MAINSEND")) == (1.0, 1.0)


def test_create_bus_refuses_a_repeated_track(reaper, call):
    kick = reaper.add_track("Kick")
    result = call("create_bus", name="Drums", track_indices=[0, 0])
    assert result == {"success": False, "error": "track_indices repeats a track: [0, 0]"}
    assert [t.name for t in reaper.tracks] == ["Kick"]
    assert kick.sends == []


def test_create_bus_send_reaper_refuses_changes_nothing(reaper, call, monkeypatch):
    from reapy import reascript_api

    kick = reaper.add_track("Kick")
    snare = reaper.add_track("Snare")
    real = reaper.CreateTrackSend
    monkeypatch.setattr(reascript_api, "CreateTrackSend",
                        lambda src, dst: -1 if src == snare.pointer else real(src, dst))
    result = call("create_bus", name="Drums", track_indices=[0, 1])
    assert result == {"success": False, "error": "REAPER did not create a send from track 1 "
                                                 "to the bus; nothing was changed"}
    assert [t.name for t in reaper.tracks] == ["Kick", "Snare"]
    assert (kick.sends, snare.sends) == ([], [])
    assert kick.value("B_MAINSEND") == snare.value("B_MAINSEND") == 1.0


def test_create_bus_no_sources(reaper, call):
    result = call("create_bus", name="Empty", track_indices=[])
    assert result == {"success": True, "bus_index": 0, "bus_guid": reaper.tracks[0].guid,
                      "bus_name": "Empty", "sends": []}
    assert [t.name for t in reaper.tracks] == ["Empty"]


def test_create_bus_bad_index_changes_nothing(reaper, call):
    kick = reaper.add_track("Kick")
    result = call("create_bus", name="Drums", track_indices=[0, 7])
    assert_no_track(result)
    assert [t.name for t in reaper.tracks] == ["Kick"]
    assert kick.sends == []


def test_create_bus_after_an_open_folder_stays_outside_it(reaper, call):
    """A folder track left open at the end would swallow the bus (and a folder
    source would then feed its own bus): the folder is closed first."""
    call("create_track", name="Drums", track_type="folder")
    call("create_track", name="Kick")
    result = call("create_bus", name="Bus", track_indices=[0])
    assert result["success"] is True
    drums, kick, bus = reaper.tracks
    assert [t.value("I_FOLDERDEPTH") for t in (drums, kick, bus)] == [1.0, -1.0, 0.0]


def test_create_bus_leaves_closed_folders_alone(reaper, call):
    call("create_track", name="Drums", track_type="folder")
    kick = reaper.add_track("Kick")
    kick.info["I_FOLDERDEPTH"] = -1.0
    call("create_bus", name="Bus", track_indices=[1])
    assert [t.value("I_FOLDERDEPTH") for t in reaper.tracks] == [1.0, -1.0, 0.0]


def test_create_bus_failure_reopens_the_folder(reaper, call, monkeypatch):
    from reapy import reascript_api

    call("create_track", name="Drums", track_type="folder")
    call("create_track", name="Kick")
    monkeypatch.setattr(reascript_api, "CreateTrackSend", lambda src, dst: -1)
    assert call("create_bus", name="Bus", track_indices=[1])["success"] is False
    assert [t.value("I_FOLDERDEPTH") for t in reaper.tracks] == [1.0, 0.0]
