"""Recording a take: start_recording checks everything a beginner can trip over
before it starts, stop_recording keeps the take without a dialog and reports
what was recorded; plus the recording file format and folder.
"""

import base64
import binascii
import os
import time
from pathlib import PurePath

import numpy as np

from reaper_mcp import actions
from reaper_mcp.actions import STOP_SAVE_RECORDED, TRANSPORT_RECORD, TRANSPORT_STOP
from reaper_mcp.guids import item_guid
from reaper_mcp.project_tools import project_file, save_project
from reaper_mcp.reaper import RPR, get_project, is_null
from reaper_mcp.record_setup_tools import DEVICE_GUIDANCE, MONITORING_WARNING, setup_warnings
from reaper_mcp.render_tools import _format_config, get_project_string, set_project_string
from reaper_mcp.track_tools import record_state
from reaper_mcp.transport_tools import QUERY_REPEAT, count_in_state, punch_mode
from reaper_mcp.undo import no_undo, undo_block
from reaper_mcp.units import linear_to_db

PLAYING, PAUSED, RECORDING = 1, 2, 4  # GetPlayState bits
# How long start_recording waits for REAPER to report that it is recording,
# and how much longer while it plays without recording yet (a count-in).
CONFIRM_POLLS, CONFIRM_SECONDS = 20, 0.05
COUNT_IN_WAIT_SECONDS = 12.0
RECORD_FORMATS = ("wav", "flac")
PEAK_BLOCK_FRAMES = 65536
CLIPPED_DB = -0.1


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def is_recording() -> bool:
    return bool(RPR.GetPlayState() & RECORDING)


def record_path() -> str:
    """The folder REAPER records into now."""
    # (project, buf, size) per REAPER's Python binding.
    return str(RPR.GetProjectPathEx(0, "", 4096)[1])


def _targets(tracks, track_index: int | None) -> list[int]:
    if track_index is not None:
        if not 0 <= track_index < len(tracks):
            raise ValueError(f"No track {track_index}: there are {len(tracks)} "
                             "(indices start at 0)")
        return [track_index]
    armed = [i for i, t in enumerate(tracks) if t.get_info_value("I_RECARM")]
    if not armed:
        raise ValueError("No track to record on: pass track_index, or arm one with arm_track")
    return armed


def _recording(tracks, targets: list[int], disarm_others: bool) -> list[int]:
    """Every track that will record: the targets, plus the tracks already armed
    unless they are disarmed first."""
    kept = [] if disarm_others else [i for i, t in enumerate(tracks)
                                     if i not in targets and t.get_info_value("I_RECARM")]
    return sorted(set(targets) | set(kept))


def _preflight(tracks, recording: list[int], position: float | None) -> str | None:
    """Why recording cannot start, or None."""
    if is_recording():
        return "REAPER is already recording: call stop_recording first"
    if not RPR.Audio_IsRunning():
        return "Audio is not running, so nothing can be recorded. " + DEVICE_GUIDANCE
    if position is not None and position < 0:
        return f"position must be 0 seconds or later, not {position}"
    for index in recording:
        problems = setup_warnings(tracks[index])
        if problems:
            return f"Track {index} ({tracks[index].name}) cannot record: " + " ".join(problems)
    return None


def _ensure_saved(save_first: bool) -> str | None:
    """None once the project has a file; otherwise why recording was refused."""
    if project_file():
        return None
    if not save_first:
        return ("The project has never been saved, so REAPER would put the recording in a "
                "general folder instead of next to the project. Save it first with "
                "save_project, or call start_recording again with save_first=true.")
    saved = save_project()
    return None if saved["success"] else f"Could not save the project: {saved['error']}"


def _arm_only(tracks, targets: list[int], disarm_others: bool) -> list[int]:
    """Arm the targets (and disarm the rest) as one undo step; returns the
    tracks disarmed. No step is recorded when nothing changes."""
    others = [i for i, t in enumerate(tracks)
              if i not in targets and disarm_others and t.get_info_value("I_RECARM")]
    to_arm = [i for i in targets if not tracks[i].get_info_value("I_RECARM")]
    if to_arm or others:
        with undo_block("Arm tracks for recording"):
            for i in to_arm:
                tracks[i].set_info_value("I_RECARM", 1)
            for i in others:
                tracks[i].set_info_value("I_RECARM", 0)
    return others


def _stop_playback() -> bool:
    """Stop playback (not a recording) so Transport: Record starts at the cursor
    instead of punching in wherever playback is; True if it was playing."""
    if not RPR.GetPlayState() & (PLAYING | PAUSED):
        return False
    actions.run(TRANSPORT_STOP)
    return True


def _confirm_recording() -> bool:
    """Wait for REAPER to report recording. While it plays without recording
    yet (counting in), keep waiting up to COUNT_IN_WAIT_SECONDS."""
    for _ in range(CONFIRM_POLLS):
        if is_recording():
            return True
        _pause(CONFIRM_SECONDS)
    for _ in range(round(COUNT_IN_WAIT_SECONDS / CONFIRM_SECONDS)):
        state = RPR.GetPlayState()
        if state & RECORDING or not state & PLAYING:
            break
        _pause(CONFIRM_SECONDS)
    return is_recording()


def _not_started(disarmed: list[int]) -> dict:
    state = RPR.GetPlayState()
    if state & PLAYING:
        error = ("REAPER is playing but not recording yet: it may still be counting in. "
                 "Check get_context before trying again; stop with stop_recording if it "
                 "records, or stop_transport if it only plays.")
    else:
        error = "REAPER did not start recording. If a window opened in REAPER, close it and try again."
    return {"success": False, "error": error, "play_state": state,
            "disarmed_tracks": disarmed, "count_in": count_in_state()}


def _global_warnings() -> list[str]:
    """Project settings (saved with the project, so easily left on from an earlier
    session) that change what a recording does."""
    warnings = []
    mode = punch_mode()
    if mode not in ("normal", None):
        warnings.append(f"REAPER's punch mode is {mode}: it only records inside the "
                        f"{'time selection' if mode == 'time_selection' else 'selected items'}. "
                        "set_punch_mode normal records everything.")
    if RPR.GetSetRepeat(QUERY_REPEAT):
        warnings.append("Repeat is on: REAPER loops and keeps recording takes until you stop. "
                        "set_loop enabled=false turns it off.")
    return warnings


def _track_warnings(tracks, recording: list[int]) -> list[str]:
    monitored = [i for i in recording if tracks[i].get_info_value("I_RECMON")]
    if not monitored:
        return []
    return [f"Monitoring is on for track(s) {monitored}. " + MONITORING_WARNING]


@no_undo()
def start_recording(
    *,
    track_index: int | None = None,
    disarm_others: bool = True,
    position: float | None = None,
    save_first: bool = False,
) -> dict:
    """Start recording, after checking it can work: not already recording, audio
    running, the project saved (so takes are stored next to it), and each track
    that will record has an input and a record mode that records. Playback is
    stopped first, so the take starts at position.
    track_index: the track to record on (armed here); by default the tracks
    already armed. disarm_others: disarm every other track, so nothing else is
    recorded by accident. position: start from here (seconds) instead of the edit
    cursor. save_first: save a never-saved project instead of refusing.
    Warns about monitoring, auto-punch and repeat. Stop with stop_recording.
    Count-in (a bar of click before the take) cannot be switched from here: it is
    REAPER's own setting (set_metronome reports it and says where to change it)."""
    tracks = get_project().tracks
    try:
        targets = _targets(tracks, track_index)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    recording = _recording(tracks, targets, disarm_others)
    error = _preflight(tracks, recording, position) or _ensure_saved(save_first)
    if error:
        return {"success": False, "error": error}
    actions.check(TRANSPORT_RECORD)  # before anything changes
    stopped = _stop_playback()
    disarmed = _arm_only(tracks, targets, disarm_others)
    if position is not None:
        get_project().cursor_position = position
    actions.run(TRANSPORT_RECORD)
    if not _confirm_recording():
        return _not_started(disarmed)
    result = {
        "success": True,
        "recording_tracks": [{"index": i, "name": tracks[i].name, **record_state(tracks[i])}
                             for i in recording],
        "disarmed_tracks": disarmed,
        "position": RPR.GetCursorPosition(),
        "count_in": count_in_state(),
        "record_path": record_path(),
        "message": "Recording. Call stop_recording to stop and keep the take.",
    }
    if stopped:
        result["stopped_playback"] = True
    warnings = _track_warnings(tracks, recording) + _global_warnings()
    if warnings:
        result["warnings"] = warnings
    return result


def _source_file(item) -> str | None:
    take = RPR.GetActiveTake(item.id)
    if is_null(take):
        return None
    source = RPR.GetMediaItemTake_Source(take)
    # (source, filename, size) per REAPER's Python binding.
    name = str(RPR.GetMediaSourceFileName(source, "", 4096)[1]) if not is_null(source) else ""
    if not name:
        return None
    return name if os.path.isabs(name) else os.path.join(record_path(), name)


def file_peak_db(path: str | None) -> float | None:
    """The highest sample level of an audio file in dBFS, read in blocks; None if
    it cannot be read."""
    import soundfile as sf

    if not path:
        return None
    try:
        peak = 0.0
        for block in sf.blocks(path, blocksize=PEAK_BLOCK_FRAMES, always_2d=True):
            if block.size:
                peak = max(peak, float(np.max(np.abs(block))))
    except (OSError, RuntimeError, sf.LibsndfileError):
        return None
    return round(linear_to_db(peak), 1)


def _take(track, item_index: int, item, *, track_index: int) -> dict:
    path = _source_file(item)
    peak = file_peak_db(path)
    return {
        "track_index": track_index,
        "track_name": track.name,
        "item_index": item_index,
        "item_guid": item_guid(item),
        "position": item.position,
        "length": item.length,
        "file": path,
        "peak_dbfs": peak,
        "clipped": peak is not None and peak >= CLIPPED_DB,
    }


def _item_states() -> dict[str, tuple[int, str | None]]:
    """Each item's GUID -> (take count, active take's file)."""
    return {item_guid(item): (item.n_takes, _source_file(item))
            for track in get_project().tracks for item in track.items}


def _is_take(item, before: dict[str, tuple[int, str | None]], old_files: set[str]) -> bool:
    """Whether the recording made this item or added a take to it: it plays a
    file that did not exist before. Recording over an item ("add takes") splits
    it, and the parts outside the recording keep playing the old file with an
    empty take added; an item playing a file from before (also the rest of an
    item auto-punch cut) is not a take. MIDI takes have no file: an existing
    MIDI item counts when it gained a take."""
    file = _source_file(item)
    guid = item_guid(item)
    if guid in before:
        takes, old_file = before[guid]
        if file is None and old_file is None:
            return item.n_takes > takes
        return file != old_file and file not in old_files
    return file not in old_files


def _new_takes(before: dict[str, tuple[int, str | None]]) -> list[dict]:
    """The items the recording made or added a take to, as takes."""
    old_files = {path for _, path in before.values() if path}
    return [_take(track, i, item, track_index=index)
            for index, track in enumerate(get_project().tracks)
            for i, item in enumerate(track.items) if _is_take(item, before, old_files)]


@no_undo()
def stop_recording() -> dict:
    """Stop recording and keep everything recorded (no dialog asks what to do with
    the files). Reports each new take: track, item, position and length (seconds),
    file, and its peak level in dBFS with clipped=true where it hit 0 dBFS.
    To throw a take away afterwards, call undo with include_manual_steps=true."""
    if not is_recording():
        return {"success": False, "error": "REAPER is not recording (stop_transport stops "
                                           "playback)"}
    before = _item_states()
    actions.run(STOP_SAVE_RECORDED)
    takes = _new_takes(before)
    result = {"success": True, "takes": takes, "record_path": record_path()}
    if not takes:
        result["warning"] = ("No new takes: check the armed track's input and record mode, "
                             "and that you recorded for more than a moment.")
    elif any(take["clipped"] for take in takes):
        result["warning"] = ("A take clipped: turn the interface's gain knob down and "
                             "record it again (check_input_level helps).")
    return result


def _sink(config: str) -> tuple[bytes, int | None]:
    """(sink type, WAV bit depth) of a base64 sink config; REAPER fills in its
    own defaults, so only these are compared."""
    try:
        raw = base64.b64decode(config, validate=True)
    except (binascii.Error, ValueError):
        raw = config.encode()
    return raw[:4], (raw[4] if raw[:4] == b"evaw" and len(raw) > 4 else None)


# REAPER keeps the recording format and folder out of its undo history (seen
# on 7.69: an undo block changing only them records nothing, and undo leaves
# them as they are), so these two tools are not undo steps; their replies
# name the previous setting, to set it back with the same tool.
@no_undo(idempotent=True)
def set_recording_format(*, format: str = "wav", bit_depth: int = 24) -> dict:
    """The file format new recordings are written in. REAPER's default (WAV,
    24-bit) is already right for most recording; change it only if asked.
    format: "wav" or "flac" (lossless and smaller; REAPER's default FLAC settings).
    bit_depth (WAV only): 24 (recommended), 16, or 32 (32-bit float).
    Not undoable (REAPER keeps it out of the undo history): to go back, call
    this again with the previous format the reply names."""
    if format.lower() not in RECORD_FORMATS:
        return {"success": False, "error": f"format must be one of {RECORD_FORMATS}, "
                                           f"not {format!r}"}
    try:
        config = _format_config(format, bit_depth)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    previous = _describe_format(get_project_string("RECORD_FORMAT"))
    set_project_string("RECORD_FORMAT", config)
    if _sink(get_project_string("RECORD_FORMAT")) != _sink(config):
        return {"success": False, "error": "REAPER did not take the recording format"}
    return {"success": True, "format": format.lower(),
            "bit_depth": bit_depth if format.lower() == "wav" else None,
            "previous": previous}


def _describe_format(config: str | None) -> dict | None:
    """{"format", "bit_depth"} of a RECORD_FORMAT config, or None if it is
    neither WAV nor FLAC (or unset)."""
    sink, bits = _sink(config or "")
    if sink == b"evaw":
        return {"format": "wav", "bit_depth": bits}
    if sink == b"calf":
        return {"format": "flac", "bit_depth": None}
    return None


def _subfolder_error(subfolder: str) -> str | None:
    path = PurePath(subfolder)
    if path.is_absolute() or subfolder.startswith("~") or ".." in path.parts:
        return (f"subfolder must be a folder inside the project's folder (e.g. \"Audio\"), "
                f"not {subfolder!r}: recordings outside it are lost when the project is "
                "moved or backed up")
    return None


@no_undo(idempotent=True)
def set_record_path(*, subfolder: str) -> dict:
    """The subfolder of the project's folder new recordings go into, e.g. "Audio";
    "" records next to the project file (REAPER's default, usually fine). Only
    folders inside the project's folder are accepted, so the takes move with it.
    Not undoable (REAPER keeps it out of the undo history): to go back, call
    this again with the previous setting the reply names."""
    error = _subfolder_error(subfolder.strip())
    if error:
        return {"success": False, "error": error}
    setting = subfolder.strip().rstrip("/")
    previous = get_project_string("RECORD_PATH") or ""
    set_project_string("RECORD_PATH", setting)
    return {"success": True, "record_path_setting": get_project_string("RECORD_PATH"),
            "record_path": record_path(), "previous_record_path_setting": previous}


TOOLS = (
    start_recording,
    stop_recording,
    set_recording_format,
    set_record_path,
)
