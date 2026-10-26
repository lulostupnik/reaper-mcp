"""Data stored with the project (SetProjExtState), as the fake keeps it.

Modelled on the API reference: an empty key deletes every key of the
extension, an empty value deletes the key; GetProjExtState fills a buffer of
the size given, so a longer value comes back cut short. Not part of the undo
state (an undo does not bring a deleted value back), and a new project tab
starts without any. Whether REAPER marks the project dirty on its own is not
known (LIVE_CHECKS.md), so the fake does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper


class ExtState:
    """Mixed into FakeReaper; start_ext_state() sets the state up."""

    def start_ext_state(self: FakeReaper) -> None:
        self.ext_state: dict[tuple[str, str], str] = {}

    def SetProjExtState(self: FakeReaper, project: int, extname: str, key: str, value: str) -> int:
        if not key:
            for stored in [k for k in self.ext_state if k[0] == extname]:
                del self.ext_state[stored]
        elif not value:
            self.ext_state.pop((extname, key), None)
        else:
            self.ext_state[(extname, key)] = value
        return sum(len(k) + len(v) for (ext, k), v in self.ext_state.items() if ext == extname)

    def GetProjExtState(self: FakeReaper, project, extname, key, buf, size) -> list:
        value = self.ext_state.get((extname, key), "")
        return [len(value), project, extname, key, value[:max(0, size - 1)], size]

    def MarkProjectDirty(self: FakeReaper, project: int) -> None:
        self.dirty = True
