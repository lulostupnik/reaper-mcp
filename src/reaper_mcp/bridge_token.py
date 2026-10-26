"""The per-install secret the MCP server shows the bridge before any request.

reaper-mcp-setup creates it once: 256 random bits, hex-encoded, in a file only
the user can read. The bridge (inside REAPER) and the MCP server both read the
same file, so another program or user on the computer that can reach the
bridge's port still cannot drive REAPER without it.

Only the standard library here: the bridge imports this inside REAPER.
"""

import os
import platform
import secrets
import stat
from pathlib import Path

TOKEN_BYTES = 32  # 256 bits
TOKEN_FILE_NAME = "token"
SETUP_HINT = "close REAPER, run reaper-mcp-setup once, then start REAPER again"


class TokenError(Exception):
    """The token file is missing, unreadable, malformed or readable by others."""


def config_dir() -> Path:
    """reaper-mcp's folder in the user's own configuration directory.

    On Linux always ~/.config, not $XDG_CONFIG_HOME: setup (run from a
    terminal) and the MCP server (started by an MCP client, maybe with
    another environment) must agree on it."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "reaper-mcp"
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "reaper-mcp"
    return Path.home() / ".config" / "reaper-mcp"


def token_path() -> Path:
    return config_dir() / TOKEN_FILE_NAME


def _valid(text: str) -> bool:
    return len(text) == TOKEN_BYTES * 2 and all(c in "0123456789abcdef" for c in text)


def _private(path: Path) -> bool:
    """True when no one but the owner can read or write path (always on Windows,
    where the per-user APPDATA folder is what keeps it private)."""
    if os.name != "posix":
        return True
    return not path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)


def read_token(path: Path | None = None) -> bytes:
    """The token in path (default: token_path()), as the bytes sent on the wire."""
    path = path or token_path()
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise TokenError(f"the bridge token file {path} does not exist; {SETUP_HINT}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise TokenError(f"cannot read the bridge token file {path} ({e}); {SETUP_HINT}") from e
    if not _valid(text):
        raise TokenError(f"the bridge token file {path} is malformed; {SETUP_HINT}")
    if not _private(path):
        raise TokenError(f"the bridge token file {path} can be read by other users; {SETUP_HINT}")
    return text.encode("ascii")


def _write_private(path: Path, text: str) -> None:
    """Write text to path, created with mode 0600 and swapped in whole."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as file:
            file.write(text + "\n")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def ensure_token(path: Path | None = None) -> tuple[Path, str]:
    """Create the token file if it is missing or unusable; keep a good one.
    A token other users could read may already be copied, so it is replaced,
    not just made private. Returns the path and what was done, for setup's
    report. Raises OSError when the folder or file cannot be written."""
    path = path or token_path()
    if path.parent.is_dir() and os.name == "posix":
        path.parent.chmod(0o700)
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        text = None
    except (OSError, UnicodeDecodeError):
        text = ""
    if text is not None and _valid(text) and _private(path):
        return path, f"kept the bridge token {path}"
    _write_private(path, secrets.token_hex(TOKEN_BYTES))
    if text is None:
        return path, f"created the bridge token {path} (readable only by you)"
    if _valid(text):
        return path, f"replaced the bridge token {path} because other users could read it"
    return path, f"replaced the unusable bridge token {path}"
