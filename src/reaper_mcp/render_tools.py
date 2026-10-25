"""Rendering, and the render settings every audio-analysis tool depends on.

How REAPER takes render settings (verified on REAPER 7.82):
- RENDER_FILE is the output *directory* and RENDER_PATTERN the file name
  without extension; REAPER appends the format's extension. A full file path
  in RENDER_FILE becomes a directory of that name.
- RENDER_FORMAT is a base64 sink configuration, set as a string. A bare
  four-character type ("evaw", "calf", ...) selects that format's defaults;
  WAV's defaults are 32-bit float, so WAV always carries its bit depth.
- RENDER_BOUNDSFLAG: 1 = entire project, 2 = time selection (0 is custom
  start/end positions).

Rendering never reaches a REAPER dialog: with nothing to render REAPER shows
a modal "Nothing to render!", and with an existing target an overwrite
prompt, and either blocks the call until someone clicks. So an empty source
is refused before rendering, and an existing target is refused unless the
caller passes overwrite=True, in which case it is removed first.
"""

import base64
import tempfile
import uuid
from pathlib import Path

from reaper_mcp.reaper import RPR, get_project

# "File: Render project, using the most recent render settings, auto-close
# render dialog". 41824 is the same without auto-close, which leaves REAPER's
# results window open after every render (spotted in PR #7).
RENDER_PROJECT = 42230
ENTIRE_PROJECT = 1
TIME_SELECTION = 2

FORMAT_TYPES = {"wav": b"evaw", "flac": b"calf", "mp3": b"l3pm", "ogg": b"vggo"}
WAV_BIT_DEPTHS = (16, 24, 32)  # 32 renders as float
SOLO_IN_PLACE = 2


def get_project_string(key: str) -> str:
    # reapy returns the out-parameters: (ok, project, key, value, is_set).
    result = RPR.GetSetProjectInfo_String(0, key, "", False)
    assert isinstance(result, (tuple, list)), result
    return str(result[3])


def set_project_string(key: str, value: str) -> None:
    RPR.GetSetProjectInfo_String(0, key, value, True)


def _format_config(format: str, bit_depth: int) -> str:
    fmt = format.lower()
    if fmt not in FORMAT_TYPES:
        raise ValueError(f"unsupported format {format!r}; use one of {sorted(FORMAT_TYPES)}")
    if fmt != "wav":
        return base64.b64encode(FORMAT_TYPES[fmt]).decode()
    if bit_depth not in WAV_BIT_DEPTHS:
        raise ValueError(f"unsupported WAV bit depth {bit_depth}; use one of {WAV_BIT_DEPTHS}")
    return base64.b64encode(FORMAT_TYPES[fmt] + bytes([bit_depth, 0, 1])).decode()


def _target(output_path: str, format: str) -> Path:
    """The file REAPER will write for output_path: its extension is the format's."""
    if format.lower() not in FORMAT_TYPES:
        raise ValueError(f"unsupported format {format!r}; use one of {sorted(FORMAT_TYPES)}")
    # Resolve the folder but not the file itself, so a symlink at the target
    # is refused (see _render) instead of followed to the file it points at.
    raw = Path(output_path).expanduser()
    path = raw.parent.resolve() / raw.name
    if "$" in path.name:
        # REAPER expands $wildcards in RENDER_PATTERN, so the file it writes
        # would not be the one checked here.
        raise ValueError(f"output file name may not contain '$': {path.name!r}")
    extension = "." + format.lower()
    if path.suffix.lower() in {"." + f for f in FORMAT_TYPES} and path.suffix.lower() != extension:
        raise ValueError(f"output_path {path.name!r} does not match format {format!r}")
    return path.with_suffix(extension)


def _render(target: Path, *, format: str, sample_rate: int, bit_depth: int,
            channels: int, bounds: int, overwrite: bool = False) -> None:
    """Configure REAPER's render settings and render to target."""
    config = _format_config(format, bit_depth)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise FileExistsError(f"{target} is not a regular file; refusing to replace it")
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists; pass overwrite=true to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)  # an existing file raises REAPER's overwrite prompt
    set_project_string("RENDER_FILE", str(target.parent))
    set_project_string("RENDER_PATTERN", target.stem)
    set_project_string("RENDER_FORMAT", config)
    RPR.GetSetProjectInfo(0, "RENDER_SRATE", float(sample_rate), True)
    RPR.GetSetProjectInfo(0, "RENDER_CHANNELS", float(channels), True)
    RPR.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", float(bounds), True)
    RPR.GetSetProjectInfo(0, "RENDER_ADDTOPROJ", 0.0, True)
    RPR.Main_OnCommand(RENDER_PROJECT, 0)


def _require_content(project) -> None:
    if project.length <= 0:
        raise RuntimeError("the project is empty; there is nothing to render")


def render_to_temp_file(sample_rate: int = 48000) -> str:
    """
    Render the current project to a temporary WAV file and return its path.
    Used by analysis and mastering tools. Caller is responsible for deleting the file.
    """
    _require_content(get_project())
    target = Path(tempfile.gettempdir()) / f"reaper-mcp-{uuid.uuid4().hex}.wav"
    _render(target, format="wav", sample_rate=sample_rate, bit_depth=24, channels=2,
            bounds=ENTIRE_PROJECT)
    return str(target)


def _rendered(target: Path) -> dict:
    if not target.is_file():
        return {"success": False, "error": f"REAPER did not write {target}"}
    return {"success": True, "output_path": str(target),
            "file_size_bytes": target.stat().st_size}


def render_project(
    *,
    output_path: str,
    format: str = "wav",
    sample_rate: int = 48000,
    bit_depth: int = 24,
    channels: int = 2,
    overwrite: bool = False,
) -> dict:
    """
    Render the entire project to a file. An existing file is only replaced
    with overwrite=true.
    format: wav, flac, mp3 (requires LAME), ogg.
    sample_rate: e.g. 44100, 48000, 96000.
    bit_depth: 16, 24, or 32 (WAV only; ignored for mp3/ogg/flac).
    channels: 1 (mono) or 2 (stereo).
    """
    target = _target(output_path, format)
    _require_content(get_project())
    _render(target, format=format, sample_rate=sample_rate, bit_depth=bit_depth,
            channels=channels, bounds=ENTIRE_PROJECT, overwrite=overwrite)
    result = _rendered(target)
    if result["success"]:
        result.update(format=format, sample_rate=sample_rate, bit_depth=bit_depth,
                      channels=channels)
    return result


def render_time_selection(  # noqa: PLR0913 -- one argument per render setting
    *,
    output_path: str,
    start: float,
    end: float,
    format: str = "wav",
    sample_rate: int = 48000,
    bit_depth: int = 24,
    channels: int = 2,
    overwrite: bool = False,
) -> dict:
    """Render a specific time range of the project to a file. An existing file
    is only replaced with overwrite=true."""
    target = _target(output_path, format)
    if not 0 <= start < end:
        return {"success": False, "error": f"need 0 <= start < end; got start={start}, end={end}"}
    project = get_project()
    _require_content(project)
    selection = project.time_selection  # a reapy TimeSelection, not a tuple
    previous = (selection.start, selection.end)
    project.time_selection = (start, end)
    try:
        _render(target, format=format, sample_rate=sample_rate, bit_depth=bit_depth,
                channels=channels, bounds=TIME_SELECTION, overwrite=overwrite)
    finally:
        project.time_selection = previous
    result = _rendered(target)
    if result["success"]:
        result.update(start=start, end=end, format=format)
    return result


def render_stems(
    *,
    output_directory: str,
    track_indices: list[int] | None = None,
    format: str = "wav",
    sample_rate: int = 48000,
    bit_depth: int = 24,
    overwrite: bool = False,
) -> dict:
    """
    Render each track as a separate stem file by soloing each track individually.
    track_indices: list of track indices, or null to render all tracks.
    Files are named after the track names in the output directory. Existing
    files are only replaced with overwrite=true.
    """
    directory = Path(output_directory).expanduser().resolve()
    project = get_project()
    _require_content(project)
    indices = track_indices if track_indices is not None else list(range(project.n_tracks))
    tracks = project.tracks
    names = _stem_names([tracks[i].name or f"Track_{i}" for i in indices])
    targets = [directory / f"{file_name}.{format.lower()}" for file_name in names]
    # Refuse before rendering anything, so a clash never leaves half the stems written.
    clashes = [str(t) for t in targets if t.exists() or t.is_symlink()]
    if clashes and not overwrite:
        return {"success": False,
                "error": f"already exist (pass overwrite=true to replace): {', '.join(clashes)}"}
    solos = [track.get_info_value("I_SOLO") for track in tracks]
    try:
        stems = [
            _render_stem(tracks, index, target, format=format, sample_rate=sample_rate,
                         bit_depth=bit_depth, overwrite=overwrite)
            for index, target in zip(indices, targets, strict=True)
        ]
    finally:
        for track, solo in zip(tracks, solos, strict=True):
            track.set_info_value("I_SOLO", solo)
    return {"success": True, "output_directory": str(directory), "stems": stems}


def _stem_names(track_names: list[str]) -> list[str]:
    """File-safe, unique names: a second "Kick" becomes "Kick_2"."""
    # Keyed case-insensitively: on macOS and Windows "Kick" and "kick" are one file.
    seen: dict[str, int] = {}
    names = []
    for raw in track_names:
        safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in raw)
        key = safe.casefold()
        seen[key] = seen.get(key, 0) + 1
        names.append(safe if seen[key] == 1 else f"{safe}_{seen[key]}")
    return names


def _render_stem(tracks, index: int, target: Path, *, format: str,
                 sample_rate: int, bit_depth: int, overwrite: bool = False) -> dict:
    # Solo through I_SOLO: reapy's solo() goes through an action that leaves
    # solo unchanged (tests/contract).
    for i, track in enumerate(tracks):
        track.set_info_value("I_SOLO", SOLO_IN_PLACE if i == index else 0)
    _render(target, format=format, sample_rate=sample_rate, bit_depth=bit_depth,
            channels=2, bounds=ENTIRE_PROJECT, overwrite=overwrite)
    return {
        "track_index": index,
        "track_name": tracks[index].name or f"Track_{index}",
        "output_path": str(target),
        "exists": target.is_file(),
    }


TOOLS = (
    render_project,
    render_time_selection,
    render_stems,
)
