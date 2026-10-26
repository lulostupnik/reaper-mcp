from reaper_mcp.guids import resolve_track_index, track_guid
from reaper_mcp.reaper import RPR, get_project, is_null
from reaper_mcp.undo import edits, read_only
from reaper_mcp.units import db_to_linear, linear_to_db

# "Track: Toggle track volume/pan envelope visible", run on the selected tracks.
SHOW_ENVELOPE = {"Volume": 40406, "Pan": 40407}


def shown_envelope(project, track, name: str):
    """The track's envelope called name, shown first if the track has none yet.

    REAPER only creates a track envelope when it is shown, and the show action
    works on the selected tracks, so the track is selected for that one action
    and the previous selection is restored afterwards.
    """
    envelope = RPR.GetTrackEnvelopeByName(track.id, name)
    if not is_null(envelope):
        return envelope
    tracks = project.tracks
    selected = [t.id for t in tracks if t.is_selected]
    RPR.SetOnlyTrackSelected(track.id)
    RPR.Main_OnCommand(SHOW_ENVELOPE[name], 0)
    for t in tracks:
        RPR.SetTrackSelected(t.id, t.id in selected)
    envelope = RPR.GetTrackEnvelopeByName(track.id, name)
    if is_null(envelope):
        raise RuntimeError(f"REAPER did not create the {name.lower()} envelope")
    return envelope


@edits()
def add_volume_automation(*, track_index: int, position: float, value_db: float) -> dict:
    """
    Add a volume automation point on a track, showing its volume envelope if needed.
    position: time in seconds. value_db: volume level in dB.
    """
    project = get_project()
    envelope = shown_envelope(project, project.tracks[track_index], "Volume")
    # Volume envelopes usually use fader scaling, where a raw linear 0.5 is near
    # silence; convert to the envelope's own scale.
    raw = RPR.ScaleToEnvelopeMode(RPR.GetEnvelopeScalingMode(envelope), db_to_linear(value_db))
    RPR.InsertEnvelopePoint(envelope, position, raw, 0, 0, False, True)
    RPR.Envelope_SortPoints(envelope)
    return {"success": True, "track_index": track_index, "position": position, "value_db": value_db}


@edits()
def add_pan_automation(*, track_index: int, position: float, pan: float) -> dict:
    """
    Add a pan automation point on a track, showing its pan envelope if needed.
    pan: -1.0 (full left) to 1.0 (full right).
    """
    project = get_project()
    envelope = shown_envelope(project, project.tracks[track_index], "Pan")
    # REAPER's pan envelope runs opposite to track pan: +1 is full left there.
    RPR.InsertEnvelopePoint(envelope, position, -pan, 0, 0, False, True)
    RPR.Envelope_SortPoints(envelope)
    return {"success": True, "track_index": track_index, "position": position, "pan": pan}


@edits()
def create_send(
    *,
    source_track_index: int, dest_track_index: int, volume_db: float = 0.0
) -> dict:
    """Create an aux send from one track to another."""
    project = get_project()
    src = project.tracks[source_track_index]
    dst = project.tracks[dest_track_index]
    # REAPER can answer with a valid index and create nothing (a send from a
    # track to itself returns 0), so the send count is the evidence.
    before = RPR.GetTrackNumSends(src.id, 0)
    send_idx = RPR.CreateTrackSend(src.id, dst.id)
    if RPR.GetTrackNumSends(src.id, 0) != before + 1:
        return {
            "success": False,
            "error": f"REAPER did not create a send from track {source_track_index} "
                     f"to track {dest_track_index}",
        }
    RPR.SetTrackSendInfo_Value(src.id, 0, send_idx, "D_VOL", db_to_linear(volume_db))
    return {
        "success": True,
        "source_track_index": source_track_index,
        "dest_track_index": dest_track_index,
        "send_index": send_idx,
        "volume_db": volume_db,
    }

# I_SENDMODE values; 2 is a deprecated post-FX mode REAPER still reads as pre-fader.
SEND_MODES = {"post_fader": 0, "pre_fx": 1, "pre_fader": 3}
SEND_MODE_NAMES = {0: "post_fader", 1: "pre_fx", 2: "pre_fader", 3: "pre_fader"}


def send_mode(track, send_index: int) -> str:
    mode = int(RPR.GetTrackSendInfo_Value(track.id, 0, send_index, "I_SENDMODE"))
    return SEND_MODE_NAMES.get(mode, str(mode))


def _pointer_from_value(value: float) -> str:
    """A track pointer read through a *_Value getter, as reapy writes track ids:
    REAPER's Python binding hands pointers back as numbers there."""
    return f"(MediaTrack*)0x{int(value):016X}"


def pointer_indices(tracks: list) -> dict[str, tuple[int, object]]:
    """Each track's pointer -> (index, track), built once per call: every
    project.tracks access is a round trip to REAPER."""
    return {t.id: (i, t) for i, t in enumerate(tracks)}


def track_sends(track, indices: dict[str, tuple[int, object]]) -> list[dict]:
    """track's sends to other tracks: volume in dB, pan, mute, mode (post_fader,
    pre_fader or pre_fx) and the destination.
    indices: pointer_indices of the project's tracks."""
    sends = []
    for i in range(RPR.GetTrackNumSends(track.id, 0)):
        dest = _pointer_from_value(RPR.GetTrackSendInfo_Value(track.id, 0, i, "P_DESTTRACK"))
        dest_index, dest_track = indices.get(dest, (None, None))
        sends.append({
            "send_index": i,
            "dest_track_index": dest_index,
            "dest_track_guid": track_guid(dest_track) if dest_track is not None else None,
            "dest_track_name": dest_track.name if dest_track is not None else None,
            "volume_db": round(linear_to_db(RPR.GetTrackSendInfo_Value(track.id, 0, i, "D_VOL")), 2),
            "pan": RPR.GetTrackSendInfo_Value(track.id, 0, i, "D_PAN"),
            "muted": bool(RPR.GetTrackSendInfo_Value(track.id, 0, i, "B_MUTE")),
            "mode": send_mode(track, i),
        })
    return sends


@read_only
def list_sends(*, track_index: int) -> dict:
    """List all sends from a track: the destination track (index, GUID, name),
    volume in dB, pan, mute and mode (post_fader, pre_fader or pre_fx)."""
    project = get_project()
    track = project.tracks[track_index]
    return {"success": True, "track_index": track_index, "guid": track_guid(track),
            "sends": track_sends(track, pointer_indices(list(project.tracks)))}


@edits(destructive=True)
def remove_send(*, source_track_index: int | str, send_index: int) -> dict:
    """Remove a send from a track by its index (see list_sends).
    source_track_index: the track's index, or its GUID from list_tracks (safer: a
    GUID still names the same track after tracks were added, deleted or moved)."""
    source_track_index, track = resolve_track_index(get_project(), source_track_index)
    if not RPR.RemoveTrackSend(track.id, 0, send_index):
        return {"success": False, "error": f"track {source_track_index} has no send {send_index}"}
    return {"success": True, "source_track_index": source_track_index, "send_index": send_index}

@edits(idempotent=True)
def set_send_volume(*, source_track_index: int, send_index: int, volume_db: float) -> dict:
    """Legacy: prefer edit_send (also pan, mute and pre/post-fader).
    Set the volume of a send in dB."""
    project = get_project()
    track = project.tracks[source_track_index]
    if not 0 <= send_index < RPR.GetTrackNumSends(track.id, 0) or not RPR.SetTrackSendInfo_Value(track.id, 0, send_index, "D_VOL", db_to_linear(volume_db)):
        return {"success": False, "error": f"track {source_track_index} has no send {send_index}"}
    return {
        "success": True,
        "source_track_index": source_track_index,
        "send_index": send_index,
        "volume_db": volume_db,
    }

def _close_open_folders(tracks: list) -> tuple | None:
    """Make the next track added at the end a top-level track.

    A track's I_FOLDERDEPTH is 1 when it starts a folder and negative when it
    closes some; while they do not add up to 0 the last folder is still open,
    and a track added after it lands inside. The last track then closes every
    open folder. Returns (track, its old depth) to undo that, or None.
    """
    if not tracks:
        return None
    depths = [int(t.get_info_value("I_FOLDERDEPTH")) for t in tracks]
    open_depth = sum(depths)
    if open_depth <= 0:
        return None
    last = tracks[-1]
    RPR.SetMediaTrackInfo_Value(last.id, "I_FOLDERDEPTH", depths[-1] - open_depth)
    return last, depths[-1]


def _unroute(routed: list, bus_track, closed: tuple | None) -> None:
    """Take back a half-made bus: the routed tracks send to the master again,
    folders closed for it reopen and the bus is deleted, which removes the
    sends to it too."""
    for track in routed:
        RPR.SetMediaTrackInfo_Value(track.id, "B_MAINSEND", 1)
    RPR.DeleteTrack(bus_track.id)
    if closed is not None:
        RPR.SetMediaTrackInfo_Value(closed[0].id, "I_FOLDERDEPTH", closed[1])


@edits()
def create_bus(*, name: str, track_indices: list[int]) -> dict:
    """
    Create a new bus track and route the given tracks to it via sends.
    track_indices: list of track indices to feed into the bus.
    The routed tracks stop sending straight to the master (or to their parent
    folder), so they are heard once, through the bus, instead of twice.
    The bus is added at the end, outside any folder.
    """
    if len(set(track_indices)) != len(track_indices):
        return {"success": False, "error": f"track_indices repeats a track: {track_indices}"}
    project = get_project()
    tracks = list(project.tracks)
    sources = [tracks[idx] for idx in track_indices]  # IndexError before any change
    closed = _close_open_folders(tracks)
    bus_idx = len(tracks)
    project.add_track(bus_idx, name)
    bus_track = project.tracks[bus_idx]
    sends = []
    for idx, src in zip(track_indices, sources, strict=True):
        send_idx = RPR.CreateTrackSend(src.id, bus_track.id)
        if send_idx < 0:
            _unroute(sources[: len(sends)], bus_track, closed)
            return {"success": False,
                    "error": f"REAPER did not create a send from track {idx} to the bus; "
                             "nothing was changed"}
        # Otherwise the track reaches the master directly and through the bus.
        RPR.SetMediaTrackInfo_Value(src.id, "B_MAINSEND", 0)
        sends.append({"track_index": idx, "send_index": send_idx})
    return {
        "success": True,
        "bus_index": bus_idx,
        "bus_guid": track_guid(bus_track),
        "bus_name": name,
        "sends": sends,
    }


TOOLS = (
    add_volume_automation,
    add_pan_automation,
    create_send,
    list_sends,
    remove_send,
    set_send_volume,
    create_bus,
)
