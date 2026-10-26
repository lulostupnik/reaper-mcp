"""The bridge handshake (a proof of the token before any request, both ways)
and the client's timeouts, with the real bridge server and client on a
loopback port.
Nothing here talks to REAPER: the bridge's server runs in a thread and never
gets as far as running a request."""

import json
import socket
import threading
import time

import pytest

from reaper_mcp import bridge, bridge_token, reaper

TOKEN = b"a" * 64


@pytest.fixture
def bridge_server():
    """A bridge LocalServer on a free port, accepting and authenticating in a
    thread the way REAPER's defer loop does. Yields (server, port)."""
    server = bridge.LocalServer(0, TOKEN)
    port = server._socket.getsockname()[1]
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            server.accept()
            server.authenticate_pending()
            time.sleep(0.005)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    yield server, port
    stop.set()
    thread.join()
    for address in list(server.connections):
        server.disconnect(address)
    for address in list(server.pending):
        server._drop_pending(address)
    server.close()


def _wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def _frame(data: bytes) -> bytes:
    return len(data).to_bytes(8, "little") + data


def _read_frame(sock: socket.socket) -> bytes:
    sock.settimeout(2)
    length = int.from_bytes(sock.recv(8, socket.MSG_WAITALL), "little")
    return sock.recv(length, socket.MSG_WAITALL)


_read_greeting = _read_frame


def _closed_by_peer(sock: socket.socket) -> bool:
    sock.settimeout(2)
    try:
        return sock.recv(100) == b""
    except ConnectionResetError:
        return True


def test_the_right_token_is_accepted(bridge_server):
    server, port = bridge_server
    client = reaper._BridgeClient(port, "127.0.0.1", TOKEN)
    try:
        assert _wait_for(lambda: len(server.connections) == 1)
        assert server.pending == {}
    finally:
        client.close()


def test_a_wrong_token_is_rejected(bridge_server):
    server, port = bridge_server
    with pytest.raises(reaper.TokenRejected, match="reaper-mcp-setup"):
        reaper._BridgeClient(port, "127.0.0.1", b"b" * 64)
    assert server.connections == {}
    assert _wait_for(lambda: server.pending == {})


def test_a_request_instead_of_the_token_is_never_run(bridge_server, monkeypatch):
    server, port = bridge_server
    run = []
    monkeypatch.setattr(server, "process_requests", lambda requests: run.append(requests))
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        _read_greeting(sock)
        request = json.dumps({"function": "HOLD", "input": None}).encode()
        sock.sendall(_frame(request))
        assert _closed_by_peer(sock)
        assert server.connections == {} and run == []
    finally:
        sock.close()


def test_an_answer_of_the_wrong_size_is_rejected(bridge_server):
    server, port = bridge_server
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        _read_greeting(sock)
        sock.sendall((bridge.AUTH_BYTES + 1).to_bytes(8, "little"))
        assert _closed_by_peer(sock)
        assert server.connections == {}
    finally:
        sock.close()


def test_the_bridge_proves_it_knows_the_token_too(bridge_server):
    server, port = bridge_server
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        instance, nonce = bridge.parse_greeting(_read_greeting(sock))
        assert instance == server.instance
        client_nonce = b"c" * bridge.NONCE_BYTES
        sock.sendall(_frame(client_nonce + bridge.client_proof(TOKEN, nonce, client_nonce)))
        reply = _read_frame(sock)
        assert reply == bridge.HANDSHAKE_OK + bridge.bridge_proof(TOKEN, nonce, client_nonce)
        assert _wait_for(lambda: len(server.connections) == 1)
    finally:
        sock.close()


def _impostor(answer=b""):
    """A listener on a free port that greets like the bridge, records what the
    client sends and replies answer (nothing when empty). Yields (port, sent)."""
    listener = socket.create_server(("127.0.0.1", 0))
    sent = []

    def serve():
        connection, _ = listener.accept()
        with connection:
            connection.sendall(_frame(bridge.greeting("impostor", b"n" * bridge.NONCE_BYTES)))
            sent.append(_read_frame(connection))
            if answer:
                connection.sendall(_frame(answer))
            else:
                connection.settimeout(3)
                try:
                    connection.recv(1)
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread, sent


def test_a_program_posing_as_the_bridge_learns_nothing_it_can_replay(bridge_server):
    server, port = bridge_server
    listener, thread, sent = _impostor(answer=bridge.HANDSHAKE_OK)
    try:
        with pytest.raises(reaper.TokenRejected, match="could not prove"):
            reaper._BridgeClient(listener.getsockname()[1], "127.0.0.1", TOKEN)
        thread.join()
    finally:
        listener.close()
    (answer,) = sent
    assert TOKEN not in answer and TOKEN.hex().encode() not in answer
    # Replaying the captured answer to the real bridge does not get in.
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        _read_greeting(sock)
        sock.sendall(_frame(answer))
        assert _closed_by_peer(sock)
        assert server.connections == {}
    finally:
        sock.close()


def test_a_stalled_partial_answer_never_blocks_reapers_main_loop(monkeypatch):
    server = bridge.LocalServer(0, TOKEN)
    port = server._socket.getsockname()[1]
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        assert _wait_for(lambda: server.accept() is not None or len(server.pending) == 1)
        _read_greeting(sock)
        sock.sendall(bridge.AUTH_BYTES.to_bytes(8, "little") + b"x" * 10)
        time.sleep(0.05)
        started = time.monotonic()
        for _ in range(3):
            server.authenticate_pending()
        assert time.monotonic() - started < 0.05
        assert len(server.pending) == 1 and server.connections == {}
    finally:
        sock.close()
        for address in list(server.pending):
            server._drop_pending(address)
        server.close()


def test_a_client_that_sends_no_token_is_dropped_after_the_timeout(bridge_server, monkeypatch):
    monkeypatch.setattr(bridge, "HANDSHAKE_TIMEOUT_SECONDS", 0.2)
    server, port = bridge_server
    sock = socket.create_connection(("127.0.0.1", port))
    try:
        _read_greeting(sock)
        assert _wait_for(lambda: len(server.pending) == 1)
        assert _closed_by_peer(sock)
        assert server.pending == {} and server.connections == {}
    finally:
        sock.close()


def test_a_new_client_pushes_out_the_oldest_unauthenticated_one(bridge_server, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_PENDING_CONNECTIONS", 1)
    server, port = bridge_server
    first = socket.create_connection(("127.0.0.1", port))
    try:
        _read_greeting(first)
        assert _wait_for(lambda: len(server.pending) == 1)
        client = reaper._BridgeClient(port, "127.0.0.1", TOKEN)  # still gets in
        try:
            assert _wait_for(lambda: len(server.connections) == 1)
            assert _closed_by_peer(first)
        finally:
            client.close()
    finally:
        first.close()


def test_the_bridge_will_not_start_without_a_token(monkeypatch, tmp_path):
    def must_not_listen(*args):
        raise AssertionError("the bridge listened without a token")

    monkeypatch.setattr(bridge, "LocalServer", must_not_listen)
    with pytest.raises(bridge_token.TokenError, match="reaper-mcp-setup"):
        bridge.main(token_file=str(tmp_path / "missing"))


# The client side: connecting needs the token file.

@pytest.fixture
def disconnected(monkeypatch):
    monkeypatch.setattr(reaper, "_connected", False)


def test_connecting_without_a_token_file_says_to_run_setup(monkeypatch, disconnected):
    def must_not_connect(*args):
        raise AssertionError("connected without a token")

    monkeypatch.setattr(reaper, "_BridgeClient", must_not_connect)
    with pytest.raises(RuntimeError) as raised:
        reaper.ensure_connected()
    message = str(raised.value)
    assert message.startswith("Cannot connect to REAPER: the bridge token file")
    assert "run reaper-mcp-setup" in message
    assert reaper._connected is False


def test_connecting_reads_the_token_file_and_uses_it(monkeypatch, disconnected):
    bridge_token.ensure_token()
    shown = []

    class Client:
        instance = "x"

        def __init__(self, port, host, token):
            shown.append((port, host, token))

    monkeypatch.setattr(reaper, "_BridgeClient", Client)
    monkeypatch.setattr(reaper.importlib, "reload", lambda module: module)
    monkeypatch.setattr(reaper._machines, "CLIENT", None)
    monkeypatch.setattr(reaper._machines, "CLIENTS", {None: None})
    reaper.ensure_connected()
    assert shown == [(bridge.PORT, "127.0.0.1", bridge_token.read_token())]


def test_a_rejected_token_tells_the_user_how_to_fix_it(monkeypatch, disconnected):
    bridge_token.ensure_token()

    def reject(*args):
        raise reaper.TokenRejected("REAPER's bridge refused this server's token; "
                                   f"{bridge_token.SETUP_HINT}")

    monkeypatch.setattr(reaper, "_BridgeClient", reject)
    with pytest.raises(RuntimeError, match="refused this server's token; close REAPER"):
        reaper.ensure_connected()


# Honest timeouts: a request with no answer is uncertain, never a success.

@pytest.fixture
def silent_bridge(monkeypatch):
    """A bridge that accepts the token, reads one request and then does what
    the test asks (behaviour[0]): "hang" (never answer) or "drop" (close)."""
    listener = socket.create_server(("127.0.0.1", 0))
    behaviour = ["hang"]
    received = []
    done = threading.Event()

    def serve():
        connection, _ = listener.accept()
        with connection:
            nonce = b"n" * bridge.NONCE_BYTES
            connection.sendall(_frame(bridge.greeting("silent", nonce)))
            client_nonce = _read_frame(connection)[:bridge.NONCE_BYTES]
            proof = bridge.bridge_proof(TOKEN, nonce, client_nonce)
            connection.sendall(_frame(bridge.HANDSHAKE_OK + proof))
            connection.settimeout(None)
            length = int.from_bytes(connection.recv(8, socket.MSG_WAITALL), "little")
            received.append(connection.recv(length, socket.MSG_WAITALL))
            if behaviour[0] == "hang":
                done.wait(5)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = reaper._BridgeClient(listener.getsockname()[1], "127.0.0.1", TOKEN)
    monkeypatch.setattr(reaper._machines, "CLIENT", client)
    monkeypatch.setattr(reaper, "_connected", True)
    yield client, behaviour, received
    done.set()
    client.close()
    thread.join()
    listener.close()


def test_a_request_reaper_never_answers_times_out_as_uncertain(silent_bridge):
    client, _, received = silent_bridge
    started = time.monotonic()
    with reaper.slow_calls(0.3), pytest.raises(reaper.OutcomeUncertain) as raised:
        client.request("SetMediaTrackInfo_Value")
    assert time.monotonic() - started < 3
    assert len(received) == 1  # it did reach REAPER
    assert str(raised.value) == "REAPER did not answer within 0.3 s during SetMediaTrackInfo_Value."
    assert raised.value.timed_out
    assert reaper.lost_call() is raised.value
    assert reaper._connected is False


def test_a_dropped_connection_mid_call_is_uncertain(silent_bridge):
    client, behaviour, _ = silent_bridge
    behaviour[0] = "drop"
    with pytest.raises(reaper.OutcomeUncertain, match="lost the connection during X"):
        client.request("X")


def test_after_a_lost_call_the_rest_of_the_tool_call_fails_at_once(silent_bridge):
    client, _, received = silent_bridge
    with reaper.slow_calls(0.2), pytest.raises(reaper.OutcomeUncertain):
        client.request("First")
    started = time.monotonic()
    with pytest.raises(reaper.OutcomeUncertain):
        client.request("Second")
    assert time.monotonic() - started < 0.1
    assert len(received) == 1  # Second was never sent


def test_slow_calls_restores_the_normal_timeout():
    with reaper.slow_calls(reaper.RENDER_TIMEOUT_SECONDS):
        assert reaper._timeout == reaper.RENDER_TIMEOUT_SECONDS
    assert reaper._timeout == reaper.REQUEST_TIMEOUT_SECONDS


# REAPER busy (a modal dialog): the handshake times out. That is not a
# rejected token, and reinstalling would not help.

@pytest.fixture
def mute_listener(monkeypatch):
    """Accepts connections and greets (greet[0]) or not, then stays silent."""
    listener = socket.create_server(("127.0.0.1", 0))
    greet = [False]
    stop = threading.Event()

    def serve():
        connection, _ = listener.accept()
        with connection:
            if greet[0]:
                connection.sendall(_frame(bridge.greeting("busy", b"n" * bridge.NONCE_BYTES)))
            stop.wait(5)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(reaper, "CONNECT_TIMEOUT_SECONDS", 0.3)
    yield listener.getsockname()[1], greet
    stop.set()
    thread.join()
    listener.close()


@pytest.mark.parametrize("greets", [False, True])
def test_a_bridge_that_never_answers_the_handshake_is_busy_not_refusing(mute_listener, greets):
    port, greet = mute_listener
    greet[0] = greets
    with pytest.raises(reaper.BridgeBusy):
        reaper._BridgeClient(port, "127.0.0.1", TOKEN)


def test_a_busy_reaper_says_to_close_its_dialog_not_to_reinstall(monkeypatch, disconnected):
    bridge_token.ensure_token()

    def busy(*args):
        raise reaper.BridgeBusy(reaper.BUSY_HINT)

    monkeypatch.setattr(reaper, "_BridgeClient", busy)
    with pytest.raises(RuntimeError) as raised:
        reaper.ensure_connected()
    message = str(raised.value)
    assert "dialog" in message
    assert "reaper-mcp-setup" not in message


def test_a_rejected_token_names_the_token_file(monkeypatch, disconnected):
    bridge_token.ensure_token()

    def reject(*args):
        raise reaper.TokenRejected("refused")

    monkeypatch.setattr(reaper, "_BridgeClient", reject)
    with pytest.raises(RuntimeError, match=str(bridge_token.token_path())):
        reaper.ensure_connected()


def test_a_half_built_client_is_closed_when_setting_up_reapy_fails(monkeypatch, disconnected):
    bridge_token.ensure_token()
    closed = []

    class Client:
        instance = "x"

        def __init__(self, *args):
            pass

        def close(self):
            closed.append(True)

    def reload(module):
        raise TimeoutError("timed out")

    monkeypatch.setattr(reaper, "_BridgeClient", Client)
    monkeypatch.setattr(reaper.importlib, "reload", reload)
    monkeypatch.setattr(reaper._machines, "CLIENT", None)
    monkeypatch.setattr(reaper._machines, "CLIENTS", {None: None})
    with pytest.raises(RuntimeError, match="dialog"):
        reaper.ensure_connected()
    assert closed == [True]


# REAPER restarted between tool calls: the old connection is dead.

def test_a_connection_reaper_closed_is_dropped_before_the_next_tool_call(bridge_server, monkeypatch):
    server, port = bridge_server
    client = reaper._BridgeClient(port, "127.0.0.1", TOKEN)
    monkeypatch.setattr(reaper._machines, "CLIENT", client)
    monkeypatch.setattr(reaper, "_connected", True)
    try:
        assert _wait_for(lambda: len(server.connections) == 1)
        reaper.start_tool_call()
        assert reaper._connected is True  # alive: kept, without a round trip
        for address in list(server.connections):
            server.disconnect(address)  # what REAPER quitting does
        assert _wait_for(client.peer_closed)
        reaper.start_tool_call()
        assert reaper._connected is False  # the next request reconnects
        assert reaper.lost_call() is None
    finally:
        client.close()
