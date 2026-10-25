"""The ReaScript REAPER runs so this server can drive it.

reapy's own activate_reapy_server.py binds its server to 0.0.0.0 with no
authentication, and every request names a Python callable to run, so anyone
who can reach the port can run any command as the user. reapy also finds that
server through REAPER's web interface, which enable_dist_api() adds on port
2307 with no password.

This bridge serves the same protocol, but only on 127.0.0.1, at a fixed port
the client connects to directly, so no web interface is needed. A client that
sends an oversized or malformed request, stalls mid-request or stops reading
replies is disconnected; the loop itself always reschedules, so one bad
connection cannot stop the bridge. A local client can still hold REAPER's
main loop through reapy's HOLD request, as with stock reapy.

It runs inside REAPER (reaper-mcp-setup installs a launcher and starts it
from __startup.lua); it is not imported by the MCP server at runtime beyond
HOST and PORT.
"""

import socket
import sys
import time
import traceback

import reapy
from reapy.tools import json
from reapy.tools.network.server import Server
from reapy.tools.network.socket import Socket

HOST = "127.0.0.1"
PORT = 2306
REAPY_VERSION = "0.10.0"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
IO_TIMEOUT_SECONDS = 5.0
_TIMEOUTS = (socket.timeout, TimeoutError)  # distinct before Python 3.10


class _BoundedSocket(Socket):
    """A reapy connection that refuses oversized, stalled or partial requests."""

    def recv(self, timeout=0.0001):
        self.settimeout(timeout)
        header = self._socket.recv(8)  # a timeout here means "no request yet"
        length = int.from_bytes(header, "little")
        if length == 0 or len(header) != 8 or length > MAX_REQUEST_BYTES:
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
    """reapy's Server, listening on the loopback interface only."""

    def __init__(self, port):
        Socket.__init__(self)
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
        try:
            connection = _BoundedSocket(connection)
            connection.send(f"{address}".encode("ascii"))
        except OSError:
            connection.close()
            return None
        self.connections[address] = connection
        return connection, address

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
        if connection is None:
            return
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def send_results(self, results):
        for address, result in results.items():
            connection = self.connections.get(address)
            if connection is None:
                continue
            try:
                self._send_result(connection, result)
            except OSError:  # client gone or not reading
                self.disconnect(address)


def main():
    if reapy.__version__ != REAPY_VERSION:
        raise RuntimeError(
            f"reaper-mcp bridge needs reapy {REAPY_VERSION}, found {reapy.__version__} "
            f"at {reapy.__file__}; run reaper-mcp-setup again")
    server = LocalServer(PORT)

    def loop():
        try:
            server.accept()
            requests = server.get_requests()
            results = server.process_requests(requests)
            server.send_results(results)
        except Exception:
            traceback.print_exc()
        finally:
            reapy.defer(loop)

    loop()
