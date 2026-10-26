"""FX parameter values in the units a plug-in displays ("-3 dB", "300 Hz", "2 s").

A value given in display units is found by bisection over what the plug-in
displays: no knowledge of each plug-in's curve is needed. Each step sets the
parameter and reads the display back (the value it had is put back after),
because REAPER's formatter for a value not set (TrackFX_FormatParamValueNormalized)
does not always follow the plug-in's curve: for ReaEQ's band gains it said
"0.0 dB" where ReaEQ then showed -6.0 (REAPER 7.69). The formatter is still
used for the unit, which the display often leaves out ("20.0" for "20.0 ms").
This only works for parameters whose display is a number that rises or falls
steadily across the range, which covers gains, frequencies and times;
switches ("On"/"Off") are matched by text.
"""

import math
import re

from reaper_mcp.reaper import RPR, Pointer

FORMAT_BUFFER = 256
BISECTION_STEPS = 40
# Normalized values tried when a value is matched by its text (switches,
# modes): fine enough for a mode list of a hundred or so entries.
TEXT_SCAN_STEPS = 128
# A number, a decimal comma accepted, or "inf"; then an optional k/M multiplier
# glued to it ("1.2k") and a unit.
VALUE = re.compile(
    r"^\s*(?P<number>[-+]?(?:inf|\d+(?:[.,]\d*)?|[.,]\d+)(?:e[-+]?\d+)?)"
    r"\s*(?P<unit>[a-z%/°]*)", re.IGNORECASE)
# Each unit -> (dimension, factor to the dimension's base unit).
UNITS = {
    "": ("", 1.0), "db": ("db", 1.0), "hz": ("hz", 1.0), "khz": ("hz", 1000.0),
    "s": ("s", 1.0), "sec": ("s", 1.0), "secs": ("s", 1.0), "seconds": ("s", 1.0),
    "ms": ("s", 0.001), "%": ("%", 1.0), "k": ("", 1000.0),
    "st": ("st", 1.0), "semitones": ("st", 1.0), "db/sec": ("db/s", 1.0), "db/s": ("db/s", 1.0),
}


def parse_value(text: str) -> tuple[float, str] | None:
    """(number in the base unit, dimension) for a text such as "-3 dB",
    "1.2 kHz", "20ms", "-inf", "4.0:1"; None when it does not start with a number.
    A unit this module does not know keeps its own name as the dimension."""
    match = VALUE.match(text.replace("−", "-"))
    if match is None:
        return None
    number = float(match["number"].replace(",", "."))
    unit = match["unit"].lower()
    dimension, factor = UNITS.get(unit, (unit, 1.0))
    return number * factor, dimension


# "1.000" or "10,000": thousands written with a separator, or a decimal with
# three digits? A target such as "1.000 Hz" is refused rather than guessed.
THOUSANDS_LOOKALIKE = re.compile(r"^\s*[-+]?[1-9]\d{0,2}(?:[.,]\d{3})+(?![\d.,])")


def ambiguous_number(text: str) -> bool:
    """Whether text starts with a number that may use a thousands separator
    ("1.000 Hz", "10,000"), which parse_value would read as a decimal."""
    return THOUSANDS_LOOKALIKE.match(text) is not None


def format_normalized(track: Pointer, fx: int, param: int, *, value: float) -> str:
    """What the plug-in would display for value, without setting it."""
    return RPR.TrackFX_FormatParamValueNormalized(track, fx, param, value, "", FORMAT_BUFFER)[5]


def displayed(track: Pointer, fx: int, param: int) -> str:
    """What the plug-in displays for the parameter now."""
    return RPR.TrackFX_GetFormattedParamValue(track, fx, param, "", FORMAT_BUFFER)[4]


def shown_text(track: Pointer, fx: int, param: int) -> str:
    """What the plug-in displays now, with the formatter's unit added when the
    display has none ("20.0" -> "20.0 ms")."""
    text = displayed(track, fx, param)
    match = VALUE.match(text.replace("−", "-"))
    if match is None or match["unit"]:
        return text
    value = RPR.TrackFX_GetParamNormalized(track, fx, param)
    unit = VALUE.match(format_normalized(track, fx, param, value=value).replace("−", "-"))
    return f"{text.strip()} {unit['unit']}" if unit and unit["unit"] else text


def _shown(track: Pointer, fx: int, param: int, *, value: float) -> float:
    """The number the plug-in displays (base unit) once the parameter is value."""
    RPR.TrackFX_SetParamNormalized(track, fx, param, value)
    parsed = parse_value(shown_text(track, fx, param))
    if parsed is None:
        raise ValueError("the parameter does not display a number")
    return parsed[0]


def check_dimension(track: Pointer, fx: int, param: int, *, dimension: str) -> None:
    """Refuse a target in another unit than the one the parameter displays
    (a target without a unit, or a display without one, is taken as is)."""
    shown = parse_value(format_normalized(track, fx, param, value=0.5))
    if shown is None:
        raise ValueError("the parameter does not display a number")
    if dimension and shown[1] and shown[1] != dimension:
        raise ValueError(f"the parameter shows values like "
                         f"'{format_normalized(track, fx, param, value=0.5)}', not in {dimension}")


def bisect_to(track: Pointer, fx: int, param: int, *, target: float) -> float:
    """The normalized value whose display first reaches target (in the base
    unit), rising or falling; a target past the range lands on its end. The
    parameter is left as it was."""
    original = RPR.TrackFX_GetParamNormalized(track, fx, param)
    try:
        return _bisect(track, fx, param, target=target)
    finally:
        RPR.TrackFX_SetParamNormalized(track, fx, param, original)


def _bisect(track: Pointer, fx: int, param: int, *, target: float) -> float:
    rising = _shown(track, fx, param, value=0.25) < _shown(track, fx, param, value=0.75)

    def short(value: float) -> bool:
        shown = _shown(track, fx, param, value=value)
        return shown < target if rising else shown > target

    # The ends first: bisection alone never lands exactly on 0 ("-inf dB").
    if not short(0.0):
        return 0.0
    if short(1.0):
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(BISECTION_STEPS):
        middle = (low + high) / 2
        low, high = (middle, high) if short(middle) else (low, middle)
    return high


def set_to_display(track: Pointer, fx: int, param: int, *, target: float,
                   dimension: str = "") -> str:
    """Set the parameter so the plug-in displays target (base unit of
    dimension); returns what it then displays."""
    check_dimension(track, fx, param, dimension=dimension)
    RPR.TrackFX_SetParamNormalized(track, fx, param, bisect_to(track, fx, param, target=target))
    return shown_text(track, fx, param)


def match_text(track: Pointer, fx: int, param: int, *, text: str) -> tuple[float | None, list[str]]:
    """(a normalized value whose display is text, case-insensitive, or None;
    the distinct texts seen while looking, in order)."""
    wanted = text.strip().casefold()
    seen: list[str] = []
    for step in range(TEXT_SCAN_STEPS + 1):
        value = step / TEXT_SCAN_STEPS
        shown = format_normalized(track, fx, param, value=value).strip()
        if shown.casefold() == wanted:
            return value, seen
        if not seen or seen[-1] != shown:
            seen.append(shown)
    return None, seen


def display_range(track: Pointer, fx: int, param: int) -> tuple[str, str]:
    """What the parameter displays at its two ends."""
    return (format_normalized(track, fx, param, value=0.0),
            format_normalized(track, fx, param, value=1.0))


def reached(shown_text: str, target: float) -> bool:
    """Whether a displayed text is target, to the display's own precision."""
    parsed = parse_value(shown_text)
    if parsed is None:
        return False
    shown = parsed[0]
    if math.isinf(target) or math.isinf(shown):
        return shown == target
    # One step of the last decimal shown, in the shown unit's own scale
    # ("50.0 ms" is good to 0.1 ms, "1.20 kHz" to 10 Hz).
    match = VALUE.match(shown_text.replace("−", "-"))
    digits = re.search(r"[.,](\d+)", match["number"])
    step = 10.0 ** -len(digits[1]) if digits else 1.0
    factor = UNITS.get(match["unit"].lower(), ("", 1.0))[1]
    return abs(shown - target) <= step * factor
