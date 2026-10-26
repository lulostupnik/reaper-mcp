"""A fake REAPER behind reapy's own surface, for tests that cannot run REAPER.

``install(monkeypatch, tmp_path)`` replaces reapy at its edges:
``reapy.connect``, the bridge connection, ``reapy.Project`` and the functions on
``reapy.reascript_api``. Everything above those (tool modules, the server,
the MCP client) runs for real. See objects.py for the fidelity rules.
Calls to those functions go through reaper.guarded, as reapy's requests do,
so a test can lose one (see transport.py).
"""

import functools
from pathlib import Path

import pytest
import reapy
from reapy import reascript_api

from reaper_mcp import reaper as reaper_module
from tests.fake_reaper.api import RPR_FUNCTIONS, FakeReaper
from tests.fake_reaper.signatures import checked


def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeReaper:
    fake = FakeReaper(tmp_path)
    monkeypatch.setattr(reapy, "connect", lambda *a, **k: None)
    monkeypatch.setattr(reaper_module, "_connect_to_bridge", fake.connect)
    # HOLD / RELEASE go over the bridge under their own names, like reapy's.
    monkeypatch.setattr(reaper_module, "_control", lambda command: reaper_module.guarded(
        command, lambda: fake.deliver(command, fake.control, (command,))))
    monkeypatch.setattr(reapy, "Project", lambda *a, **k: fake.project)
    for name in RPR_FUNCTIONS:
        function = _over_bridge(fake, name, checked(name, getattr(fake, name)))
        monkeypatch.setattr(reascript_api, name, function, raising=False)
    return fake


def _over_bridge(fake: FakeReaper, name: str, function):
    @functools.wraps(function)
    def call(*args):
        return reaper_module.guarded(name, lambda: fake.deliver(name, function, args))

    return call


__all__ = ["FakeReaper", "install"]
