import functools
import inspect
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer

from reaper_mcp import (
    analysis_tools,
    audio_tools,
    context_tools,
    fx_tools,
    mastering_tools,
    midi_tools,
    mixing_tools,
    project_template_tools,
    project_tools,
    reaper,
    record_setup_tools,
    recording_tools,
    region_render,
    render_tools,
    template_tools,
    track_tools,
    transport_tools,
    undo_tools,
)
from reaper_mcp.undo import as_undo_step, kind_of

logger = logging.getLogger("reaper_mcp.server")

_server = MCPServer("reaper-mcp")

# reapy drives REAPER over a single shared socket: reapy.tools.network.Client
# keeps one module-global connection and its request() does an unguarded
# send-then-recv round trip. Two concurrent calls interleave their length
# prefixes and payloads on that socket and can read each other's replies.
#
# Under the old mcp.server.fastmcp this was unreachable, because v1 invoked
# sync tool functions inline on the event loop. MCP v2 dispatches them through
# anyio.to_thread.run_sync instead, so any client issuing parallel tool calls
# would run two of our tools on different worker threads at once. Every tool
# here is a sync def that ends up in reapy, so serialize them all behind one
# lock. This matches the effective v1 behaviour (one REAPER call at a time)
# without blocking the event loop.
_reaper_lock = threading.Lock()


def _serialized(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Run the tool under the REAPER lock, reporting failure in the result.

    A tool's result is a dict with "success"; an exception becomes
    {"success": False, "error": str(e)} instead of an MCP error, which is the
    contract every tool here has always had. functools.wraps keeps the
    signature, which the SDK reads to build the tool's input schema.

    When a call to REAPER timed out or its connection dropped, the result says
    the outcome is uncertain, whatever the tool itself returned (it may have
    caught the error and carried on); what it returned is kept as
    partial_result.
    """
    kind = getattr(fn, "_tool_kind", None)
    read_only = bool(kind and kind.annotations.read_only_hint)

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> dict:
        with _reaper_lock:
            reaper.start_tool_call()
            partial = None
            try:
                result = partial = fn(*args, **kwargs)
            except Exception as e:
                logger.error("%s failed: %s", fn.__name__, e)
                result = {"success": False, "error": str(e)}
            lost = reaper.lost_call()
            if lost is None:
                return result
            return uncertain_result(lost, read_only=read_only, partial=partial)

    return run


def uncertain_result(lost: reaper.OutcomeUncertain, *, read_only: bool = False,
                     partial: dict | None = None) -> dict:
    result = {"success": False, "outcome": "uncertain", "error": str(lost),
              "next_step": reaper.uncertain_next_step(lost, read_only=read_only)}
    if partial is not None:
        result["partial_result"] = partial
    return result


mcp = _server

for _module in (
    context_tools,
    undo_tools,
    project_tools,
    track_tools,
    record_setup_tools,
    recording_tools,
    transport_tools,
    midi_tools,
    fx_tools,
    audio_tools,
    mixing_tools,
    render_tools,
    region_render,
    mastering_tools,
    analysis_tools,
    template_tools,
    project_template_tools,
):
    for _tool in _module.TOOLS:
        # cleandoc: the SDK sends the docstring verbatim, source indentation included.
        # Project-changing tools run as one named undo step, inside the lock.
        _server.tool(
            description=inspect.cleandoc(_tool.__doc__ or ""),
            annotations=kind_of(_tool).annotations,
        )(_serialized(as_undo_step(_tool)))


@_server.resource(
    "reaper://project/summary",
    name="project_summary",
    description="The current REAPER project at a glance; the same as get_project_summary.",
    mime_type="application/json",
)
def project_summary_resource() -> str:
    return json.dumps(_serialized(context_tools.get_project_summary)())
