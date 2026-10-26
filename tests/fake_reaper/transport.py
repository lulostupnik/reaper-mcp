"""The fake's side of the bridge connection: requests that get lost.

In REAPER a request can time out or lose its connection after REAPER has run
it, or before. lose_request() makes the next call of one function fail that
way, with or without its effect, so tests can check that a tool never
reports such a call as a success. The failure is raised as the socket would
raise it (an OSError) and goes through reaper_mcp.reaper.guarded, the same
code reapy's requests go through.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING

from reapy.errors import DistError

if TYPE_CHECKING:
    from tests.fake_reaper.api import FakeReaper


class Transport:
    """Mixed into FakeReaper; start_transport() sets the state up."""

    def start_transport(self: FakeReaper) -> None:
        # function name -> [calls to let through first, whether REAPER ran
        # the lost one, the OSError the client sees]
        self.lost_requests: dict[str, list] = {}
        self.requests: list[str] = []
        # The bridge's instance id; a test changes it to restart REAPER.
        self.bridge_instance = "fake-reaper-1"

    def lose_request(self: FakeReaper, name: str, *, applied: bool,
                     error: type[OSError] = TimeoutError, skip: int = 0) -> None:
        """The next call of name (after skip more) gets no answer; applied:
        REAPER ran it anyway."""
        self.lost_requests[name] = [skip, applied, error]

    def deliver(self: FakeReaper, name: str, function: Callable, args: tuple) -> object:
        self.requests.append(name)
        loss = self.lost_requests.get(name)
        if loss is not None and loss[0] > 0:
            loss[0] -= 1
            loss = None
        if loss is None:
            return _inside_reaper(function, args)
        _, applied, error = self.lost_requests.pop(name)
        if applied:
            _inside_reaper(function, args)
        raise error(f"fake: no answer to {name}")


def _inside_reaper(function: Callable, args: tuple) -> object:
    """function(*args) as REAPER runs it: an exception inside REAPER comes back
    as DistError, never as the transport's OSError (which means a lost request)."""
    try:
        return function(*args)
    except OSError as e:
        raise DistError("".join(traceback.format_exception(e))) from e
