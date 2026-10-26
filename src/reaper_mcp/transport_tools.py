"""Metronome, time selection, loop and punch recording.

The metronome and punch mode are REAPER options, not edits to the project's
content: they are read first and only changed when they differ, so a toggle
action never inverts them, and REAPER is asked afterwards whether they took.
The time selection and loop range are selections, like the edit cursor:
REAPER does not keep them in its undo history by default, so these tools are
not undo steps either.

Count-in is only reported, never changed: where REAPER keeps it could not be
confirmed (see LIVE_CHECKS.md), and guessing would flip some other option.
"""

from reaper_mcp import actions
from reaper_mcp.actions import (
    RECORD_MODE_ITEMS,
    RECORD_MODE_NORMAL,
    RECORD_MODE_TIME_SELECTION,
    TOGGLE_METRONOME,
)
from reaper_mcp.reaper import RPR
from reaper_mcp.undo import no_undo

# The project's metronome flags (also the first number on the .rpp METRONOME
# line): &1 = metronome on (the state of "Options: Toggle metronome"),
# &16 = count-in before recording (unverified).
METRONOME_FLAGS = "projmetroen"
METRONOME_ON = 1
COUNT_IN_RECORD = 16
COUNT_IN_GUIDANCE = (
    "Count-in is set in REAPER itself: right-click the metronome button on the transport "
    "bar and tick \"Count-in before recording\" (Options > Metronome/pre-roll settings has "
    "the number of bars)."
)
# The punch modes set_punch_mode offers; punch_mode() also recognises
# "selected_items", so a mode left on from REAPER's menus is still reported.
PUNCH_MODES = {
    "normal": RECORD_MODE_NORMAL,
    "time_selection": RECORD_MODE_TIME_SELECTION,
}
KNOWN_PUNCH_MODES = {**PUNCH_MODES, "selected_items": RECORD_MODE_ITEMS}
SET_REPEAT, CLEAR_REPEAT, QUERY_REPEAT = 1, 0, -1


def _metronome_flags() -> int | None:
    # (retval, name, value, size) per REAPER's Python binding.
    ok, _, value, _ = RPR.get_config_var_string(METRONOME_FLAGS, "", 64)
    try:
        return int(float(value)) if ok else None
    except ValueError:
        return None


def metronome_state() -> bool | None:
    """Whether the metronome is on; None when REAPER reports no state."""
    try:
        return actions.is_on(TOGGLE_METRONOME)
    except RuntimeError:
        return None


def count_in_state() -> bool | None:
    """Whether count-in before recording seems to be on; None when it cannot be
    read, or when the flags' metronome bit disagrees with the metronome's own
    toggle state (then they are not the flags this module thinks they are)."""
    flags = _metronome_flags()
    metronome = metronome_state()
    if flags is None or metronome is None or bool(flags & METRONOME_ON) != metronome:
        return None
    return bool(flags & COUNT_IN_RECORD)


@no_undo(idempotent=True)
def set_metronome(*, enabled: bool | None = None) -> dict:
    """Turn the metronome (click) on or off; leave enabled out to only report it.
    Also reports count_in (REAPER plays the click for a bar or two before it
    starts recording; null if unknown). Count-in cannot be changed from here:
    right-click the metronome button on REAPER's transport bar and tick
    "Count-in before recording"."""
    if enabled is not None:
        actions.set_toggle(TOGGLE_METRONOME, enabled)
    return {"success": True, "metronome": actions.is_on(TOGGLE_METRONOME),
            "count_in": count_in_state(), "count_in_how_to_change": COUNT_IN_GUIDANCE}


def time_range(*, loop: bool) -> dict | None:
    """The time selection, or the loop range with loop=True; None when empty."""
    # (project, is_set, is_loop, start, end, allow_autoseek) per REAPER's Python binding.
    result = RPR.GetSet_LoopTimeRange2(0, False, loop, 0.0, 0.0, False)
    start, end = result[3], result[4]
    return {"start": start, "end": end} if end > start else None


def _range_error(start: float, end: float) -> str | None:
    if not 0 <= start <= end:
        return f"need 0 <= start <= end (seconds); got start={start}, end={end}"
    return None


def _ranges() -> dict:
    return {"time_selection": time_range(loop=False), "loop_range": time_range(loop=True)}


def _linked_warning(before: dict, after: dict, *, changed: str, other: str) -> dict:
    """A warning when setting one range moved the other too (REAPER's default
    "Loop points linked to time selection")."""
    if before[other] == after[other]:
        return {}
    return {"warning": f"Setting the {changed.replace('_', ' ')} also changed the "
                       f"{other.replace('_', ' ')}: REAPER links loop points to the time "
                       "selection (Options > Loop points linked to time selection). An "
                       "auto-punch region follows the time selection."}


@no_undo(idempotent=True)
def set_time_selection(*, start: float, end: float) -> dict:
    """Select the time from start to end (seconds). start == end clears the
    selection. Auto-punch recording (set_punch_mode) records only inside it.
    Reports the loop range too: REAPER usually links it to the time selection."""
    error = _range_error(start, end)
    if error:
        return {"success": False, "error": error}
    before = _ranges()
    RPR.GetSet_LoopTimeRange2(0, True, False, start, end, False)
    after = _ranges()
    return {"success": True, **after,
            **_linked_warning(before, after, changed="time_selection", other="loop_range")}


@no_undo(idempotent=True)
def set_loop(*, enabled: bool, start: float | None = None, end: float | None = None) -> dict:
    """Turn repeat (looped playback) on or off. start/end (seconds, both or
    neither): also set the loop range. Turning repeat on needs a loop range.
    Reports the time selection too: REAPER usually links it to the loop range."""
    if (start is None) != (end is None):
        return {"success": False, "error": "pass both start and end, or neither"}
    before = _ranges()
    if start is not None and end is not None:
        error = _range_error(start, end)
        if not error and enabled and end <= start:
            error = "a loop to repeat needs end > start"
        if error:
            return {"success": False, "error": f"{error}; nothing was changed"}
    elif enabled and before["loop_range"] is None:
        return {"success": False, "error": "there is no loop range to repeat: pass start and end"}
    if start is not None:
        RPR.GetSet_LoopTimeRange2(0, True, True, start, end, False)
    RPR.GetSetRepeat(SET_REPEAT if enabled else CLEAR_REPEAT)
    after = _ranges()
    return {"success": True, "repeat": bool(RPR.GetSetRepeat(QUERY_REPEAT)), **after,
            **_linked_warning(before, after, changed="loop_range", other="time_selection")}


def punch_mode() -> str | None:
    """The punch (record) mode now on, or None if REAPER reports none of them."""
    for mode, action in KNOWN_PUNCH_MODES.items():
        if RPR.GetToggleCommandState(action.command) == 1:
            return mode
    return None


@no_undo(idempotent=True)
def set_punch_mode(*, mode: str) -> dict:
    """How recording starts and stops, for every project: REAPER keeps this
    option on, in later projects and sessions too, until it is set back to normal.
    mode: "normal" (records from the moment you start until you stop) or
    "time_selection" (auto-punch: plays the whole time but only records inside the
    time selection; set it first with set_time_selection)."""
    if mode not in PUNCH_MODES:
        return {"success": False, "error": f"mode must be one of {list(PUNCH_MODES)}, not {mode!r}"}
    selection = time_range(loop=False)
    if mode == "time_selection" and selection is None:
        return {"success": False, "error": "there is no time selection to punch into: "
                                           "set one with set_time_selection first"}
    actions.run(PUNCH_MODES[mode])
    now = punch_mode()
    if now is not None and now != mode:
        return {"success": False, "error": f"REAPER's punch mode is still {now}"}
    return {"success": True, "mode": mode, "confirmed": now == mode,
            "time_selection": selection}


TOOLS = (
    set_metronome,
    set_time_selection,
    set_loop,
    set_punch_mode,
)
