"""The ReaScript REAPER runs so this server can drive it.

reapy's own activate_reapy_server.py binds its server to 0.0.0.0 with no
authentication, and every request names a Python callable to run, so anyone
who can reach the port can run any command as the user. reapy also finds that
server through REAPER's web interface, which enable_dist_api() adds on port
2307 with no password.

This bridge serves the same protocol, but only on 127.0.0.1, at a fixed port
the client connects to directly, so no web interface is needed. Being on the
same computer is not enough: each side proves it knows the per-install secret
token (see bridge_token.py) without ever sending it, so a program that
listens on the port while REAPER is closed learns nothing it can replay:

1. bridge -> client: GREETING_PREFIX, this bridge's instance id (new each
   time REAPER starts) and a fresh random nonce, in ASCII.
2. client -> bridge: its own random nonce and client_proof(), an
   HMAC-SHA256 of both nonces keyed with the token.
3. bridge -> client: HANDSHAKE_OK and bridge_proof(), the same HMAC with
   another label, so the client knows it reached the real bridge before it
   sends any request.

A wrong proof, or none within HANDSHAKE_TIMEOUT_SECONDS, gets the connection
closed before any request is read. Pending handshakes are read without ever
blocking REAPER's main loop; when MAX_PENDING_CONNECTIONS are waiting, the
oldest is closed to make room. A client that sends an oversized or malformed
request, stalls mid-request or stops reading replies is disconnected; the
loop itself always reschedules, so one bad connection cannot stop the bridge.
A local program without the token cannot drive REAPER, but it can still
keep the port busy; an authenticated client can hold REAPER's main loop
through reapy's HOLD request, as with stock reapy.

It runs inside REAPER (reaper-mcp-setup installs a launcher and starts it
from __startup.lua); the MCP server imports only its constants and the
handshake helpers.
"""

import hashlib
import hmac
import secrets
import socket
import sys
import time
import traceback
from pathlib import Path

import reapy
from reapy.tools import json
from reapy.tools.network.server import Server
from reapy.tools.network.socket import Socket

from reaper_mcp import bridge_token

HOST = "127.0.0.1"
PORT = 2306
REAPY_VERSION = "0.10.0"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
IO_TIMEOUT_SECONDS = 5.0
HANDSHAKE_TIMEOUT_SECONDS = 5.0
HANDSHAKE_OK = b"ok"
GREETING_PREFIX = "reaper-mcp-bridge/2"
NONCE_BYTES = 32
PROOF_BYTES = hashlib.sha256().digest_size
AUTH_BYTES = NONCE_BYTES + PROOF_BYTES  # the client's nonce and proof
MAX_PENDING_CONNECTIONS = 16  # connected but not authenticated yet
_TIMEOUTS = (socket.timeout, TimeoutError)  # distinct before Python 3.10


def client_proof(token, bridge_nonce, client_nonce):
    """What the client sends to show it knows token."""
    return hmac.new(token, b"client" + bridge_nonce + client_nonce, hashlib.sha256).digest()


def bridge_proof(token, bridge_nonce, client_nonce):
    """What the bridge answers to show it knows token too."""
    return hmac.new(token, b"bridge" + client_nonce + bridge_nonce, hashlib.sha256).digest()


def greeting(instance, nonce):
    return f"{GREETING_PREFIX} {instance} {nonce.hex()}".encode("ascii")


def parse_greeting(data):
    """(instance, nonce) from a greeting; ValueError when it is not one."""
    parts = data.decode("ascii").split(" ")
    if len(parts) != 3 or parts[0] != GREETING_PREFIX:
        raise ValueError(f"not a reaper-mcp bridge greeting: {data[:60]!r}")
    nonce = bytes.fromhex(parts[2])
    if len(nonce) != NONCE_BYTES:
        raise ValueError("bad nonce in the bridge greeting")
    return parts[1], nonce


class _Pending:
    """A connection that has not proved it knows the token yet."""

    def __init__(self, connection, nonce):
        self.connection = connection
        self.nonce = nonce
        self.deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS
        self.received = b""

    def read_available(self):
        """Read what has arrived, never waiting. Returns the client's nonce and
        proof once all of it is in, else None; raises ConnectionAbortedError
        for a closed connection or a message that is not an answer."""
        wanted = 8 + AUTH_BYTES - len(self.received)
        self.connection.settimeout(0.0)
        try:
            chunk = self.connection._socket.recv(wanted)
        except (BlockingIOError, *_TIMEOUTS):
            return None
        if not chunk:
            raise ConnectionAbortedError
        self.received += chunk
        if len(self.received) >= 8 and int.from_bytes(self.received[:8], "little") != AUTH_BYTES:
            raise ConnectionAbortedError
        return self.received[8:] if len(self.received) == 8 + AUTH_BYTES else None


class _BoundedSocket(Socket):
    """A reapy connection that refuses oversized, stalled or partial requests."""

    def recv(self, timeout=0.0001, limit=MAX_REQUEST_BYTES):
        self.settimeout(timeout)
        header = self._socket.recv(8)  # a timeout here means "no request yet"
        length = int.from_bytes(header, "little")
        if length == 0 or len(header) != 8 or length > limit:
            # Server._get_request treats this as the client disconnecting.
            raise ConnectionAbortedError
        return self._recv_exactly(length)

    def _recv_exactly(self, length):
        # With a timeout set, recv(MSG_WAITALL) can return early, so read
        # until the whole body is in or the deadline passes.
        deadline = time.monotonic() + IO_TIMEOUT_SECONDS
        chunks, received = [], 0
        while received < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionAbortedError
            self.settimeout(remaining)
            try:
                chunk = self._socket.recv(min(length - received, 1 << 20))
            except _TIMEOUTS as e:
                raise ConnectionAbortedError from e
            if not chunk:
                raise ConnectionAbortedError
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    def send(self, data):
        self.settimeout(IO_TIMEOUT_SECONDS)
        super().send(data)


class LocalServer(Server):
    """reapy's Server, listening on the loopback interface only, serving
    only clients that proved they know the token."""

    def __init__(self, port, token):
        Socket.__init__(self)
        self._token = token
        self.instance = secrets.token_hex(8)
        # address -> _Pending, until the client proves it knows the token.
        self.pending = {}
        if sys.platform.startswith("linux"):
            # Lets REAPER restart while old connections sit in TIME_WAIT. Linux
            # still refuses a second listener on the port, even on 0.0.0.0;
            # macOS and Windows would allow one, so they don't get this option.
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.bind((HOST, port))
        self.listen()
        self.connections = {}
        self.settimeout(0.0001)

    def accept(self, *args, **kwargs):
        try:
            connection, address = self._socket.accept()
        except OSError:  # includes the timeout when nobody is connecting
            return None
        if len(self.pending) >= MAX_PENDING_CONNECTIONS:
            # Make room by closing the oldest: a flood of idle connections
            # then cannot lock a real client out for good.
            self._drop_pending(next(iter(self.pending)))
        nonce = secrets.token_bytes(NONCE_BYTES)
        try:
            connection = _BoundedSocket(connection)
            connection.send(greeting(self.instance, nonce))
        except OSError:
            connection.close()
            return None
        self.pending[address] = _Pending(connection, nonce)
        return connection, address

    def authenticate_pending(self):
        """Admit each pending client that sent the right proof; close the ones
        that sent anything else or nothing in time. Never waits for data."""
        for address, pending in list(self.pending.items()):
            try:
                answer = pending.read_available()
            except OSError:
                self._drop_pending(address)
                continue
            if answer is None:
                if time.monotonic() > pending.deadline:
                    self._drop_pending(address)
                continue
            client_nonce, proof = answer[:NONCE_BYTES], answer[NONCE_BYTES:]
            expected = client_proof(self._token, pending.nonce, client_nonce)
            if not hmac.compare_digest(proof, expected):
                self._drop_pending(address)
                continue
            del self.pending[address]
            connection = pending.connection
            try:
                connection.send(HANDSHAKE_OK + bridge_proof(self._token, pending.nonce, client_nonce))
            except OSError:
                _close(connection)
                continue
            self.connections[address] = connection

    def _drop_pending(self, address):
        _close(self.pending.pop(address).connection)

    def _get_request(self, connection, address):
        try:
            request = connection.recv()
            return json.loads(request.decode())
        except _TIMEOUTS:
            return None
        except (OSError, ValueError, UnicodeDecodeError):
            # Disconnected, stalled, oversized or malformed: drop the client.
            return {"function": self.disconnect,
                    "input": {"args": (address,), "kwargs": {}}}

    def disconnect(self, address):
        connection = self.connections.pop(address, None)
        if connection is not None:
            _close(connection)

    def send_results(self, results):
        for address, result in results.items():
            connection = self.connections.get(address)
            if connection is None:
                continue
            try:
                self._send_result(connection, result)
            except OSError:  # client gone or not reading
                self.disconnect(address)


def _close(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def main(token_file=None):
    """Serve until REAPER quits. token_file is the token's path, which the
    launcher passes so REAPER's environment cannot point it elsewhere."""
    if reapy.__version__ != REAPY_VERSION:
        raise RuntimeError(
            f"reaper-mcp bridge needs reapy {REAPY_VERSION}, found {reapy.__version__} "
            f"at {reapy.__file__}; run reaper-mcp-setup again")
    # Refuses to start without a usable token rather than serve anyone.
    token = bridge_token.read_token(None if token_file is None else Path(token_file))
    server = LocalServer(PORT, token)

    def loop():
        try:
            server.accept()
            server.authenticate_pending()
            requests = server.get_requests()
            results = server.process_requests(requests)
            server.send_results(results)
        except Exception:
            traceback.print_exc()
        finally:
            reapy.defer(loop)

    loop()
