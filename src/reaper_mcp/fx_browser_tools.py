"""The plugins installed in REAPER, read from the plugin caches REAPER keeps in
its resource folder (EnumInstalledFX hands its names back through const char**
out-parameters, which REAPER's Python binding does not return reliably, so the
cache files are read instead).

- reaper-vstplugins*.ini: ``file=id,id,Name (Vendor)``, instruments end in
  ``!!!VSTi``; VST3 files end in .vst3.
- reaper-jsfx.ini: ``NAME path "JS: Description"``; added by "JS:path".
- reaper-clap-*.ini: one section per file, ``plugin.id=flags|Name (Vendor)``.
- reaper-auplugins*.ini (macOS): ``Vendor: Name=...``.
"""

import difflib
import shlex
from pathlib import Path

from reaper_mcp.reaper import RPR
from reaper_mcp.undo import read_only

INSTRUMENT_MARK = "!!!VSTi"
MIN_SCORE = 0.6
DEFAULT_LIMIT = 50


def _plugin(name: str, kind: str, *, instrument: bool | None, add_name: str) -> dict:
    return {"name": name, "type": kind, "instrument": instrument, "add_fx_name": add_name}


def _vst(path: Path) -> list[dict]:
    plugins = []
    for line in path.read_text(errors="replace").splitlines():
        key, _, value = line.partition("=")
        parts = value.split(",", 2)
        if line.startswith("[") or len(parts) < 3 or not parts[2].strip():
            continue
        name = parts[2].strip()
        instrument = name.endswith(INSTRUMENT_MARK)
        name = name.removesuffix(INSTRUMENT_MARK).strip()
        kind = "VST3" if key.lower().endswith(".vst3") else "VST"
        plugins.append(_plugin(name, kind + ("i" if instrument else ""), instrument=instrument,
                               add_name=f"{kind}: {name}"))
    return plugins


def _jsfx(path: Path) -> list[dict]:
    plugins = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("NAME "):
            continue
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) < 3:
            continue
        name = fields[2].removeprefix("JS:").strip()
        plugins.append(_plugin(name, "JS", instrument=False, add_name=f"JS:{fields[1]}"))
    return plugins


def _clap(path: Path) -> list[dict]:
    plugins = []
    for line in path.read_text(errors="replace").splitlines():
        key, _, value = line.partition("=")
        if line.startswith("[") or key == "_" or "|" not in value:
            continue
        name = value.split("|", 1)[1].strip()
        if name:
            # The flags before "|" are not documented: whether it is an
            # instrument is unknown (None), not False.
            plugins.append(_plugin(name, "CLAP", instrument=None, add_name=name))
    return plugins


def _au(path: Path) -> list[dict]:
    plugins = []
    for line in path.read_text(errors="replace").splitlines():
        key, _, _ = line.partition("=")
        if line.startswith("[") or ":" not in key:
            continue
        name = key.strip()
        plugins.append(_plugin(name, "AU", instrument=False, add_name=f"AU: {name}"))
    return plugins


READERS = (("reaper-vstplugins*.ini", _vst), ("reaper-jsfx.ini", _jsfx),
           ("reaper-clap*.ini", _clap), ("reaper-auplugins*.ini", _au))


def installed_plugins(resource: Path) -> list[dict]:
    """Every plugin in REAPER's caches under resource, each once."""
    plugins, seen = [], set()
    for pattern, reader in READERS:
        for path in sorted(resource.glob(pattern)):
            for plugin in reader(path):
                key = (plugin["type"], plugin["name"].casefold())
                if key not in seen:
                    seen.add(key)
                    plugins.append(plugin)
    return plugins


def score(query: str, name: str) -> float:
    """How well name matches query: above 1 when it contains the query, 0.9
    when it contains all of its words, else the closest word's similarity."""
    query, name = query.casefold().strip(), name.casefold()
    if query in name:
        return 1.5 if name.startswith(query) else 1.2
    words = query.split()
    if words and all(word in name for word in words):
        return 0.9
    candidates = name.replace("(", " ").replace(")", " ").replace("/", " ").split()
    return max((difflib.SequenceMatcher(None, query, word).ratio() for word in candidates),
               default=0.0)


@read_only
def list_installed_fx(
    *, query: str = "", instruments_only: bool = False, limit: int = DEFAULT_LIMIT
) -> dict:
    """
    Find installed plugins (VST, VST3, JS, CLAP, AU) by their name, tolerating
    typos: query="reacomp" or "valhala" finds them. It matches names only, not
    categories: "comp" finds plugins with "comp" in the name, but "eq" will not
    find an EQ called "Pro-Q". Empty query lists them all (up to limit).
    instrument is true/false for VST, VST3 and AU, null (unknown) for CLAP, false
    for JS; instruments_only keeps true and null. LV2 plugins are not listed, nor
    plugins REAPER has not scanned yet (Options > Preferences > Plug-ins).
    Pass a result's add_fx_name to add_fx.
    """
    resource = Path(RPR.GetResourcePath())
    plugins = installed_plugins(resource)
    if instruments_only:
        plugins = [p for p in plugins if p["instrument"] is not False]
    if query.strip():
        scored = [(score(query, p["name"]), p) for p in plugins]
        plugins = [p for s, p in sorted(scored, key=lambda sp: (-sp[0], sp[1]["name"].casefold()))
                   if s >= MIN_SCORE]
    else:
        plugins = sorted(plugins, key=lambda p: p["name"].casefold())
    result = {"success": True, "count": len(plugins), "plugins": plugins[:max(limit, 0)]}
    if len(plugins) > limit:
        result["note"] = f"Showing {limit} of {len(plugins)}; narrow the query or raise limit"
    if not plugins and not query.strip():
        result["note"] = ("No plugin caches found in REAPER's resource folder "
                          f"({resource}); REAPER writes them when it scans plugins")
    return result


TOOLS = (list_installed_fx,)
