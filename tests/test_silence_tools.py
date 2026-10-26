"""remove_silence, trim_silence and normalize_item, analyzing real audio files
written for each test, against the fake REAPER."""

import pytest

from tests.fake_reaper.objects import ItemState
from tests.helpers import write_wav

# 1 s silence, 1 s tone, 1 s silence, 1 s tone, 1 s silence.
GAPPY = [(1.0, 0.0), (1.0, 0.5), (1.0, 0.0), (1.0, 0.5), (1.0, 0.0)]


def _audio(reaper, tmp_path, segments=GAPPY, position=10.0, length=None, **kwargs):
    path = write_wav(tmp_path / "take.wav", segments)
    track = reaper.add_track("Vox")
    length = sum(s for s, _ in segments) if length is None else length
    item = ItemState(reaper.new_pointer("MediaItem"), position, length, midi=False,
                     source_file=path, **kwargs)
    track.items.append(item)
    return track, item


def _spans(track):
    return [(round(i.position, 6), round(i.length, 6)) for i in track.items]


# remove_silence


def test_remove_silence_splits_and_deletes_the_silent_parts(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path)
    result = call("remove_silence", item=item.guid)
    assert result["success"] is True, result
    assert result["removed"] == [{"start": 10.0, "end": 10.9}, {"start": 12.1, "end": 12.9},
                                 {"start": 14.1, "end": 15.0}]
    assert result["deleted_pieces"] == 3
    assert _spans(track) == [(10.9, 1.2), (12.9, 1.2)]
    assert [k["guid"] for k in result["kept"]] == [i.guid for i in track.items]
    # The kept audio stays where it was: each piece plays the file from its own time.
    assert [round(i.take_info["D_STARTOFFS"], 6) for i in track.items] == [0.9, 2.9]
    assert all(i.info["D_FADEINLEN"] == i.info["D_FADEOUTLEN"] == 0.01 for i in track.items)
    assert len(reaper.undo_steps) == 1
    call("undo")
    assert _spans(reaper.tracks[0]) == [(10.0, 5.0)]


def test_remove_silence_respects_playrate(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path, length=2.5)
    item.take_info["D_PLAYRATE"] = 2.0
    result = call("remove_silence", item=item.guid)
    assert result["removed"] == [{"start": 10.0, "end": 10.4}, {"start": 11.1, "end": 11.4},
                                 {"start": 12.1, "end": 12.5}]
    assert _spans(track) == [(10.4, 0.7), (11.4, 0.7)]


def test_remove_silence_respects_the_start_offset(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path, length=4.0)
    item.take_info["D_STARTOFFS"] = 1.0
    result = call("remove_silence", item=item.guid)
    assert result["removed"] == [{"start": 11.1, "end": 11.9}, {"start": 13.1, "end": 14.0}]
    assert _spans(track) == [(10.0, 1.1), (11.9, 1.2)]
    assert track.items[0].info["D_FADEINLEN"] == 0.0  # the item's own start was not cut


def test_remove_silence_reads_a_section_from_its_parent_file(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, length=3.0)
    item.take.section = (1.0, 3.0, False)
    result = call("remove_silence", item=item.guid, dry_run=True)
    assert result["removed"] == [{"start": 11.1, "end": 11.9}]


def test_remove_silence_follows_a_looped_source(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, segments=[(0.5, 0.5), (0.5, 0.0)], length=3.0,
                     info={"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 0.0, "B_LOOPSRC": 1.0})
    result = call("remove_silence", item=item.guid, dry_run=True)
    assert result["removed"] == [{"start": 10.6, "end": 10.9}, {"start": 11.6, "end": 11.9},
                                 {"start": 12.6, "end": 13.0}]


def test_remove_silence_dry_run_changes_nothing(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path)
    result = call("remove_silence", item=item.guid, dry_run=True, padding=0.0, min_silence=1.0)
    assert result["dry_run"] is True and len(result["removed"]) == 3
    assert _spans(track) == [(10.0, 5.0)]


def test_remove_silence_min_silence_keeps_short_gaps(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path)
    result = call("remove_silence", item=item.guid, min_silence=1.5)
    assert result["removed"] == []
    assert result["message"].startswith("No silence found")
    assert _spans(track) == [(10.0, 5.0)]


def test_remove_silence_refuses_an_all_silent_item(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path, segments=[(2.0, 0.0)])
    result = call("remove_silence", item=item.guid)
    assert result["success"] is False and "Lower threshold_db" in result["error"]
    assert _spans(track) == [(10.0, 2.0)]


def test_remove_silence_refuses_reversed_takes(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path)
    item.take.section = (0.0, 5.0, True)
    result = call("remove_silence", item=item.guid)
    assert result["success"] is False and "reversed" in result["error"]
    assert _spans(track) == [(10.0, 5.0)]


def test_remove_silence_warns_about_take_fx(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path)
    item.take.fx_count = 1
    result = call("remove_silence", item=item.guid, dry_run=True)
    assert result["warnings"] == ["The take has take FX; the analysis reads the recording "
                                  "without them"]


def test_remove_silence_refuses_midi_missing_files_and_locked(reaper, call, tmp_path):
    track = reaper.add_track("Keys")
    midi = ItemState(reaper.new_pointer("MediaItem"), 0.0, 2.0, midi=True)
    missing = ItemState(reaper.new_pointer("MediaItem"), 4.0, 2.0, midi=False,
                        source_file=str(tmp_path / "gone.wav"))
    track.items += [midi, missing]
    assert "MIDI" in call("remove_silence", item=midi.guid)["error"]
    assert "cannot be found" in call("remove_silence", item=missing.guid)["error"]
    _, locked = _audio(reaper, tmp_path, info={"C_LOCK": 1.0})
    assert "locked" in call("remove_silence", item=locked.guid)["error"]


def _stereo(tmp_path, left, right, rate=48000):
    import numpy as np
    import soundfile as sf

    path = tmp_path / "stereo.wav"
    sf.write(path, np.stack([left, right], axis=1), rate, subtype="FLOAT")
    return str(path)


def test_remove_silence_hears_only_the_channel_the_take_plays(reaper, call, tmp_path):
    import numpy as np

    rate = 48000
    t = np.arange(3 * rate) / rate
    left = np.where((t >= 1) & (t < 2), 0.5 * np.sin(2 * np.pi * 220 * t), 0.0)
    right = np.random.default_rng(0).normal(0, 0.0126, len(t))  # about -38 dBFS of hiss
    path = _stereo(tmp_path, left, right)
    track = reaper.add_track("Vox")
    item = ItemState(reaper.new_pointer("MediaItem"), 0.0, 3.0, midi=False, source_file=path)
    track.items.append(item)
    assert call("remove_silence", item=item.guid, dry_run=True)["removed"] == []
    item.take_info["I_CHANMODE"] = 3.0  # left only
    assert call("remove_silence", item=item.guid, dry_run=True)["removed"] == [
        {"start": 0.0, "end": 0.9}, {"start": 2.1, "end": 3.0}]
    item.take_info["I_CHANMODE"] = 4.0  # right only: hiss all the way
    assert call("remove_silence", item=item.guid, dry_run=True)["removed"] == []
    item.take_info["I_CHANMODE"] = 1.0  # reversed stereo still hears both
    assert call("remove_silence", item=item.guid, dry_run=True)["removed"] == []


def test_remove_silence_refuses_stretch_markers(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path)
    item.take.stretch_markers = 2
    result = call("remove_silence", item=item.guid)
    assert result["success"] is False and "stretch markers" in result["error"]
    assert "stretch markers" in call("trim_silence", item=item.guid)["error"]
    assert _spans(track) == [(10.0, 5.0)]
    normalized = call("normalize_item", item=item.guid)
    assert normalized["success"] is True
    assert "stretch markers" in normalized["warnings"][0]


def test_remove_silence_warns_that_other_takes_are_cut_too(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path)
    item.extra_takes = 1
    result = call("remove_silence", item=item.guid, dry_run=True)
    assert "other takes are cut" in result["warnings"][0]


def test_remove_silence_dry_run_totals(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path)
    result = call("remove_silence", item=0, track_index=0, dry_run=True)
    assert (result["removed_seconds"], result["pieces_after"]) == (2.6, 2)
    assert item.guid in result["note"]


def test_remove_silence_reads_only_the_looped_part_it_plays(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, length=2.0,
                     info={"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 0.0, "B_LOOPSRC": 1.0})
    item.take_info["D_STARTOFFS"] = 1.0
    result = call("remove_silence", item=item.guid, dry_run=True)
    assert result["removed"] == [{"start": 11.1, "end": 12.0}]


def test_remove_silence_refuses_negative_settings(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path)
    result = call("remove_silence", item=item.guid, padding=-1.0)
    assert result == {"success": False, "error": "min_silence and padding must be >= 0 seconds"}


# trim_silence


def test_trim_silence_trims_both_ends_only(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path)
    result = call("trim_silence", item=item.guid)
    assert result["success"] is True, result
    assert (result["trimmed_start_seconds"], result["trimmed_end_seconds"]) == (0.9, 0.9)
    assert _spans(track) == [(10.9, 3.2)]
    assert round(item.take_info["D_STARTOFFS"], 6) == 0.9


def test_trim_silence_dry_run_and_nothing_to_trim(reaper, call, tmp_path):
    track, item = _audio(reaper, tmp_path)
    assert call("trim_silence", item=item.guid, dry_run=True)["trimmed_start_seconds"] == 0.9
    assert _spans(track) == [(10.0, 5.0)]
    track2, loud = _audio(reaper, tmp_path, segments=[(1.0, 0.5)], position=0.0)
    result = call("trim_silence", item=loud.guid)
    assert (result["trimmed_start_seconds"], result["trimmed_end_seconds"]) == (0.0, 0.0)
    assert _spans(track2) == [(0.0, 1.0)]


def test_trim_silence_refuses_an_all_silent_item(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, segments=[(1.0, 0.0)])
    assert call("trim_silence", item=item.guid)["success"] is False


# normalize_item


def test_normalize_item_to_peak(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path)
    result = call("normalize_item", item=item.guid)
    assert result["success"] is True, result
    assert (result["mode"], result["target_db"]) == ("peak", -1.0)
    assert result["take_volume_db"] == pytest.approx(5.02, abs=0.01)
    assert result["previous_take_volume_db"] == 0.0
    assert item.take_info["D_VOL"] == pytest.approx(10 ** (5.0206 / 20), rel=1e-3)
    assert "warnings" not in result


def test_normalize_item_to_lufs(reaper, call, tmp_path):
    import pyloudnorm
    import soundfile as sf

    _, item = _audio(reaper, tmp_path)
    data, rate = sf.read(item.source_file)
    expected = -20.0 - pyloudnorm.Meter(rate).integrated_loudness(data)
    result = call("normalize_item", item=item.guid, mode="LUFS", target_db=-20.0)
    assert result["take_volume_db"] == pytest.approx(expected, abs=0.05)


def test_normalize_item_measures_only_the_items_part(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, segments=[(1.0, 0.9), (1.0, 0.25)], length=1.0)
    item.take_info["D_STARTOFFS"] = 1.0
    result = call("normalize_item", item=item.guid, target_db=0.0)
    assert result["take_volume_db"] == pytest.approx(12.04, abs=0.01)


def test_normalize_item_keeps_polarity(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path)
    item.take_info["D_VOL"] = -1.0
    call("normalize_item", item=item.guid)
    assert item.take_info["D_VOL"] < 0


def test_normalize_item_takes_the_item_volume_into_account(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, info={"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 0.0,
                                             "D_VOL": 0.5})
    result = call("normalize_item", item=item.guid)
    # Peak -6.02 dBFS, item at -6.02 dB: the take makes up the rest to reach -1.
    assert result["take_volume_db"] == pytest.approx(11.04, abs=0.01)
    assert result["item_volume_db"] == pytest.approx(-6.02, abs=0.01)
    assert "item's own volume" in result["warnings"][0]


def test_normalize_item_refuses_a_silenced_item(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, info={"D_FADEINLEN": 0.0, "D_FADEOUTLEN": 0.0,
                                             "D_VOL": 0.0})
    result = call("normalize_item", item=item.guid)
    assert result["success"] is False and "-inf dB" in result["error"]


def test_normalize_item_refuses_more_than_edit_item_allows(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, segments=[(1.0, 0.05)])  # peak -26 dBFS
    result = call("normalize_item", item=item.guid)
    assert result["success"] is False and "+25.0 dB" in result["error"]


def test_normalize_item_refuses_a_nearly_silent_item(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path, segments=[(1.0, 0.001)])
    result = call("normalize_item", item=item.guid)
    assert result["success"] is False and "nearly silent" in result["error"]
    assert item.take_info.get("D_VOL", 1.0) == 1.0


def test_normalize_item_refuses_unknown_mode(reaper, call, tmp_path):
    _, item = _audio(reaper, tmp_path)
    result = call("normalize_item", item=item.guid, mode="rms")
    assert result == {"success": False, "error": "mode must be one of ['lufs', 'peak']"}
