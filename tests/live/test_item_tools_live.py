"""Item, take and silence tools against a running REAPER.

Where a tool reads the take's file itself (remove_silence, normalize_item),
REAPER's own offline render of the item is the reference: what the tool
says is silent or how loud it is must match what REAPER plays.
"""

import re
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tests.live.rpr import RPR

pytestmark = pytest.mark.live

RATE = 48000
CROSSFADE_ON_SPLIT = 40912  # Options: Toggle crossfade on split
CROSSFADE_OVERLAPS = 41059  # Item: Crossfade any overlapping items
TOGGLE_REVERSE = 41051  # Item properties: Toggle take reverse
WINDOW = 0.01


def write(path, left, right=None) -> str:
    right = left if right is None else right
    sf.write(path, np.stack([left, right], axis=1), RATE)
    return str(path)


def sine(seconds: float, hz: float = 440.0, amplitude: float = 0.1) -> np.ndarray:
    return amplitude * np.sin(2 * np.pi * hz * np.arange(int(RATE * seconds)) / RATE)


def gapped(seconds_pattern: list[tuple[str, float]]) -> np.ndarray:
    """("tone"|"gap", seconds) pieces joined: gaps are digital silence."""
    return np.concatenate([sine(s) if kind == "tone" else np.zeros(int(RATE * s))
                           for kind, s in seconds_pattern])


GAPPED = [("tone", 1.0), ("gap", 1.0), ("tone", 1.0), ("gap", 1.5), ("tone", 1.5)]


def insert(project, track, path: str, position: float = 0.0) -> str:
    RPR.SetOnlyTrackSelected(track.id)
    project.cursor_position = position
    RPR.InsertMedia(path, 0)
    return RPR.GetTrackMediaItem(track.id, RPR.CountTrackMediaItems(track.id) - 1)


def value(item: str, param: str) -> float:
    return RPR.GetMediaItemInfo_Value(item, param)


def take_value(item: str, param: str, take: str | None = None) -> float:
    return RPR.GetMediaItemTakeInfo_Value(take or RPR.GetActiveTake(item), param)


def guid(item: str) -> str:
    return RPR.GetSetMediaItemInfo_String(item, "GUID", "", False)[3]


def render(call, tmp_path, start: float, end: float, name: str = "r.wav") -> np.ndarray:
    out = tmp_path / name
    result = call("render_time_selection", output_path=str(out), start=start, end=end,
                  bit_depth=32, overwrite=True)
    assert result["success"] is True, result
    data, rate = sf.read(str(out), always_2d=True)
    assert rate == RATE
    return data


def silent_runs(data: np.ndarray, *, offset: float, threshold_db: float = -45.0,
                min_seconds: float = 0.4) -> list[tuple[float, float]]:
    """Stretches of data quieter than threshold_db (RMS per 10 ms window)."""
    size = int(RATE * WINDOW)
    count = len(data) // size
    rms = np.sqrt(np.mean(data[:count * size].reshape(count, size, -1) ** 2, axis=(1, 2)))
    quiet = 20 * np.log10(np.maximum(rms, 1e-12)) < threshold_db
    runs, start = [], None
    for i, q in enumerate([*quiet, False]):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * WINDOW >= min_seconds:
                runs.append((offset + start * WINDOW, offset + i * WINDOW))
            start = None
    return runs


def assert_same_runs(removed: list[dict], expected: list[tuple[float, float]], tol=0.03):
    got = [(r["start"], r["end"]) for r in removed]
    assert len(got) == len(expected), (got, expected)
    for (a, b), (x, y) in zip(got, expected, strict=True):
        assert a == pytest.approx(x, abs=tol) and b == pytest.approx(y, abs=tol), (got, expected)


@pytest.fixture
def option(live_project):
    """option(command, on): set a REAPER toggle option for the test, put back after."""
    changed: list[int] = []

    def set_option(command: int, on: bool) -> None:
        if bool(RPR.GetToggleCommandState(command)) != on:
            RPR.Main_OnCommand(command, 0)
            changed.append(command)

    yield set_option
    for command in changed:
        RPR.Main_OnCommand(command, 0)


# The actions


def test_the_glue_and_crop_action_ids_have_the_expected_names(live_project):
    assert RPR.kbd_getTextFromCmd(40362, 0) == "Item: Glue items, ignoring time selection"
    assert RPR.kbd_getTextFromCmd(40131, 0) == "Take: Crop to active take in items"


def test_glue_makes_one_item_over_the_extent_and_restores_the_selection(
        project_in_tmp, live_project, call, tmp_path):
    track = live_project.add_track(0, "g")
    a = insert(live_project, track, write(tmp_path / "a.wav", sine(1.0)), 0.0)
    b = insert(live_project, track, write(tmp_path / "b.wav", sine(1.0, 880)), 1.5)
    other = insert(live_project, track, write(tmp_path / "c.wav", sine(1.0)), 4.0)
    RPR.SelectAllMediaItems(0, False)
    RPR.SetMediaItemSelected(other, True)
    RPR.SetMediaItemSelected(a, True)
    originals = {guid(a), guid(b)}
    result = call("glue_items", items_to_glue=[guid(a), guid(b)])
    assert result["success"] is True, result
    glued = result["item"]
    assert glued["position"] == pytest.approx(0.0)
    # The rendered file is whole samples long: the end lands within a sample.
    assert glued["position"] + glued["length"] == pytest.approx(2.5, abs=1 / RATE)
    items = [RPR.GetTrackMediaItem(track.id, i) for i in range(RPR.CountTrackMediaItems(track.id))]
    assert len(items) == 2
    print(f"glue reuses the first item: {glued['guid'] in originals}")
    path = glued["source_file"]
    assert path and path.startswith(RPR.GetProjectPath("", 512)[0]), path
    # The user's selection is back: the other item, not the glued-away ones.
    assert RPR.IsMediaItemSelected(other)
    assert not RPR.IsMediaItemSelected(items[0])


def test_crop_to_active_take_keeps_the_active_take_and_undo_brings_the_rest(
        live_project, call, tmp_path):
    track = live_project.add_track(0, "t")
    item = insert(live_project, track, write(tmp_path / "one.wav", sine(1.0)))
    take = RPR.AddTakeToMediaItem(item)
    RPR.SetMediaItemTake_Source(take, RPR.PCM_Source_CreateFromFile(
        write(tmp_path / "two.wav", sine(1.0, 880))))
    RPR.SetActiveTake(take)
    active_name = RPR.GetSetMediaItemTakeInfo_String(take, "P_NAME", "", False)[3]
    RPR.SelectAllMediaItems(0, False)
    result = call("crop_to_active_take", item=guid(item))
    assert result["success"] is True, result
    assert RPR.CountTakes(item) == 1
    assert RPR.GetSetMediaItemTakeInfo_String(RPR.GetActiveTake(item), "P_NAME", "", False)[3] \
        == active_name
    assert not RPR.IsMediaItemSelected(item)  # selection put back
    assert call("undo")["success"] is True
    assert RPR.CountTakes(item) == 2


# Splits, trims and fades


def test_split_keeps_the_left_guid_and_offsets_the_right_takes(live_project, call, tmp_path):
    track = live_project.add_track(0, "s")
    item = insert(live_project, track, write(tmp_path / "t.wav", sine(4.0)))
    RPR.SetMediaItemTakeInfo_Value(RPR.GetActiveTake(item), "D_STARTOFFS", 0.25)
    RPR.SetMediaItemTakeInfo_Value(RPR.GetActiveTake(item), "D_PLAYRATE", 2.0)
    RPR.SetMediaItemInfo_Value(item, "D_LENGTH", 1.5)
    RPR.SetMediaItemInfo_Value(item, "D_FADEINLEN", 0.1)
    RPR.SetMediaItemInfo_Value(item, "D_FADEOUTLEN", 0.2)
    original = guid(item)
    result = call("split_item", item=original, times=[0.6])
    assert result["success"] is True, result
    left, right = (RPR.GetTrackMediaItem(track.id, i) for i in range(2))
    assert guid(left) == original and guid(right) != original
    assert (value(left, "D_LENGTH"), value(right, "D_POSITION")) == pytest.approx((0.6, 0.6))
    assert take_value(right, "D_STARTOFFS") == pytest.approx(0.25 + 0.6 * 2.0)
    # The left keeps the fade-in, the right the fade-out; each gets REAPER's
    # default short fade (10 ms) at the cut.
    assert value(left, "D_FADEINLEN") == pytest.approx(0.1)
    assert value(right, "D_FADEOUTLEN") == pytest.approx(0.2)
    assert value(left, "D_FADEOUTLEN") <= 0.011 and value(right, "D_FADEINLEN") <= 0.011


def test_split_cuts_close_to_the_edges_and_to_each_other(live_project, call, tmp_path):
    track = live_project.add_track(0, "s")
    item = insert(live_project, track, write(tmp_path / "t.wav", sine(2.0)))
    result = call("split_item", item=guid(item), times=[0.001, 1.0, 1.001, 1.999])
    assert result["success"] is True, result
    assert RPR.CountTrackMediaItems(track.id) == 5
    assert RPR.SplitMediaItem(RPR.GetTrackMediaItem(track.id, 0), 50.0).endswith("0x0000000000000000")


def test_crossfade_on_split_lengthens_the_left_piece(live_project, call, tmp_path, option):
    """With Options > "crossfade on split" on (off by default here), REAPER
    makes the left piece longer by the crossfade; remove_silence still works."""
    option(CROSSFADE_ON_SPLIT, True)
    track = live_project.add_track(0, "x")
    item = insert(live_project, track, write(tmp_path / "t.wav", sine(4.0)))
    call("split_item", item=guid(item), times=[2.0])
    left = RPR.GetTrackMediaItem(track.id, 0)
    assert value(left, "D_LENGTH") > 2.0
    assert value(left, "D_FADEOUTLEN_AUTO") > 0
    gaps = insert(live_project, live_project.add_track(1, "g"),
                  write(tmp_path / "g.wav", gapped(GAPPED)))
    result = call("remove_silence", item=guid(gaps), padding=0.0)
    assert result["success"] is True, result
    assert len(result["kept"]) == 3


def test_a_start_trim_keeps_every_take_aligned(live_project, call, tmp_path):
    track = live_project.add_track(0, "t")
    item = insert(live_project, track, write(tmp_path / "one.wav", sine(4.0)))
    second = RPR.AddTakeToMediaItem(item)
    RPR.SetMediaItemTake_Source(second, RPR.PCM_Source_CreateFromFile(
        write(tmp_path / "two.wav", sine(4.0, 880))))
    RPR.SetMediaItemTakeInfo_Value(second, "D_PLAYRATE", 2.0)
    first = RPR.GetTake(item, 0)
    assert call("edit_audio_item", track_index=0, item_index=0, start_trim=0.5)["success"]
    assert take_value(item, "D_STARTOFFS", first) == pytest.approx(0.5)
    assert take_value(item, "D_STARTOFFS", second) == pytest.approx(1.0)
    live_project.time_selection = (1.0, 2.0)
    assert call("trim_item_to_time_selection", item=guid(item))["success"] is True
    assert take_value(item, "D_STARTOFFS", first) == pytest.approx(1.0)
    assert take_value(item, "D_STARTOFFS", second) == pytest.approx(2.0)


def test_fade_shapes_as_heard(live_project, call, tmp_path):
    """Gain at 25/50/75 % through a 1 s fade-in for each C_FADEINSHAPE, from a
    render. Printed for LIVE_CHECKS; 0 is linear."""
    track = live_project.add_track(0, "f")
    item = insert(live_project, track, write(tmp_path / "dc.wav", np.full(RATE * 2, 0.5)))
    RPR.SetMediaItemInfo_Value(item, "D_FADEINLEN", 1.0)
    gains = {}
    for shape in range(7):
        RPR.SetMediaItemInfo_Value(item, "C_FADEINSHAPE", shape)
        data = render(call, tmp_path, 0.0, 1.0, f"shape{shape}.wav")[:, 0] / 0.5
        gains[shape] = [round(float(data[int(RATE * f)]), 3) for f in (0.25, 0.5, 0.75)]
    print("fade-in gain at 25/50/75%:", gains)
    assert gains[0] == pytest.approx([0.25, 0.5, 0.75], abs=0.01)
    assert len({tuple(g) for g in gains.values()}) >= 5


def test_auto_crossfades_override_manual_fades(live_project, call, tmp_path):
    track = live_project.add_track(0, "x")
    a = insert(live_project, track, write(tmp_path / "a.wav", np.full(RATE * 4, 0.5)), 0.0)
    b = insert(live_project, track, write(tmp_path / "b.wav", np.full(RATE * 4, 0.5)), 3.0)
    RPR.SelectAllMediaItems(0, False)
    RPR.SetMediaItemSelected(a, True)
    RPR.SetMediaItemSelected(b, True)
    RPR.Main_OnCommand(CROSSFADE_OVERLAPS, 0)
    assert value(b, "D_FADEINLEN_AUTO") == pytest.approx(1.0)
    listed = call("list_items", track_index=0, detail=True)["items"]
    print("list_items fades with an auto crossfade:", [
        {k: v for k, v in i.items() if "fade" in k} for i in listed])
    # A manual fade-in of 0.2 s is not what plays while the auto fade is set.
    RPR.SetMediaItemInfo_Value(b, "D_FADEINLEN", 0.2)
    RPR.SetMediaItemInfo_Value(live_project.tracks[0].items[0].id, "B_MUTE", 1)
    auto = render(call, tmp_path, 3.0, 4.0, "auto.wav")[:, 0]
    # Halfway up the 1 s auto fade (equal power: 0.5 * 0.71), not done as a
    # 0.2 s manual fade would be.
    assert auto[int(RATE * 0.5)] == pytest.approx(0.5 * 0.71, abs=0.03)
    assert call("edit_item", item=guid(b), fade_in=0.2)["success"] is True
    assert value(b, "D_FADEINLEN_AUTO") == 0.0
    manual = render(call, tmp_path, 3.0, 4.0, "manual.wav")[:, 0]
    assert manual[int(RATE * 0.5)] == pytest.approx(0.5, abs=0.01)


# Sources


def test_source_types_and_a_reversed_take(live_project, call, tmp_path):
    track = live_project.add_track(0, "t")
    path = write(tmp_path / "t.wav", sine(2.0))
    item = insert(live_project, track, path)
    source = RPR.GetMediaItemTake_Source(RPR.GetActiveTake(item))
    assert RPR.GetMediaSourceType(source, "", 64)[1] == "WAVE"
    assert RPR.PCM_Source_GetSectionInfo(source, 0.0, 0.0, False)[0] == 0
    assert RPR.GetMediaSourceLength(source, False)[:1] == [pytest.approx(2.0)]
    midi = track.add_midi_item(4.0, 5.0)
    assert RPR.GetMediaSourceType(RPR.GetMediaItemTake_Source(
        RPR.GetActiveTake(midi.id)), "", 64)[1].startswith("MIDI")
    RPR.SelectAllMediaItems(0, False)
    RPR.SetMediaItemSelected(item, True)
    RPR.Main_OnCommand(TOGGLE_REVERSE, 0)
    section = RPR.GetMediaItemTake_Source(RPR.GetActiveTake(item))
    assert RPR.GetMediaSourceType(section, "", 64)[1] == "SECTION"
    is_section, _, _, _, reverse = RPR.PCM_Source_GetSectionInfo(section, 0.0, 0.0, False)
    assert is_section and reverse
    parent = RPR.GetMediaSourceParent(section)
    assert RPR.GetMediaSourceType(parent, "", 64)[1] == "WAVE"
    # The file itself, or REAPER's copy of it in the project's folder (it
    # copies media there when a saved project takes them in).
    file = RPR.GetMediaSourceFileName(parent, "", 4096)[1]
    assert Path(file).stem.startswith(Path(path).stem) and Path(file).is_file(), file
    listed = call("list_items", track_index=0, detail=True)["items"][0]
    assert listed["source_file"] == file
    refused = call("remove_silence", item=guid(item))
    assert refused["success"] is False and "reversed" in refused["error"]


def test_stretch_markers_are_counted_and_refused_for_cuts(live_project, call, tmp_path):
    track = live_project.add_track(0, "t")
    item = insert(live_project, track, write(tmp_path / "g.wav", gapped(GAPPED)))
    take = RPR.GetActiveTake(item)
    assert RPR.GetTakeNumStretchMarkers(take) == 0
    RPR.SetTakeStretchMarker(take, -1, 0.5, -1.0)
    assert RPR.GetTakeNumStretchMarkers(take) == 1
    refused = call("remove_silence", item=guid(item))
    assert refused["success"] is False and "stretch markers" in refused["error"]


# remove_silence against REAPER's own render


def _gapped_item(live_project, tmp_path, *, offset=0.0, rate=1.0, length=None, loop=False):
    track = live_project.add_track(0, "v")
    item = insert(live_project, track, write(tmp_path / "g.wav", gapped(GAPPED)))
    take = RPR.GetActiveTake(item)
    RPR.SetMediaItemTakeInfo_Value(take, "D_STARTOFFS", offset)
    RPR.SetMediaItemTakeInfo_Value(take, "D_PLAYRATE", rate)
    RPR.SetMediaItemInfo_Value(item, "B_LOOPSRC", 1 if loop else 0)
    RPR.SetMediaItemInfo_Value(item, "D_LENGTH", length or (6.0 - offset) / rate)
    RPR.SetMediaItemInfo_Value(item, "D_POSITION", 1.0)
    return item


@pytest.mark.parametrize(("offset", "rate", "length", "loop"), [
    (0.0, 1.0, None, False), (0.0, 2.0, None, False), (0.7, 1.0, None, False),
    (0.0, 1.0, 14.0, True), (0.7, 2.0, 5.0, True)])
def test_remove_silence_dry_run_matches_the_render(live_project, call, tmp_path,
                                                   offset, rate, length, loop):
    item = _gapped_item(live_project, tmp_path, offset=offset, rate=rate, length=length,
                        loop=loop)
    end = value(item, "D_POSITION") + value(item, "D_LENGTH")
    expected = silent_runs(render(call, tmp_path, 1.0, end), offset=1.0)
    result = call("remove_silence", item=guid(item), padding=0.0, dry_run=True)
    assert result["success"] is True, result
    assert_same_runs(result["removed"], expected)


def test_remove_silence_on_a_section_take_matches_the_render(live_project, call, tmp_path):
    """A section (a part of the file, as Item properties > Section makes it)
    starting 1.5 s into the file, played from 0.5 s into the section."""
    item = _gapped_item(live_project, tmp_path)
    ok, _, chunk, _, _ = RPR.GetItemStateChunk(item, "", 1 << 20, False)
    assert ok
    wave = re.search(r"<SOURCE WAVE\n.*?\n\s*>", chunk, re.S).group(0)
    section = f"<SOURCE SECTION\nLENGTH 4\nSTARTPOS 1.5\nOVERLAP 0\n{wave}\n>"
    assert RPR.SetItemStateChunk(item, chunk.replace(wave, section), False)
    take = RPR.GetActiveTake(item)
    info = RPR.PCM_Source_GetSectionInfo(RPR.GetMediaItemTake_Source(take), 0.0, 0.0, False)
    assert (info[0], info[2], info[3], info[4]) == (1, pytest.approx(1.5), pytest.approx(4.0), 0)
    RPR.SetMediaItemTakeInfo_Value(take, "D_STARTOFFS", 0.5)
    RPR.SetMediaItemInfo_Value(item, "D_LENGTH", 3.5)
    expected = silent_runs(render(call, tmp_path, 1.0, 4.5), offset=1.0)
    result = call("remove_silence", item=guid(item), padding=0.0, dry_run=True)
    assert result["success"] is True, result
    assert_same_runs(result["removed"], expected)


def test_remove_silence_cuts_what_the_render_says_is_silent(live_project, call, tmp_path):
    item = _gapped_item(live_project, tmp_path, rate=2.0)
    before = render(call, tmp_path, 1.0, 4.0, "before.wav")
    result = call("remove_silence", item=guid(item), padding=0.05)
    assert result["success"] is True, result
    assert len(result["kept"]) == 3
    after = render(call, tmp_path, 1.0, 4.0, "after.wav")
    # The same sound where sound was, the same silence where it was cut
    # (100 ms RMS windows; fades and padding make the edges differ a little).
    def envelope(data):
        size = RATE // 10
        return np.sqrt(np.mean(data[:len(data) // size * size, 0].reshape(-1, size) ** 2, axis=1))
    assert np.max(np.abs(envelope(before) - envelope(after))) < 0.02


# Levels and channels


def test_normalize_item_peaks_at_the_target_through_the_item_volume(live_project, call, tmp_path):
    track = live_project.add_track(0, "n")
    item = insert(live_project, track, write(tmp_path / "t.wav", sine(2.0, amplitude=0.3)))
    assert call("edit_item", item=guid(item), volume_db=-6.0)["success"] is True
    result = call("normalize_item", item=guid(item), target_db=-1.0)
    assert result["success"] is True, result
    peak = np.max(np.abs(render(call, tmp_path, 0.0, 2.0)))
    assert 20 * np.log10(peak) == pytest.approx(-1.0, abs=0.1)


def _hz(channel: np.ndarray) -> set[int]:
    spectrum = np.abs(np.fft.rfft(channel))
    freqs = np.fft.rfftfreq(len(channel), 1 / RATE)
    return {int(round(f)) for f, s in zip(freqs, spectrum, strict=True)
            if s > 0.2 * spectrum.max()}


def test_channel_modes_as_rendered(live_project, call, tmp_path):
    """I_CHANMODE 0 normal, 1 reverse stereo, 2 downmix, 3 left, 4 right, as
    item_audio reads them: checked against what REAPER plays."""
    track = live_project.add_track(0, "c")
    item = insert(live_project, track, write(tmp_path / "lr.wav", sine(1.0, 440), sine(1.0, 880)))
    expected = {0: ({440}, {880}), 1: ({880}, {440}), 2: ({440, 880}, {440, 880}),
                3: ({440}, {440}), 4: ({880}, {880})}
    for mode, (left, right) in expected.items():
        RPR.SetMediaItemTakeInfo_Value(RPR.GetActiveTake(item), "I_CHANMODE", mode)
        data = render(call, tmp_path, 0.0, 1.0, f"mode{mode}.wav")
        assert (_hz(data[:, 0]), _hz(data[:, 1])) == (left, right), mode


# Copies


def test_duplicate_item_copies_with_new_guids_and_the_same_sound(live_project, call, tmp_path):
    track = live_project.add_track(0, "d")
    item = insert(live_project, track, write(tmp_path / "t.wav", sine(1.0)))
    take = RPR.GetActiveTake(item)
    assert RPR.TakeFX_AddByName(take, "ReaEQ", 1) == 0
    RPR.SelectAllMediaItems(0, False)
    RPR.SetMediaItemSelected(item, True)
    RPR.Main_OnCommand(40693, 0)  # Take: Toggle take volume envelope
    original = RPR.GetItemStateChunk(item, "", 1 << 20, False)[2]
    assert "<FXCHAIN" in original or "TAKEFX" in original
    assert "<VOLENV" in original
    result = call("duplicate_item", item=guid(item), position=2.0)
    assert result["success"] is True, result
    copy = RPR.GetTrackMediaItem(track.id, 1)
    copied = RPR.GetItemStateChunk(copy, "", 1 << 20, False)[2]
    ids = lambda chunk: set(re.findall(r"\{[0-9A-F-]{36}\}", chunk, re.I))  # noqa: E731
    assert ids(original) and not ids(original) & ids(copied)
    assert RPR.TakeFX_GetFXGUID(take, 0) != RPR.TakeFX_GetFXGUID(RPR.GetActiveTake(copy), 0)
    first = render(call, tmp_path, 0.0, 1.0, "first.wav")
    second = render(call, tmp_path, 2.0, 3.0, "second.wav")
    assert np.max(np.abs(first - second)) < 1e-4


def test_a_duplicated_midi_item_is_not_pooled(live_project, call):
    track = live_project.add_track(0, "m")
    midi = track.add_midi_item(0.0, 1.0)
    take = RPR.GetActiveTake(midi.id)
    RPR.MIDI_InsertNote(take, False, False, 0, 240, 0, 60, 100, False)
    result = call("duplicate_item", item=guid(midi.id), position=2.0)
    assert result["success"] is True, result
    copy = RPR.GetActiveTake(RPR.GetTrackMediaItem(track.id, 1))
    RPR.MIDI_InsertNote(copy, False, False, 0, 240, 0, 64, 100, False)
    # (retval, take, notes, ccs, sysex)
    assert RPR.MIDI_CountEvts(take, 0, 0, 0)[2] == 1
    assert RPR.MIDI_CountEvts(copy, 0, 0, 0)[2] == 2


def test_glue_into_a_folder_reaper_cannot_write_is_refused_without_a_dialog(
        project_in_tmp, live_project, call, tmp_path):
    track = live_project.add_track(0, "g")
    item = insert(live_project, track, write(tmp_path / "a.wav", sine(1.0)))
    tmp_path.chmod(0o555)
    try:
        result = call("glue_items", items_to_glue=[guid(item)])
    finally:
        tmp_path.chmod(0o755)
    assert result["success"] is False and "nothing was glued" in result["error"], result
    assert RPR.CountTrackMediaItems(track.id) == 1
