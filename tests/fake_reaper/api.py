"""REAPER's state, and the ReaScript functions the server calls on it."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path

from reaper_mcp.reaper import ReaScriptAPI
from tests.fake_reaper.envelopes import Envelopes
from tests.fake_reaper.ext_state import ExtState
from tests.fake_reaper.fx import FX
from tests.fake_reaper.items import Items
from tests.fake_reaper.markers import Markers
from tests.fake_reaper.objects import ItemState, Send, TrackState
from tests.fake_reaper.project import FakeProject
from tests.fake_reaper.recording import Recording, template_track_names, track_template_text
from tests.fake_reaper.render import render, render_targets
from tests.fake_reaper.session import Session
from tests.fake_reaper.transport import Transport

RENDER_PROJECT = 42230
STRING_PROJECT_INFO = {"RENDER_FILE", "RENDER_PATTERN", "RENDER_FORMAT", "PROJECT_TITLE",
                       "RECORD_PATH", "RECORD_FORMAT"}
NUMERIC_PROJECT_INFO = {
    "RENDER_SRATE", "RENDER_CHANNELS", "RENDER_BOUNDSFLAG", "RENDER_ADDTOPROJ",
    "RENDER_STARTPOS", "RENDER_ENDPOS", "RENDER_SETTINGS", "RENDER_NORMALIZE",
    "RENDER_TAILFLAG", "RENDER_TAILMS",
}
NEW_PROJECT_TAB = 41929  # New project tab, ignore default template
NEW_PROJECT_TAB_FROM_TEMPLATE = 40859  # New project tab (from the default template)
SAVE_AS_PROJECT_FILE = 8  # Main_SaveProjectEx: the file becomes the project's
# Main_SaveProjectEx: save the selected tracks as a track template, with their items.
SAVE_TRACK_TEMPLATE, TEMPLATE_WITH_MEDIA = 1, 2
TRANSPORT = {1007: "playing", 1016: "stopped"}
# Track: Toggle track volume / pan envelope visible (on the selected tracks).
SHOW_ENVELOPE = {40406: "Volume", 40407: "Pan"}
# The send settings SetTrackSendInfo_Value changes, and the Send field each sets.
SEND_FIELDS = {"D_VOL": "volume", "D_PAN": "pan", "B_MUTE": "muted", "I_SENDMODE": "mode"}


# What REAPER fills a short sink config up with (its defaults; the exact bytes
# are made up, only the fact that it stores a longer config is modelled).
SINK_DEFAULTS = {b"evaw": b"\x20\x00\x01\x00\x00\x00", b"calf": b"\x18\x00\x00\x00\x05\x00\x00\x00"}


def _sink_config(value: str) -> str:
    """REAPER stores the whole sink configuration, not the string it was given:
    a short config (e.g. FLAC's bare "calf") comes back with its defaults filled in."""
    try:
        raw = base64.b64decode(value, validate=True) if value else b""
    except ValueError:
        raw = value.encode()  # a bare 4-byte sink type, e.g. "evaw"
    defaults = SINK_DEFAULTS.get(raw[:4])
    if defaults is None:
        return value
    full = raw + defaults[len(raw) - 4:]
    return base64.b64encode(full).decode()


@dataclass
class FakeReaper(Items, Session, Recording, Transport, Markers, Envelopes, FX, ExtState):
    tmp: Path
    bpm: float = 120.0
    # bpm is in quarter notes, as REAPER displays it. The project-settings time
    # signature follows the last marker at 0, even once that marker is deleted.
    time_signature: tuple[int, int] = (4, 4)
    # [position, bpm, numerator, denominator] per tempo/time signature marker.
    tempo_markers: list[list[float]] = field(default_factory=list)
    cursor: float = 0.0
    time_selection: tuple[float, float] = (0.0, 0.0)
    project_name: str = "Test Project"
    project_path: str = ""
    transport: str = "stopped"
    tracks: list[TrackState] = field(default_factory=list)
    project_info: dict[str, float | str] = field(default_factory=dict)
    envelopes: dict[tuple[str, str], list[tuple[float, float]]] = field(default_factory=dict)
    opened: list[str] = field(default_factory=list)
    saves: list[bool] = field(default_factory=list)
    saved_paths: list[str] = field(default_factory=list)
    commands: list[int] = field(default_factory=list)
    renders: list[Path] = field(default_factory=list)
    # project_info as it was at each render.
    render_settings: list[dict] = field(default_factory=list)
    # Projects moved behind a new tab: (tracks, markers, regions, tempo markers).
    background_tabs: list[tuple] = field(default_factory=list)
    # Names of the soloed tracks at each render, in render order.
    render_solos: list[list[str]] = field(default_factory=list)
    silent: bool = False
    # (amplitude, frequency) of the left and right channels of every render.
    signal: list[tuple[float, float]] = field(default_factory=lambda: [(0.5, 440.0), (0.25, 660.0)])
    # Seconds of material in the project; 0 is an empty project.
    length: float = 4.0
    # Track names the user's default project template gives a new project tab.
    default_template_tracks: list[str] = field(default_factory=list)
    # Glue keeps the first selected item (its GUID) and gives it the glued take,
    # instead of making a new item; real REAPER's way is not confirmed yet.
    glue_reuses_first_item: bool = False
    # Seconds the glued item's length differs from the glued extent (REAPER may
    # round it to whole samples of the rendered file).
    glue_length_error: float = 0.0
    # Renders follow the mixer: each channel of signal is scaled by the summed
    # volume of the tracks heard (the soloed ones, if any; never muted ones).
    # Off by default, so a render does not depend on which tracks exist.
    mix_by_tracks: bool = False
    _pointer: int = 0

    def __post_init__(self) -> None:
        self.master = TrackState(self.new_pointer("MediaTrack"), "MASTER")
        self.project = FakeProject(self)
        self.start_session()
        self.start_markers()
        self.start_recording_state()
        self.start_transport()
        self.start_ext_state()

    def new_pointer(self, kind: str) -> str:
        self._pointer += 1
        return f"({kind}*)0x{self._pointer:016X}"

    def add_track(self, name: str = "") -> TrackState:
        self.project.add_track(len(self.tracks), name)
        return self.tracks[-1]

    def track(self, pointer: str) -> TrackState:
        for state in [*self.tracks, self.master]:
            if state.pointer == pointer:
                return state
        raise ValueError(f"no track {pointer}")

    def time_signature_at_start(self) -> tuple[int, int]:
        for position, _, numerator, denominator in self.tempo_markers:
            if position == 0.0:
                return int(numerator), int(denominator)
        return self.time_signature

    def show_envelope(self, track: TrackState, name: str) -> None:
        self.envelopes[(track.pointer, name)] = []

    # Actions, undo and UI

    def Main_OnCommand(self, command: int, flag: int) -> None:
        self.commands.append(command)
        if command == RENDER_PROJECT:
            self.render_settings.append(dict(self.project_info))
            self.renders.extend(render(self))
            self.render_solos.append([t.name for t in self.tracks if t.info["I_SOLO"]])
        elif command in (NEW_PROJECT_TAB, NEW_PROJECT_TAB_FROM_TEMPLATE):
            self._new_tab(from_template=command == NEW_PROJECT_TAB_FROM_TEMPLATE)
        elif self.recording_command(command) or self.item_command(command):
            pass
        elif command in TRANSPORT:
            self.transport = TRANSPORT[command]
        elif command in SHOW_ENVELOPE:
            for state in self.tracks:
                if state.selected:
                    self.envelopes.setdefault((state.pointer, SHOW_ENVELOPE[command]), [])

    def _new_tab(self, *, from_template: bool) -> None:
        """The current project moves to a background tab, untouched; the new tab
        has its own (empty) undo history, no file and no title."""
        self.background_tabs.append(
            (list(self.tracks), list(self.markers), list(self.regions), list(self.tempo_markers),
             self.project_name, self.project_file, list(self.undo_steps))
        )
        self.tracks, self.markers, self.regions, self.tempo_markers = [], [], [], []
        self.project_name, self.project_file, self.dirty = "", "", False
        self.undo_steps, self.redo_steps = [], []
        self.project_info.pop("PROJECT_TITLE", None)
        self.ext_state = {}
        if from_template:
            for name in self.default_template_tracks:
                self.add_track(name)

    def Main_openProject(self, path: str) -> None:
        if path.startswith("template:"):
            self._open_template(path.removeprefix("template:"))
            return
        if path.lower().endswith(".rpp"):
            if not path.startswith("noprompt:") and self.dirty:
                raise AssertionError("REAPER would open a modal 'save changes?' dialog")
            path = path.removeprefix("noprompt:")
        self.opened.append(path)
        if path.lower().endswith(".rtracktemplate"):
            for line in Path(path).read_text().splitlines():
                if line.startswith("<TRACK"):
                    self.add_track(f"from {Path(path).stem}")

    def _open_template(self, path: str) -> None:
        """A project file loaded as a template replaces the project's contents and
        leaves it untitled; like any open, it would ask to save unsaved changes."""
        if self.dirty:
            raise AssertionError("REAPER would open a modal 'save changes?' dialog")
        self.opened.append("template:" + path)
        self.tracks = []
        for name in template_track_names(Path(path)):
            self.add_track(name)
        self.project_file, self.project_name = "", ""

    def Main_SaveProjectEx(self, project: int, path: str, options: int) -> None:
        """Without option 8 REAPER writes a copy and the open project keeps its file.
        Option 1 writes the selected tracks as a track template instead."""
        if not os.access(Path(path).parent, os.W_OK):
            raise AssertionError("REAPER would block on its modal 'Error creating project "
                                 "file' dialog until someone clicks OK")
        self.saved_paths.append(path)
        if options & SAVE_TRACK_TEMPLATE:
            names = [t.name for t in self.tracks if t.selected]
            Path(path).write_text(track_template_text(
                names, with_items=bool(options & TEMPLATE_WITH_MEDIA)))
            return
        tracks = track_template_text([t.name for t in self.tracks], with_items=False)
        Path(path).write_text(f"<REAPER_PROJECT 0.1\n{tracks}>\n")
        if options & SAVE_AS_PROJECT_FILE:
            self.project_file, self.project_name, self.dirty = path, Path(path).name, False

    def TrackList_AdjustWindows(self, is_minor: bool) -> None:
        pass

    def UpdateArrange(self) -> None:
        pass

    def GetResourcePath(self) -> str:
        return str(self.tmp / "resources")

    # Project settings

    def GetSetProjectInfo(self, project: int, desc: str, value: float, is_set: bool) -> float:
        # REAPER ignores numeric keys it does not have, such as RENDER_FORMAT
        # (a string setting) and RENDER_FORMAT2.
        if is_set and desc in NUMERIC_PROJECT_INFO:
            self.project_info[desc] = value
        return float(self.project_info.get(desc, 0.0))

    def GetSetProjectInfo_String(self, project: int, desc: str, value: str, is_set: bool):
        if desc == "RENDER_TARGETS":  # read-only, derived from the other settings
            return (True, project, desc, render_targets(self), is_set)
        if desc not in STRING_PROJECT_INFO:
            return (False, project, desc, "", is_set)  # REAPER: unknown key, nothing set
        if is_set:
            self.project_info[desc] = _sink_config(value) if desc == "RECORD_FORMAT" else value
        return (True, project, desc, self.project_info.get(desc, ""), is_set)

    # Tempo and time signature (return shapes as REAPER 7.82 gives them)

    def Master_GetTempo(self) -> float:
        # REAPER recomputes its tempo map from the markers only when an undo
        # block ends (seen on 7.69), so inside one the markers' old tempo shows.
        markers = (self._before_block["tempo_markers"] if self.undo_depth
                   else self.tempo_markers)
        for position, bpm, _, _ in markers:
            if position == 0.0:
                return bpm
        return self.bpm

    def CountTempoTimeSigMarkers(self, project: int) -> int:
        return len(self.tempo_markers)

    def GetTempoTimeSigMarker(self, project, index, *_out) -> list:
        position, bpm, numerator, denominator = self.tempo_markers[index]
        return [True, project, index, position, 0, 0.0, bpm, numerator, denominator, False]

    def SetTempoTimeSigMarker(self, project, index, position, _measure, _beat, bpm, *rest) -> bool:
        numerator, denominator, _linear = rest
        marker = [position, bpm, numerator, denominator]
        if position == 0.0:
            self.time_signature = (int(numerator), int(denominator))
        if index == -1:
            self.tempo_markers.append(marker)
            self.tempo_markers.sort(key=lambda m: m[0])
        else:
            self.tempo_markers[index] = marker
        return True

    def DeleteTempoTimeSigMarker(self, project: int, index: int) -> bool:
        del self.tempo_markers[index]
        return True

    def TimeMap_GetTimeSigAtTime(self, project, time, *_out) -> list:
        numerator, denominator = self.time_signature_at_start()
        return [project, time, numerator, denominator, self.bpm]

    # Tracks

    def CountTracks(self, project: int) -> int:
        return len(self.tracks)

    def GetTrack(self, project: int, index: int) -> str:
        """REAPER returns a null pointer for an index past the last track."""
        if not 0 <= index < len(self.tracks):
            return "(MediaTrack*)0x0000000000000000"
        return self.tracks[index].pointer

    def GetTrackGUID(self, track: str) -> str:
        """REAPER's Python binding hands the GUID* back as a pointer string, an
        address, not the "{...}" text (reaper_python.py: rpr_unpackp('GUID*', r))."""
        return "(GUID*)0x" + self.track(track).pointer[-16:]

    def GetSetMediaTrackInfo_String(self, track: str, param: str, value: str, is_set: bool) -> list:
        state = self.track(track)
        if is_set and param == "P_NAME":
            state.name = value
            return [True, track, param, value, is_set]
        if is_set or param not in ("GUID", "P_NAME"):
            return [False, track, param, "", is_set]
        return [True, track, param, state.guid if param == "GUID" else state.name, is_set]

    def DeleteTrack(self, track: str) -> None:
        """Sends to the deleted track go with it."""
        self.tracks.remove(self.track(track))
        for state in self.tracks:
            state.sends = [send for send in state.sends if send.dest != track]

    def SetMediaTrackInfo_Value(self, track: str, param: str, value: float) -> bool:
        """C_LANEPLAYS:N on a fixed-lane track sets which lane plays; 1 plays it
        exclusively, so the other lanes stop playing."""
        state = self.track(track)
        if param.startswith("C_LANEPLAYS:"):
            lane = int(param.split(":")[1])
            for item in state.items:
                if item.value("I_FIXEDLANE") == lane:
                    item.info["C_LANEPLAYS"] = value
                elif value == 1:
                    item.info["C_LANEPLAYS"] = 0.0
            return True
        state.info[param] = value
        return True

    def ColorToNative(self, r: int, g: int, b: int) -> int:
        return r | (g << 8) | (b << 16)

    def SetOnlyTrackSelected(self, track: str) -> None:
        for state in self.tracks:
            state.selected = state.pointer == track

    def SetTrackSelected(self, track: str, selected: bool) -> None:
        self.track(track).selected = selected

    def ReorderSelectedTracks(self, before_index: int, make_prev_folder: int) -> bool:
        """Move the selected tracks, in order, to just before track before_index."""
        moving = [t for t in self.tracks if t.selected]
        anchor = next((t for t in self.tracks[before_index:] if not t.selected), None)
        staying = [t for t in self.tracks if not t.selected]
        at = staying.index(anchor) if anchor is not None else len(staying)
        self.tracks[:] = staying[:at] + moving + staying[at:]
        return True

    # Media and FX

    def InsertMedia(self, path: str, mode: int) -> int:
        """REAPER keeps each track's items in time order, so a new item can land
        anywhere in the list."""
        for state in self.tracks:
            if state.selected:
                state.items.append(ItemState(self.new_pointer("MediaItem"), self.cursor, 2.0, False))
                state.items.sort(key=lambda item: item.position)
        return 1

    # Sends

    def CreateTrackSend(self, source: str, dest: str) -> int:
        """Like REAPER 7.82: a send from a track to itself returns 0 and is not
        created; a null destination makes a hardware output."""
        state = self.track(source)
        if dest == source:
            return 0
        if dest.endswith("0x0000000000000000"):
            state.hardware_outputs += 1
            return state.hardware_outputs - 1
        state.sends.append(Send(dest))
        return len(state.sends) - 1

    def RemoveTrackSend(self, track: str, category: int, send_index: int) -> bool:
        """REAPER returns false for a send index that does not exist; it does not raise."""
        sends = self.track(track).sends
        if not 0 <= send_index < len(sends):
            return False
        del sends[send_index]
        return True

    def GetTrackNumSends(self, track: str, category: int) -> int:
        """category 0: sends, -1: receives, 1: hardware outputs."""
        if category < 0:
            return sum(s.dest == track for t in self.tracks for s in t.sends)
        if category > 0:
            return self.track(track).hardware_outputs
        return len(self.track(track).sends)

    def GetTrackSendInfo_Value(self, track: str, category: int, i: int, param: str) -> float:
        send = self.track(track).sends[i]
        # REAPER's Python binding returns P_DESTTRACK as the pointer's address, a number.
        destination = float(int(send.dest.split("0x")[1], 16))
        return {"D_VOL": send.volume, "D_PAN": send.pan, "B_MUTE": float(send.muted),
                "I_SENDMODE": float(send.mode), "P_DESTTRACK": destination}[param]

    def SetTrackSendInfo_Value(self, track, category, i, param, value) -> bool:
        sends = self.track(track).sends
        if not 0 <= i < len(sends):
            return False  # REAPER reports a bad send index by returning false
        send = sends[i]
        if param in SEND_FIELDS:
            setattr(send, SEND_FIELDS[param], type(getattr(send, SEND_FIELDS[param]))(value))
        return True


# ReaScript functions are capitalized, except a few REAPER names in lower case
# (kbd_getTextFromCmd, get_config_var_string...), taken from the declared surface.
RPR_FUNCTIONS = sorted(
    name for name in dir(FakeReaper)
    if (name[:1].isupper() or (name[:1].islower() and hasattr(ReaScriptAPI, name)))
    and callable(getattr(FakeReaper, name))
)
