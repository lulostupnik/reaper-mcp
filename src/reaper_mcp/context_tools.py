"""get_context and get_project_summary: what an assistant should look at first.

get_context is the moment: transport, cursor, selections, what was touched
last. get_project_summary is the project: every track with its routing,
recording setup and FX, plus markers, regions and tempo, in one call.
"""

from reaper_mcp.guids import pointer_guid, track_guid
from reaper_mcp.mixing_tools import pointer_indices, track_sends
from reaper_mcp.project_tools import (
    current_tempo,
    markers_and_regions,
    project_file,
    time_signature_text,
)
from reaper_mcp.reaper import RPR, Pointer, get_project, is_null
from reaper_mcp.track_tools import mix_state, record_state
from reaper_mcp.transport_tools import count_in_state, metronome_state, punch_mode, time_range
from reaper_mcp.undo import read_only

# GetPlayState bits.
PLAYING, PAUSED, RECORDING = 1, 2, 4
LOW_WORD = 0xFFFF
FX_INDEX_BITS = 0xFFFFFF
INPUT_FX_FLAG = 0x1000000  # GetLastTouchedFX: "next 8 bits are 01" = record input FX


def _play_state(bits: int) -> str:
    if bits & RECORDING:
        return "recording (paused)" if bits & PAUSED else "recording"
    if bits & PAUSED:
        return "paused"
    return "playing" if bits & PLAYING else "stopped"


def _track_ref(project, pointer: Pointer) -> dict:
    index = int(RPR.GetMediaTrackInfo_Value(pointer, "IP_TRACKNUMBER")) - 1
    return {"index": index, "guid": pointer_guid(pointer),
            "name": project.tracks[index].name}


def _selected_tracks(project) -> list[dict]:
    return [_track_ref(project, RPR.GetSelectedTrack(0, i))
            for i in range(RPR.CountSelectedTracks(0))]


def _selected_item(pointer: Pointer) -> dict:
    track = RPR.GetMediaItem_Track(pointer)
    return {
        "guid": RPR.GetSetMediaItemInfo_String(pointer, "GUID", "", False)[3],
        "track_index": int(RPR.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER")) - 1,
        "track_guid": pointer_guid(track),
        "item_index": int(RPR.GetMediaItemInfo_Value(pointer, "IP_ITEMNUMBER")),
        "position": RPR.GetMediaItemInfo_Value(pointer, "D_POSITION"),
        "length": RPR.GetMediaItemInfo_Value(pointer, "D_LENGTH"),
    }


def _selected_items() -> list[dict]:
    return [_selected_item(RPR.GetSelectedMediaItem(0, i))
            for i in range(RPR.CountSelectedMediaItems(0))]


def _last_touched_fx() -> dict | None:
    """The FX whose parameter was touched last, on a track or the master; item FX
    are reported without a name."""
    valid, track_number, fx_number, param = RPR.GetLastTouchedFX(0, 0, 0)
    if not valid:
        return None
    track_no, item_no = track_number & LOW_WORD, track_number >> 16
    if item_no:
        return {"on": "item", "track_index": track_no - 1, "item_index": item_no - 1,
                "take_index": fx_number >> 16, "fx_index": fx_number & LOW_WORD,
                "param_index": param}
    track = RPR.GetMasterTrack(0) if track_no == 0 else RPR.GetTrack(0, track_no - 1)
    if is_null(track):
        return None
    return {
        "on": "master" if track_no == 0 else "track",
        "track_index": track_no - 1 if track_no else None,
        "fx_index": fx_number & FX_INDEX_BITS,
        "input_fx": bool(fx_number & INPUT_FX_FLAG),
        "fx_name": RPR.TrackFX_GetFXName(track, fx_number, "", 256)[3],
        "param_index": param,
    }


@read_only
def get_context() -> dict:
    """What is happening in REAPER right now: play state, edit cursor and play
    position (seconds), time selection and loop range (null when empty), whether
    repeat is on, REAPER's punch mode, metronome and count-in (null if unknown),
    the selected tracks and items (with GUIDs), the FX touched last,
    whether the project has unsaved changes, and the project file ("" if never saved)."""
    project = get_project()
    return {
        "success": True,
        "play_state": _play_state(RPR.GetPlayState()),
        "edit_cursor": RPR.GetCursorPosition(),
        "play_position": RPR.GetPlayPosition(),
        "time_selection": time_range(loop=False),
        "loop_range": time_range(loop=True),
        "repeat": bool(RPR.GetSetRepeat(-1)),
        "punch_mode": punch_mode(),
        "metronome": metronome_state(),
        "count_in": count_in_state(),
        "selected_tracks": _selected_tracks(project),
        "selected_items": _selected_items(),
        "last_touched_fx": _last_touched_fx(),
        "unsaved_changes": bool(RPR.IsProjectDirty(0)),
        "project_file": project_file(),
    }


def _track_summary(index: int, track, indices: dict) -> dict:
    return {
        "index": index,
        "guid": track_guid(track),
        "name": track.name,
        **mix_state(track),
        **record_state(track),
        "fx": [fx.name for fx in track.fxs],
        "sends": track_sends(track, indices),
        "item_count": track.n_items,
    }


@read_only
def get_project_summary() -> dict:
    """The whole project in one call: name, file, tempo, time signature, length,
    the master (volume, FX), and every track with its GUID, name, volume (dB), pan,
    mute/solo, record arm, record input, input monitoring, FX names, sends and item
    count; plus markers and regions."""
    project = get_project()
    markers, regions = markers_and_regions()
    master = project.master_track
    tracks = list(project.tracks)
    indices = pointer_indices(tracks)
    return {
        "success": True,
        "name": project.name,
        "project_file": project_file(),
        "tempo": current_tempo(),
        "time_signature": time_signature_text(),
        "length": project.length,
        "master": {**mix_state(master), "fx": [fx.name for fx in master.fxs]},
        "tracks": [_track_summary(i, t, indices) for i, t in enumerate(tracks)],
        "markers": markers,
        "regions": regions,
    }


TOOLS = (
    get_context,
    get_project_summary,
)
