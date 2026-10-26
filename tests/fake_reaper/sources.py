"""The fake REAPER's media sources: the files takes play, and sections of
them (reversed or cut parts).

A take's source is "(PCM_source*)0x<the take's digits>"; a section's parent
(the file) has an F in front of the digits (see item_state). Sections and
in-project MIDI have no file name of their own. CalculateNormalization
(normalizeTo 0 = LUFS-I, 2 = peak) is assumed to return the linear gain that
reaches the target (see LIVE_CHECKS.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import soundfile as sf

from tests.fake_reaper.item_state import ItemState, TakeState

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper

NULL_SOURCE = "(PCM_source*)0x0000000000000000"


class Sources:
    """Mixed into FakeReaper (through Items)."""

    def GetMediaItemTake_Source(self: FakeReaper, take: str) -> str:
        return "(PCM_source*)0x" + self._take(take)[1].digits

    def _source(self: FakeReaper, source: str) -> tuple[ItemState, TakeState, bool]:
        """(item, take, is the section's parent) of a source pointer."""
        item, take = self._take(source)
        return item, take, source.split("0x")[-1].startswith("F")

    def GetMediaSourceParent(self: FakeReaper, source: str) -> str:
        _, take, parent = self._source(source)
        if parent or take.section is None:
            return NULL_SOURCE
        return "(PCM_source*)0xF" + take.digits[1:]

    def GetMediaSourceType(self: FakeReaper, source: str, buf: str, size: int) -> list:
        item, take, parent = self._source(source)
        kind = "MIDI" if item.midi else "SECTION" if take.section and not parent else "WAVE"
        return [source, kind, size]

    def GetMediaSourceFileName(self: FakeReaper, source: str, buf: str, size: int) -> list:
        """In-project MIDI sources and sections have no file name of their own."""
        _, take, parent = self._source(source)
        return [source, "" if take.section and not parent else take.source_file, size]

    def _file_length(self: FakeReaper, take: TakeState) -> float:
        try:
            return float(sf.info(take.source_file).duration)
        except (OSError, RuntimeError):
            return 0.0

    def GetMediaSourceLength(self: FakeReaper, source: str, is_qn: bool) -> list:
        _, take, parent = self._source(source)
        if take.section and not parent:
            return [take.section[1], source, False]
        return [self._file_length(take), source, False]

    def PCM_Source_GetSectionInfo(self: FakeReaper, source, offs, length, rev) -> list:
        _, take, parent = self._source(source)
        if take.section is None or parent:
            return [False, source, 0.0, 0.0, False]
        return [True, source, *take.section]
