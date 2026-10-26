"""Getting ready to record: the audio device, a track's input, arming,
monitoring and record mode, and a level check before the take.

None of these start recording. The audio device itself cannot be changed
through REAPER's API, so get_audio_setup explains where to change it.
"""

import time

from reaper_mcp.reaper import RPR, get_project, string_or_none
from reaper_mcp.track_tools import ALL_MIDI_INPUTS, record_state
from reaper_mcp.undo import edits, read_only
from reaper_mcp.units import linear_to_db

DEVICE_INFO = {"system": "MODE", "input_device": "IDENT_IN", "output_device": "IDENT_OUT"}
STEREO_INPUT = 1024
NO_INPUT = -1
MIDI_FLAG = 4096
MAX_MIDI_CHANNEL = 16
MONITOR_MODES = {"off": 0, "on": 1, "tape": 2}
RECORD_MODE_VALUES = {"input": 0, "output": 1, "none": 2, "midi_overdub": 7, "midi_replace": 8}
MIDI_RECORD_MODES = {7, 8}
# I_RECMODE values that record the track's input (the others record its output).
INPUT_RECORD_MODES = {0, *MIDI_RECORD_MODES}
RECORD_MODE_NONE = 2

# check_input_level: how often the meter is read, and the limits of a check.
POLL_SECONDS = 0.05
MIN_CHECK_SECONDS, MAX_CHECK_SECONDS = 0.5, 10.0
NO_SIGNAL_DB = -60.0  # peaks below this: nothing is coming in
QUIET_DB = -24.0      # peaks below this: too quiet to record well
LOW_DB = -18.0        # peaks below this: usable, but a little low
CLIP_DB = -1.0        # peaks at or above this: clipping, or about to
HOT_DB = -6.0         # peaks above this: good, but little headroom left

DEVICE_GUIDANCE = (
    "The audio device cannot be changed from here: REAPER only lets you choose it in "
    "Options > Preferences > Audio > Device. On Ubuntu with PipeWire, pick the audio system "
    "JACK (PipeWire provides it) or ALSA and choose the Scarlett as input and output device; "
    "ALSA takes the interface for REAPER alone, so other programs go silent while REAPER runs. "
    "Use the sample rate your other work uses (44100 or 48000) and a block size of 128 or 256 "
    "for low latency. Click Apply, then call get_audio_setup again to check."
)
MONITORING_WARNING = (
    "Monitoring plays the input back through REAPER. Use headphones: with a microphone and "
    "speakers it feeds back (a loud howl). If your interface's Direct Monitor is on as well, "
    "you hear yourself twice, the REAPER copy slightly late: turn one of them off."
)
NO_SIGNAL_ADVICE = [
    "Check the cable is plugged into the socket the track records from (record_input).",
    "Condenser microphones need 48V phantom power: press the 48V button on the interface. "
    "Dynamic microphones and instruments do not need it.",
    "For guitar or bass in the instrument socket, press the INST button.",
    "Turn the gain knob up: its ring should light green when you play or sing.",
    "Check get_audio_setup shows your interface as the input device, with audio running.",
]
LOW_ADVICE = [
    "Usable, but a little low: turn the gain knob up a touch, aiming for peaks around "
    "-12 dBFS.",
]
QUIET_ADVICE = [
    "Turn the interface's gain knob up until your loudest parts light its ring green "
    "(rarely amber), or move closer to the microphone.",
]
CLIP_ADVICE = [
    "Turn the interface's gain knob down until its ring never lights red: aim for peaks "
    "around -12 dBFS. Clipping on the way in cannot be fixed later.",
]


def _device_info(attribute: str) -> str | None:
    # (retval, attribute, desc, size) per REAPER's Python binding.
    ok, _, desc, _ = RPR.GetAudioDeviceInfo(attribute, "", 512)
    return str(desc) if ok else None


def _number(text: str | None) -> int | None:
    try:
        return int(float(text)) if text else None
    except ValueError:
        return None


def _channels(count: int, name_of) -> list[dict]:
    return [{"number": i + 1, "name": string_or_none(name_of, i) or f"Channel {i + 1}"}
            for i in range(count)]


def _ms(samples: int, rate: int | None) -> float | None:
    return round(samples / rate * 1000.0, 1) if rate else None


@read_only
def get_audio_setup() -> dict:
    """The audio interface REAPER is using: audio system (JACK, ALSA...), input
    and output device, sample rate, block size, sample format (bits), whether audio is
    running, the input and output channels (numbered from 1, with names) and the
    input/output latency in samples and milliseconds. The device cannot be
    changed through the API: how_to_change says where to change it in REAPER."""
    rate = _number(_device_info("SRATE"))
    in_latency, out_latency = RPR.GetInputOutputLatency(0, 0)
    inputs = _channels(RPR.GetNumAudioInputs(), RPR.GetInputChannelName)
    outputs = _channels(RPR.GetNumAudioOutputs(), RPR.GetOutputChannelName)
    running = bool(RPR.Audio_IsRunning())
    warnings = []
    if not running:
        warnings.append("Audio is not running: REAPER has no working audio device. "
                        + DEVICE_GUIDANCE)
    elif not inputs:
        warnings.append("REAPER sees no audio inputs, so nothing can be recorded. "
                        + DEVICE_GUIDANCE)
    return {
        "success": True,
        "audio_running": running,
        **{key: _device_info(attribute) for key, attribute in DEVICE_INFO.items()},
        "sample_rate": rate,
        "block_size": _number(_device_info("BSIZE")),
        "sample_format": _device_info("BPS"),  # bits per sample, as REAPER words it
        "input_count": len(inputs),
        "inputs": inputs,
        "output_count": len(outputs),
        "outputs": outputs,
        "latency": {
            "input_samples": in_latency, "output_samples": out_latency,
            "input_ms": _ms(in_latency, rate), "output_ms": _ms(out_latency, rate),
        },
        "warnings": warnings,
        "how_to_change": DEVICE_GUIDANCE,
    }


def _input_value(input: str, channel: int, midi_channel: int) -> int:
    """I_RECINPUT for an input preset; ValueError when the interface has no such input."""
    if input == "none":
        return NO_INPUT
    if input == "midi":
        if not 0 <= midi_channel <= MAX_MIDI_CHANNEL:
            raise ValueError(f"midi_channel must be 0 (all) to 16, not {midi_channel}")
        return ALL_MIDI_INPUTS | midi_channel
    if input not in ("mono", "stereo"):
        raise ValueError(f"input must be mono, stereo, midi or none, not {input!r}")
    count = RPR.GetNumAudioInputs()
    if count == 0:
        raise ValueError("REAPER sees no audio inputs (is the interface connected and chosen "
                         "in get_audio_setup?)")
    last = count if input == "mono" else count - 1
    if not 1 <= channel <= last:
        raise ValueError(f"the interface has {count} input(s), so a {input} input starts at "
                         f"1 to {last}, not {channel}")
    return channel - 1 + (STEREO_INPUT if input == "stereo" else 0)


@edits(idempotent=True)
def set_track_input(
    *, track_index: int, input: str, channel: int = 1, midi_channel: int = 0
) -> dict:
    """Choose what a track records from.
    input: "mono" (one input, numbered from 1: channel), "stereo" (inputs channel and
    channel+1), "midi" (every MIDI device; midi_channel 0 = all channels, or 1-16)
    or "none". On a Focusrite Scarlett Solo input 1 is the microphone (XLR) socket
    and input 2 the instrument/line socket, so a guitar is mono channel=2.
    Audio inputs are checked against the interface's input count."""
    try:
        value = _input_value(input, channel, midi_channel)
    except ValueError as e:
        return {"success": False, "error": f"{e}; nothing was changed"}
    track = get_project().tracks[track_index]
    track.set_info_value("I_RECINPUT", value)
    return {"success": True, "track_index": track_index, **record_state(track)}


def _armed_indices(tracks) -> list[int]:
    return [i for i, track in enumerate(tracks) if track.get_info_value("I_RECARM")]


def setup_warnings(track) -> list[str]:
    """What would keep an armed track from recording anything."""
    warnings = []
    value = int(track.get_info_value("I_RECINPUT"))
    mode = int(track.get_info_value("I_RECMODE"))
    if value < 0 and mode in INPUT_RECORD_MODES:
        warnings.append("The track has no record input: set one with set_track_input.")
    if mode == RECORD_MODE_NONE:
        warnings.append("The record mode is none: the track will not record "
                        "(set_track_record_mode input).")
    elif mode in MIDI_RECORD_MODES and value >= 0 and not value & MIDI_FLAG:
        warnings.append("MIDI record modes need a MIDI input (set_track_input midi).")
    return warnings


def _arming_warnings(track) -> list[str]:
    warnings = setup_warnings(track)
    if track.get_info_value("I_RECMON"):
        warnings.append("Monitoring is on for this track. " + MONITORING_WARNING)
    return warnings


@edits(idempotent=True)
def arm_track(*, track_index: int, armed: bool = True, exclusive: bool = False) -> dict:
    """Arm (or disarm) a track for recording. Arming does not start recording.
    exclusive: also disarm every other track, so only this one records."""
    tracks = get_project().tracks
    track = tracks[track_index]
    disarmed = []
    if exclusive and armed:
        disarmed = [i for i in _armed_indices(tracks) if i != track_index]
        for i in disarmed:
            tracks[i].set_info_value("I_RECARM", 0)
    track.set_info_value("I_RECARM", 1 if armed else 0)
    return {
        "success": True,
        "track_index": track_index,
        **record_state(track),
        "disarmed_tracks": disarmed,
        "armed_tracks": _armed_indices(tracks),
        "warnings": _arming_warnings(track) if armed else [],
    }


@edits(idempotent=True)
def set_monitoring(*, track_index: int, mode: str) -> dict:
    """Input monitoring: hear what the track's input receives, through REAPER.
    mode: "off", "on", or "tape" (heard while stopped and recording, not while
    playing back). Use headphones when it is on (see the warning in the reply)."""
    if mode not in MONITOR_MODES:
        return {"success": False, "error": f"mode must be one of {list(MONITOR_MODES)}, "
                                           f"not {mode!r}; nothing was changed"}
    track = get_project().tracks[track_index]
    track.set_info_value("I_RECMON", MONITOR_MODES[mode])
    result = {"success": True, "track_index": track_index, **record_state(track)}
    if mode != "off":
        result["warning"] = MONITORING_WARNING
        if not track.get_info_value("I_RECARM"):
            result["note"] = ("Monitoring only works while the track is armed: arm it with "
                              "arm_track.")
    return result


@edits(idempotent=True)
def set_track_record_mode(*, track_index: int, mode: str) -> dict:
    """What an armed track records (per track; the punch mode is set_punch_mode).
    Most recording needs no change: "input" is the default.
    mode: "input" (the input: voice, guitar, keyboard; the usual choice),
    "output" (the track's own stereo output, FX included), "midi_overdub" (add
    notes to existing MIDI items), "midi_replace" (replace the notes where you
    play) or "none" (armed for monitoring only, records nothing)."""
    if mode not in RECORD_MODE_VALUES:
        return {"success": False, "error": f"mode must be one of {list(RECORD_MODE_VALUES)}, "
                                           f"not {mode!r}; nothing was changed"}
    track = get_project().tracks[track_index]
    track.set_info_value("I_RECMODE", RECORD_MODE_VALUES[mode])
    return {"success": True, "track_index": track_index, **record_state(track),
            "warnings": setup_warnings(track)}


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.monotonic()


def _verdict(peak_db: float) -> tuple[str, list[str]]:
    if peak_db < NO_SIGNAL_DB:
        return "no signal", NO_SIGNAL_ADVICE
    if peak_db < QUIET_DB:
        return "too quiet", QUIET_ADVICE
    if peak_db < LOW_DB:
        return "good", LOW_ADVICE
    if peak_db >= CLIP_DB:
        return "clipping", CLIP_ADVICE
    if peak_db > HOT_DB:
        return "good", ["Good, if a little hot: leave some room for louder moments."]
    return "good", ["Good level for recording."]


def _level_track(tracks, track_index: int | None) -> int:
    """The track to measure: the given one, or the only armed one."""
    if track_index is None:
        armed = _armed_indices(tracks)
        if len(armed) != 1:
            raise ValueError("no track is armed: arm one with arm_track" if not armed else
                             f"tracks {armed} are armed: pass track_index")
        return armed[0]
    if not 0 <= track_index < len(tracks):
        raise ValueError(f"No track {track_index}: there are {len(tracks)}")
    if not tracks[track_index].get_info_value("I_RECARM"):
        raise ValueError(f"track {track_index} is not armed: arm it with arm_track first "
                         "(its meter only shows the input while armed)")
    return track_index


def _sample(pointer: str, seconds: float) -> tuple[float, float, float]:
    """(peak dBFS, average dBFS, seconds actually listened) of the track's meter.
    The average is the mean of the meter's (linear) readings, so one loud spike
    raises the peak but barely the average. The time, not the number of readings, is
    limited: each reading is a round trip to REAPER."""
    for channel in (0, 1):
        RPR.Track_GetPeakHoldDB(pointer, channel, True)  # clear the hold
    start = _clock()
    deadline = start + seconds
    readings = []
    while not readings or _clock() < deadline:
        _pause(max(0.0, min(POLL_SECONDS, deadline - _clock())))
        readings.append(max(RPR.Track_GetPeakInfo(pointer, 0), RPR.Track_GetPeakInfo(pointer, 1)))
    # Peak hold is in hundredths of a dB: 0.02 = +2 dB. It catches peaks between readings.
    hold_db = max(RPR.Track_GetPeakHoldDB(pointer, channel, False) for channel in (0, 1)) * 100
    peak_db = max(linear_to_db(max(readings)), hold_db)
    average_db = linear_to_db(sum(readings) / len(readings))
    return round(peak_db, 1), round(average_db, 1), round(_clock() - start, 1)


def _meter_warnings(track) -> list[str]:
    """The meter may show the level after the track's volume and FX, not what
    comes into the interface."""
    warnings = []
    if abs(track.get_info_value("D_VOL") - 1.0) > 1e-6:
        warnings.append("The track's volume is not 0 dB, which may change the meter: the "
                        "level shown may differ from what reaches the interface.")
    if track.n_fxs:
        warnings.append("The track has FX, which may change the meter: the level shown may "
                        "differ from what reaches the interface.")
    return warnings


@read_only
def check_input_level(*, track_index: int | None = None, seconds: float = 3.0) -> dict:
    """Listen to an armed track's input for a few seconds (0.5 to 10) and say whether
    the level is right to record: "no signal" (with things to check), "too quiet",
    "good" or "clipping". Play or sing as loud as you will in the take while it
    listens. Reports the peak level in dBFS (aim for about -12) and the average
    level: an average far below the peak means the peak was a short loud moment,
    such as one bump or plosive, and the rest was quieter. Never starts recording.
    track_index: the track to check; by default the only armed track."""
    if not MIN_CHECK_SECONDS <= seconds <= MAX_CHECK_SECONDS:
        return {"success": False, "error": f"seconds must be {MIN_CHECK_SECONDS} to "
                                           f"{MAX_CHECK_SECONDS}, not {seconds}"}
    if not RPR.Audio_IsRunning():
        return {"success": False, "error": "Audio is not running, so there is no input to "
                                           "measure. " + DEVICE_GUIDANCE}
    tracks = get_project().tracks
    try:
        index = _level_track(tracks, track_index)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    track = tracks[index]
    value = int(track.get_info_value("I_RECINPUT"))
    if value < 0 or value & MIDI_FLAG:
        return {"success": False, "error": f"track {index} records "
                f"{'nothing' if value < 0 else 'MIDI'}, which has no audio level: "
                "set an audio input with set_track_input"}
    peak_db, average_db, listened = _sample(track.id, seconds)
    verdict, advice = _verdict(peak_db)
    return {
        "success": True,
        "track_index": index,
        "track_name": track.name,
        "record_input": record_state(track)["record_input"],
        "seconds": listened,
        "peak_dbfs": peak_db,
        "average_dbfs": average_db,
        "verdict": verdict,
        "advice": advice,
        "warnings": _meter_warnings(track),
    }


TOOLS = (
    get_audio_setup,
    set_track_input,
    arm_track,
    set_monitoring,
    set_track_record_mode,
    check_input_level,
)
