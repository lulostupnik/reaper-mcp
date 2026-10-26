"""reaper-mcp-setup against a throwaway REAPER resource folder."""

import socket
import threading
import time

import pytest

from reaper_mcp import bridge, bridge_token, setup_reaper

TOKEN = b"0" * 64


@pytest.fixture
def resource(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_reaper.platform, "system", lambda: "Linux")
    (tmp_path / "Scripts").mkdir()
    (tmp_path / "reaper.ini").write_text("[reaper]\nlinux_audio_mode=1\n\n[verchk]\nlastt=1\n")
    (tmp_path / "reaper-kb.ini").write_text('KEY 1 65 40001 0\n')
    (tmp_path / "Scripts" / "__startup.lua").write_text('reaper.ShowConsoleMsg("mine")\n')
    return tmp_path


def test_install_is_idempotent_and_keeps_existing_settings(resource):
    setup_reaper.install(resource)
    setup_reaper.install(resource)
    launcher = resource / "Scripts" / setup_reaper.LAUNCHER_NAME
    action = setup_reaper.command_id(launcher)

    assert "from reaper_mcp.bridge import main" in launcher.read_text()
    kb = (resource / "reaper-kb.ini").read_text().splitlines()
    assert kb[0] == "KEY 1 65 40001 0"
    assert [line for line in kb if action in line] == [
        f'SCR 4 0 {action} "Custom: {setup_reaper.LAUNCHER_NAME}" "{launcher}"'
    ]
    startup = (resource / "Scripts" / "__startup.lua").read_text()
    assert startup.startswith(setup_reaper.BEGIN)  # before any `return` in the user's code
    assert startup.endswith('reaper.ShowConsoleMsg("mine")\n')
    assert startup.count(setup_reaper.BEGIN) == 1
    assert f'NamedCommandLookup("_{action}")' in startup
    ini = (resource / "reaper.ini").read_text()
    assert "csurf" not in ini  # never adds a web interface
    assert "[verchk]\nlastt=1" in ini


def test_install_creates_the_token_once_and_the_launcher_names_its_file(resource):
    first = setup_reaper.install(resource)
    token = bridge_token.read_token()
    second = setup_reaper.install(resource)

    assert bridge_token.read_token() == token  # a running MCP server keeps working
    assert any(line.startswith("created the bridge token") for line in first)
    assert any(line.startswith("kept the bridge token") for line in second)
    launcher = (resource / "Scripts" / setup_reaper.LAUNCHER_NAME).read_text()
    assert f"TOKEN_FILE = {str(bridge_token.token_path())!r}" in launcher
    assert "main(token_file=TOKEN_FILE)" in launcher


def test_a_token_folder_setup_cannot_write_is_a_clear_setup_error(resource, monkeypatch):
    def denied(path=None):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(bridge_token, "ensure_token", denied)
    with pytest.raises(setup_reaper.SetupError, match="belongs to you"):
        setup_reaper.install(resource)


def test_uninstall_leaves_the_token(resource):
    setup_reaper.install(resource)
    token = bridge_token.read_token()
    setup_reaper.uninstall(resource)
    assert bridge_token.read_token() == token


def test_install_leaves_a_configured_python_alone(resource):
    ini = resource / "reaper.ini"
    ini.write_text("[reaper]\npythonlibdll64=libpython3.11.so\n")
    setup_reaper.install(resource)
    assert ini.read_text() == "[reaper]\npythonlibdll64=libpython3.11.so\n"


def test_uninstall_removes_only_what_setup_added(resource):
    before_kb = (resource / "reaper-kb.ini").read_text()
    setup_reaper.install(resource)
    setup_reaper.uninstall(resource)
    assert not (resource / "Scripts" / setup_reaper.LAUNCHER_NAME).exists()
    assert (resource / "Scripts" / "__startup.lua").read_text() == 'reaper.ShowConsoleMsg("mine")\n'
    assert (resource / "reaper-kb.ini").read_text() == before_kb


def test_uninstall_deletes_a_startup_file_it_created(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_reaper.platform, "system", lambda: "Darwin")
    setup_reaper.install(tmp_path)
    setup_reaper.uninstall(tmp_path)
    assert not (tmp_path / "Scripts" / "__startup.lua").exists()


def test_removes_what_reapy_configure_left_behind(resource):
    (resource / "reaper.ini").write_text(
        "\ufeff[reaper]\ncsurf_cnt=2\ncsurf_0=OSC 0 8000 ''\n"
        "csurf_1=HTTP 0 2307 '' 'index.html' 0 ''\n")
    (resource / "reaper-kb.ini").write_text(
        'SCR 4 0 RSabc "Custom: activate_reapy_server.py" "/x/reapy/reascripts/'
        'activate_reapy_server.py"\nKEY 1 65 _RSabc 0\nKEY 1 66 40001 0\n')
    (resource / "reaper-extstate.ini").write_text(
        "[reapy]\nactivate_reapy_server=_RSabc\n[other]\nkeep=1\n")
    report = setup_reaper.install(resource)

    ini = (resource / "reaper.ini").read_text()
    assert ini.count("[reaper]") == 1  # the BOM did not hide the section
    assert "2307" not in ini and "csurf_cnt=1" in ini and "csurf_0=OSC 0 8000 ''" in ini
    kb = (resource / "reaper-kb.ini").read_text()
    assert "activate_reapy_server" not in kb and "_RSabc" not in kb
    assert "KEY 1 66 40001 0" in kb
    assert (resource / "reaper-extstate.ini").read_text() == "[other]\nkeep=1\n"
    assert not any("WARNING" in line for line in report)


def test_warns_about_another_passwordless_web_interface(resource):
    (resource / "reaper.ini").write_text(
        "[reaper]\npythonlibdll64=x\ncsurf_cnt=2\n"
        "csurf_0=HTTP 0 8080 '' 'index.html' 0 ''\n"
        "csurf_1=HTTP 0 9090 'me:secret' 'index.html' 0 ''\n")
    report = setup_reaper.install(resource)
    warnings = [line for line in report if "WARNING" in line]
    assert len(warnings) == 1 and "8080" in warnings[0]
    assert "csurf_0=HTTP 0 8080" in (resource / "reaper.ini").read_text()  # the user's own


def test_uninstall_removes_key_bindings_to_the_bridge(resource):
    setup_reaper.install(resource)
    action = setup_reaper.command_id(resource / "Scripts" / setup_reaper.LAUNCHER_NAME)
    with (resource / "reaper-kb.ini").open("a") as kb:
        kb.write(f"KEY 1 70 _{action} 0\n")
    setup_reaper.uninstall(resource)
    assert action not in (resource / "reaper-kb.ini").read_text()


def test_a_broken_startup_block_stops_setup_before_writing(resource):
    startup = resource / "Scripts" / "__startup.lua"
    startup.write_text(setup_reaper.BEGIN + "\nmine()\n")
    with pytest.raises(setup_reaper.SetupError):
        setup_reaper.install(resource)
    assert startup.read_text() == setup_reaper.BEGIN + "\nmine()\n"
    assert not (resource / "Scripts" / setup_reaper.LAUNCHER_NAME).exists()


def test_bridge_listens_on_loopback_only():
    server = bridge.LocalServer(0, TOKEN)
    try:
        host, port = server._socket.getsockname()
        assert host == "127.0.0.1"
    finally:
        server.close()


def test_bridge_drops_an_oversized_request():
    listener = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(listener.getsockname())
    accepted, _ = listener.accept()
    try:
        connection = bridge._BoundedSocket(accepted)
        client.sendall((bridge.MAX_REQUEST_BYTES + 1).to_bytes(8, "little"))
        with pytest.raises(ConnectionAbortedError):
            connection.recv(timeout=1)
    finally:
        for s in (client, accepted, listener):
            s.close()


@pytest.fixture
def tcp_pair():
    listener = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(listener.getsockname())
    accepted, _ = listener.accept()
    yield client, accepted
    for s in (client, accepted, listener):
        s.close()


def test_bridge_reads_a_body_that_arrives_in_pieces(tcp_pair):
    client, accepted = tcp_pair
    connection = bridge._BoundedSocket(accepted)
    body = b"x" * 200

    def send_slowly():
        client.sendall(len(body).to_bytes(8, "little") + body[:100])
        time.sleep(0.3)
        client.sendall(body[100:])

    sender = threading.Thread(target=send_slowly)
    sender.start()
    assert connection.recv(timeout=1) == body
    sender.join()


def test_bridge_drops_a_client_that_stalls_mid_request(tcp_pair, monkeypatch):
    monkeypatch.setattr(bridge, "IO_TIMEOUT_SECONDS", 0.2)
    client, accepted = tcp_pair
    client.sendall((10).to_bytes(8, "little") + b"abc")
    with pytest.raises(ConnectionAbortedError):
        bridge._BoundedSocket(accepted).recv(timeout=1)


def test_bridge_disconnects_a_malformed_request_and_keeps_serving():
    server = bridge.LocalServer(0, TOKEN)
    port = server._socket.getsockname()[1]
    try:
        bad = socket.create_connection(("127.0.0.1", port))
        deadline = time.monotonic() + 2
        while not server.connections and time.monotonic() < deadline:
            server.accept()
        bad.recv(100)  # the address greeting
        bad.sendall((4).to_bytes(8, "little") + b"\xff{]x")
        time.sleep(0.1)
        requests = server.get_requests()
        server.send_results(server.process_requests(requests))
        assert server.connections == {}
        bad.close()
    finally:
        server.close()
