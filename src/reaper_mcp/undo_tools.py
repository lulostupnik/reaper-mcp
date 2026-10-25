"""undo, redo and get_undo_state: REAPER's own undo history, as Ctrl+Z sees it.

The history is the project's, not this server's: the last step may be one the
user made by hand, or REAPER's own step for media just recorded. So undo and
redo only act on this server's steps ("MCP: ...") unless told otherwise, and
every reply names the step it undid or redid.
"""

from reaper_mcp.reaper import RPR, ensure_connected, string_or_none
from reaper_mcp.undo import FAILED_SUFFIX, UNDO_PREFIX, no_undo, read_only

RECORDING = 4  # GetPlayState bit


def _next_undo() -> str | None:
    return string_or_none(RPR.Undo_CanUndo2, 0)


def _next_redo() -> str | None:
    return string_or_none(RPR.Undo_CanRedo2, 0)


def _state() -> dict:
    return {"next_undo": _next_undo(), "next_redo": _next_redo()}


def _refusal(step: str, verb: str, include_manual_steps: bool) -> str | None:
    """Why step must not be undone/redone now, or None."""
    if RPR.GetPlayState() & RECORDING:
        return f"REAPER is recording; stop the transport before {verb}ing anything"
    if not include_manual_steps and not step.startswith(UNDO_PREFIX):
        return (f"The step to {verb} is '{step}', which was not made by this server (it was "
                "made by hand in REAPER, or is a recording). Ask the user before passing "
                "include_manual_steps=true: it may remove a take they just recorded.")
    return None


def _note(step: str) -> dict:
    if step.endswith(FAILED_SUFFIX):
        return {"note": "that step was a tool call that failed; it changed nothing intended"}
    return {}


@read_only
def get_undo_state() -> dict:
    """Say what undo and redo would do right now: the name of the step undo would
    take back (next_undo) and the one redo would bring back (next_redo), or null.
    Steps made by this server start with "MCP: "."""
    ensure_connected()
    return {"success": True, **_state()}


@no_undo(destructive=True)
def undo(*, include_manual_steps: bool = False) -> dict:
    """Undo the last change in the project, like Ctrl+Z in REAPER, and say which
    step was undone. Every change this server makes is one step ("MCP: ..."), so
    one undo takes back one whole tool call.
    Only this server's steps are undone: when the last step was made by hand in
    REAPER or is recorded media (undoing it deletes the take just recorded), the
    tool refuses and names it; pass include_manual_steps=true only after the user
    confirms. Never undoes while recording."""
    ensure_connected()
    step = _next_undo()
    if step is None:
        return {"success": False, "error": "Nothing to undo"}
    refusal = _refusal(step, "undo", include_manual_steps)
    if refusal:
        return {"success": False, "error": refusal, "next_undo": step}
    if not RPR.Undo_DoUndo2(0):
        return {"success": False, "error": f"REAPER could not undo '{step}'"}
    return {"success": True, "undone": step, **_note(step), **_state()}


@no_undo(destructive=True)
def redo(*, include_manual_steps: bool = False) -> dict:
    """Redo the last undone change, like Ctrl+Shift+Z in REAPER, and say which
    step was redone. Like undo, only redoes this server's steps unless
    include_manual_steps=true, and never while recording."""
    ensure_connected()
    step = _next_redo()
    if step is None:
        return {"success": False, "error": "Nothing to redo"}
    refusal = _refusal(step, "redo", include_manual_steps)
    if refusal:
        return {"success": False, "error": refusal, "next_redo": step}
    if not RPR.Undo_DoRedo2(0):
        return {"success": False, "error": f"REAPER could not redo '{step}'"}
    return {"success": True, "redone": step, **_note(step), **_state()}


TOOLS = (
    undo,
    redo,
    get_undo_state,
)
