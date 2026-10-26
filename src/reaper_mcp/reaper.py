"""The one module that imports reapy.

Every tool reaches REAPER through here: ``RPR`` for ReaScript API calls and
``get_project()`` for reapy's object model. Keeping reapy behind one module
gives the type checker a declared surface to check calls against (reapy
attaches the ReaScript functions at runtime, so its own stub declares none of
them), and gives tests a single seam to replace with a fake.
"""

import importlib
import logging
import secrets
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol, TypeVar, cast

import reapy
from reapy import reascript_api as _reascript_api
from reapy.errors import DistError
from reapy.tools.network import client as _client
from reapy.tools.network import machines as _machines

from reaper_mcp import bridge, bridge_token

logger = logging.getLogger("reaper_mcp.reaper")
T = TypeVar("T")

# reapy hands REAPER object pointers across the bridge as strings such as
# "(MediaTrack*)0x0000600001234567". A missing object is a null pointer string,
# "(TrackEnvelope*)0x0000000000000000", which is truthy: test with is_null().
Pointer = str


def is_null(pointer: Pointer) -> bool:
    return not pointer or pointer.endswith("0x0000000000000000")


class ReaScriptAPI(Protocol):
    """The subset of REAPER's ReaScript API this server calls.

    Parameters are positional-only, as REAPER's are; the leading underscore
    marks their names as documentation, not API.
    """

    # Actions, undo and UI
    def Main_OnCommand(self, _command: int, _flag: int, /) -> None: ...
    def Main_openProject(self, _path: str, /) -> None: ...
    def Undo_BeginBlock2(self, _project: int, /) -> None: ...
    def Undo_EndBlock2(self, _project: int, _description: str, _flags: int, /) -> None: ...
    def PreventUIRefresh(self, _count: int, /) -> None: ...
    def TrackList_AdjustWindows(self, _is_minor: bool, /) -> None: ...
    def UpdateArrange(self) -> None: ...
    def Undo_CanUndo2(self, _project: int, /) -> str: ...
    def Undo_CanRedo2(self, _project: int, /) -> str: ...
    def Undo_DoUndo2(self, _project: int, /) -> int: ...
    def Undo_DoRedo2(self, _project: int, /) -> int: ...
    def GetResourcePath(self) -> str: ...
    def kbd_getTextFromCmd(self, _command: int, _section: int, /) -> str: ...
    def GetToggleCommandState(self, _command: int, /) -> int: ...
    def get_config_var_string(self, _name: str, _buf: str, _size: int, /) -> list: ...

    # Audio device
    def Audio_IsRunning(self) -> int: ...
    def GetAudioDeviceInfo(self, _attribute: str, _desc: str, _size: int, /) -> list: ...
    def GetNumAudioInputs(self) -> int: ...
    def GetNumAudioOutputs(self) -> int: ...
    def GetInputChannelName(self, _channel: int, /) -> str: ...
    def GetOutputChannelName(self, _channel: int, /) -> str: ...
    def GetInputOutputLatency(self, _input: int, _output: int, /) -> list: ...

    def Main_SaveProjectEx(self, _project: int, _path: str, _options: int, /) -> None: ...
    def EnumProjects(self, _index: int, _filename: str, _size: int, /) -> list: ...
    def IsProjectDirty(self, _project: int, /) -> int: ...
    def GetProjectPathEx(self, _project: int, _buf: str, _size: int, /) -> list: ...

    # Transport, cursor and selection ranges
    def GetPlayState(self) -> int: ...
    def GetCursorPosition(self) -> float: ...
    def GetPlayPosition(self) -> float: ...
    def GetSetRepeat(self, _value: int, /) -> int: ...
    def GetSet_LoopTimeRange2(  # noqa: PLR0917 -- REAPER's own signature
        self, _project: int, _is_set: bool, _is_loop: bool, _start: float, _end: float,
        _allow_autoseek: bool, /,
    ) -> list: ...
    def GetLastTouchedFX(self, _track: int, _fx: int, _param: int, /) -> list: ...

    # Project settings
    def GetSetProjectInfo(  # noqa: PLR0917 -- REAPER's own signature
        self, _project: int, _desc: str, _value: float, _is_set: bool, /
    ) -> float: ...
    def GetSetProjectInfo_String(  # noqa: PLR0917 -- REAPER's own signature
        self, _project: int, _desc: str, _value: str, _is_set: bool, /
    ) -> object: ...

    # Tempo and time signature
    def Master_GetTempo(self) -> float: ...
    def CountTempoTimeSigMarkers(self, _project: int, /) -> int: ...
    def GetTempoTimeSigMarker(  # noqa: PLR0913, PLR0917 -- REAPER's own signature
        self, _project: int, _index: int, _pos: float, _measure: int, _beat: float,
        _bpm: float, _num: int, _denom: int, _linear: bool, /,
    ) -> list: ...
    def SetTempoTimeSigMarker(  # noqa: PLR0913, PLR0917 -- REAPER's own signature
        self, _project: int, _index: int, _pos: float, _measure: int, _beat: float,
        _bpm: float, _num: int, _denom: int, _linear: bool, /,
    ) -> bool: ...
    def TimeMap_GetTimeSigAtTime(  # noqa: PLR0917 -- REAPER's own signature
        self, _project: int, _time: float, _num: int, _denom: int, _tempo: float, /
    ) -> list: ...

    # Markers and regions (REAPER 7: string out-parameters of EnumProjectMarkers
    # do not survive the Python binding, so names are read through these).
    def CountProjectMarkers(self, _project: int, _markers: int, _regions: int, /) -> list: ...
    def GetRegionOrMarker(self, _project: int, _index: int, _guid: str, /) -> Pointer: ...
    def GetRegionOrMarkerInfo_Value(self, _project: int, _marker: Pointer, _param: str, /) -> float: ...
    def GetSetRegionOrMarkerInfo_String(  # noqa: PLR0917 -- REAPER's own signature
        self, _project: int, _marker: Pointer, _param: str, _value: str, _is_set: bool, /
    ) -> list: ...

    # Tracks
    def CountTracks(self, _project: int, /) -> int: ...
    def GetTrack(self, _project: int, _index: int, /) -> Pointer: ...
    def GetSetMediaTrackInfo_String(  # noqa: PLR0917 -- REAPER's own signature
        self, _track: Pointer, _param: str, _value: str, _is_set: bool, /
    ) -> list: ...
    def GetMasterTrack(self, _project: int, /) -> Pointer: ...
    def GetMediaTrackInfo_Value(self, _track: Pointer, _param: str, /) -> float: ...
    def CountSelectedTracks(self, _project: int, /) -> int: ...
    def GetSelectedTrack(self, _project: int, _index: int, /) -> Pointer: ...
    def DeleteTrack(self, _track: Pointer, /) -> None: ...
    def SetMediaTrackInfo_Value(self, _track: Pointer, _param: str, _value: float, /) -> bool: ...
    def ColorToNative(self, _r: int, _g: int, _b: int, /) -> int: ...
    def SetOnlyTrackSelected(self, _track: Pointer, /) -> None: ...
    def SetTrackSelected(self, _track: Pointer, _selected: bool, /) -> None: ...
    def ReorderSelectedTracks(self, _before_index: int, _make_prev_folder: int, /) -> bool: ...
    def Track_GetPeakInfo(self, _track: Pointer, _channel: int, /) -> float: ...
    def Track_GetPeakHoldDB(self, _track: Pointer, _channel: int, _clear: bool, /) -> float: ...

    # Media items
    def InsertMedia(self, _path: str, _mode: int, /) -> int: ...
    def CountSelectedMediaItems(self, _project: int, /) -> int: ...
    def GetSelectedMediaItem(self, _project: int, _index: int, /) -> Pointer: ...
    def GetMediaItem_Track(self, _item: Pointer, /) -> Pointer: ...
    def GetMediaItemInfo_Value(self, _item: Pointer, _param: str, /) -> float: ...
    def GetSetMediaItemInfo_String(  # noqa: PLR0917 -- REAPER's own signature
        self, _item: Pointer, _param: str, _value: str, _is_set: bool, /
    ) -> list: ...

    def GetActiveTake(self, _item: Pointer, /) -> Pointer: ...
    def GetMediaItemTake_Source(self, _take: Pointer, /) -> Pointer: ...
    def GetMediaSourceFileName(self, _source: Pointer, _buf: str, _size: int, /) -> list: ...

    # FX
    def TrackFX_GetFXName(  # noqa: PLR0917 -- REAPER's own signature
        self, _track: Pointer, _fx: int, _buf: str, _size: int, /
    ) -> list: ...
    def TrackFX_Delete(self, _track: Pointer, _fx_index: int, /) -> bool: ...
    def TrackFX_SetParamNormalized(  # noqa: PLR0917 -- REAPER's own signature
        self, _track: Pointer, _fx: int, _param: int, _value: float, /
    ) -> bool: ...
    def TrackFX_SetPreset(self, _track: Pointer, _fx: int, _preset: str, /) -> bool: ...
    def TrackFX_GetFormattedParamValue(  # noqa: PLR0917 -- REAPER's own signature
        self, _track: Pointer, _fx: int, _param: int, _buf: str, _size: int, /
    ) -> list: ...
    def TrackFX_FormatParamValueNormalized(  # noqa: PLR0917 -- REAPER's own signature
        self, _track: Pointer, _fx: int, _param: int, _value: float, _buf: str, _size: int, /
    ) -> list: ...

    # Sends
    def CreateTrackSend(self, _source: Pointer, _dest: Pointer, /) -> int: ...
    def RemoveTrackSend(self, _track: Pointer, _category: int, _send_index: int, /) -> bool: ...
    def GetTrackNumSends(self, _track: Pointer, _category: int, /) -> int: ...
    def GetTrackSendInfo_Value(  # noqa: PLR0917 -- REAPER's own signature
        self, _track: Pointer, _category: int, _send_index: int, _param: str, /
    ) -> float: ...
    def SetTrackSendInfo_Value(  # noqa: PLR0917 -- REAPER's own signature
        self, _track: Pointer, _category: int, _send_index: int, _param: str, _value: float, /
    ) -> bool: ...

    # Envelopes
    def GetTrackEnvelopeByName(self, _track: Pointer, _name: str, /) -> Pointer: ...
    def InsertEnvelopePoint(  # noqa: PLR0917 -- REAPER's own signature
        self,
        _envelope: Pointer,
        _time: float,
        _value: float,
        _shape: int,
        _tension: float,
        _selected: bool,
        _no_sort: bool,
        /,
    ) -> bool: ...
    def Envelope_SortPoints(self, _envelope: Pointer, /) -> bool: ...
    def GetEnvelopeScalingMode(self, _envelope: Pointer, /) -> int: ...
    def ScaleToEnvelopeMode(self, _mode: int, _value: float, /) -> float: ...


RPR = cast(ReaScriptAPI, _reascript_api)


def string_or_none(function: Callable[..., str], *args: object) -> str | None:
    """function(*args) for a ReaScript function returning a C string that may
    be NULL, or None for NULL.

    REAPER's Python binding decodes such a result with r.decode(), so NULL
    raises AttributeError inside REAPER, which reapy hands back as DistError.
    """
    try:
        return function(*args) or None
    except (DistError, AttributeError):
        return None

# A ReaScript call normally answers in milliseconds; this much silence means
# REAPER is stuck (a modal dialog, a hung plug-in). Rendering gets longer, via
# slow_calls().
REQUEST_TIMEOUT_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 10.0
RENDER_TIMEOUT_SECONDS = 3600.0
BUSY_HINT = (
    "REAPER is running but not answering. It is probably waiting on an open "
    "dialog or window (for example Preferences, a plug-in window or a 'Save "
    "changes?' question): close it in REAPER, then try again")

_connected = False
_connecting = False
_timeout = REQUEST_TIMEOUT_SECONDS
# The call lost in the current tool call, if any; see guarded().
_lost: "OutcomeUncertain | None" = None
# The instance id of the bridge (REAPER run) last connected to.
_bridge_instance: str | None = None
# Calls owed to that REAPER once it answers again (e.g. closing an undo
# block), and the bridge instance they are owed to.
_owed: list[Callable[[], object]] = []
_owed_to: str | None = None


class OutcomeUncertain(RuntimeError):
    """A request reached (or may have reached) REAPER, but no answer came back:
    it timed out or the connection dropped. REAPER may or may not have done it."""

    def __init__(self, message: str, *, timed_out: bool) -> None:
        super().__init__(message)
        self.timed_out = timed_out


class TokenRejected(ConnectionError):
    """The bridge closed the connection instead of accepting the token's
    proof, or the listener could not prove it knows the token."""


class BridgeBusy(ConnectionError):
    """REAPER accepted the connection but did not answer the handshake in time."""


def uncertain_next_step(lost: OutcomeUncertain, *, read_only: bool) -> str:
    """What to do after lost, for a tool that changes the project or not."""
    if lost.timed_out:
        cause = ("REAPER is probably waiting on an open dialog or plug-in window. "
                 "Ask the user to look at REAPER and close it")
        if read_only:
            return f"This tool changes nothing. {cause}, then try again."
        return (f"It is unknown whether this tool's change was made, and REAPER may "
                f"still finish it once it is unblocked. {cause}; once REAPER responds "
                "normally, call get_context and retry only if the change is not there.")
    if read_only:
        return ("This tool changes nothing. REAPER may have closed or crashed: "
                "once it is running again, try again.")
    return ("It is unknown whether this tool's change was made; REAPER may have closed "
            "or crashed. Once REAPER is running, call get_context and retry only if "
            "the change is not there.")


def ensure_connected() -> None:
    global _connected, _connecting, _bridge_instance
    if _connected or _connecting:
        return
    if _lost is not None:  # no reconnecting in the middle of a tool call
        raise OutcomeUncertain(str(_lost), timed_out=_lost.timed_out)
    _connecting = True  # connecting makes requests, which come back here
    try:
        _bridge_instance = _connect_to_bridge()
        _connected = True
        logger.info("Connected to REAPER")
    except bridge_token.TokenError as e:
        raise RuntimeError(f"Cannot connect to REAPER: {e}.") from e
    except TokenRejected as e:
        raise RuntimeError(
            f"Cannot connect to REAPER: {e} (token file: {bridge_token.token_path()}).") from e
    except (BridgeBusy, TimeoutError) as e:
        raise RuntimeError(f"Cannot connect to REAPER: {BUSY_HINT}.") from e
    except Exception as e:
        raise RuntimeError(
            f"Cannot connect to REAPER: {e}. "
            "Make sure REAPER is running and the bridge is installed: "
            "close REAPER, run reaper-mcp-setup once, then start REAPER again."
        ) from e
    finally:
        _connecting = False
    _pay_debts()


def _pay_debts() -> None:
    """Run the calls owed to this REAPER, best effort: one that fails is
    logged, not reported as the current tool's. Owed calls are dropped when
    REAPER restarted, since the new one has nothing left open.

    Once a call is lost while paying, paying stops: the rest stay owed to
    the next connection. That includes a debt that caught the loss itself
    and owed itself again, which would otherwise be paid again at once,
    forever."""
    global _lost
    if _owed and _owed_to != _bridge_instance:
        logger.info("REAPER restarted; dropping %d owed calls", len(_owed))
        _owed.clear()
    while _owed:
        call = _owed.pop(0)  # tried once: a lost attempt may still have run
        try:
            call()
        except OutcomeUncertain:
            pass  # handled below, like a debt that caught the loss itself
        except Exception as e:
            logger.warning("an owed call to REAPER failed: %s", e)
        if _lost is not None:
            _lost = None  # the debt's loss, not the tool's
            raise RuntimeError(
                f"Cannot reach REAPER: {BUSY_HINT}. Nothing was sent for this tool.")


def owe_on_reconnect(*calls: Callable[[], object]) -> None:
    """Run calls, in order, once this same REAPER answers again (at the next
    connection); they are dropped if REAPER restarted in between."""
    global _owed_to
    _owed.extend(calls)
    _owed_to = _bridge_instance


def start_tool_call() -> None:
    """Forget a call lost during the previous tool call, and drop a connection
    REAPER closed since (it quit or restarted), so the first request
    reconnects instead of being lost in a dead socket."""
    global _lost
    _lost = None
    client = _machines.CLIENT
    if _connected and isinstance(client, _BridgeClient) and client.peer_closed():
        logger.info("REAPER closed the connection; reconnecting")
        _drop_connection()


def lost_call() -> OutcomeUncertain | None:
    """The call lost during the current tool call, if one was."""
    return _lost


@contextmanager
def slow_calls(seconds: float) -> Iterator[None]:
    """Let each call inside take up to seconds before it counts as lost."""
    global _timeout
    previous, _timeout = _timeout, seconds
    try:
        yield
    finally:
        _timeout = previous


def guarded(name: str, send: Callable[[], T]) -> T:
    """send(), the transport of one request named name to REAPER.

    A transport failure (OSError: timeout, reset, closed) means the request
    may or may not have run, so it becomes OutcomeUncertain, the connection
    is dropped (a late answer would otherwise be read as the next call's),
    and every later call in the same tool call fails the same way at once
    instead of waiting out another timeout. The next tool call reconnects.
    """
    global _lost
    if _connecting:  # nothing changes while connecting: let ensure_connected report it
        return send()
    if _lost is not None:
        raise OutcomeUncertain(str(_lost), timed_out=_lost.timed_out)
    ensure_connected()
    try:
        return send()
    except OSError as e:
        _drop_connection()
        timed_out = isinstance(e, TimeoutError)
        what = (f"did not answer within {_timeout:g} s" if timed_out
                else "lost the connection")
        _lost = OutcomeUncertain(f"REAPER {what} during {name}.", timed_out=timed_out)
        raise _lost from e


def _drop_connection() -> None:
    global _connected
    _connected = False
    client = _machines.CLIENT
    if client is not None:
        try:
            client.close()
        except OSError:
            pass


class _BridgeClient(_client.Client):
    """reapy's client for the bridge: proves it knows the token (and checks
    that the bridge does) first, and gives up on an answer after the current
    timeout instead of waiting forever."""

    def __init__(self, port: int, host: str, token: bytes) -> None:
        self._token = token
        self.instance = ""
        try:
            super().__init__(port, host)
        except BaseException:
            self._socket.close()
            raise

    def _connect(self, port: int, host: str) -> None:
        self._socket.settimeout(CONNECT_TIMEOUT_SECONDS)
        self._socket.connect((host, port))
        try:
            greeting = self._recv_message(CONNECT_TIMEOUT_SECONDS)
            self.instance, nonce = bridge.parse_greeting(greeting)
            self.address = greeting.decode("ascii")
            client_nonce = secrets.token_bytes(bridge.NONCE_BYTES)
            proof = bridge.client_proof(self._token, nonce, client_nonce)
            self._send_message(client_nonce + proof, CONNECT_TIMEOUT_SECONDS)
            try:
                reply = self._recv_message(CONNECT_TIMEOUT_SECONDS)
            except (ConnectionError, EOFError) as e:
                raise TokenRejected("REAPER's bridge refused this server's token; "
                                    f"{bridge_token.SETUP_HINT}") from e
        except TimeoutError as e:  # REAPER is stuck, not refusing
            raise BridgeBusy(BUSY_HINT) from e
        expected = bridge.HANDSHAKE_OK + bridge.bridge_proof(self._token, nonce, client_nonce)
        if reply != expected:
            raise TokenRejected(
                f"the program on {host}:{port} could not prove it is REAPER's bridge "
                f"with this server's token; {bridge_token.SETUP_HINT}")

    def peer_closed(self) -> bool:
        """True when the bridge closed this connection (REAPER quit or
        restarted) or sent something nobody asked for. Never waits."""
        try:
            self._socket.settimeout(0.0)
            self._socket.recv(1, socket.MSG_PEEK)  # b"" when closed
        except BlockingIOError:
            return False  # open, with nothing waiting: the normal case
        except OSError:
            pass
        return True

    def request(self, function, input=None):
        name = getattr(function, "__name__", str(function))
        return guarded(name, lambda: super(_BridgeClient, self).request(function, input))

    def send(self, data: bytes) -> None:
        self._send_message(data, _timeout)

    def recv(self, timeout: float | None = None) -> bytes:
        return self._recv_message(_timeout)

    def _get_result(self):
        try:
            return super()._get_result()
        except ValueError as e:  # a garbled answer: the stream is out of step
            raise ConnectionAbortedError("garbled answer from REAPER") from e

    def _send_message(self, data: bytes, timeout: float) -> None:
        self._socket.settimeout(timeout)
        self._socket.sendall(len(data).to_bytes(8, "little") + data)

    def _recv_message(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        length = int.from_bytes(self._recv_exactly(8, deadline), "little")
        return self._recv_exactly(length, deadline)

    def _recv_exactly(self, length: int, deadline: float) -> bytes:
        chunks, received = [], 0
        while received < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for REAPER")
            self._socket.settimeout(remaining)
            try:
                chunk = self._socket.recv(min(length - received, 1 << 20))
            except socket.timeout as e:  # TimeoutError's alias only from 3.10
                raise TimeoutError("timed out waiting for REAPER") from e
            if not chunk:
                raise ConnectionAbortedError("REAPER closed the connection")
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)


def _connect_to_bridge() -> str:
    """Connect reapy straight to the bridge on 127.0.0.1, proving it knows
    the token, and return the bridge's instance id.

    reapy.connect() would first ask REAPER's web interface for the server
    port; the bridge listens on a fixed loopback port instead (see bridge.py),
    so no web interface has to be enabled.
    """
    token = bridge_token.read_token()  # before connecting: a clear error if missing
    client = _BridgeClient(bridge.PORT, bridge.HOST, token)
    _machines.CLIENTS["localhost"] = client
    _machines.CLIENTS[None] = client
    _machines.CLIENT = client
    try:
        # Fills reascript_api in place with the functions REAPER exposes; RPR
        # is this same module object.
        importlib.reload(_reascript_api)
        _refresh_fx_functions()
    except BaseException:
        client.close()  # not left half set up and registered with the bridge
        raise
    return client.instance


def get_project() -> reapy.Project:
    ensure_connected()
    return reapy.Project()


def _refresh_fx_functions() -> None:
    """Refill reapy's FX function tables from the now-filled reascript_api.

    reapy.FX copies the TrackFX_/TakeFX_ functions out of reascript_api into a
    class-level table when reapy is imported. reapy's own connect happens
    during that import; this server connects later, so the table would stay
    empty and every FX property (name, params, ...) would fail. The tables are
    updated in place because FX instances hold references to them.
    """
    from reapy.core.fx import fx as reapy_fx

    for prefix, table in reapy_fx.FX.functions.items():
        table.clear()
        table.update({
            name.replace(prefix, ""): function
            for name, function in vars(_reascript_api).items()
            if name.startswith(prefix)
        })
