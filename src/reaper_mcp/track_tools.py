from reaper_mcp.guids import item_guid, resolve_track_index, track_guid
from reaper_mcp.reaper import RPR, get_project
from reaper_mcp.undo import edits, read_only
from reaper_mcp.units import db_to_linear, linear_to_db

# I_RECINPUT for MIDI: 4096 marks a MIDI input; bits 5-10 are the device
# (63 = all devices) and bits 0-4 the channel (0 = all channels).
MIDI_INPUT = 4096
ALL_MIDI_DEVICES = 63
ALL_MIDI_INPUTS = MIDI_INPUT | ALL_MIDI_DEVICES << 5
# Audio input numbers from 512 up are REAPER's loopback (ReaRoute) channels.
LOOPBACK_START = 512
MONITORING = {0: "off", 1: "on", 2: "tape (off while playing)"}
# I_RECMODE: what an armed track records.
RECORD_MODES = {
    0: "input", 1: "output (stereo)", 2: "none (monitoring only)",
    3: "output (stereo, latency compensated)", 4: "MIDI output", 5: "output (mono)",
    6: "output (mono, latency compensated)", 7: "MIDI overdub", 8: "MIDI replace",
}


@edits()
def create_track(*, name: str, track_type: str = "audio") -> dict:
    """
    Create a new track at the end of the project.
    track_type: audio, midi, instrument, folder.
    midi and instrument tracks record from every MIDI device on every channel.
    Instrument tracks also have input monitoring on: once the track is armed for
    recording, playing the keyboard sounds through the instrument plugin (add it
    with add_fx). A track that is not armed stays silent while you play.
    """
    project = get_project()
    idx = project.n_tracks
    project.add_track(idx, name)
    track = project.tracks[idx]

    if track_type == "midi":
        RPR.SetMediaTrackInfo_Value(track.id, "I_RECINPUT", ALL_MIDI_INPUTS)
    elif track_type == "instrument":
        RPR.SetMediaTrackInfo_Value(track.id, "I_RECINPUT", ALL_MIDI_INPUTS)
        RPR.SetMediaTrackInfo_Value(track.id, "I_RECMON", 1)
    elif track_type == "folder":
        RPR.SetMediaTrackInfo_Value(track.id, "I_FOLDERDEPTH", 1)

    return {
        "success": True,
        "track_index": idx,
        "guid": track_guid(track),
        "name": track.name,
        "type": track_type,
    }


def describe_input(value: int) -> str:
    """REAPER's I_RECINPUT in words, e.g. "mono input 1" or "all MIDI devices, all channels"."""
    if value < 0:
        return "none"
    if value & MIDI_INPUT:
        device, channel = (value >> 5) & 63, value & 31
        devices = {ALL_MIDI_DEVICES: "all MIDI devices", 62: "virtual MIDI keyboard"}
        channels = f"channel {channel}" if channel else "all channels"
        return f"{devices.get(device, f'MIDI device {device}')}, {channels}"
    start = (value & 1023) + 1
    if start > LOOPBACK_START:
        return f"loopback/ReaRoute input {start - LOOPBACK_START}"
    if value & 2048:
        return f"multichannel from input {start}"
    if value & 1024:
        return f"stereo input {start}/{start + 1}"
    return f"mono input {start}"


def record_state(track) -> dict:
    """Arming, input, monitoring and record mode: what decides what a recording
    on track captures."""
    return {
        "armed": bool(track.get_info_value("I_RECARM")),
        "record_input": describe_input(int(track.get_info_value("I_RECINPUT"))),
        "monitoring": MONITORING.get(int(track.get_info_value("I_RECMON")), "unknown"),
        "record_mode": RECORD_MODES.get(int(track.get_info_value("I_RECMODE")), "unknown"),
    }


@edits(destructive=True)
def delete_track(*, track_index: int | str) -> dict:
    """Delete a track.
    track_index: the track's index, or its GUID from list_tracks (safer: a GUID
    still names the same track after tracks were added, deleted or moved)."""
    track_index, track = resolve_track_index(get_project(), track_index)
    RPR.DeleteTrack(track.id)
    return {"success": True, "deleted_index": track_index}

@edits(idempotent=True)
def rename_track(*, track_index: int, name: str) -> dict:
    """Rename a track."""
    project = get_project()
    track = project.tracks[track_index]
    track.name = name
    return {"success": True, "track_index": track_index, "name": track.name}

# REAPER's I_SOLO: 0 = not soloed, 2 = soloed in place (REAPER's default solo).
SOLO_IN_PLACE = 2


@edits(idempotent=True)
def set_track_volume(*, track_index: int, volume_db: float) -> dict:
    """Set track volume in dB. Range: roughly -150 to +12 dB."""
    track = get_project().tracks[track_index]
    track.set_info_value("D_VOL", db_to_linear(volume_db))
    return {"success": True, "track_index": track_index, "volume_db": _volume_db(track)}


@edits(idempotent=True)
def set_track_pan(*, track_index: int, pan: float) -> dict:
    """Set track pan. -1.0 = full left, 0.0 = center, 1.0 = full right."""
    track = get_project().tracks[track_index]
    track.set_info_value("D_PAN", pan)
    return {"success": True, "track_index": track_index, "pan": track.get_info_value("D_PAN")}


@edits(idempotent=True)
def set_track_mute(*, track_index: int, muted: bool) -> dict:
    """Mute or unmute a track."""
    track = get_project().tracks[track_index]
    track.set_info_value("B_MUTE", 1 if muted else 0)
    return {"success": True, "track_index": track_index, "muted": track.is_muted}


@edits(idempotent=True)
def set_track_solo(*, track_index: int, soloed: bool) -> dict:
    """Solo or unsolo a track."""
    # Through I_SOLO: reapy's solo() and is_solo setter go through an action
    # that leaves solo unchanged (tests/contract).
    track = get_project().tracks[track_index]
    track.set_info_value("I_SOLO", SOLO_IN_PLACE if soloed else 0)
    return {"success": True, "track_index": track_index, "soloed": track.is_solo}


def _volume_db(track) -> float:
    return round(linear_to_db(track.get_info_value("D_VOL")), 2)


def mix_state(track) -> dict:
    return {
        "volume_db": _volume_db(track),
        "pan": track.get_info_value("D_PAN"),
        "muted": track.is_muted,
        "soloed": track.is_solo,
    }


def _item_summary(index: int, item) -> dict:
    # reapy's active_take is a truthy Take even for an empty item (a null take).
    return {
        "index": index,
        "guid": item_guid(item),
        "position": item.position,
        "length": item.length,
        "name": item.active_take.name if item.n_takes else "",
    }


@read_only
def get_track_info(*, track_index: int) -> dict:
    """Get detailed information about a track including FX, items and its
    recording setup (armed, record input, monitoring, record mode)."""
    track = get_project().tracks[track_index]
    fxs = track.fxs
    fx_list = [
        {"index": i, "name": fxs[i].name, "enabled": fxs[i].is_enabled}
        for i in range(track.n_fxs)
    ]
    items = [_item_summary(i, item) for i, item in enumerate(track.items)]
    return {
        "success": True,
        "track_index": track_index,
        "guid": track_guid(track),
        "name": track.name,
        **mix_state(track),
        **record_state(track),
        "fx_count": len(fx_list),
        "fx": fx_list,
        "item_count": len(items),
        "items": items,
    }


@read_only
def list_tracks() -> dict:
    """List all tracks in the current project with their basic parameters and
    recording setup (armed, record input, monitoring, record mode); compact:
    get_project_summary adds routing and FX names."""
    tracks = [
        {
            "index": i,
            "guid": track_guid(track),
            "name": track.name,
            **mix_state(track),
            **record_state(track),
            "fx_count": track.n_fxs,
            "item_count": track.n_items,
        }
        for i, track in enumerate(get_project().tracks)
    ]
    return {"success": True, "count": len(tracks), "tracks": tracks}


@edits(idempotent=True)
def set_track_color(*, track_index: int, r: int, g: int, b: int) -> dict:
    """Set track color using RGB values (0–255 each)."""
    project = get_project()
    track = project.tracks[track_index]
    color = RPR.ColorToNative(r, g, b) | 0x1000000
    RPR.SetMediaTrackInfo_Value(track.id, "I_CUSTOMCOLOR", color)
    return {"success": True, "track_index": track_index, "r": r, "g": g, "b": b}


TOOLS = (
    create_track,
    delete_track,
    rename_track,
    set_track_volume,
    set_track_pan,
    set_track_mute,
    set_track_solo,
    get_track_info,
    list_tracks,
    set_track_color,
)
