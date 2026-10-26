"""Fixtures for tests that drive a real, running REAPER (selected with --live).

Registered as a plugin by tests/conftest.py, so tests/live and tests/contract
share them.

Isolation is by reset, not by a new project: REAPER's "New project" asks
whether to save a dirty project in a modal dialog, which would hang the run.
"""

import sys

import pytest
import reapy

from reaper_mcp import reaper
from tests.live.rpr import RPR
from tests.live.watchdog import Watchdog, guard_reapy

RESET_BPM = 120.0
SAVE_AS_PROJECT_FILE = 8  # Main_SaveProjectEx: the copy becomes the project's file


def _connect() -> reapy.Project:
    try:
        reaper.ensure_connected()
        return reapy.Project()
    except Exception as e:  # reapy raises several unrelated types here
        pytest.fail(
            f"--live was given but REAPER is not reachable through reapy: {e}. Start REAPER "
            "with the reaper-mcp bridge installed (see CONTRIBUTING.md, 'Live tests')."
        )


def reset(project: reapy.Project) -> None:
    for track in reversed(list(project.tracks)):
        track.delete()
    # From the last index down: reapy's Marker.delete goes by enumeration
    # index, which shifts as marks are deleted, and left some behind.
    for i in reversed(range(RPR.CountProjectMarkers(0, 0, 0)[0])):
        RPR.DeleteProjectMarkerByIndex(0, i)
    master = project.master_track
    for i in reversed(range(master.n_fxs)):
        master.fxs[i].delete()
    for i in reversed(range(RPR.CountTempoTimeSigMarkers(0))):
        RPR.DeleteTempoTimeSigMarker(0, i)
    # Deleting the last marker leaves its time signature as the project's, so
    # write a 4/4 one and delete that.
    RPR.SetTempoTimeSigMarker(0, -1, 0.0, -1, -1, RESET_BPM, 4, 4, False)
    RPR.DeleteTempoTimeSigMarker(0, 0)
    project.bpm = RESET_BPM


@pytest.fixture(scope="session")
def dialog_watchdog():
    """End the run, naming the dialog, if REAPER blocks on one."""
    watchdog = Watchdog()
    if not watchdog.available:
        print("[live watchdog] not guarding: needs macOS System Events or X11's xwininfo/xprop", file=sys.__stderr__)
    watchdog.start()
    with pytest.MonkeyPatch.context() as monkeypatch:
        guard_reapy(watchdog, monkeypatch)
        yield watchdog
    watchdog.stop()


@pytest.fixture(scope="session")
def scratch_folder(tmp_path_factory):
    return tmp_path_factory.mktemp("live-project")


@pytest.fixture
def live_project(dialog_watchdog, scratch_folder):
    """The running REAPER's current project, emptied before and after the test.
    A project never saved is first saved under tmp: REAPER writes the media it
    makes or copies (glue, imported files, recordings) into the project's
    folder, which for an unsaved project is ~/Documents/REAPER Media."""
    project = _connect()
    if not RPR.EnumProjects(-1, "", 4096)[2]:
        RPR.Main_SaveProjectEx(0, str(scratch_folder / "live.rpp"), SAVE_AS_PROJECT_FILE)
    reset(project)
    yield project
    reset(project)


@pytest.fixture
def project_in_tmp(live_project, tmp_path):
    """The project saved as a file under tmp_path, so what REAPER writes next
    to it (glued items, recordings) stays there. A never-saved project would
    write into REAPER's default media folder (~/Documents/REAPER Media)."""
    RPR.Main_SaveProjectEx(0, str(tmp_path / "live.rpp"), SAVE_AS_PROJECT_FILE)
    assert RPR.GetProjectPath("", 4096)[0].startswith(str(tmp_path))
    return live_project


def write_tone(path, frequency: float, seconds: float = 2.0, amplitude: float = 0.1) -> None:
    import numpy as np
    import soundfile as sf

    rate = 48000
    tone = amplitude * np.sin(2 * np.pi * frequency * np.arange(int(rate * seconds)) / rate)
    sf.write(path, np.stack([tone, tone], axis=1), rate)


@pytest.fixture
def add_tone(live_project, tmp_path):
    """add_tone(name, frequency) -> a new track holding a 2 s stereo sine at 0 s."""

    def add(name: str, frequency: float) -> reapy.Track:
        wav = tmp_path / f"{name}.wav"
        write_tone(wav, frequency)
        track = live_project.add_track(live_project.n_tracks, name)
        RPR.SetOnlyTrackSelected(track.id)
        live_project.cursor_position = 0.0
        RPR.InsertMedia(str(wav), 0)
        return track

    return add
