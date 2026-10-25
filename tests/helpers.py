"""Assertions shared by the fake-REAPER tests."""

import base64
import re

RENDER = 42230
WAV_24 = base64.b64encode(b"evaw\x18\x00\x01").decode()


# The render settings a fresh fake project reads back, as the temp render restores them.
UNSET_RENDER_SETTINGS = {
    "RENDER_FILE": "", "RENDER_PATTERN": "", "RENDER_FORMAT": "",
    "RENDER_SRATE": 0.0, "RENDER_CHANNELS": 0.0, "RENDER_BOUNDSFLAG": 0.0,
    "RENDER_ADDTOPROJ": 0.0,
}


def assert_rendered_and_deleted(reaper, rate=48000):
    """One render of the whole project to a 24-bit stereo .wav temp file, removed
    afterwards, with the project's render settings put back."""
    assert reaper.commands == [RENDER]
    [path] = reaper.renders
    assert path.suffix == ".wav"
    assert not path.exists()
    assert reaper.project_info == UNSET_RENDER_SETTINGS
    [settings] = reaper.render_settings
    assert settings == {
        "RENDER_FILE": str(path.parent),
        "RENDER_PATTERN": path.stem,
        "RENDER_FORMAT": WAV_24,
        "RENDER_SRATE": float(rate),
        "RENDER_CHANNELS": 2.0,
        "RENDER_BOUNDSFLAG": 1.0,
        "RENDER_ADDTOPROJ": 0.0,
    }


def assert_no_track(result: dict) -> None:
    """The reply of an editing tool given a track index that names no track:
    refused before anything (an undo step included) happened."""
    assert result["success"] is False, result
    assert re.fullmatch(r"No track -?\d+: there are \d+ \(indices start at 0\); nothing was changed",
                        result["error"]), result
