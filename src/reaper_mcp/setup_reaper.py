"""reaper-mcp-setup: let REAPER start the localhost-only bridge.

Run it once with REAPER closed (REAPER rewrites its .ini files on exit). It:
  1. writes Scripts/reaper_mcp_bridge.py, a launcher that puts this
     environment's packages first on REAPER's Python path and runs bridge.main();
  2. registers that script as an action in reaper-kb.ini;
  3. adds a marked block at the top of Scripts/__startup.lua that runs the
     action whenever REAPER starts;
  4. removes what reapy's configure_reaper()/enable_dist_api() left behind (its
     password-less web interface on port 2307, its server action and its saved
     action ID), since through those `import reapy` can start reapy's own
     server, which listens on every network interface;
  5. on Linux, points REAPER's ReaScript Python at this interpreter's libpython
     when Python is not configured yet;
  6. creates the bridge's secret token (bridge_token.py) in a file only the
     user can read, if there is no usable one yet. Running setup again keeps
     it, so a running MCP server goes on working.

It never enables reapy's distant API or REAPER's web interface, and
--uninstall removes what steps 1-3 added (the token file stays, harmless).
"""

import argparse
import hashlib
import os
import platform
import shlex
import sys
import sysconfig
from pathlib import Path

import psutil

from reaper_mcp import bridge_token

LAUNCHER_NAME = "reaper_mcp_bridge.py"
BEGIN = "-- BEGIN reaper-mcp bridge (added by reaper-mcp-setup)"
END = "-- END reaper-mcp bridge"
REAPY_ACTION_SCRIPT = "activate_reapy_server.py"
REAPY_WEB_INTERFACE_PORT = "2307"

LAUNCHER = '''\
# Starts the reaper-mcp bridge (127.0.0.1 only). Written by reaper-mcp-setup.
import site
import sys

PATHS = {paths!r}
TOKEN_FILE = {token_file!r}
# First on the path, so the pinned reapy wins over any other installed copy;
# addsitedir then reads the .pth files (the editable install's source folder).
sys.path[:0] = [p for p in PATHS if p not in sys.path]
for path in PATHS:
    site.addsitedir(path)

from reaper_mcp.bridge import main

main(token_file=TOKEN_FILE)
'''

STARTUP_BLOCK = f'''{BEGIN}
local reaper_mcp_bridge = reaper.NamedCommandLookup("_{{command_id}}")
if reaper_mcp_bridge ~= 0 then reaper.Main_OnCommand(reaper_mcp_bridge, 0) end
{END}
'''


class SetupError(Exception):
    pass


def default_resource_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "REAPER"
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "REAPER"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "REAPER"


def reaper_is_running() -> bool:
    for process in psutil.process_iter(["name"]):
        name = (process.info["name"] or "").lower()
        if name in {"reaper", "reaper.exe"}:
            return True
    return False


def command_id(launcher: Path) -> str:
    """A stable action ID in REAPER's format: "RS" and 40 hex digits."""
    return "RS" + hashlib.sha1(str(launcher).encode()).hexdigest()


# REAPER's files are UTF-8, sometimes with a BOM; anything undecodable is kept
# byte for byte rather than failing halfway through setup.
def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="surrogateescape")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", errors="surrogateescape")


def _site_paths() -> list[str]:
    paths = [sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]]
    return list(dict.fromkeys(paths))


def _strip_block(text: str) -> str:
    start = text.find(BEGIN)
    if start == -1:
        return text
    end = text.find(END, start)
    if end == -1:
        raise SetupError(f"__startup.lua has '{BEGIN}' but no '{END}'; fix it by hand")
    newline = text.find("\n", end)
    return text[:start] + ("" if newline == -1 else text[newline + 1:])


def _kb_without(kb_text: str, *, scripts: tuple[str, ...]) -> list[str]:
    """reaper-kb.ini without the SCR actions running these scripts or keys bound to them."""
    lines = kb_text.splitlines()
    ids = set()
    for line in lines:
        if line.startswith("SCR ") and any(script in line for script in scripts):
            parts = line.split()
            if len(parts) > 3:
                ids.add(parts[3])
    return [line for line in lines
            if not (line.startswith("SCR ") and any(script in line for script in scripts))
            and not (line.startswith("KEY ") and any(f"_{i}" in line.split() for i in ids))]


def _ini_section_get(lines: list[str], section: str, key: str) -> str | None:
    current = None
    for line in lines:
        if line.startswith("[") and line.rstrip().endswith("]"):
            current = line.strip()[1:-1]
        elif current == section and line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


def _ini_section_set(lines: list[str], *, section: str, key: str, value: str) -> list[str]:
    out, current, done = [], None, False
    for line in lines:
        if line.startswith("[") and line.rstrip().endswith("]"):
            if current == section and not done:
                out.append(f"{key}={value}")
                done = True
            current = line.strip()[1:-1]
        elif current == section and line.startswith(key + "="):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        if current != section:
            out.append(f"[{section}]")
        out.append(f"{key}={value}")
    return out


def _ini_drop_section(text: str, section: str) -> str:
    out, skipping = [], False
    for line in text.splitlines():
        if line.startswith("[") and line.rstrip().endswith("]"):
            skipping = line.strip()[1:-1].lower() == section.lower()
        if not skipping:
            out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def _web_interface(value: str) -> tuple[str, str] | None:
    """(port, "user:password") for a csurf line that is a web interface."""
    try:
        fields = shlex.split(value)
    except ValueError:
        return None
    if len(fields) < 4 or fields[0] != "HTTP":
        return None
    return fields[2], fields[3]


def _remove_reapy_web_interface(lines: list[str], report: list[str]) -> list[str]:
    """Drop the password-less interface reapy adds on 2307, renumbering csurf_N."""
    count = int(_ini_section_get(lines, "reaper", "csurf_cnt") or "0")
    entries = [_ini_section_get(lines, "reaper", f"csurf_{i}") or "" for i in range(count)]
    kept = [e for e in entries if _web_interface(e) != (REAPY_WEB_INTERFACE_PORT, "")]
    if len(kept) == len(entries):
        return lines
    lines = [line for line in lines if not line.startswith("csurf_")]
    lines = _ini_section_set(lines, section="reaper", key="csurf_cnt", value=str(len(kept)))
    for i, entry in enumerate(kept):
        lines = _ini_section_set(lines, section="reaper", key=f"csurf_{i}", value=entry)
    report.append("removed reapy's password-less web interface (port 2307)")
    return lines


def _passwordless_web_interfaces(lines: list[str]) -> list[str]:
    count = int(_ini_section_get(lines, "reaper", "csurf_cnt") or "0")
    found = []
    for i in range(count):
        interface = _web_interface(_ini_section_get(lines, "reaper", f"csurf_{i}") or "")
        if interface and not interface[1]:
            found.append(interface[0])
    return found


def _configure_reaper_ini(resource: Path, report: list[str]) -> None:
    ini = resource / "reaper.ini"
    if not ini.exists():
        report.append("reaper.ini not found; set REAPER's Python in Preferences > Plug-ins > ReaScript")
        return
    original = _read(ini)
    lines = _remove_reapy_web_interface(original.splitlines(), report)
    if platform.system() == "Linux":
        lines = _configure_linux_python(lines, report)
    for port in _passwordless_web_interfaces(lines):
        report.append(f"WARNING: REAPER has a web interface on port {port} with no password; "
                      "anyone on your network can control REAPER through it. Add a password "
                      "in Preferences > Control/OSC/web.")
    text = "\n".join(lines) + "\n"
    if text != original:
        _write(ini, text)


def _configure_linux_python(lines: list[str], report: list[str]) -> list[str]:
    if _ini_section_get(lines, "reaper", "pythonlibdll64"):
        report.append("REAPER's Python is already configured; left as is")
        return lines
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    library = sysconfig.get_config_var("INSTSONAME") or ""
    if not (libdir and library and (Path(libdir) / library).exists()):
        report.append("could not find libpython; set it in Preferences > Plug-ins > ReaScript")
        return lines
    for key, value in (("pythonlibpath64", libdir), ("pythonlibdll64", library), ("reascript", "1")):
        lines = _ini_section_set(lines, section="reaper", key=key, value=value)
    report.append(f"REAPER's Python set to {Path(libdir) / library}")
    return lines


def _remove_reapy_leftovers(resource: Path, report: list[str]) -> None:
    extstate = resource / "reaper-extstate.ini"
    text = _read(extstate)
    if "[reapy]" in text.lower():
        _write(extstate, _ini_drop_section(text, "reapy"))
        report.append("removed reapy's saved server action from reaper-extstate.ini")


def install(resource: Path, token_file: Path | None = None) -> list[str]:
    """token_file: where the bridge token lives (default: bridge_token.token_path())."""
    report: list[str] = []
    scripts = resource / "Scripts"
    startup = scripts / "__startup.lua"
    startup_text = _strip_block(_read(startup))  # fails before anything is written

    try:
        token_file, done = bridge_token.ensure_token(token_file)
    except OSError as e:
        where = token_file or bridge_token.token_path()
        raise SetupError(
            f"cannot write the bridge token {where} ({e}); make sure its folder "
            f"{where.parent} belongs to you (it may have been created with sudo)") from e
    report.append(done)

    scripts.mkdir(parents=True, exist_ok=True)
    launcher = scripts / LAUNCHER_NAME
    _write(launcher, LAUNCHER.format(paths=_site_paths(), token_file=str(token_file)))
    report.append(f"wrote {launcher}")

    action = command_id(launcher)
    kb = resource / "reaper-kb.ini"
    kb_text = _read(kb)
    kb_lines = _kb_without(kb_text, scripts=(str(launcher), REAPY_ACTION_SCRIPT))
    if any(REAPY_ACTION_SCRIPT in line for line in kb_text.splitlines()):
        report.append("removed reapy's server action from reaper-kb.ini")
    kb_lines.append(f'SCR 4 0 {action} "Custom: {LAUNCHER_NAME}" "{launcher}"')
    _write(kb, "\n".join(kb_lines) + "\n")
    report.append(f"registered action _{action}")

    # First in the file: a `return` in the user's own startup code would
    # otherwise make a block after it a Lua syntax error.
    _write(startup, STARTUP_BLOCK.format(command_id=action) + startup_text)
    report.append(f"REAPER will start the bridge from {startup}")

    _remove_reapy_leftovers(resource, report)
    _configure_reaper_ini(resource, report)
    return report


def uninstall(resource: Path) -> list[str]:
    report: list[str] = []
    scripts = resource / "Scripts"
    launcher = scripts / LAUNCHER_NAME
    startup = scripts / "__startup.lua"
    if startup.exists():
        text = _strip_block(_read(startup))
        if text.strip():
            _write(startup, text)
        else:
            startup.unlink()
        report.append(f"removed the bridge from {startup}")
    kb = resource / "reaper-kb.ini"
    if kb.exists():
        _write(kb, "\n".join(_kb_without(_read(kb), scripts=(str(launcher),))) + "\n")
        report.append("unregistered the bridge action")
    if launcher.exists():
        launcher.unlink()
        report.append(f"deleted {launcher}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="reaper-mcp-setup",
        description="Let REAPER start the reaper-mcp bridge (localhost only).")
    parser.add_argument("--resource-path", type=Path, default=None,
                        help="REAPER's resource folder (default: the standard location)")
    parser.add_argument("--uninstall", action="store_true", help="remove what setup added")
    args = parser.parse_args()

    resource = (args.resource_path or default_resource_path()).expanduser()
    if not resource.is_dir():
        print(f"REAPER resource folder not found: {resource}", file=sys.stderr)
        return 1
    if reaper_is_running():
        print("Close REAPER first: it rewrites its settings when it quits.", file=sys.stderr)
        return 1
    try:
        report = uninstall(resource) if args.uninstall else install(resource)
    except SetupError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(report))
    if not args.uninstall:
        print("Done. Start REAPER; the bridge listens on 127.0.0.1 only and "
              "answers only clients that know the token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
