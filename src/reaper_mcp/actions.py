"""REAPER actions run by command ID, checked against the action's name first.

A wrong command ID can run any action, and some actions open a modal dialog
that hangs the bridge until someone clicks. So before an action runs, REAPER
is asked for its name (kbd_getTextFromCmd) and the action only runs if the
name is the expected one. The core transport and metronome actions
(verified=True) have had the same IDs in every REAPER for decades; they run
even when REAPER cannot name them or names them differently (a LangPack
translates action names), so stopping a recording never refuses. Anything
else is refused unless its name matches.
"""

from dataclasses import dataclass

from reaper_mcp.reaper import RPR, string_or_none

# kbd_getTextFromCmd's section: a null KbdSectionInfo* is the main section, and
# REAPER's Python binding turns anything that is not a pointer string into null.
MAIN_SECTION = 0
# GetToggleCommandState: the action has no on/off state.
NO_TOGGLE = -1


@dataclass(frozen=True)
class Action:
    command: int
    name: str
    # The ID is well known: run it even if REAPER cannot name it or translates its name.
    verified: bool = False


TRANSPORT_RECORD = Action(1013, "Transport: Record", verified=True)
TRANSPORT_STOP = Action(1016, "Transport: Stop", verified=True)
STOP_SAVE_RECORDED = Action(40667, "Transport: Stop (save all recorded media)", verified=True)
TOGGLE_METRONOME = Action(40364, "Options: Toggle metronome", verified=True)
RECORD_MODE_NORMAL = Action(40252, "Record: Set record mode to normal")
RECORD_MODE_TIME_SELECTION = Action(40076, "Record: Set record mode to time selection auto-punch")
RECORD_MODE_ITEMS = Action(40253, "Record: Set record mode to selected item auto-punch")


def action_name(command: int) -> str | None:
    """REAPER's name for a main-section action, or None if it gives none."""
    return string_or_none(RPR.kbd_getTextFromCmd, command, MAIN_SECTION)


def check(action: Action) -> None:
    """Raise unless REAPER confirms that action.command is the expected action."""
    name = action_name(action.command)
    if action.verified:
        return
    if name is None:
        raise RuntimeError(f"REAPER could not confirm that action {action.command} is "
                           f'"{action.name}", so it was not run; nothing was changed')
    if name.strip().lower() != action.name.lower():
        raise RuntimeError(f'Action {action.command} is "{name}" in this REAPER, not '
                           f'"{action.name}", so it was not run; nothing was changed')


def run(action: Action) -> None:
    check(action)
    RPR.Main_OnCommand(action.command, 0)


def is_on(action: Action) -> bool:
    """The on/off state of a toggle action; RuntimeError if it has none."""
    state = RPR.GetToggleCommandState(action.command)
    if state == NO_TOGGLE:
        raise RuntimeError(f'Action {action.command} ("{action.name}") reports no on/off state '
                           "in this REAPER; nothing was changed")
    return bool(state)


def set_toggle(action: Action, on: bool) -> bool:
    """Bring a toggle action to the wanted state, reading it first so a toggle
    never inverts it; returns the state REAPER reports afterwards."""
    if is_on(action) != on:
        run(action)
    return is_on(action)
