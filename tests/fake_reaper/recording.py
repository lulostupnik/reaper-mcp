"""The fake REAPER's audio device, meters, actions by name, metronome and
record modes, and recording itself.

Modelled on REAPER's documentation and its Python binding (reaper_python.py):
- GetAudioDeviceInfo returns (retval, attribute, desc, size); false, "" when
  the device is closed. GetInputOutputLatency returns (input, output) samples.
  GetInputChannelName decodes a NULL result, which raises through reapy (see
  session._null_string_error). kbd_getTextFromCmd gives "" for an unknown
  command on REAPER 7.69 (seen live).
- Track_GetPeakInfo is linear (1.0 = 0 dB); Track_GetPeakHoldDB is in
  hundredths of a dB and holds the loudest reading since it was cleared. An
  armed track's meter shows its input (TrackState.input_levels varies it).
- Transport: Record arms nothing by itself: armed tracks with an input and a
  record mode other than none record. "Transport: Stop (save all recorded
  media)" adds one item per such track, a file in the recording folder, and
  REAPER's own undo point. Plain "Transport: Stop" while recording can open
  REAPER's "save/delete recorded files" prompt (a preference): the fake
  raises ModalDialog for it, so a tool can never depend on that.
- Transport: Record while playing punches in at the play position, not the
  edit cursor.
- Recording over an existing item (record_overlap): "new_item" adds an item
  on top; "add_take" adds the take to the existing item (loop/lane recording,
  "add takes to existing items": same GUID, one more take); "split" cuts the
  existing item around the take (auto-punch), the right-hand part a new item
  (new GUID) playing the old file; "takes" is REAPER 7.69's default, seen
  live: the existing item is split at the take's edges, the parts outside it
  keep the old file and gain an empty take, the part under it becomes a new
  item whose active take is the new file, and the take past the old item's
  end is a new item too.
- RECORD_FORMAT is stored as REAPER's whole sink config, so a short config
  reads back longer (see api._sink_config).
- The metronome flags live in the project config variable "projmetroen":
  &1 = metronome on, which "Options: Toggle metronome" toggles; &16 =
  count-in before recording (unverified, see LIVE_CHECKS.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import soundfile as sf

from tests.fake_reaper.objects import ItemState
from tests.fake_reaper.render import ModalDialog, _format
from tests.fake_reaper.session import _null_string_error

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper

ACTION_NAMES = {
    1007: "Transport: Play",
    1013: "Transport: Record",
    1016: "Transport: Stop",
    40364: "Options: Toggle metronome",
    40252: "Record: Set record mode to normal",
    40076: "Record: Set record mode to time selection auto-punch",
    40253: "Record: Set record mode to selected item auto-punch",
    40667: "Transport: Stop (save all recorded media)",
    40668: "Transport: Stop (DELETE all recorded media)",
    40859: "New project tab",
    41929: "New project tab, ignore default template",
    42230: "File: Render project, using the most recent render settings, auto-close render dialog",
    40363: "Options: Show metronome/pre-roll settings",
    40362: "Item: Glue items, ignoring time selection",
    40131: "Take: Crop to active take in items",
}
TOGGLE_METRONOME = 40364
PUNCH_COMMANDS = {40252: "normal", 40076: "time_selection", 40253: "selected_items"}
RECORD, STOP, STOP_SAVE, STOP_DELETE = 1013, 1016, 40667, 40668
MIDI_FLAG = 4096
RECORD_MODE_NONE = 2
RATE = 48000


class Recording:
    """Mixed into FakeReaper; start_recording_state() sets the state up."""

    def start_recording_state(self: FakeReaper) -> None:
        self.audio_running = True
        self.audio_inputs = ["Scarlett Solo Input 1", "Scarlett Solo Input 2"]
        self.audio_outputs = ["Scarlett Solo Output 1", "Scarlett Solo Output 2"]
        self.device_info = {"MODE": "JACK", "IDENT_IN": "Scarlett Solo USB",
                            "IDENT_OUT": "Scarlett Solo USB", "SRATE": "48000",
                            "BSIZE": "256", "BPS": "32"}
        self.latency = (288, 544)
        self.config = {"projmetroen": "6"}
        self.punch_mode = "normal"
        self.record_start = 0.0
        self.record_seconds = 3.0
        self.record_overlap = "new_item"
        self.peak_hold_clears = 0
        self.recorded_files: list[Path] = []
        self._before_recording: dict = {}

    def recording_command(self: FakeReaper, command: int) -> bool:
        """Perform a recording or option action; False if command is not one."""
        if command == TOGGLE_METRONOME:
            self.config["projmetroen"] = str(int(self.config["projmetroen"]) ^ 1)
        elif command in PUNCH_COMMANDS:
            self.punch_mode = PUNCH_COMMANDS[command]
        elif command == RECORD:
            self._before_recording = self.snapshot()
            playing = self.transport == "playing"
            self.record_start = self.play_position if playing else self.cursor
            self.transport = "recording"
        elif command == STOP and self.transport == "recording":
            raise ModalDialog("REAPER may ask what to do with the recorded files")
        elif command in (STOP_SAVE, STOP_DELETE):
            if self.transport == "recording" and command == STOP_SAVE:
                self._keep_recording()
            self.transport = "stopped"
        else:
            return False
        return True

    def _keep_recording(self: FakeReaper) -> None:
        for track in self.tracks:
            value = int(track.value("I_RECINPUT"))
            if not track.value("I_RECARM") or value < 0 or track.value("I_RECMODE") == RECORD_MODE_NONE:
                continue
            midi = bool(value & MIDI_FLAG)
            source = "" if midi else str(self._write_take(track))
            if not self._record_over(track, source):
                track.items.append(ItemState(self.new_pointer("MediaItem"), self.record_start,
                                             self.record_seconds, midi=midi, source_file=source))
            track.items.sort(key=lambda i: i.position)
        self.undo_steps.append(("Recorded media", self._before_recording))
        self.redo_steps.clear()
        self.dirty = True

    def _record_over(self: FakeReaper, track, source: str) -> bool:
        """Apply record_overlap to the items the take overlaps; True when the take
        went into an existing item instead of a new one."""
        start, end = self.record_start, self.record_start + self.record_seconds
        overlapped = [i for i in track.items if i.position < end and i.position + i.length > start]
        if not overlapped or self.record_overlap == "new_item":
            return False
        if self.record_overlap == "add_take":
            overlapped[0].extra_takes += 1
            overlapped[0].source_file = source
            return True
        if self.record_overlap == "takes":
            self._record_as_takes(track, overlapped, start, end, source)
            return True
        for item in overlapped:  # "split"
            item_end = item.position + item.length
            if item_end > end:
                track.items.append(ItemState(self.new_pointer("MediaItem"), end, item_end - end,
                                             midi=item.midi, source_file=item.source_file))
            item.length = max(0.0, start - item.position)
        track.items[:] = [i for i in track.items if i.length > 0]
        return False

    def _record_as_takes(self: FakeReaper, track, overlapped, start, end, source) -> None:
        def piece(position, length, file, extra_takes):
            item = ItemState(self.new_pointer("MediaItem"), position, length, midi=False,
                             source_file=file, extra_takes=extra_takes)
            track.items.append(item)

        covered = start
        for item in sorted(overlapped, key=lambda i: i.position):
            item_end = item.position + item.length
            if item.position > covered:
                piece(covered, item.position - covered, source, 1)
            if item_end > end:
                piece(end, item_end - end, item.source_file, item.extra_takes + 1)
            piece(max(start, item.position), min(end, item_end) - max(start, item.position),
                  source, item.extra_takes + 1)
            item.length = start - item.position
            item.extra_takes += 1
            covered = min(end, item_end)
        if covered < end:
            piece(covered, end - covered, source, 1)
        track.items[:] = [i for i in track.items if i.length > 0]

    def _write_take(self: FakeReaper, track) -> Path:
        extension, subtype = _format(str(self.project_info.get("RECORD_FORMAT", "")))
        folder = Path(self.record_folder())
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{track.name or 'Track'}-{len(self.recorded_files) + 1:03d}.{extension}"
        t = np.arange(int(RATE * self.record_seconds)) / RATE
        sf.write(path, track.input_level * np.sin(2 * np.pi * 220 * t), RATE,
                 subtype=subtype or "PCM_16")
        self.recorded_files.append(path)
        return path

    def record_folder(self: FakeReaper) -> str:
        """Where REAPER records: RECORD_PATH, relative to the project's folder;
        with no project file, REAPER's default recording folder."""
        setting = str(self.project_info.get("RECORD_PATH", ""))
        if setting and Path(setting).is_absolute():
            return setting
        base = Path(self.project_file).parent if self.project_file else self.tmp / "REAPER Media"
        return str(base / setting) if setting else str(base)

    # Actions and options

    def kbd_getTextFromCmd(self: FakeReaper, command: int, section: int) -> str:
        return ACTION_NAMES.get(command, "")

    def GetToggleCommandState(self: FakeReaper, command: int) -> int:
        if command == TOGGLE_METRONOME:
            return int(self.config["projmetroen"]) & 1
        if command in PUNCH_COMMANDS:
            return int(self.punch_mode == PUNCH_COMMANDS[command])
        return -1

    def get_config_var_string(self: FakeReaper, name: str, buf: str, size: int) -> list:
        if name not in self.config:
            return [False, name, "", size]
        return [True, name, self.config[name], size]

    # Audio device

    def Audio_IsRunning(self: FakeReaper) -> int:
        return int(self.audio_running)

    def GetAudioDeviceInfo(self: FakeReaper, attribute: str, desc: str, size: int) -> list:
        if not self.audio_running or attribute not in self.device_info:
            return [False, attribute, "", size]
        return [True, attribute, self.device_info[attribute], size]

    def GetNumAudioInputs(self: FakeReaper) -> int:
        return len(self.audio_inputs) if self.audio_running else 0

    def GetNumAudioOutputs(self: FakeReaper) -> int:
        return len(self.audio_outputs) if self.audio_running else 0

    def GetInputChannelName(self: FakeReaper, channel: int) -> str:
        if not 0 <= channel < self.GetNumAudioInputs():
            raise _null_string_error()
        return self.audio_inputs[channel]

    def GetOutputChannelName(self: FakeReaper, channel: int) -> str:
        if not 0 <= channel < self.GetNumAudioOutputs():
            raise _null_string_error()
        return self.audio_outputs[channel]

    def GetInputOutputLatency(self: FakeReaper, input: int, output: int) -> list:
        return list(self.latency) if self.audio_running else [0, 0]

    def GetProjectPathEx(self: FakeReaper, project: int, buf: str, size: int) -> list:
        return [project, self.record_folder(), size]

    # Meters

    def _meter(self: FakeReaper, track: str, reading: int | None = None) -> float:
        state = self.track(track)
        if not (self.audio_running and state.value("I_RECARM")):
            return 0.0
        if not state.input_levels:
            return state.input_level
        levels = state.input_levels
        return levels[min(state.meter_reads if reading is None else reading, len(levels) - 1)]

    def Track_GetPeakInfo(self: FakeReaper, track: str, channel: int) -> float:
        level = self._meter(track)
        if channel == 1:
            self.track(track).meter_reads += 1
        return level

    def Track_GetPeakHoldDB(self: FakeReaper, track: str, channel: int, clear: bool) -> float:
        """The loudest reading since the hold was last cleared."""
        state = self.track(track)
        if clear:
            self.peak_hold_clears += 1
            state.hold_from = state.meter_reads
        last = max(state.hold_from, state.meter_reads - 1)
        level = max(self._meter(track, i) for i in range(state.hold_from, last + 1))
        return max(-150.0, 20 * np.log10(level)) / 100 if level > 0 else -1.5


def track_template_text(names: list[str], *, with_items: bool) -> str:
    """What REAPER writes for a track template of tracks with these names."""
    chunks = []
    for name in names:
        item = "  <ITEM\n  >\n" if with_items else ""
        chunks.append(f'<TRACK\n  NAME "{name}"\n{item}>\n')
    return "".join(chunks)


def template_track_names(path: Path) -> list[str]:
    return re.findall(r'^\s*NAME "(.*)"$', path.read_text(), flags=re.MULTILINE)
