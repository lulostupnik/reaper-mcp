import os
import time
from pathlib import Path

from reaper_mcp.reaper import RPR, ensure_connected, get_project
from reaper_mcp.undo import edits, no_undo, read_only, undo_block

# New project tabs, so nothing open is replaced and REAPER never asks to save:
# "New project tab, ignore default template" gives an empty project, and
# "New project tab" starts from the user's default project template.
NEW_PROJECT_TAB = 41929
NEW_PROJECT_TAB_FROM_TEMPLATE = 40859
PROJECT_SUFFIX = ".rpp"
DEFAULT_TEMPO = 120.0
DEFAULT_TIME_SIGNATURE = "4/4"
# Main_openProject prefix: never ask to save the project being replaced.
NO_PROMPT = "noprompt:"
# Main_SaveProjectEx option: the saved file becomes the open project's file.
SAVE_AS_PROJECT_FILE = 8


def _parse_time_signature(text: str) -> tuple[int, int]:
    """ "3/4" -> (3, 4); ValueError for anything else."""
    num, sep, denom = text.partition("/")
    if sep and num.strip().isdigit() and denom.strip().isdigit() and int(num) > 0 and int(denom) > 0:
        return int(num), int(denom)
    raise ValueError(f"time_signature must look like 4/4 or 6/8, not {text!r}")


@no_undo()
def create_project(
    *,
    tempo: float | None = None,
    time_signature: str | None = None,
    name: str = "",
    use_default_template: bool = True,
) -> dict:
    """Create a new REAPER project in a new project tab. The current project stays
    open in its own tab, unsaved changes included.
    use_default_template: start from the default project template set in REAPER's
    preferences (its tracks, routing and settings); without one configured the
    project starts empty. false: always start empty.
    tempo / time_signature (e.g. "3/4"): set them; when omitted a template keeps
    its own, and an empty project gets 120 BPM 4/4 with use_default_template=false.
    name: the song's name; save_project uses it as the file name on the first save."""
    signature = None
    try:
        if time_signature:
            signature = _parse_time_signature(time_signature)
        elif not use_default_template:
            signature = _parse_time_signature(DEFAULT_TIME_SIGNATURE)
    except ValueError as e:
        return {"success": False, "error": f"{e}; no project was created"}
    if tempo is None and not use_default_template:
        tempo = DEFAULT_TEMPO
    if tempo is not None and not tempo > 0:
        return {"success": False, "error": f"tempo must be above 0 BPM, not {tempo}; "
                                           "no project was created"}
    ensure_connected()
    command = NEW_PROJECT_TAB_FROM_TEMPLATE if use_default_template else NEW_PROJECT_TAB
    RPR.Main_OnCommand(command, 0)
    with undo_block("Set up new project"):
        _set_up_project(tempo, signature, name)
    return {
        "success": True,
        "name": name,
        "from_default_template": use_default_template,
        "tempo": current_tempo(),
        "time_signature": time_signature_text(),
    }


def _set_up_project(tempo: float | None, signature: tuple[int, int] | None, name: str) -> None:
    project = get_project()
    if tempo is not None:
        project.bpm = tempo
    if signature:
        _set_time_signature(*signature)
    if name:
        RPR.GetSetProjectInfo_String(0, "PROJECT_TITLE", name, True)


def documents_dir() -> Path:
    """The user's documents folder (~/Documentos on a Spanish desktop), from
    XDG's user-dirs.dirs; ~/Documents when that names none."""
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        lines = config.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == "XDG_DOCUMENTS_DIR":
            folder = Path(value.strip().strip('"').replace("$HOME", str(home)))
            # A documents folder set to $HOME itself means "none" in XDG.
            if folder.is_absolute() and folder != home:
                return folder
    return home / "Documents"


def _free_path(folder: Path, stem: str) -> Path:
    """folder/stem.rpp, or stem (2).rpp, stem (3).rpp... if taken."""
    path, number = folder / f"{stem}{PROJECT_SUFFIX}", 2
    while path.exists() or path.is_symlink():
        path, number = folder / f"{stem} ({number}){PROJECT_SUFFIX}", number + 1
    return path


def _default_save_path(project) -> str:
    """Where a never-saved project goes: a new file named after the project,
    never one that already exists."""
    proj_name = (project.name or _project_title()
                 or f"Project {time.strftime('%Y-%m-%d %H-%M-%S')}")
    proj_name = "".join(c if c.isalnum() or c in " _-." else "_" for c in proj_name)
    if proj_name.lower().endswith(PROJECT_SUFFIX):
        proj_name = proj_name[: -len(PROJECT_SUFFIX)]
    default_dir = documents_dir() / "REAPER Projects"
    os.makedirs(default_dir, exist_ok=True)
    return str(_free_path(default_dir, proj_name))


def project_file() -> str:
    """The open project's .rpp file, or "" while it has never been saved."""
    # (project, index, filename, size) per REAPER's Python binding; -1 = current project.
    return str(RPR.EnumProjects(-1, "", 4096)[2])


def _project_title() -> str:
    return str(RPR.GetSetProjectInfo_String(0, "PROJECT_TITLE", "", False)[3])


def _same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


@no_undo(destructive=True, idempotent=True)
def save_project(*, project_path: str = "", overwrite: bool = False) -> dict:
    """Save the current project as a .rpp file, which then becomes the open
    project's file (later saves and new recordings go next to it). With no path,
    a project that has a file is saved to it again; one that was never saved gets
    a new file in the documents folder's "REAPER Projects" folder, named after the
    project (name from create_project, else the time), with " (2)" added if that
    name is taken. An existing file at a given path is only replaced with
    overwrite=true. The reply's project_path is the file written."""
    ensure_connected()
    own_file = project_file()
    if not project_path:
        project_path = own_file or _default_save_path(get_project())
    path = absolute_path(project_path)
    if path.suffix.lower() != PROJECT_SUFFIX:
        return {"success": False, "error": f"project_path must end in .rpp: {project_path}"}
    if path.is_symlink():
        return {"success": False, "error": f"{path} is a symlink; refusing to write through it"}
    # Saving the project over its own file is an ordinary save.
    if path.exists() and not overwrite and not _same_file(str(path), own_file):
        return {"success": False,
                "error": f"{path} already exists; pass overwrite=true to replace it"}
    project_path = str(path)
    os.makedirs(path.parent, exist_ok=True)
    # reapy's Project.save takes a "force save as" flag, not a path.
    RPR.Main_SaveProjectEx(0, project_path, SAVE_AS_PROJECT_FILE)
    if not os.path.isfile(project_path):
        return {"success": False, "error": f"REAPER did not write {project_path}"}
    if not _same_file(project_file(), project_path):
        return {"success": False,
                "error": f"REAPER wrote {project_path}, but the open project is still "
                         f"{project_file() or 'unsaved'}"}
    # The file may have existed before; a project still needing a save was not written.
    if RPR.IsProjectDirty(0):
        return {"success": False,
                "error": f"REAPER could not save to {project_path}; the project still has "
                         "unsaved changes (check the folder's permissions and free space)"}
    return {"success": True, "project_path": project_path}


def absolute_path(path: str) -> Path:
    """path with ~ expanded and its folder resolved, but not its last component.

    REAPER resolves a relative path against its own working directory, not this
    server's, so every path handed to REAPER is made absolute here first; the
    last component is left unresolved so a symlink there can be detected.
    """
    raw = Path(path).expanduser()
    return raw.parent.resolve() / raw.name


@no_undo(destructive=True)
def load_project(*, project_path: str) -> dict:
    """Open a REAPER project (.rpp) from the given file path in place of the
    current one. Refuses while the current project has unsaved changes: save it
    first (save_project), or open a new tab with create_project."""
    path = absolute_path(project_path)
    if path.suffix.lower() != PROJECT_SUFFIX:
        return {"success": False, "error": f"not a REAPER project (.rpp): {project_path}"}
    if not path.is_file():
        return {"success": False, "error": f"File not found: {project_path}"}
    project_path = str(path)
    ensure_connected()
    if RPR.IsProjectDirty(0):
        return {"success": False,
                "error": "The current project has unsaved changes; save it with save_project "
                         "first, so nothing is lost. Nothing was opened."}
    # noprompt: REAPER would otherwise ask to save in a dialog, which hangs the bridge.
    RPR.Main_openProject(NO_PROMPT + project_path)
    project = get_project()
    return {
        "success": True,
        "name": project.name,
        "tempo": current_tempo(),
        "time_signature": time_signature_text(),
        "project_path": project_path,
    }


def markers_and_regions() -> tuple[list[dict], list[dict]]:
    """Project markers and regions in timeline order; number is the one REAPER
    shows (what the marker tools take)."""
    markers: list[dict] = []
    regions: list[dict] = []
    total = RPR.CountProjectMarkers(0, 0, 0)[0]
    for i in range(total):
        handle = RPR.GetRegionOrMarker(0, i, "")
        name = RPR.GetSetRegionOrMarkerInfo_String(0, handle, "P_NAME", "", False)[4]
        start = RPR.GetRegionOrMarkerInfo_Value(0, handle, "D_STARTPOS")
        number = int(RPR.GetRegionOrMarkerInfo_Value(0, handle, "I_NUMBER"))
        if RPR.GetRegionOrMarkerInfo_Value(0, handle, "B_ISREGION"):
            end = RPR.GetRegionOrMarkerInfo_Value(0, handle, "D_ENDPOS")
            regions.append({"number": number, "name": name, "start": start, "end": end})
        else:
            markers.append({"number": number, "name": name, "position": start})
    return markers, regions


def current_tempo() -> float:
    """The tempo in quarter notes per minute, as REAPER displays it.

    reapy's Project.bpm setter takes quarter notes, but its getter counts beats
    of the time signature's denominator: 240 for 6/8 at a quarter-note 120.
    """
    return RPR.Master_GetTempo()


def time_signature_text() -> str:
    """The time signature at the start of the project, e.g. "3/4".

    reapy's Project.time_signature is (bpm, numerator), not a time signature.
    """
    _, _, numerator, denominator, _ = RPR.TimeMap_GetTimeSigAtTime(0, 0.0, 0, 0, 0)
    return f"{numerator}/{denominator}"


def _marker_at_start() -> list | None:
    """REAPER's tempo/time signature marker at position 0, if there is one:
    [_, _, index, position, measure, beat, bpm, numerator, denominator, linear]."""
    if RPR.CountTempoTimeSigMarkers(0) == 0:
        return None
    marker = RPR.GetTempoTimeSigMarker(0, 0, 0.0, 0, 0.0, 0.0, 0, 0, False)
    return marker if marker[3] <= 1e-9 else None


def _set_time_signature(numerator: int, denominator: int) -> None:
    """Set the time signature at the start of the project.

    REAPER keeps it on a tempo/time signature marker at position 0: edit that
    marker when there is one, otherwise add one at the current tempo.
    """
    marker = _marker_at_start()
    if marker is None:
        RPR.SetTempoTimeSigMarker(0, -1, 0.0, -1, -1, current_tempo(), numerator, denominator, False)
        return
    # Keep the marker's own tempo and linear flag: only the signature changes.
    bpm, linear = marker[6], marker[9]
    RPR.SetTempoTimeSigMarker(0, 0, 0.0, -1, -1, bpm, numerator, denominator, linear)


def _set_tempo(project, bpm: float) -> None:
    """Set the tempo at the start of the project.

    With a tempo/time signature marker at position 0, reapy's Project.bpm
    setter (SetCurrentBPM) changes the playing tempo but can leave the
    marker's own BPM stale (seen on REAPER 7.82 with a 3/4 marker), so the
    marker is edited directly, keeping its time signature.
    """
    marker = _marker_at_start()
    if marker is None:
        project.bpm = bpm
        return
    numerator, denominator, linear = marker[7], marker[8], marker[9]
    RPR.SetTempoTimeSigMarker(0, 0, 0.0, -1, -1, bpm, numerator, denominator, linear)


@read_only
def get_project_info() -> dict:
    """Get basic information about the current project: name, file, recording
    folder, tempo, track count, length, markers and regions. For every track's
    routing, recording setup and FX use get_project_summary."""
    project = get_project()
    markers, regions = markers_and_regions()
    return {
        "success": True,
        "name": project.name,
        "project_file": project_file(),
        "path": project.path,
        "tempo": current_tempo(),
        "time_signature": time_signature_text(),
        "length": project.length,
        "track_count": project.n_tracks,
        "markers": markers,
        "regions": regions,
    }


@edits(idempotent=True)
def set_tempo(*, bpm: float) -> dict:
    """Set the project tempo in BPM."""
    _set_tempo(get_project(), bpm)
    return {"success": True, "tempo": current_tempo()}


@edits(idempotent=True)
def set_time_signature(*, numerator: int, denominator: int) -> dict:
    """Set the project time signature, e.g. 4/4, 3/4, 6/8."""
    _set_time_signature(numerator, denominator)
    return {"success": True, "time_signature": time_signature_text()}


TOOLS = (
    create_project,
    save_project,
    load_project,
    get_project_info,
    set_tempo,
    set_time_signature,
)
