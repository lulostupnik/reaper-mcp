"""Fail a live run loudly when REAPER opens a dialog, instead of hanging.

A REAPER modal ("Nothing to render!", the overwrite prompt, "Save changes?")
blocks the reapy call that caused it until someone clicks, so a live test
would wait forever. This thread polls REAPER's windows through System Events
and, if anything but the main window stays open past a grace period, records
what it saw in ``tripped``. guard_reapy() makes reapy's blocking read poll
that flag and end the run with it. It never clicks anything: a dialog is a
finding, not noise.

(Signals were tried first and do not work here: a SIGINT aimed at the main
thread did not interrupt reapy's read under pytest, and pytest's capture
hid the message.)

On macOS it asks System Events; on Linux under X11 (or XWayland) it lists
REAPER's mapped windows with xwininfo and reads their state with xprop.
Elsewhere it reports that it is not guarding.
"""

import re
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

GRACE_SECONDS = 15.0
POLL_SECONDS = 1.0
# Non-modal windows REAPER opens around a render: the progress window, and the
# results window ("Finished in 0:00 ...") it reuses and does not always close.
# Neither blocks a call; a modal (AXDialog) is always reported.
TRANSIENT_PREFIXES = ("Rendering", "Finished in")

# One line per window: title, tab, subrole. REAPER's alerts ("Nothing to
# render!") have an empty title and subrole AXDialog, so titles alone miss them.
_WINDOWS = """
tell application "System Events" to tell process "REAPER"
    set out to ""
    repeat with w in windows
        set out to out & (name of w as text) & tab & (subrole of w as text) & linefeed
    end repeat
end tell
return out
"""


def _osascript(script: str) -> str:
    # rstrip only: a leading tab is an empty window title, not whitespace.
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    return out.stdout.rstrip("\n") or out.stderr.strip()


def _describe(index: int) -> str:
    return _osascript(
        'tell application "System Events" to tell process "REAPER" to get '
        f"{{value of every static text, name of every button}} of window {index}"
    )


def _dialogs() -> dict[str, int]:
    """Open dialogs, keyed by a label (title, or subrole when untitled), to window index."""
    found = {}
    for index, line in enumerate(_osascript(_WINDOWS).splitlines(), start=1):
        title, _, subrole = line.partition("\t")
        if " - REAPER v" in title or title.startswith(TRANSIENT_PREFIXES):
            continue
        if title or subrole in ("AXDialog", "AXSystemDialog"):
            found[title or f"untitled {subrole}"] = index
    return found


# xwininfo -root -tree line of a REAPER window: id "title": ("REAPER" "REAPER").
_X11_WINDOW = re.compile(r'^\s*(0x[0-9a-f]+) "(.*)": \("REAPER" "REAPER"\)')


def _x11(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout


def _x11_dialogs() -> dict[str, str]:
    """REAPER's mapped windows other than the main one, keyed by title, to window id."""
    found = {}
    for line in _x11("xwininfo", "-root", "-tree").splitlines():
        match = _X11_WINDOW.match(line)
        if not match:
            continue
        window, title = match.groups()
        if " - REAPER v" in title or title.startswith(TRANSIENT_PREFIXES):
            continue
        if "IsViewable" in _x11("xwininfo", "-id", window):
            found[title or f"untitled {window}"] = window
    return found


def _x11_describe(window: str) -> str:
    state = _x11("xprop", "-id", window, "_NET_WM_WINDOW_TYPE", "_NET_WM_STATE")
    return " ".join(state.split())


def _platform() -> tuple | None:
    """(list dialogs, describe one) for this system, or None if it cannot be watched."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        return _dialogs, _describe
    if sys.platform.startswith("linux") and shutil.which("xwininfo") and shutil.which("xprop"):
        try:
            probe = subprocess.run(["xwininfo", "-root"], capture_output=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None
        return (_x11_dialogs, _x11_describe) if probe.returncode == 0 else None
    return None


class Watchdog:
    def __init__(self) -> None:
        self._platform = _platform()
        self.available = self._platform is not None
        self.tripped: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if self.available:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        assert self._platform is not None
        list_dialogs, describe = self._platform
        first_seen: dict[str, float] = {}
        while not self._stop.wait(POLL_SECONDS):
            try:
                open_now = list_dialogs()
            except (subprocess.SubprocessError, OSError):
                continue
            now = time.monotonic()
            first_seen = {w: first_seen.get(w, now) for w in open_now}
            stuck = [w for w, t in first_seen.items() if now - t >= GRACE_SECONDS]
            if stuck:
                self.tripped = "; ".join(f"{w!r}: {describe(open_now[w])}" for w in stuck)
                return


def guard_reapy(watchdog: Watchdog, monkeypatch) -> None:
    """Make reapy's blocking reads give up when the watchdog trips.

    reapy waits for REAPER's reply with no timeout; this reads the same bytes
    in one-second slices and ends the run, naming the dialog, once REAPER is
    known to be blocked on one.
    """
    from reapy.tools.network.socket import Socket

    original = Socket.recv

    def recv(self, timeout=0.0001):
        if timeout is not None:
            return original(self, timeout)
        self.settimeout(1.0)
        while True:
            try:
                length = int.from_bytes(self._socket.recv(8), "little")
                break
            except socket.timeout:
                if watchdog.tripped:
                    pytest.exit(f"REAPER is blocked on a dialog: {watchdog.tripped}", returncode=3)
        if length == 0:
            raise ConnectionAbortedError
        self.settimeout(None)
        return self._socket.recv(length, socket.MSG_WAITALL)

    monkeypatch.setattr(Socket, "recv", recv)
    _guard_bridge_client(watchdog, monkeypatch)


def _guard_bridge_client(watchdog: Watchdog, monkeypatch) -> None:
    """The same for the server's own bridge client, which reads with its own
    deadline (60 s) instead of reapy's Socket.recv: wait in one-second slices
    and end the run as soon as the watchdog names a dialog."""
    from reaper_mcp.reaper import _BridgeClient

    def recv_exactly(self, length: int, deadline: float) -> bytes:
        chunks, received = [], 0
        while received < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for REAPER")
            self._socket.settimeout(min(remaining, 1.0))
            try:
                chunk = self._socket.recv(min(length - received, 1 << 20))
            except socket.timeout:
                if watchdog.tripped:
                    pytest.exit(f"REAPER is blocked on a dialog: {watchdog.tripped}", returncode=3)
                continue
            if not chunk:
                raise ConnectionAbortedError("REAPER closed the connection")
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    monkeypatch.setattr(_BridgeClient, "_recv_exactly", recv_exactly)
