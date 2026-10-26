"""The fake REAPER's media items and takes: the item and take
functions, splitting, moving, deleting, item state chunks, item selection, and the glue and crop actions.

Modelled on the ReaScript reference and REAPER's Python binding:
- Pointer arguments that name nothing make REAPER crash, not fail; the fake
  raises ValueError for them (ValidatePtr2 is how a script checks first).
- SplitMediaItem returns the right-hand part (a new item, new GUIDs) or a
  null pointer when the position is not strictly inside the item. The left
  part keeps its fade-in, the right part its fade-out.
- A track keeps its items in time order (see InsertMedia).
- A state chunk is RPP text: item lines, then each take (the first starts at
  its NAME line, later ones at a "TAKE" line, "TAKE SEL" for the active one)
  with its GUID, an FXID line per take FX and <SOURCE>. REAPER's many other lines are stood in for by a
  FAKE_ITEM_INFO / FAKE_TAKE_INFO line holding the settings as JSON.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from tests.fake_reaper.item_state import NULL_TAKE, ItemState, TakeState, copy_item
from tests.fake_reaper.sources import Sources

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper

NULL_ITEM = "(MediaItem*)0x0000000000000000"
GLUE_ITEMS = 40362  # Item: Glue items, ignoring time selection
CROP_TO_ACTIVE_TAKE = 40131  # Take: Crop to active take in items
ITEM_ACTIONS = {GLUE_ITEMS: "Item: Glue items, ignoring time selection",
                CROP_TO_ACTIVE_TAKE: "Take: Crop to active take in items"}


class Items(Sources):
    """Mixed into FakeReaper."""

    # Lookups

    def _item(self: FakeReaper, pointer: str):
        for track in self.tracks:
            for item in track.items:
                if item.pointer == pointer:
                    return track, item
        raise ValueError(f"no item {pointer}: REAPER would crash on a dangling pointer")

    def _take(self: FakeReaper, pointer: str) -> tuple[ItemState, TakeState]:
        digits = pointer.split("0x")[-1].removeprefix("F").rjust(16, "0")
        for track in self.tracks:
            for item in track.items:
                for take in item.takes:
                    if take.digits == digits:
                        return item, take
        raise ValueError(f"no take {pointer}: REAPER would crash on a dangling pointer")

    def _sort(self: FakeReaper, track) -> None:
        track.items.sort(key=lambda item: item.position)

    # Items on tracks

    def CountTrackMediaItems(self: FakeReaper, track: str) -> int:
        return len(self.track(track).items)

    def GetTrackMediaItem(self: FakeReaper, track: str, index: int) -> str:
        items = self.track(track).items
        return items[index].pointer if 0 <= index < len(items) else NULL_ITEM

    def GetMediaItemInfo_Value(self: FakeReaper, item: str, param: str) -> float:
        track, state = self._item(item)
        values = {"IP_ITEMNUMBER": float(track.items.index(state)), "I_CURTAKE": float(state.active),
                  "D_POSITION": state.position, "D_LENGTH": state.length,
                  "B_UISEL": float(state.selected)}
        return values[param] if param in values else state.value(param)

    def SetMediaItemInfo_Value(self: FakeReaper, item: str, param: str, value: float) -> bool:
        track, state = self._item(item)
        if param == "D_POSITION":
            state.position = value
            self._sort(track)
        elif param == "D_LENGTH":
            state.length = value
        elif param == "I_CURTAKE":
            if not 0 <= int(value) < len(state.takes):
                return False
            state.active = int(value)
        elif param == "B_UISEL":
            state.selected = bool(value)
        else:
            state.info[param] = value
        return True

    def GetSetMediaItemInfo_String(self: FakeReaper, item, param, value, is_set) -> list:
        _, state = self._item(item)
        if param != "GUID" or is_set:
            return [False, item, param, "", is_set]
        return [True, item, param, state.guid, is_set]

    def SplitMediaItem(self: FakeReaper, item: str, position: float) -> str:
        track, state = self._item(item)
        if not state.position < position < state.end:
            return NULL_ITEM
        right = copy_item(state, self.new_pointer("MediaItem"))
        cut = position - state.position
        right.position, right.length = position, state.end - position
        for take in right.takes:
            take.info["D_STARTOFFS"] = take.value("D_STARTOFFS") + cut * take.info["D_PLAYRATE"]
        right.info["D_FADEINLEN"] = 0.0
        state.info["D_FADEOUTLEN"] = 0.0
        state.length = cut
        track.items.insert(track.items.index(state) + 1, right)
        return right.pointer

    def DeleteTrackMediaItem(self: FakeReaper, track: str, item: str) -> bool:
        owner, state = self._item(item)
        if owner.pointer != track:
            return False
        owner.items.remove(state)
        return True

    def MoveMediaItemToTrack(self: FakeReaper, item: str, track: str) -> bool:
        owner, state = self._item(item)
        destination = self.track(track)
        owner.items.remove(state)
        destination.items.append(state)
        self._sort(destination)
        return True

    def AddMediaItemToTrack(self: FakeReaper, track: str) -> str:
        """A new, empty item (no take) at 0."""
        state = ItemState(self.new_pointer("MediaItem"), 0.0, 0.0, False, has_take=False)
        destination = self.track(track)
        destination.items.insert(0, state)
        return state.pointer

    # Selection

    def SetMediaItemSelected(self: FakeReaper, item: str, selected: bool) -> None:
        self._item(item)[1].selected = selected

    def SelectAllMediaItems(self: FakeReaper, project: int, selected: bool) -> None:
        for track in self.tracks:
            for item in track.items:
                item.selected = selected

    def ValidatePtr2(self: FakeReaper, project: int, pointer: str, kind: str) -> bool:
        if kind != "MediaItem*":
            raise NotImplementedError(kind)
        return any(i.pointer == pointer for t in self.tracks for i in t.items)

    # Takes

    def CountTakes(self: FakeReaper, item: str) -> int:
        return len(self._item(item)[1].takes)

    def GetTake(self: FakeReaper, item: str, index: int) -> str:
        takes = self._item(item)[1].takes
        return takes[index].pointer if 0 <= index < len(takes) else NULL_TAKE

    def GetActiveTake(self: FakeReaper, item: str) -> str:
        state = self._item(item)[1]
        return state.take.pointer if state.takes else NULL_TAKE

    def SetActiveTake(self: FakeReaper, take: str) -> None:
        item, state = self._take(take)
        item.active = item.takes.index(state)

    def GetMediaItemTake_Item(self: FakeReaper, take: str) -> str:
        return self._take(take)[0].pointer

    def GetMediaItemTakeInfo_Value(self: FakeReaper, take: str, param: str) -> float:
        return self._take(take)[1].value(param)

    def SetMediaItemTakeInfo_Value(self: FakeReaper, take: str, param: str, value: float) -> bool:
        self._take(take)[1].info[param] = value
        return True

    def GetSetMediaItemTakeInfo_String(self: FakeReaper, take, param, value, is_set) -> list:
        _, state = self._take(take)
        if param == "P_NAME" and is_set:
            state.name = value
        if param not in ("P_NAME", "GUID") or (is_set and param == "GUID"):
            return [False, take, param, "", is_set]
        return [True, take, param, state.name if param == "P_NAME" else state.guid, is_set]

    def TakeFX_GetCount(self: FakeReaper, take: str) -> int:
        return self._take(take)[1].fx_count

    def GetTakeNumStretchMarkers(self: FakeReaper, take: str) -> int:
        return self._take(take)[1].stretch_markers

    # State chunks

    def GetItemStateChunk(self: FakeReaper, item, buf, size, undo) -> list:
        _, state = self._item(item)
        lines = ["<ITEM", f"POSITION {state.position}", f"LENGTH {state.length}",
                 f"IGUID {state.guid}", "FAKE_ITEM_INFO " + json.dumps(state.info)]
        for index, take in enumerate(state.takes):
            if index:
                lines.append("TAKE SEL" if index == state.active else "TAKE")
            lines += [f'NAME "{take.name}"', f"GUID {take.guid}",
                      *(f"FXID {take.fx_guid(i)}" for i in range(take.fx_count)),
                      "FAKE_TAKE_INFO " + json.dumps(take.info),
                      "<SOURCE MIDI" if state.midi else "<SOURCE WAVE",
                      f'FILE "{take.source_file}"', ">"]
        return [True, item, "\n".join([*lines, ">"]) + "\n", size, undo]

    def SetItemStateChunk(self: FakeReaper, item: str, chunk: str, undo: bool) -> bool:
        track, state = self._item(item)
        takes: list[TakeState] = []
        state.active = 0
        for line in chunk.splitlines():
            key, _, rest = line.strip().partition(" ")
            if key in ("POSITION", "LENGTH"):
                setattr(state, key.lower(), float(rest))
            elif key == "IGUID":
                state.guid = rest
            elif key == "FAKE_ITEM_INFO":
                state.info = json.loads(rest)
            elif key == "TAKE" or (key == "NAME" and not takes):
                takes.append(TakeState())
                if rest == "SEL":
                    state.active = len(takes) - 1
            if key == "NAME":
                takes[-1].name = rest.strip('"')
            elif key == "GUID" and takes:
                takes[-1].guid = rest
            elif key == "FXID":
                takes[-1].fx_guids.append(rest)
                takes[-1].fx_count = len(takes[-1].fx_guids)
            elif key == "FAKE_TAKE_INFO":
                takes[-1].info = json.loads(rest)
            elif key == "FILE":
                takes[-1].source_file = rest.strip('"')
            elif key == "<SOURCE":
                state.midi = rest == "MIDI"
        state.takes = takes
        self._sort(track)
        return True

    # Glue and crop

    def item_command(self: FakeReaper, command: int) -> bool:
        if command == GLUE_ITEMS:
            for track in self.tracks:
                self._glue(track)
        elif command == CROP_TO_ACTIVE_TAKE:
            for item in self._selected_items():
                item.takes, item.active = [item.take], 0
        else:
            return False
        return True

    def _glue(self: FakeReaper, track) -> None:
        """The selected items on track become one new, selected item playing a
        new file; REAPER renders it, the fake only names it."""
        chosen = [item for item in track.items if item.selected]
        if not chosen:
            return
        start, end = min(i.position for i in chosen), max(i.end for i in chosen)
        end += self.glue_length_error
        path = str(self.tmp / f"glued-{len(self.commands):03d}.wav")
        glued = ItemState(self.new_pointer("MediaItem"), start, end - start, False,
                          source_file=path, selected=True)
        glued.take.name = re.sub(r"\.wav$", "", path.rsplit("/", 1)[-1])
        if self.glue_reuses_first_item:
            first = min(chosen, key=lambda i: i.position)
            first.position, first.length = start, end - start
            first.takes, first.active = glued.takes, 0
            glued = first
        track.items[:] = [i for i in track.items if not i.selected] + [glued]
        self._sort(track)
