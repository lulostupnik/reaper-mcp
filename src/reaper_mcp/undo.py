"""What each tool does to the project, and the undo step around the ones that edit it.

Every tool is marked with one of three decorators:

- ``read_only``: looks at REAPER and changes nothing in the project.
- ``edits``: changes the project. The server runs it inside one named REAPER
  undo step (Undo_BeginBlock2 / Undo_EndBlock2), so a single Ctrl+Z, or the
  undo tool, takes back the whole tool call.
- ``no_undo``: acts on REAPER without being an edit REAPER can undo: the
  transport, writing files, opening a project, or the undo history itself.
  Wrapping these in an undo step would add meaningless entries (or, for
  undo/redo and opening a project tab, act on the wrong history).

The marks also become the tool's MCP annotations (readOnlyHint,
destructiveHint, idempotentHint), so clients can tell a question from a change.
"""

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from mcp.types import ToolAnnotations

from reaper_mcp.reaper import RPR, ensure_connected

# Undo_EndBlock2's extraflags: -1 records every kind of state (UNDO_STATE_ALL).
UNDO_STATE_ALL = -1
UNDO_PREFIX = "MCP: "
# Appended to the step of a tool call that failed: REAPER may still record its
# block, and the history should not pass it off as a change.
FAILED_SUFFIX = " (failed)"
# Tool arguments that name tracks by index, checked before an undo block opens.
TRACK_INDEX_ARGS = ("track_index", "source_track_index", "dest_track_index")
TRACK_INDEX_LISTS = ("track_indices",)
MAX_ARG_TEXT = 40


@dataclass(frozen=True)
class Kind:
    undoable: bool
    annotations: ToolAnnotations


def _mark(fn: Callable, kind: Kind) -> Callable:
    fn._tool_kind = kind  # type: ignore[attr-defined]
    return fn


def read_only(fn: Callable) -> Callable:
    """Mark a tool that only reads REAPER's state."""
    return _mark(fn, Kind(False, ToolAnnotations(read_only_hint=True, open_world_hint=False)))


def _changing(*, undoable: bool, destructive: bool, idempotent: bool) -> Callable:
    annotations = ToolAnnotations(
        read_only_hint=False, destructive_hint=destructive,
        idempotent_hint=idempotent, open_world_hint=False,
    )
    return lambda fn: _mark(fn, Kind(undoable, annotations))


def edits(*, destructive: bool = False, idempotent: bool = False) -> Callable:
    """Mark a tool that changes the project; it runs as one undo step.
    destructive: it can remove or overwrite something (undo still brings it back)."""
    return _changing(undoable=True, destructive=destructive, idempotent=idempotent)


def no_undo(*, destructive: bool = False, idempotent: bool = False) -> Callable:
    """Mark a tool that acts on REAPER but is not an undoable project edit."""
    return _changing(undoable=False, destructive=destructive, idempotent=idempotent)


def kind_of(fn: Callable) -> Kind:
    kind = getattr(fn, "_tool_kind", None)
    if kind is None:
        raise TypeError(f"tool {fn.__name__} is not marked read_only, edits or no_undo")
    return kind


# Descriptions of the undo blocks open right now, innermost last. Tools run
# one at a time (server.py's lock), so a module-level stack is enough.
_open_blocks: list[str] = []


def describe(description: str) -> None:
    """Rename the undo step being recorded, e.g. with the name of what was inserted."""
    if _open_blocks:
        _open_blocks[-1] = UNDO_PREFIX + description


@contextmanager
def undo_block(description: str) -> Iterator[None]:
    """Everything done inside is one REAPER undo step named description,
    with the UI redrawn once at the end instead of after every change.
    The step's name gets FAILED_SUFFIX when the body raises or calls failed()."""
    ensure_connected()
    RPR.Undo_BeginBlock2(0)
    RPR.PreventUIRefresh(1)
    _open_blocks.append(UNDO_PREFIX + description)
    ok = False
    try:
        yield
        ok = True
    finally:
        name = _open_blocks.pop()
        if not ok and not name.endswith(FAILED_SUFFIX):
            name += FAILED_SUFFIX
        RPR.PreventUIRefresh(-1)
        RPR.Undo_EndBlock2(0, name, UNDO_STATE_ALL)
        RPR.TrackList_AdjustWindows(False)
        RPR.UpdateArrange()


def failed() -> None:
    """Mark the undo step being recorded as a failed tool call."""
    if _open_blocks and not _open_blocks[-1].endswith(FAILED_SUFFIX):
        _open_blocks[-1] += FAILED_SUFFIX


def step_name(fn: Callable) -> str:
    """set_track_volume -> "Set track volume"."""
    return fn.__name__.replace("_", " ").capitalize()


def _short(value: Any) -> str:
    text = f"{value:g}" if isinstance(value, float) else str(value)
    return text if len(text) <= MAX_ARG_TEXT else "..." + text[-(MAX_ARG_TEXT - 3):]


def step_description(fn: Callable, kwargs: dict) -> str:
    """The step's name with the call's arguments, so undo can say which track or
    value it took back: "Set track volume (track_index=2, volume_db=-6)"."""
    if not kwargs:
        return step_name(fn)
    args = ", ".join(f"{key}={_short(value)}" for key, value in kwargs.items())
    return f"{step_name(fn)} ({args})"


def _bad_track_index(kwargs: dict) -> str | None:
    """An error for a track index in kwargs that names no track, or None.
    Checked before the undo block opens, so a call that cannot do anything
    leaves no empty step in the history (and does not clear redo)."""
    indices = [kwargs[key] for key in TRACK_INDEX_ARGS if key in kwargs]
    for key in TRACK_INDEX_LISTS:
        indices.extend(kwargs.get(key) or [])
    indices = [i for i in indices if isinstance(i, int) and not isinstance(i, bool)]
    if not indices:
        return None
    ensure_connected()
    count = RPR.CountTracks(0)
    for index in indices:
        if not 0 <= index < count:
            return f"No track {index}: there are {count} (indices start at 0); nothing was changed"
    return None


def as_undo_step(fn: Callable[..., dict]) -> Callable[..., dict]:
    """fn, run inside one undo step when it edits the project."""
    if not kind_of(fn).undoable:
        return fn

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> dict:
        error = _bad_track_index(kwargs)
        if error:
            return {"success": False, "error": error}
        with undo_block(step_description(fn, kwargs)):
            result = fn(*args, **kwargs)
            if not result.get("success", False):
                failed()
            return result

    return run
