"""The bridge's per-install secret token: where it lives, its permissions,
and that setup creates it once and keeps it."""

import stat

import pytest

from reaper_mcp import bridge_token

REAL_CONFIG_DIR = bridge_token.config_dir  # before the autouse fixture replaces it


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ensure_token_creates_a_256_bit_token_only_the_user_can_read():
    path, done = bridge_token.ensure_token()

    assert path == bridge_token.token_path()
    assert done.startswith("created the bridge token")
    text = path.read_text().strip()
    assert len(text) == 64 and int(text, 16) >= 0  # 32 random bytes, hex
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert bridge_token.read_token() == text.encode("ascii")
    assert [p.name for p in path.parent.iterdir()] == ["token"]  # no temporary left


def test_ensure_token_keeps_an_existing_token():
    path, _ = bridge_token.ensure_token()
    token = path.read_bytes()
    _, done = bridge_token.ensure_token()
    assert path.read_bytes() == token
    assert done.startswith("kept the bridge token")


def test_two_installs_get_different_tokens(tmp_path):
    first, _ = bridge_token.ensure_token(tmp_path / "a" / "token")
    second, _ = bridge_token.ensure_token(tmp_path / "b" / "token")
    assert first.read_bytes() != second.read_bytes()


def test_ensure_token_replaces_a_token_others_could_read():
    path, _ = bridge_token.ensure_token()
    token = path.read_bytes()
    path.chmod(0o644)
    path.parent.chmod(0o755)
    _, done = bridge_token.ensure_token()
    assert path.read_bytes() != token  # it may have been copied already
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert "because other users could read it" in done


def test_ensure_token_replaces_a_malformed_token():
    path = bridge_token.token_path()
    path.parent.mkdir(parents=True)
    path.write_text("not a token\n")
    path.chmod(0o600)
    _, done = bridge_token.ensure_token()
    assert done.startswith("replaced")
    assert len(bridge_token.read_token()) == 64


def test_read_token_without_a_file_says_to_run_setup():
    with pytest.raises(bridge_token.TokenError) as raised:
        bridge_token.read_token()
    assert "does not exist" in str(raised.value)
    assert "reaper-mcp-setup" in str(raised.value)


def test_read_token_refuses_a_file_others_can_read():
    path, _ = bridge_token.ensure_token()
    path.chmod(0o640)
    with pytest.raises(bridge_token.TokenError, match="other users"):
        bridge_token.read_token()


def test_read_token_refuses_a_malformed_file():
    path, _ = bridge_token.ensure_token()
    path.write_text("abc\n")
    with pytest.raises(bridge_token.TokenError, match="malformed"):
        bridge_token.read_token()


@pytest.mark.parametrize(("system", "env", "expected"), [
    ("Linux", {"XDG_CONFIG_HOME": "/x/conf"}, "HOME/.config/reaper-mcp"),
    ("Linux", {}, "HOME/.config/reaper-mcp"),
    ("Darwin", {}, "HOME/Library/Application Support/reaper-mcp"),
    ("Windows", {"APPDATA": "/x/AppData/Roaming"}, "/x/AppData/Roaming/reaper-mcp"),
])
def test_config_dir_per_system(monkeypatch, tmp_path, system, env, expected):
    monkeypatch.setattr(bridge_token, "config_dir", REAL_CONFIG_DIR)
    monkeypatch.setattr(bridge_token.platform, "system", lambda: system)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert str(bridge_token.config_dir()) == expected.replace("HOME", str(tmp_path))
