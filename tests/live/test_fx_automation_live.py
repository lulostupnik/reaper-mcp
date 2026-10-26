"""FX usability, automation and routing tools against a running REAPER.
Levels are checked in REAPER's offline renders."""

import numpy as np
import pytest
import soundfile as sf

from tests.live.rpr import RPR

pytestmark = pytest.mark.live

RATE = 48000


def fx_names(track) -> list[str]:
    return [RPR.TrackFX_GetFXName(track.id, i, "", 256)[3] for i in range(RPR.TrackFX_GetCount(track.id))]


def shown(track, fx: int, param: int) -> str:
    return RPR.TrackFX_GetFormattedParamValue(track.id, fx, param, "", 256)[4]


def param_index(track, fx: int, name: str) -> int:
    names = [RPR.TrackFX_GetParamName(track.id, fx, i, "", 256)[4]
             for i in range(RPR.TrackFX_GetNumParams(track.id, fx))]
    return names.index(name)


def params(track, fx: int) -> list[float]:
    return [round(RPR.TrackFX_GetParamNormalized(track.id, fx, i), 6)
            for i in range(RPR.TrackFX_GetNumParams(track.id, fx))]


def render(call, tmp_path, start: float, end: float, name: str = "r.wav") -> np.ndarray:
    out = tmp_path / name
    result = call("render_time_selection", output_path=str(out), start=start, end=end,
                  bit_depth=32, overwrite=True)
    assert result["success"] is True, result
    return sf.read(str(out), always_2d=True)[0]


def rms_db(data: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(data ** 2)) + 1e-12))


@pytest.fixture
def tone_track(add_tone):
    """Track 0 playing a 2 s 440 Hz tone at -20 dBFS (0.1 peak)."""
    return add_tone("tone", 440)


# The plugin list and add_fx names


def test_enum_installed_fx_gives_no_names_through_reapy(live_project):
    """Its name/ident are const char** out-parameters: reapy returns only the
    flag, so the plugin caches are still the way to list plugins."""
    assert RPR.EnumInstalledFX(0, "", "") in (1, True)


def test_list_installed_fx_reads_the_vst_and_js_caches(live_project, call):
    comp = call("list_installed_fx", query="reacomp", limit=3)["plugins"][0]
    assert (comp["name"], comp["add_fx_name"], comp["instrument"]) == (
        "ReaComp (Cockos)", "VST: ReaComp (Cockos)", False)
    synth = call("list_installed_fx", query="reasynth", limit=3)["plugins"][0]
    print("reasynth:", synth)
    assert synth["instrument"] is True and synth["type"] == "VSTi"
    js = call("list_installed_fx", query="loudness meter", limit=3)["plugins"]
    print("js loudness:", js)
    assert any(p["type"] == "JS" for p in js)


@pytest.mark.parametrize("name", ["VST: ReaComp (Cockos)", "VST: ReaSynth (Cockos)",
                                  "JS:analysis/loudness_meter", "ReaEQ"])
def test_add_fx_takes_each_add_fx_name(live_project, call, name):
    track = live_project.add_track(0, "t")
    result = call("add_fx", track_index=0, fx_name=name)
    assert result["success"] is True, result
    print(name, "->", fx_names(track))
    assert len(fx_names(track)) == 1


def test_add_fx_with_a_js_path_with_spaces(live_project, call):
    listed = call("list_installed_fx", query="", limit=2000)["plugins"]
    spaced = [p for p in listed if p["type"] == "JS" and " " in p["add_fx_name"]]
    print("JS with spaces in the path:", [p["add_fx_name"] for p in spaced[:3]])
    if not spaced:
        pytest.skip("no installed JS plugin has a space in its path")
    track = live_project.add_track(0, "t")
    assert call("add_fx", track_index=0, fx_name=spaced[0]["add_fx_name"])["success"] is True
    assert len(fx_names(track)) == 1


def test_add_fx_at_a_position(live_project, call):
    track = live_project.add_track(0, "t")
    for name in ("ReaEQ", "ReaComp"):
        call("add_fx", track_index=0, fx_name=name)
    first = call("add_fx", track_index=0, fx_name="ReaDelay", position=0)
    assert first["fx_index"] == 0, first
    past = call("add_fx", track_index=0, fx_name="ReaGate", position=9)
    assert past["fx_index"] == 3, past
    assert [n.split(":")[1].split("(")[0].strip() for n in fx_names(track)] == [
        "ReaDelay", "ReaEQ", "ReaComp", "ReaGate"]


# Moving and copying FX


def test_move_and_copy_fx_land_where_asked_with_their_settings(live_project, call):
    track = live_project.add_track(0, "a")
    other = live_project.add_track(1, "b")
    for name in ("ReaEQ", "ReaComp", "ReaDelay"):
        call("add_fx", track_index=0, fx_name=name)
    RPR.TrackFX_SetParamNormalized(track.id, 0, 0, 0.123)
    eq = params(track, 0)
    moved = call("move_fx", track_index=0, fx_index=0, to_position=2)
    assert moved["success"] is True and moved["fx_index"] == 2, moved
    assert params(track, 2) == eq
    back = call("move_fx", track_index=0, fx_index=2, to_position=0)
    assert back["success"] is True and back["fx_index"] == 0, back
    copied = call("copy_fx", track_index=0, fx_index=0, to_track_index=0, to_position=1)
    assert copied["success"] is True and copied["fx_index"] == 1, copied
    assert params(track, 1) == eq
    across = call("move_fx", track_index=0, fx_index=0, to_track_index=1)
    assert across["success"] is True, across
    assert params(other, across["fx_index"]) == eq
    chains = (fx_names(track), fx_names(other))
    assert call("undo")["success"] is True
    assert (len(fx_names(track)), len(fx_names(other))) == (4, 0)
    assert call("redo")["success"] is True
    assert (fx_names(track), fx_names(other)) == chains


# Presets


def test_presets_are_listed_and_the_settings_put_back(live_project, call):
    track = live_project.add_track(0, "t")
    call("add_fx", track_index=0, fx_name="ReaEQ")
    RPR.TrackFX_SetParamNormalized(track.id, 0, 1, 0.3)
    before = params(track, 0)
    index, _, _, count = RPR.TrackFX_GetPresetIndex(track.id, 0, 0)
    print("ReaEQ preset index/count before:", index, count)
    listed = call("list_fx_presets", track_index=0, fx_index=0)
    assert listed["success"] is True, listed
    print("list_fx_presets:", {k: v for k, v in listed.items() if k != "presets"},
          listed.get("presets", [])[:5])
    assert params(track, 0) == before
    print("dropdown after listing:", RPR.TrackFX_GetPreset(track.id, 0, "", 256)[3])
    names = listed["presets"]
    assert len(names) == count
    pick = names[min(2, len(names) - 1)]
    pick_name = pick["name"] if isinstance(pick, dict) else pick
    pick_index = pick["index"] if isinstance(pick, dict) else names.index(pick)
    by_name = call("load_fx_preset", track_index=0, fx_index=0, preset_name=pick_name)
    print("load by name:", by_name)
    by_index = call("load_fx_preset", track_index=0, fx_index=0, preset_index=pick_index)
    assert by_index["success"] is True, by_index
    assert RPR.TrackFX_GetPreset(track.id, 0, "", 256)[3] == pick_name


def test_listing_presets_of_a_compressor_reports_its_settings_restored(live_project, call,
                                                                      tone_track):
    """ReaComp has meter-like outputs (gain reduction): they must not make
    settings_restored false when nothing is wrong."""
    call("add_fx", track_index=0, fx_name="ReaComp")
    listed = call("list_fx_presets", track_index=0, fx_index=0)
    print("ReaComp list_fx_presets:", {k: v for k, v in listed.items() if k != "presets"})
    assert listed["success"] is True, listed
    assert listed.get("settings_restored", True) is True


# Values by display text


@pytest.mark.parametrize(("plugin", "param", "value", "expected"), [
    ("ReaEQ", "Freq-Band 2", "300 Hz", 300.0),
    ("ReaEQ", "Freq-Band 2", "1.5 kHz", 1500.0),
    ("ReaEQ", "Gain-Band 2", "-3 dB", -3.0),
    ("ReaComp", "Attack", "20 ms", 20.0),
    ("ReaComp", "Release", "100 ms", 100.0),
    ("ReaComp", "Thresh", "-12 dB", -12.0),
    ("ReaComp", "Ratio", "4:1", 4.0),
    ("ReaDelay", "1: Length (time)", "250 ms", 250.0),
    ("ReaVerbate", "Wet", "0 dB", 0.0),
])
def test_set_fx_parameter_by_name_lands_on_the_value_shown(live_project, call, plugin, param,
                                                           value, expected):
    track = live_project.add_track(0, "t")
    call("add_fx", track_index=0, fx_name=plugin)
    result = call("set_fx_parameter_by_name", track_index=0, fx_index=0, param_name=param,
                  value=value)
    names = [RPR.TrackFX_GetParamName(track.id, 0, i, "", 256)[4]
             for i in range(RPR.TrackFX_GetNumParams(track.id, 0))]
    print(plugin, param, value, "->", result, "| params:", names[:8])
    assert result["success"] is True, result
    text = shown(track, 0, param_index(track, 0, result.get("param_name", param)))
    number = float(text.replace(":1", "").split()[0])
    print("shown:", text)
    assert result["reached"] is True, result
    assert number == pytest.approx(expected, rel=0.02, abs=0.05)


# Automation


def test_automation_modes_match_reapers_actions(live_project):
    """I_AUTOMODE as REAPER's own "Set track automation mode" actions set it."""
    track = live_project.add_track(0, "t")
    RPR.SetOnlyTrackSelected(track.id)
    modes = {40400: 0, 40401: 1, 40402: 2, 40403: 3, 40404: 4, 42023: 5}  # trim/read ... latch preview
    for command, mode in modes.items():
        RPR.Main_OnCommand(command, 0)
        assert track.get_info_value("I_AUTOMODE") == mode, RPR.kbd_getTextFromCmd(command, 0)
    RPR.Main_OnCommand(40400, 0)


def test_volume_envelope_points_round_trip(live_project, call, tone_track):
    """Showing the volume envelope adds one point (0 dB at 0 s); points read
    back in dB; clear_automation deletes from the last index down."""
    result = call("set_volume_over_range", track_index=0, start=0.5, end=1.5, change_db=-6.0,
                  ramp_seconds=0.0)
    assert result["success"] is True, result
    points = call("get_envelope_points", track_index=0)
    print("points after set_volume_over_range:", points["points"])
    env = RPR.GetTrackEnvelopeByName(tone_track.id, "Volume")
    raw = [RPR.GetEnvelopePoint(env, i, 0.0, 0.0, 0, 0.0, False)[3:6]
           for i in range(RPR.CountEnvelopePoints(env))]
    print("raw points (time, value, shape):", raw)
    value = RPR.Envelope_Evaluate(env, 1.0, 48000.0, 1, 0.0, 0.0, 0.0, 0.0)[5]
    assert 20 * np.log10(RPR.ScaleFromEnvelopeMode(1, value)) == pytest.approx(-6.0, abs=0.05)
    cleared = call("clear_automation", track_index=0, start=0.4, end=1.6)
    print("clear_automation:", cleared)
    assert cleared["success"] is True, cleared
    value = RPR.Envelope_Evaluate(env, 1.0, 48000.0, 1, 0.0, 0.0, 0.0, 0.0)[5]
    assert 20 * np.log10(RPR.ScaleFromEnvelopeMode(1, value)) == pytest.approx(0.0, abs=0.05)


def test_a_volume_range_is_heard_in_the_render(live_project, call, tone_track, tmp_path):
    call("set_volume_over_range", track_index=0, start=0.5, end=1.5, change_db=-6.0,
         ramp_seconds=0.0)
    data = render(call, tmp_path, 0.0, 2.0)
    inside, outside = data[int(0.7 * RATE):int(1.3 * RATE)], data[int(1.6 * RATE):int(1.9 * RATE)]
    assert rms_db(inside) - rms_db(outside) == pytest.approx(-6.0, abs=0.1)


def test_volume_points_above_the_envelope_lane_are_heard_as_set(live_project, call,
                                                                tone_track, tmp_path):
    result = call("set_volume_over_range", track_index=0, start=0.5, end=1.5, change_db=20.0,
                  ramp_seconds=0.0)
    assert "plays them as set" in result["note"], result
    data = render(call, tmp_path, 0.0, 2.0)
    inside, outside = data[int(0.7 * RATE):int(1.3 * RATE)], data[int(1.6 * RATE):int(1.9 * RATE)]
    assert rms_db(inside) - rms_db(outside) == pytest.approx(20.0, abs=0.1)


@pytest.mark.parametrize("shape", [1, 2, 5])
def test_clearing_inside_a_segment_keeps_the_curve(live_project, call, tone_track, shape):
    """clear_automation splits a segment with a point copying its shape and
    tension: the curve after the cleared range is as it was."""
    RPR.SetOnlyTrackSelected(tone_track.id)
    RPR.Main_OnCommand(40406, 0)
    env = RPR.GetTrackEnvelopeByName(tone_track.id, "Volume")
    for i in reversed(range(RPR.CountEnvelopePoints(env))):
        RPR.DeleteEnvelopePointEx(env, -1, i)
    RPR.InsertEnvelopePoint(env, 0.0, RPR.ScaleToEnvelopeMode(1, 1.0), shape, 0.4, False, True)
    RPR.InsertEnvelopePoint(env, 4.0, RPR.ScaleToEnvelopeMode(1, 0.25), 0, 0.0, False, True)
    RPR.Envelope_SortPoints(env)
    times = [2.6, 3.0, 3.5]
    before = [RPR.Envelope_Evaluate(env, t, 48000.0, 1, 0.0, 0.0, 0.0, 0.0)[5] for t in times]
    assert call("clear_automation", track_index=0, start=1.0, end=2.5)["success"] is True
    after = [RPR.Envelope_Evaluate(env, t, 48000.0, 1, 0.0, 0.0, 0.0, 0.0)[5] for t in times]
    print("shape", shape, "before", before, "after", after)
    assert after == pytest.approx(before, rel=1e-3)


def test_pan_knob_and_pan_envelope_as_heard(live_project, call, tmp_path):
    """Making the pan envelope moves the knob's pan into it and centres the knob,
    so the range is heard as set; a knob moved later adds to the envelope, as
    the tool's note says."""
    track = live_project.add_track(0, "mono")
    wav = tmp_path / "mono.wav"
    sf.write(wav, 0.1 * np.sin(2 * np.pi * 440 * np.arange(RATE * 2) / RATE), RATE)
    RPR.SetOnlyTrackSelected(track.id)
    live_project.cursor_position = 0.0
    RPR.InsertMedia(str(wav), 0)
    track.set_info_value("D_PAN", 0.3)
    first = call("set_pan_over_range", track_index=0, start=0.0, end=1.0, pan=-0.5,
                 ramp_seconds=0.0)
    assert "note" not in first and track.get_info_value("D_PAN") == 0.0
    assert first["points"][-1]["value"] == pytest.approx(0.3)  # the knob, after the range
    centred = render(call, tmp_path, 0.2, 0.8, "a.wav")
    assert rms_db(centred[:, 0]) - rms_db(centred[:, 1]) == pytest.approx(6.0, abs=0.2)
    track.set_info_value("D_PAN", 0.3)
    again = call("set_pan_over_range", track_index=0, start=0.0, end=1.0, pan=-0.5,
                 ramp_seconds=0.0)
    assert "combines" in again["note"]
    combined = render(call, tmp_path, 0.2, 0.8, "b.wav")
    assert rms_db(combined[:, 0]) - rms_db(combined[:, 1]) < 3.0  # pulled back toward centre


# Folders and sends


def test_moving_tracks_into_and_out_of_a_folder(live_project, call):
    for name in ("bus", "a", "b"):
        live_project.add_track(live_project.n_tracks, name)
    moved = call("move_track_into_folder", track_index=2, folder_track_index=0,
                 allow_new_folder=True)
    assert moved["success"] is True, moved
    depths = [int(t.get_info_value("I_FOLDERDEPTH")) for t in live_project.tracks]
    print("after into:", [t.name for t in live_project.tracks], depths)
    assert [t.name for t in live_project.tracks] == ["bus", "b", "a"]
    assert RPR.GetParentTrack(live_project.tracks[1].id) == live_project.tracks[0].id
    out = call("move_track_out_of_folder", track_index=1)
    assert out["success"] is True, out
    print("after out:", [t.name for t in live_project.tracks],
          [int(t.get_info_value("I_FOLDERDEPTH")) for t in live_project.tracks])
    assert call("undo")["success"] is True
    assert RPR.GetParentTrack(live_project.tracks[1].id) == live_project.tracks[0].id


def test_a_track_in_a_folder_is_heard_through_the_folder_track(live_project, call, add_tone,
                                                               tmp_path):
    add_tone("bus", 220)
    add_tone("child", 440)
    live_project.tracks[0].items[0].delete()
    assert call("move_track_into_folder", track_index=1, folder_track_index=0,
                allow_new_folder=True)["success"] is True
    loud = rms_db(render(call, tmp_path, 0.0, 2.0, "a.wav"))
    live_project.tracks[0].set_info_value("D_VOL", 0.5)
    assert rms_db(render(call, tmp_path, 0.0, 2.0, "b.wav")) - loud == pytest.approx(-6.02, abs=0.1)


@pytest.mark.parametrize(("mode", "fader_matters", "fx_matters"), [
    ("post_fader", True, True), ("pre_fx", False, False), ("pre_fader", False, True)])
def test_send_modes_as_rendered(live_project, call, add_tone, tmp_path, mode, fader_matters,
                                fx_matters):
    source = add_tone("src", 440)
    dest = live_project.add_track(1, "dest")
    source.set_info_value("B_MAINSEND", 0)
    assert call("create_send", source_track_index=0, dest_track_index=1)["success"] is True
    edited = call("edit_send", source_track_index=0, send_index=0, mode=mode)
    assert edited["success"] is True, edited
    print(mode, "->", edited, "I_SENDMODE", RPR.GetTrackSendInfo_Value(source.id, 0, 0, "I_SENDMODE"))
    base = rms_db(render(call, tmp_path, 0.0, 2.0, "base.wav"))
    source.set_info_value("D_VOL", 0.5)
    fader = rms_db(render(call, tmp_path, 0.0, 2.0, "fader.wav")) - base
    source.set_info_value("D_VOL", 1.0)
    fx = RPR.TrackFX_AddByName(source.id, "ReaEQ", False, -1)
    RPR.TrackFX_SetParamNormalized(source.id, fx, param_index(source, fx, "Global Gain"), 0.0)
    gained = rms_db(render(call, tmp_path, 0.0, 2.0, "fx.wav")) - base
    print(mode, "fader -6 dB ->", round(fader, 2), "| fx gain down ->", round(gained, 2))
    assert (abs(fader) > 3) == fader_matters
    assert (abs(gained) > 3) == fx_matters
    assert dest.n_items == 0
