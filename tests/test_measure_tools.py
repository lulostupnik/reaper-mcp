"""measure_track and compare_measurements against the fake REAPER.

The fake renders 440 Hz at 0.5 (left) and 660 Hz at 0.25 (right); with
reaper.mix_by_tracks each channel is scaled by the summed volume of the
tracks heard, so soloing and faders show up in the numbers.
"""

import math
import tempfile

import pytest

from reaper_mcp.units import db_to_linear
from tests.fake_reaper.objects import FXState
from tests.helpers import UNSET_RENDER_SETTINGS

PEAK_DB = round(20 * math.log10(0.5), 2)
RMS_DB = round(10 * math.log10((0.5**2 / 2 + 0.25**2 / 2) / 2), 2)
SOLO_IN_PLACE = 2.0


@pytest.fixture
def band(reaper):
    reaper.mix_by_tracks = True
    reaper.add_track("Vocal")
    reaper.add_track("Guitar")
    return reaper


def test_measure_track_solos_it_alone_for_the_render_then_puts_solo_back(band, call):
    band.tracks[1].info["I_SOLO"] = 1.0  # the user had the guitar soloed
    result = call("measure_track", track_index=0)

    assert result["success"] is True
    assert (result["track_index"], result["track_name"]) == (0, "Vocal")
    assert result["track_guid"] == band.tracks[0].guid
    assert (result["start"], result["end"]) == (None, None)
    assert band.render_solos == [["Vocal"]]
    assert [t.info["I_SOLO"] for t in band.tracks] == [0.0, 1.0]
    assert band.project_info == UNSET_RENDER_SETTINGS
    [path] = band.renders
    assert not path.exists()
    m = result["measurement"]
    assert m["sample_peak_dbfs"] == pytest.approx(PEAK_DB, abs=0.01)
    assert m["rms_db"] == pytest.approx(RMS_DB, abs=0.01)
    assert m["crest_factor_db"] == pytest.approx(PEAK_DB - RMS_DB, abs=0.02)
    assert m["integrated_lufs"] is not None
    assert (m["clipping"], m["silent"], m["seconds"]) == (False, False, 4.0)
    assert result["notes"] == []


def test_measure_track_hears_only_that_track(band, call):
    band.tracks[1].info["D_VOL"] = db_to_linear(6.0)  # a loud guitar must not count
    result = call("measure_track", track_index=0)
    assert result["measurement"]["sample_peak_dbfs"] == pytest.approx(PEAK_DB, abs=0.01)


def test_measure_track_by_guid(band, call):
    result = call("measure_track", track_index=band.tracks[1].guid)
    assert result["track_index"] == 1
    assert band.render_solos == [["Guitar"]]


def test_measure_track_unknown_track_renders_nothing(band, call):
    result = call("measure_track", track_index=5)
    assert result["success"] is False and "No track 5" in result["error"]
    assert band.renders == []


def test_a_muted_track_is_measured_unmuted_and_stays_muted(band, call):
    band.tracks[0].info["B_MUTE"] = 1.0
    result = call("measure_track", track_index=0)
    assert result["measurement"]["silent"] is False
    assert band.tracks[0].info["B_MUTE"] == 1.0
    assert "muted" in result["notes"][0]


def test_measure_track_over_a_range_restores_the_time_selection(band, call):
    band.time_selection = (10.0, 12.0)
    result = call("measure_track", track_index=0, start=1.0, end=2.5)
    assert (result["start"], result["end"]) == (1.0, 2.5)
    assert result["measurement"]["seconds"] == 1.5
    assert band.render_settings[0]["RENDER_BOUNDSFLAG"] == 2.0
    assert band.time_selection == (10.0, 12.0)


@pytest.mark.parametrize(("start", "end", "error"), [
    (1.0, None, "give both start and end"),
    (None, 2.0, "give both start and end"),
    (2.0, 2.0, "need 0 <= start < end"),
    (-1.0, 2.0, "need 0 <= start < end"),
])
def test_measure_track_refuses_a_bad_range_before_rendering(band, call, start, end, error):
    result = call("measure_track", track_index=0, start=start, end=end)
    assert result["success"] is False and error in result["error"]
    assert band.renders == [] and band.requests == []


def test_a_silent_track_has_no_levels(band, call):
    band.silent = True
    result = call("measure_track", track_index=0)
    m = result["measurement"]
    assert (m["integrated_lufs"], m["rms_db"], m["sample_peak_dbfs"]) == (None, None, None)
    assert m["silent"] is True
    assert "silent" in result["notes"][0]


def test_too_short_for_lufs_says_to_use_rms(band, call):
    result = call("measure_track", track_index=0, start=0.0, end=0.2)
    assert result["measurement"]["integrated_lufs"] is None
    assert result["measurement"]["rms_db"] is not None
    assert "rms_db" in result["notes"][0]


def test_a_lost_render_leaves_no_temp_file(band, call, monkeypatch, tmp_path):
    """REAPER may have rendered the file when the reply was lost; nobody
    would ever delete it (they were left in /tmp)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()
    band.lose_request("Main_OnCommand", applied=True)
    assert call("measure_track", track_index=0)["outcome"] == "uncertain"
    assert list((tmp_path / "temp").iterdir()) == []


def test_a_lost_render_puts_solo_and_time_selection_back_at_the_next_call(band, call):
    band.tracks[1].info["I_SOLO"] = 1.0
    band.time_selection = (3.0, 4.0)
    band.lose_request("Main_OnCommand", applied=True)
    assert call("measure_track", track_index=0, start=0.0, end=1.0)["outcome"] == "uncertain"
    assert band.tracks[1].info["I_SOLO"] == 0.0  # nothing could be sent after the loss
    assert call("get_context")["success"] is True
    assert [t.info["I_SOLO"] for t in band.tracks] == [0.0, 1.0]
    assert band.time_selection == (3.0, 4.0)
    assert band.project_info == UNSET_RENDER_SETTINGS


# Before/after


def test_a_3_db_fader_move_is_verified(band, call):
    before = call("measure_track", track_index=0)
    assert call("set_track_volume", track_index=0, volume_db=3.0)["success"] is True
    after = call("measure_track", track_index=0)

    result = call("compare_measurements", before=before, after=after, expected_change_db=3.0)
    assert result["verdict"] == "verified" and result["verified"] is True
    assert result["change_db"] == pytest.approx(3.0, abs=0.05)
    assert result["changes_db"]["sample_peak_dbfs"] == pytest.approx(3.0, abs=0.05)
    assert "verified" in result["message"]


def test_a_change_that_missed_is_not_verified(band, call):
    before = call("measure_track", track_index=0)
    call("set_track_volume", track_index=0, volume_db=1.0)
    after = call("measure_track", track_index=0)
    result = call("compare_measurements", before=before, after=after, expected_change_db=3.0)
    assert result["verdict"] == "not_verified" and result["verified"] is False
    assert "not verified" in result["message"]


def test_without_an_expectation_the_changes_are_only_reported(band, call):
    before = call("measure_track", track_index=0)
    call("set_track_volume", track_index=0, volume_db=-2.0)
    after = call("measure_track", track_index=0)
    result = call("compare_measurements", before=before, after=after, metric="rms_db")
    assert (result["verdict"], result["verified"]) == (None, None)
    assert result["change_db"] == pytest.approx(-2.0, abs=0.05)
    assert "quieter" in result["message"]


def test_measurements_of_different_tracks_cannot_verify(band, call):
    vocal = call("measure_track", track_index=0)
    guitar = call("measure_track", track_index=1)
    result = call("compare_measurements", before=vocal, after=guitar, expected_change_db=0.0)
    assert (result["verdict"], result["verified"], result["comparable"]) == (
        "cannot_verify", None, False)
    assert "different tracks" in result["message"]


def test_measurements_of_different_ranges_cannot_verify(band, call):
    whole = call("measure_track", track_index=0)
    part = call("measure_track", track_index=0, start=0.0, end=1.0)
    result = call("compare_measurements", before=whole, after=part, expected_change_db=0.0)
    assert result["verdict"] == "cannot_verify"


def test_silence_cannot_verify(band, call):
    before = call("measure_track", track_index=0)
    band.silent = True
    after = call("measure_track", track_index=0)
    result = call("compare_measurements", before=before, after=after, expected_change_db=3.0)
    assert (result["verdict"], result["verified"]) == ("cannot_verify", None)
    assert result["change_db"] is None


IDENTITY = {"track_guid": "{A}", "start": None, "end": None, "include_master_fx": False}


def test_clipping_after_is_warned_about(call, reaper):
    before = {**IDENTITY, "measurement": {"integrated_lufs": -20.0, "clipping": False}}
    after = {**IDENTITY, "measurement": {"integrated_lufs": -17.0, "clipping": True}}
    result = call("compare_measurements", before=before, after=after, expected_change_db=3.0)
    assert result["verdict"] == "verified"
    assert "clips" in result["warning"]


@pytest.mark.parametrize(("arguments", "error"), [
    ({"metric": "peak"}, "metric must be one of"),
    ({"tolerance_db": 0.0}, "tolerance_db must be more than 0"),
])
def test_compare_measurements_refusals(call, reaper, arguments, error):
    result = call("compare_measurements", before={}, after={}, **arguments)
    assert result["success"] is False and error in result["error"]


def test_only_the_measurement_parts_cannot_verify(band, call):
    """Without track and range, two different tracks could pass for one."""
    vocal = call("measure_track", track_index=0)
    guitar = call("measure_track", track_index=1)
    result = call("compare_measurements", before=vocal["measurement"],
                  after=guitar["measurement"], expected_change_db=0.0)
    assert (result["verdict"], result["comparable"]) == ("cannot_verify", False)
    assert "whole measure_track replies" in result["message"]
    assert result["changes_db"] is None


def test_incomparable_measurements_report_no_changes(band, call):
    vocal = call("measure_track", track_index=0)
    band.tracks[1].info["D_VOL"] = db_to_linear(4.0)
    guitar = call("measure_track", track_index=1)
    result = call("compare_measurements", before=vocal, after=guitar)
    assert result["changes_db"] is None


# The user's render dialog and master FX must not change the numbers


def test_render_normalization_does_not_hide_a_fader_move(band, call):
    band.project_info.update({"RENDER_NORMALIZE": 1.0, "RENDER_SETTINGS": 2.0,
                              "RENDER_TAILFLAG": 4.0, "RENDER_TAILMS": 1000.0})
    before = call("measure_track", track_index=0, start=0.0, end=2.0)
    call("set_track_volume", track_index=0, volume_db=3.0)
    after = call("measure_track", track_index=0, start=0.0, end=2.0)
    assert after["measurement"]["seconds"] == 2.0  # no tail
    result = call("compare_measurements", before=before, after=after, expected_change_db=3.0)
    assert result["verdict"] == "verified"
    assert band.project_info["RENDER_NORMALIZE"] == 1.0
    assert band.project_info["RENDER_SETTINGS"] == 2.0
    assert band.project_info["RENDER_TAILFLAG"] == 4.0


@pytest.fixture
def limited(band):
    band.master.fxs.append(FXState("ReaLimit", [0.5, 0.5, 0.5]))
    band.master.fxs.append(FXState("ReaEQ", [0.5, 0.5, 0.5], enabled=False))
    return band


def test_master_fx_are_bypassed_for_the_measurement_and_switched_back_on(limited, call):
    before = call("measure_track", track_index=0)
    call("set_track_volume", track_index=0, volume_db=3.0)
    after = call("measure_track", track_index=0)
    assert [fx.enabled for fx in limited.master.fxs] == [True, False]
    assert after["master_fx"] == ["VST: ReaLimit (Cockos)"]
    assert "were bypassed" in after["what_is_measured"]
    result = call("compare_measurements", before=before, after=after, expected_change_db=3.0)
    assert result["verdict"] == "verified"


def test_tracks_with_solo_defeat_are_named_as_heard(band, call):
    band.tracks[1].info["B_SOLO_DEFEAT"] = 1.0
    alone = call("measure_track", track_index=0)
    assert any("solo defeat" in n and "Guitar" in n for n in alone["notes"]), alone["notes"]
    assert not any("solo defeat" in n for n in call("measure_track", track_index=1)["notes"])


def test_bypassing_the_master_fx_leaves_no_undo_step_and_a_clean_project(limited, call):
    """Each FX's own switch (TrackFX_SetEnabled) would record "FX bypass toggle"
    and mark the project dirty (7.69); the chain's bypass (I_FXEN) does not."""
    steps = list(limited.undo_steps)
    limited.dirty = False
    assert call("measure_track", track_index=0)["success"] is True
    assert limited.undo_steps == steps and limited.dirty is False
    assert limited.master.value("I_FXEN") == 1.0


def test_through_a_master_limiter_a_fader_move_hides_and_the_reply_says_why(limited, call):
    before = call("measure_track", track_index=0, include_master_fx=True)
    call("set_track_volume", track_index=0, volume_db=3.0)
    after = call("measure_track", track_index=0, include_master_fx=True)
    assert "may not show up 1:1" in after["notes"][0]
    result = call("compare_measurements", before=before, after=after, expected_change_db=3.0)
    assert result["verdict"] == "not_verified"
    assert "include_master_fx=false" in result["hint"]


def test_measurements_with_and_without_master_fx_cannot_verify(limited, call):
    before = call("measure_track", track_index=0)
    after = call("measure_track", track_index=0, include_master_fx=True)
    result = call("compare_measurements", before=before, after=after, expected_change_db=0.0)
    assert result["verdict"] == "cannot_verify"


def test_a_lost_render_switches_the_master_fx_back_on_at_the_next_call(limited, call):
    limited.lose_request("Main_OnCommand", applied=True)
    assert call("measure_track", track_index=0)["outcome"] == "uncertain"
    assert call("get_context")["success"] is True
    assert [fx.enabled for fx in limited.master.fxs] == [True, False]


@pytest.mark.parametrize("transport", ["playing", "recording"])
def test_measure_track_refuses_while_the_transport_runs(band, call, transport):
    band.transport = transport
    result = call("measure_track", track_index=0)
    assert result["success"] is False and transport in result["error"]
    assert band.renders == []
    assert [t.info["I_SOLO"] for t in band.tracks] == [0.0, 0.0]


def test_a_range_past_the_end_of_the_project_is_refused(band, call):
    result = call("measure_track", track_index=0, start=1000.0, end=1001.0)
    assert result["success"] is False and "past the end of the project" in result["error"]
    assert band.renders == []


def test_a_range_ending_past_the_project_is_cut_at_its_end(band, call):
    result = call("measure_track", track_index=0, start=3.0, end=10.0)
    assert (result["start"], result["end"]) == (3.0, 4.0)
    assert result["measurement"]["seconds"] == 1.0
    assert "past the end of the project" in result["notes"][0]
