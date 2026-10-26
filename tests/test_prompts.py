"""The beginner guides served as MCP prompts: listed, readable, and naming
only tools that exist."""

import re

import anyio
from mcp.client import Client

from reaper_mcp.server import mcp

EXPECTED = {"record_vocals_scarlett_solo", "gain_staging_check", "critique_my_mix",
            "basic_vocal_chain"}


async def _prompts() -> tuple[list, dict[str, str], set[str]]:
    async with Client(mcp) as client:
        listed = (await client.list_prompts()).prompts
        texts = {}
        for prompt in listed:
            result = await client.get_prompt(prompt.name)
            [message] = result.messages
            assert message.role == "user"
            texts[prompt.name] = message.content.text
        tools = {tool.name for tool in (await client.list_tools()).tools}
    return listed, texts, tools


def test_prompts_are_listed_with_descriptions():
    listed, _, _ = anyio.run(_prompts)
    assert {p.name for p in listed} == EXPECTED
    assert all(p.description and not p.arguments for p in listed)


def test_prompts_name_only_real_tools():
    _, texts, tools = anyio.run(_prompts)
    for name, text in texts.items():
        named = set(re.findall(r"`([a-z_]+)`", text))
        assert named, name
        assert named <= tools, (name, named - tools)


def test_prompts_stay_short():
    _, texts, _ = anyio.run(_prompts)
    assert all(len(text) < 2500 for text in texts.values())
