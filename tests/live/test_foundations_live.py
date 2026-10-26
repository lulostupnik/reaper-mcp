"""The undo layer, GUIDs and the context readers against a running REAPER.

Every tool that edits the project must be one undo step named "MCP: ...",
which one undo takes back whole. Through the bridge that only holds while the
server holds REAPER's main loop around the block (see reaper.hold_main_loop):
without it REAPER recorded each request as its own "ReaScript: Run" step.

A ReaScript call outside an undo block records no undo point, and the next
recorded step takes its change along. So the tests record their own setup
as a step (checkpoint) before calling the tool they check.
"""

import difflib

import pytest
import reapy

from reaper_mcp.reaper import string_or_none
from reaper_mcp.undo import undo_block
from tests.live.rpr import RPR

pytestmark = pytest.mark.live


def next_undo() -> str | None:
    return string_or_none(RPR.Undo_CanUndo2, 0)


def next_redo() -> str | None:
    return string_or_none(RPR.Undo_CanRedo2, 0)


def checkpoint(name: str = "live test setup") -> None:
    # Undo_OnStateChange would only record items; -1: every kind of state.
    RPR.Undo_OnStateChangeEx2(0, name, -1, -1)


def volume_points(track) -> int:
    envelope = RPR.GetTrackEnvelopeByName(track.id, "Volume")
    return 0 if envelope.endswith("0x0000000000000000") else RPR.CountEnvelopePoints(envelope)


def mix_state(project) -> list[tuple]:
    """What the tools below change, per track."""
    return [(t.name, round(t.get_info_value("D_VOL"), 6), t.n_fxs, t.n_items,
             RPR.GetTrackNumSends(t.id, 0), t.get_info_value("B_MAINSEND"), volume_points(t))
            for t in project.tracks]


EDITS = {
    "set_track_volume": {"track_index": 0, "volume_db": -6.0},
    "add_fx": {"track_index": 0, "fx_name": "ReaEQ"},
    "create_midi_item": {"track_index": 0, "start_position": 0.0, "length": 2.0},
    "add_volume_automation": {"track_index": 0, "position": 1.0, "value_db": -3.0},
    "create_bus": {"name": "Bus", "track_indices": [0, 1]},
}


@pytest.mark.parametrize("tool", list(EDITS))
def test_a_tool_call_is_one_named_step_that_undo_takes_back(live_project, call, tool):
    live_project.add_track(0, "a")
    live_project.add_track(1, "b")
    checkpoint()
    before = mix_state(live_project)
    result = call(tool, **EDITS[tool])
    assert result["success"] is True, result
    assert mix_state(live_project) != before
    step = next_undo()
    assert step is not None and step.startswith("MCP: " + tool.replace("_", " ").capitalize()), step

    undone = call("undo")
    assert undone["success"] is True, undone
    assert undone["undone"] == step
    assert mix_state(live_project) == before
    assert next_redo() == step

    assert call("redo")["redone"] == step
    assert mix_state(live_project) != before


def test_nested_blocks_are_one_step_named_after_the_outer_one(live_project):
    checkpoint()
    with undo_block("outer"):
        live_project.add_track(0, "a")
        with undo_block("inner"):
            live_project.add_track(1, "b")
    assert next_undo() == "MCP: outer"
    assert RPR.Undo_DoUndo2(0) == 1  # nonzero on success through reapy
    assert live_project.n_tracks == 0
    assert RPR.Undo_DoRedo2(0) == 1
    assert live_project.n_tracks == 2


def test_a_tool_failing_before_any_change_leaves_no_step_and_keeps_redo(live_project, call):
    """REAPER skips a block in which nothing changed, so a failed call that
    changed nothing leaves no "(failed)" step and the redo list stays."""
    live_project.add_track(0, "a")
    checkpoint()
    call("set_track_volume", track_index=0, volume_db=-6.0)
    call("undo")
    step = next_redo()
    assert step is not None
    result = call("remove_fx", track_index=0, fx_index=3)
    assert result["success"] is False
    assert next_undo() == "live test setup"
    assert next_redo() == step


def test_a_tool_failing_after_a_change_leaves_a_failed_step(live_project, call):
    live_project.add_track(0, "a")
    checkpoint()
    with pytest.raises(RuntimeError), undo_block("Half done"):
        live_project.tracks[0].set_info_value("D_VOL", 0.5)
        raise RuntimeError("failed half-way")
    assert next_undo() == "MCP: Half done (failed)"
    assert call("undo")["undone"] == "MCP: Half done (failed)"
    assert live_project.tracks[0].get_info_value("D_VOL") == 1.0


def test_a_bad_track_index_leaves_no_step(live_project, call):
    live_project.add_track(0, "a")
    checkpoint()
    call("set_track_volume", track_index=0, volume_db=-6.0)
    call("undo")
    step = next_redo()
    assert call("set_track_volume", track_index=5, volume_db=-6.0)["success"] is False
    assert next_redo() == step


def test_undo_refuses_a_step_not_made_by_the_server(live_project, call):
    checkpoint()
    live_project.add_track(0, "by hand")
    checkpoint("Add track by hand")
    result = call("undo")
    assert result["success"] is False
    assert "include_manual_steps" in result["error"]
    assert call("undo", include_manual_steps=True)["success"] is True
    assert live_project.n_tracks == 0


# Lines REAPER writes that undo does not (and need not) restore: view state,
# selection, and a recording-lane line it adds lazily.
VIEW_STATE = ("CURSOR ", "ZOOM ", "VZOOMEX ", "SEL ", "LANEREC ")
EMPTY_FX_CHAIN = ["<FXCHAIN", "SHOW 0", "LASTSEL 0", "DOCKED 0", ">"]


def _comparable(lines: list[str]) -> list[str]:
    """lines without view state, empty FX chain stubs (REAPER leaves one on a
    track whose FX were removed) and plug-in state blobs, which also carry the
    plug-in's last program name; fx_values() compares the FX parameters."""
    out: list[str] = []
    in_plugin = False
    for line in lines:
        text = line.strip()
        if in_plugin:
            in_plugin = text != ">"
            continue
        if text.startswith(("<VST ", "<JS ", "<AU ", "<CLAP ")):
            in_plugin = True
            out.append(text)
        elif not text.startswith(VIEW_STATE):
            out.append(text)
            if out[-len(EMPTY_FX_CHAIN):] == EMPTY_FX_CHAIN:
                del out[-len(EMPTY_FX_CHAIN):]
    return out


@reapy.inside_reaper()  # one hold: no wait for REAPER's loop between the many reads
def fx_values() -> list[str]:
    tracks = [RPR.GetMasterTrack(0)] + [RPR.GetTrack(0, i) for i in range(RPR.CountTracks(0))]
    return [f"{t[-6:]} fx{fx} p{p} {RPR.TrackFX_GetParamNormalized(t, fx, p):.6f}"
            for t in tracks for fx in range(RPR.TrackFX_GetCount(t))
            for p in range(RPR.TrackFX_GetNumParams(t, fx))]


def project_text(tmp_path) -> str:
    """The whole project as REAPER saves it (a copy: the open project keeps
    its file and unsaved state), without the header line's timestamp, made
    comparable, with every FX parameter's value appended."""
    copy = tmp_path / "state.rpp"
    RPR.Main_SaveProjectEx(0, str(copy), 0)
    lines = copy.read_text(errors="replace").splitlines()[1:]
    return "\n".join(_comparable(lines) + fx_values())


def same(a: str, b: str) -> bool:
    """a == b; prints the changed lines when not (shown for a failing test)."""
    if a != b:
        print("\n".join(difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=1)))
    return a == b


WAV_24 = "ZXZhdxgAAQ=="  # RECORD_FORMAT: WAV, 24 bit


TRACK_TEMPLATE = """<TRACK
  NAME "From template"
>
"""

# Every tool that edits the project (set_record_path and set_recording_format
# are not undo steps: see test_record_settings_are_not_in_reapers_undo_history),
# with arguments that change something in
# the setup of test_every_edit_is_one_step_that_undo_takes_back_whole.
ALL_EDITS = {
    "import_audio_file": {"file_path": "{wav}", "track_index": 1, "position": 3.0},
    "edit_audio_item": {"track_index": 0, "item_index": 0, "start_trim": 0.5, "fade_in": 0.1},
    "adjust_pitch": {"track_index": 0, "item_index": 0, "semitones": 2.0},
    "adjust_playback_rate": {"track_index": 0, "item_index": 0, "rate": 0.5},
    "add_fx": {"track_index": 1, "fx_name": "ReaComp"},
    "remove_fx": {"track_index": 0, "fx_index": 0},
    "set_fx_parameter": {"track_index": 0, "fx_index": 0, "param_index": 0, "value": 0.2},
    "bypass_fx": {"track_index": 0, "fx_index": 0, "bypassed": True},
    "add_master_fx": {"fx_name": "ReaComp"},
    "set_master_fx_parameter": {"fx_index": 0, "param_index": 0, "value": 0.2},
    "set_master_volume": {"volume_db": -3.0},
    "apply_mastering_chain": {},
    "apply_limiter": {},
    "normalize_project": {"target_lufs": -20.0},
    "create_midi_item": {"track_index": 1, "start_position": 0.0, "length": 2.0},
    "add_midi_note": {"track_index": 1, "item_index": 0, "pitch": 60, "start": 0.0, "length": 0.5},
    "create_chord_progression": {"track_index": 1, "chords": "C,G", "start_position": 4.0},
    "create_drum_pattern": {"track_index": 1, "pattern": "k.s.", "start_position": 8.0},
    "add_volume_automation": {"track_index": 0, "position": 1.0, "value_db": -3.0},
    "add_pan_automation": {"track_index": 0, "position": 1.0, "pan": 0.5},
    "create_send": {"source_track_index": 1, "dest_track_index": 0},
    "remove_send": {"source_track_index": 0, "send_index": 0},
    "set_send_volume": {"source_track_index": 0, "send_index": 0, "volume_db": -9.0},
    "create_bus": {"name": "Bus", "track_indices": [0, 1]},
    "set_tempo": {"bpm": 97.0},
    "set_time_signature": {"numerator": 3, "denominator": 4},
    "set_track_input": {"track_index": 0, "input": "mono", "channel": 2},
    "arm_track": {"track_index": 0},
    "set_monitoring": {"track_index": 0, "mode": "tape"},
    "set_track_record_mode": {"track_index": 0, "mode": "output"},
    "insert_track_template": {"template": "{template}"},
    "create_track": {"name": "new", "track_type": "instrument"},
    "delete_track": {"track_index": 1},
    "rename_track": {"track_index": 0, "name": "renamed"},
    "set_track_volume": {"track_index": 0, "volume_db": -6.0},
    "set_track_pan": {"track_index": 0, "pan": 0.5},
    "set_track_mute": {"track_index": 0, "muted": True},
    "set_track_solo": {"track_index": 0, "soloed": True},
    "set_track_color": {"track_index": 0, "r": 200, "g": 10, "b": 10},
}


@pytest.mark.parametrize("tool", list(ALL_EDITS))
def test_every_edit_is_one_step_that_undo_takes_back_whole(
        live_project, call, add_tone, tmp_path, tool):
    add_tone("a", 440)
    live_project.tracks[0].add_fx("ReaEQ")
    other = live_project.add_track(1, "b")
    other.add_midi_item(20.0, 21.0)
    RPR.CreateTrackSend(live_project.tracks[0].id, other.id)
    live_project.master_track.add_fx("ReaEQ")
    live_project.master_track.set_info_value("D_VOL", 1.0)
    for key, value in (("RECORD_PATH", ""), ("RECORD_FORMAT", WAV_24)):
        RPR.GetSetProjectInfo_String(0, key, value, True)
    template = tmp_path / "t.RTrackTemplate"
    template.write_text(TRACK_TEMPLATE)
    checkpoint()
    before = project_text(tmp_path)
    arguments = {key: str(value).format(wav=tmp_path / "a.wav", template=template)
                 if isinstance(value, str) else value for key, value in ALL_EDITS[tool].items()}

    result = call(tool, **arguments)
    assert result["success"] is True, result
    after = project_text(tmp_path)
    assert after != before
    step = next_undo()
    assert step is not None and step.startswith("MCP: " + tool.replace("_", " ").capitalize())
    assert not step.endswith("(failed)")

    assert call("undo")["undone"] == step
    assert same(before, project_text(tmp_path))
    assert call("redo")["redone"] == step
    assert same(after, project_text(tmp_path))


@pytest.mark.parametrize(("tool", "arguments", "key"), [
    ("set_record_path", {"subfolder": "Takes"}, "RECORD_PATH"),
    ("set_recording_format", {"format": "flac"}, "RECORD_FORMAT"),
])
def test_record_settings_are_not_in_reapers_undo_history(live_project, call, tool, arguments,
                                                         key):
    """REAPER records no step for them, and undoing an earlier step leaves them:
    so the tools are not undo steps and name the previous setting instead."""
    live_project.add_track(0, "a")
    RPR.GetSetProjectInfo_String(0, key, "" if key == "RECORD_PATH" else WAV_24, True)
    checkpoint()
    call("set_track_volume", track_index=0, volume_db=-6.0)
    result = call(tool, **arguments)
    assert result["success"] is True, result
    assert next_undo() == "MCP: Set track volume (track_index=0, volume_db=-6)"
    changed = RPR.GetSetProjectInfo_String(0, key, "", False)[3]
    call("undo")
    assert RPR.GetSetProjectInfo_String(0, key, "", False)[3] == changed
    RPR.GetSetProjectInfo_String(0, key, "" if key == "RECORD_PATH" else WAV_24, True)
