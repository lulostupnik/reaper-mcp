"""Routing: a send's pan, mute and pre/post-fader mode, and folders.

REAPER stores folders as one number per track, I_FOLDERDEPTH: 1 opens a
folder (the track is its parent), -n closes n folders after the track, 0 is
neither; the numbers over all tracks must add up to 0. These tools work on
the tree those numbers describe, move a track (with anything inside it) as
one block, then write every track's number again from the new tree, so the
sum stays valid whatever the starting point.
"""

from reaper_mcp.guids import track_guid
from reaper_mcp.mixing_tools import SEND_MODES, pointer_indices, track_sends
from reaper_mcp.reaper import RPR, get_project
from reaper_mcp.undo import edits, read_only
from reaper_mcp.units import db_to_linear


def _send_changes(*, volume_db, pan, muted, mode) -> dict[str, float]:
    changes: dict[str, float] = {}
    if volume_db is not None:
        changes["D_VOL"] = db_to_linear(volume_db)
    if pan is not None:
        if not -1.0 <= pan <= 1.0:
            raise ValueError("pan must be from -1 (left) to 1 (right)")
        changes["D_PAN"] = pan
    if muted is not None:
        changes["B_MUTE"] = float(muted)
    if mode is not None:
        if mode not in SEND_MODES:
            raise ValueError(f"mode must be one of {list(SEND_MODES)}")
        changes["I_SENDMODE"] = SEND_MODES[mode]
    if not changes:
        raise ValueError("Nothing to change: give volume_db, pan, muted or mode")
    return changes


@edits(idempotent=True)
def edit_send(
    *, source_track_index: int, send_index: int, volume_db: float | None = None,
    pan: float | None = None, muted: bool | None = None, mode: str | None = None,
) -> dict:
    """
    Change a send (see list_sends): volume_db, pan (-1 left .. 1 right), muted, and
    mode: post_fader (the usual: follows the track's fader, for reverb/effect
    sends), pre_fader (ignores the track's fader, for a headphone/monitor mix) or
    pre_fx (before the track's FX and fader). Only the values given change.
    """
    project = get_project()
    track = project.tracks[source_track_index]
    try:
        changes = _send_changes(volume_db=volume_db, pan=pan, muted=muted, mode=mode)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    if not 0 <= send_index < RPR.GetTrackNumSends(track.id, 0):
        return {"success": False, "error": f"track {source_track_index} has no send {send_index}"}
    for param, value in changes.items():
        RPR.SetTrackSendInfo_Value(track.id, 0, send_index, param, value)
    send = track_sends(track, pointer_indices(list(project.tracks)))[send_index]
    return {"success": True, "source_track_index": source_track_index, **send}


# Folders


def parents(depths: list[int]) -> list[int | None]:
    """Each track's parent folder track index (None at top level), read the way
    REAPER reads I_FOLDERDEPTH: closing more folders than are open closes all."""
    stack: list[int] = []
    result: list[int | None] = []
    for index, depth in enumerate(depths):
        result.append(stack[-1] if stack else None)
        if depth > 0:
            stack.append(index)
        for _ in range(min(-depth, len(stack))):
            stack.pop()
    return result


def _children(parent_of: list[int | None]) -> dict[int | None, list[int]]:
    children: dict[int | None, list[int]] = {None: []}
    for index, parent in enumerate(parent_of):
        children.setdefault(index, [])
        children.setdefault(parent, []).append(index)
    return children


def _flatten(children: dict[int | None, list[int]]) -> tuple[list[int], list[int]]:
    """(track order, each one's level) walking the tree depth first."""
    order: list[int] = []
    levels: list[int] = []

    def walk(node: int, level: int) -> None:
        order.append(node)
        levels.append(level)
        for child in children[node]:
            walk(child, level + 1)

    for top in children[None]:
        walk(top, 0)
    return order, levels


def depths_for(levels: list[int]) -> list[int]:
    """The I_FOLDERDEPTH of each track in order, from the tree levels: the
    level change to the next track (back to 0 after the last), so they add up to 0."""
    following = [*levels[1:], 0]
    return [after - level for level, after in zip(levels, following, strict=True)]


def subtree(children: dict[int | None, list[int]], node: int) -> list[int]:
    nodes = [node]
    for child in children[node]:
        nodes.extend(subtree(children, child))
    return nodes


def _depths(tracks: list) -> list[int]:
    return [int(t.get_info_value("I_FOLDERDEPTH")) for t in tracks]


def _apply(tracks: list, order: list[int], *, new_depths: list[int], block: list[int]) -> None:
    """Move block (old indices, contiguous) to where order puts it, then write
    every track's folder depth for the new order. The selection is put back."""
    if order != list(range(len(tracks))):
        after = order.index(block[-1]) + 1
        before = order[after] if after < len(order) else len(tracks)
        selected = [t.id for t in tracks if t.is_selected]
        RPR.SetOnlyTrackSelected(tracks[block[0]].id)
        for index in block[1:]:
            RPR.SetTrackSelected(tracks[index].id, True)
        RPR.ReorderSelectedTracks(before, 0)
        for track in tracks:
            RPR.SetTrackSelected(track.id, track.id in selected)
    for position, old in enumerate(order):
        RPR.SetMediaTrackInfo_Value(tracks[old].id, "I_FOLDERDEPTH", new_depths[position])


def _structure(project) -> list[dict]:
    tracks = list(project.tracks)
    parent_of = parents(_depths(tracks))
    children = _children(parent_of)
    _, levels = _flatten(children)
    return [{"index": i, "name": t.name, "guid": track_guid(t), "level": levels[i],
             "parent_index": parent_of[i], "is_folder": bool(children[i])}
            for i, t in enumerate(tracks)]


@read_only
def get_folder_structure() -> dict:
    """The project's folders: for every track its level (0 = top), the index of
    the folder track it is in (parent_index, null at top level) and whether it
    is a folder itself."""
    return {"success": True, "tracks": _structure(get_project())}


def _regroup(project, track_index: int, *, new_parent: int | None, after: int | None) -> dict:
    """Move track_index (and its contents) under new_parent (None: top level),
    as the last child, or right after sibling `after` when given."""
    tracks = list(project.tracks)
    children = _children(parents(_depths(tracks)))
    block = subtree(children, track_index)
    for siblings in children.values():
        if track_index in siblings:
            siblings.remove(track_index)
    siblings = children[new_parent]
    siblings.insert(siblings.index(after) + 1 if after is not None else len(siblings),
                    track_index)
    order, levels = _flatten(children)
    guid = track_guid(tracks[track_index])
    _apply(tracks, order, new_depths=depths_for(levels), block=block)
    structure = _structure(project)
    moved = next(t for t in structure if t["guid"] == guid)
    now = list(project.tracks)
    if [t.id for t in now] != [tracks[i].id for i in order] or _depths(now) != depths_for(levels):
        return {"success": False, "error": "REAPER's tracks and folders do not look as "
                                           "expected afterwards; undo this step",
                "tracks": structure}
    return {"success": True, "track": moved, "tracks": structure}


@edits()
def move_track_into_folder(
    *, track_index: int, folder_track_index: int, allow_new_folder: bool = False
) -> dict:
    """
    Put a track (with anything inside it) into a folder, as its last track.
    folder_track_index: the folder track. A normal track (one with audio, say)
    is refused unless allow_new_folder is true; then it becomes a folder.
    The track's sound then goes through the folder track (its volume, mute and FX
    apply), unless the track only feeds a bus.
    """
    tracks = list(get_project().tracks)
    folder = get_project().tracks[folder_track_index]  # IndexError for a bad index first
    children = _children(parents(_depths(tracks)))
    if folder_track_index in subtree(children, track_index):
        return {"success": False, "error": "A track cannot go into itself or into a "
                                           "folder inside it"}
    new_folder = not children[folder_track_index]
    if new_folder and not allow_new_folder:
        return {"success": False, "error": f"Track {folder_track_index} ('{folder.name}') is not a "
                                           "folder. Check the index; to make it one (its volume "
                                           "and FX then apply to the tracks inside), pass "
                                           "allow_new_folder=true"}
    result = _regroup(get_project(), track_index, new_parent=folder_track_index, after=None)
    if result["success"] and new_folder:
        result["note"] = f"Track {folder_track_index} is now a folder"
    return result


@edits()
def move_track_out_of_folder(*, track_index: int) -> dict:
    """
    Take a track (with anything inside it) out of its folder: it moves up one level,
    right after the folder. A folder left empty becomes a normal track.
    """
    project = get_project()
    parent_of = parents(_depths(list(project.tracks)))
    parent = parent_of[track_index]
    if parent is None:
        return {"success": False, "error": f"Track {track_index} is not in a folder"}
    return _regroup(project, track_index, new_parent=parent_of[parent], after=parent)


TOOLS = (edit_send, get_folder_structure, move_track_into_folder, move_track_out_of_folder)
