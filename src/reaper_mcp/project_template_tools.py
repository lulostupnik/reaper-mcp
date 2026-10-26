"""Project templates (a ready-made starting point: tracks, inputs, FX, routing)
and saving tracks as track templates.

Project templates are .RPP files in REAPER's ProjectTemplates folder. Opening
one with Main_openProject's "template:" prefix gives a new, untitled project
with the template's contents, as File > New project from template does; it is
opened in a new project tab, so the current project is never replaced and
REAPER never asks to save it. Saving writes a copy: the open project keeps its
own file.
"""

import difflib
from pathlib import Path

from reaper_mcp.project_tools import (
    NEW_PROJECT_TAB,
    current_tempo,
    project_file,
    time_signature_text,
)
from reaper_mcp.reaper import RPR, ensure_connected, get_project
from reaper_mcp.template_tools import get_template_dir
from reaper_mcp.undo import no_undo, read_only

PROJECT_TEMPLATE_SUFFIX = ".rpp"
TRACK_TEMPLATE_SUFFIX = ".RTrackTemplate"
# Main_openProject: load the file as a template (untitled, its media not owned).
TEMPLATE_PREFIX = "template:"
# Main_SaveProjectEx options.
SAVE_SELECTED_TRACKS_AS_TEMPLATE, WITH_MEDIA, WITH_ENVELOPES = 1, 2, 4
UNSAFE_NAME_CHARS = '/\\:*?"<>|'


def project_template_dir() -> Path:
    """REAPER's ProjectTemplates folder."""
    ensure_connected()
    return Path(RPR.GetResourcePath()) / "ProjectTemplates"


def _project_templates(folder: Path) -> list[dict]:
    if not folder.is_dir():
        return []
    return [
        {"name": path.relative_to(folder).with_suffix("").as_posix(), "file": str(path)}
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.suffix.lower() == PROJECT_TEMPLATE_SUFFIX
        and not any(part.startswith(".") for part in path.relative_to(folder).parts)
    ]


@read_only
def list_project_templates() -> dict:
    """List the project templates (.RPP files in REAPER's ProjectTemplates folder,
    subfolders included) that new_project_from_template can start from."""
    folder = project_template_dir()
    templates = _project_templates(folder)
    return {"success": True, "template_directory": str(folder), "count": len(templates),
            "templates": templates}


def _resolve_project_template(template: str, folder: Path) -> Path:
    """A template file by path, or by name from list_project_templates (case-insensitive)."""
    direct = Path(template).expanduser()
    if direct.is_absolute() and direct.is_file() and direct.suffix.lower() == ".rpp":
        return direct
    key = template.replace("\\", "/").strip().strip("/")
    if key.lower().endswith(PROJECT_TEMPLATE_SUFFIX):
        key = key[: -len(PROJECT_TEMPLATE_SUFFIX)]
    templates = _project_templates(folder)
    for match in (lambda t: t["name"].lower() == key.lower(),
                  lambda t: t["name"].rsplit("/", 1)[-1].lower() == key.lower()):
        found = [t for t in templates if match(t)]
        if len(found) == 1:
            return Path(found[0]["file"])
        if len(found) > 1:
            raise ValueError(f"'{template}' matches several templates: "
                             f"{', '.join(t['name'] for t in found)}")
    names = [t["name"] for t in templates]
    close = difflib.get_close_matches(key, names, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise FileNotFoundError(f"No project template '{template}' in {folder}.{hint} "
                            f"Available: {', '.join(names) or 'none'}.")


@no_undo()
def new_project_from_template(*, template: str) -> dict:
    """Start a new project from a project template, in a new project tab (the
    current project stays open in its tab). template: a name from
    list_project_templates, or the path of a .RPP file. The new project is
    untitled: save it with save_project before recording. Each call opens
    another tab, so do not retry it just to be sure."""
    path = _resolve_project_template(template, project_template_dir())
    RPR.Main_OnCommand(NEW_PROJECT_TAB, 0)
    RPR.Main_openProject(TEMPLATE_PREFIX + str(path))
    project = get_project()
    return {
        "success": True,
        "template": str(path),
        "track_count": project.n_tracks,
        "tracks": [track.name for track in project.tracks],
        "tempo": current_tempo(),
        "time_signature": time_signature_text(),
        "project_file": project_file(),
        "message": ("New project from the template, in a new project tab; the previous "
                    "project is still open in its own tab. Save the new one with save_project "
                    "before recording."),
    }


def _existing_variant(folder: Path, stem: str, suffix: str) -> Path | None:
    """An existing file that is the same template name in another letter case
    (Song.rpp for Song.RPP): Linux would otherwise keep both."""
    if not folder.is_dir():
        return None
    wanted = f"{stem}{suffix}".lower()
    return next((p for p in sorted(folder.iterdir()) if p.name.lower() == wanted), None)


def _template_file(folder: Path, name: str, suffix: str, *, overwrite: bool) -> Path:
    """folder/name+suffix, refusing names that are paths and existing files
    unless overwrite."""
    stem = name.strip()
    if stem.lower().endswith(suffix.lower()):
        stem = stem[: -len(suffix)]
    if not stem or stem.startswith(".") or any(c in UNSAFE_NAME_CHARS for c in stem):
        raise ValueError(f"template name {name!r} must be a plain file name "
                         f"(no {UNSAFE_NAME_CHARS} and not starting with '.')")
    path = _existing_variant(folder, stem, suffix) or folder / f"{stem}{suffix}"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FileExistsError(f"{path} is not a regular file; refusing to replace it")
    if path.exists() and not overwrite:
        raise FileExistsError(f"a template named {stem!r} already exists ({path}); "
                              "pass overwrite=true to replace it")
    folder.mkdir(parents=True, exist_ok=True)
    return path


@no_undo(destructive=True, idempotent=True)
def save_project_template(
    *, name: str, overwrite: bool = False, include_items: bool = False
) -> dict:
    """Save the current project as a project template named name (in REAPER's
    ProjectTemplates folder), to start new songs from with new_project_from_template.
    The open project keeps its own file. An existing template is only replaced with
    overwrite=true. A project with items (recordings, MIDI) is refused unless
    include_items=true: every new song would start with them. Remove them first
    for a clean template."""
    items = sum(track.n_items for track in get_project().tracks)
    if items and not include_items:
        return {"success": False, "error": f"The project has {items} item(s); a template with "
                                           "them starts every new song with them (and refers to "
                                           "their audio files). Remove them first, or pass "
                                           "include_items=true to keep them."}
    path = _template_file(project_template_dir(), name, ".RPP", overwrite=overwrite)
    RPR.Main_SaveProjectEx(0, str(path), 0)
    if not path.is_file():
        return {"success": False, "error": f"REAPER did not write {path}"}
    return {"success": True, "name": path.stem, "file": str(path), "items": items}


def _selected() -> list[str]:
    return [RPR.GetSelectedTrack(0, i) for i in range(RPR.CountSelectedTracks(0))]


def _select(pointers: list[str]) -> None:
    for i in range(RPR.CountTracks(0)):
        track = RPR.GetTrack(0, i)
        RPR.SetTrackSelected(track, track in pointers)


@no_undo(destructive=True, idempotent=True)
def save_track_template(
    *,
    name: str,
    track_indices: list[int] | None = None,
    include_items: bool = False,
    include_envelopes: bool = True,
    overwrite: bool = False,
) -> dict:
    """Save tracks (with their FX, input, routing between them and settings) as a
    track template named name in REAPER's TrackTemplates folder, for
    insert_track_template. track_indices: the tracks to save; by default the
    selected ones. include_items: also save their items. The track selection is
    left as it was. An existing template is only replaced with overwrite=true."""
    count = RPR.CountTracks(0)
    if track_indices is not None:
        bad = [i for i in track_indices if not 0 <= i < count]
        if bad or not track_indices:
            return {"success": False, "error": f"track_indices must name tracks 0 to "
                                               f"{count - 1}; got {track_indices}"}
    elif not RPR.CountSelectedTracks(0):
        return {"success": False, "error": "No track is selected: pass track_indices"}
    path = _template_file(get_template_dir(), name, TRACK_TEMPLATE_SUFFIX,
                          overwrite=overwrite)
    options = (SAVE_SELECTED_TRACKS_AS_TEMPLATE | (WITH_MEDIA if include_items else 0)
               | (WITH_ENVELOPES if include_envelopes else 0))
    selection = _selected()
    try:
        if track_indices is not None:
            _select([RPR.GetTrack(0, i) for i in track_indices])
        chosen = set(_selected())
        saved = [track.name for track in get_project().tracks if track.id in chosen]
        RPR.Main_SaveProjectEx(0, str(path), options)
    finally:
        _select(selection)
    if not path.is_file():
        return {"success": False, "error": f"REAPER did not write {path}"}
    return {"success": True, "name": path.stem, "file": str(path), "tracks": saved}


TOOLS = (
    list_project_templates,
    new_project_from_template,
    save_project_template,
    save_track_template,
)
