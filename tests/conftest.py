import json

import anyio
import pytest
from mcp.client import Client
from mcp.types import TextContent

from reaper_mcp import bridge_token
from reaper_mcp import reaper as reaper_module
from reaper_mcp.server import mcp
from tests.fake_reaper import FakeReaper, install

pytest_plugins = ["tests.live.fixtures"]


def pytest_addoption(parser):
    parser.addoption(
        "--live", action="store_true",
        help="also run tests marked `live` against a running REAPER; unreachable REAPER fails",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs a running REAPER; selected with --live")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="needs a running REAPER; run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def private_state(request, monkeypatch, tmp_path):
    """Keeps every test away from the user's real bridge token (unless --live,
    which needs it to reach REAPER) and from the connection state an earlier
    test left behind."""
    if not request.config.getoption("--live"):
        monkeypatch.setattr(bridge_token, "config_dir", lambda: tmp_path / "reaper-mcp-config")
        monkeypatch.setattr(reaper_module, "_connected", False)
        monkeypatch.setattr(reaper_module, "_bridge_instance", None)
    monkeypatch.setattr(reaper_module, "_lost", None)
    monkeypatch.setattr(reaper_module, "_owed", [])
    monkeypatch.setattr(reaper_module, "_owed_to", None)
    monkeypatch.setattr(reaper_module, "_timeout", reaper_module.REQUEST_TIMEOUT_SECONDS)


@pytest.fixture
def reaper(monkeypatch, tmp_path):
    """A fake REAPER behind reapy, so the tools run end to end without one.
    Every test also checks that undo blocks and UI-refresh holds were balanced."""
    fake: FakeReaper = install(monkeypatch, tmp_path)
    # documents_dir reads XDG's user-dirs.dirs under $HOME, which tests point at tmp.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    yield fake
    assert fake.undo_depth == 0, "an undo block was left open"
    assert fake.ui_refresh_prevented == 0, "PreventUIRefresh was left on"
    assert not fake.held, "REAPER's main loop was left held"


async def _call(name: str, arguments: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    # Tools are annotated `-> dict`, which the SDK serializes as one JSON text
    # block rather than structured content.
    [block] = result.content
    assert isinstance(block, TextContent), block
    return json.loads(block.text)


def _call_tool(tool: str, /, **arguments) -> dict:
    # Positional-only, so a tool argument called `name` cannot collide with it.
    return anyio.run(_call, tool, arguments)


@pytest.fixture
def call():
    """call(tool, **arguments) -> the tool's result dict, through the MCP client."""
    return _call_tool
