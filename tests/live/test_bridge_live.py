"""The bridge's token and handshake against the bridge running in REAPER."""

import socket
import stat
import time

import pytest

from reaper_mcp import bridge, bridge_token
from reaper_mcp.reaper import TokenRejected, _BridgeClient

pytestmark = pytest.mark.live


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_token_is_private_to_the_user():
    path = bridge_token.token_path()
    assert (mode(path.parent), mode(path)) == (0o700, 0o600)
    assert len(bridge_token.read_token()) == bridge_token.TOKEN_BYTES * 2


def test_a_wrong_token_is_disconnected_and_reaper_keeps_serving(live_project, call):
    with pytest.raises(TokenRejected):
        _BridgeClient(bridge.PORT, bridge.HOST, b"0" * (bridge_token.TOKEN_BYTES * 2))
    assert call("get_context")["success"] is True


def test_a_client_stuck_half_way_through_the_handshake_does_not_slow_reaper(live_project, call):
    stuck = socket.create_connection((bridge.HOST, bridge.PORT), timeout=5)
    try:
        stuck.settimeout(5)
        length = int.from_bytes(stuck.recv(8), "little")
        bridge.parse_greeting(stuck.recv(length))
        stuck.sendall(bridge.AUTH_BYTES.to_bytes(8, "little") + b"x" * 10)  # then nothing
        started = time.monotonic()
        for _ in range(5):
            call("get_context")
        assert time.monotonic() - started < 5.0
        # Dropped once HANDSHAKE_TIMEOUT_SECONDS pass without the rest.
        stuck.settimeout(bridge.HANDSHAKE_TIMEOUT_SECONDS + 5)
        assert stuck.recv(1) == b""
    finally:
        stuck.close()
    assert call("get_context")["success"] is True
