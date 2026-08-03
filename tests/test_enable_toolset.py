"""The one-click capability unlock.

When a task needs something the user switched off, ArcBot must ask rather than
fail or improvise — and on approval the new tools have to be usable *in the same
turn*, or the flow is just a slower failure.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from arcbot.agent import Agent
from arcbot.asks import AskResult
from arcbot.events import Ask
from arcbot.providers.base import Chunk, Provider, ToolCall, Usage


class ScriptedProvider(Provider):
    mode = "model"
    context_window = 200_000

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.tools_offered: list[list[str]] = []

    async def stream(self, messages, tools, *, system=""):
        self.tools_offered.append([t["name"] for t in tools])
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        for chunk in step:
            yield chunk
        yield Chunk(usage=Usage(5, 5), done=True, stop_reason="end_turn")


def call(call_id, name, **args):
    return Chunk(tool_calls=[ToolCall(call_id, name, json.dumps(args))])


def answer_with(agent: Agent, decision: str):
    """Make the broker respond automatically, recording what was asked."""
    asked: list[dict] = []

    async def fake_ask(kind, payload, *, default="deny", timeout=None):
        asked.append({"kind": kind, **payload})
        return AskResult(decision)

    agent.broker.ask = fake_ask
    return asked


async def build(store, bus, script) -> Agent:
    agent = Agent(store, bus)
    await agent.start()
    agent.provider = ScriptedProvider(script)
    agent._ready = True
    return agent


@pytest.mark.asyncio
class TestEnableToolset:
    async def test_approval_makes_the_tools_usable_in_the_same_turn(self, store, bus):
        store.settings.toolsets = ["core", "files"]
        agent = await build(store, bus, [
            [call("c1", "enable_toolset", toolset="system", reason="I need to see memory usage.")],
            [call("c2", "which_program", program="python3")],
            [Chunk(text="Python is installed.")],
        ])
        asked = answer_with(agent, "allow")
        await agent.send("what's installed?")
        await asyncio.wait_for(agent._turn_task, timeout=30)

        assert asked and asked[0]["kind"] == Ask.TOOLSET
        assert asked[0]["toolset"] == "system"
        assert asked[0]["reason"] == "I need to see memory usage."

        assert "system" in agent.registry.enabled_toolsets
        assert "system" in store.settings.toolsets, "the choice should be remembered"
        # The very next model call must already see the new tools.
        assert "which_program" in agent.provider.tools_offered[1]
        results = [m for m in agent.messages if m.get("name") == "which_program"]
        assert results and "python3" in results[0]["content"]
        await agent.shutdown()

    async def test_a_refusal_tells_the_model_to_stop_asking(self, store, bus):
        store.settings.toolsets = ["core", "files"]
        agent = await build(store, bus, [
            [call("c1", "enable_toolset", toolset="desktop", reason="To tile the windows.")],
            [Chunk(text="I cannot arrange windows without that capability.")],
        ])
        answer_with(agent, "deny")
        await agent.send("tile my windows")
        await asyncio.wait_for(agent._turn_task, timeout=30)

        assert "desktop" not in agent.registry.enabled_toolsets
        assert "desktop" not in store.settings.toolsets
        result = next(m for m in agent.messages if m.get("name") == "enable_toolset")
        assert "declined" in result["content"]
        assert "Do not ask again" in result["content"]
        await agent.shutdown()

    async def test_an_unknown_toolset_lists_the_real_ones(self, store, bus):
        agent = await build(store, bus, [
            [call("c1", "enable_toolset", toolset="telepathy", reason="why not")],
            [Chunk(text="ok")],
        ])
        answer_with(agent, "allow")
        await agent.send("read minds")
        await asyncio.wait_for(agent._turn_task, timeout=30)
        result = next(m for m in agent.messages if m.get("name") == "enable_toolset")
        assert "No toolset called" in result["content"]
        assert "desktop" in result["content"]
        await agent.shutdown()

    async def test_already_enabled_is_a_no_op(self, store, bus):
        agent = await build(store, bus, [
            [call("c1", "enable_toolset", toolset="files", reason="need files")],
            [Chunk(text="ok")],
        ])
        asked = answer_with(agent, "allow")
        await agent.send("read a file")
        await asyncio.wait_for(agent._turn_task, timeout=30)
        assert not asked, "an already-enabled toolset should not prompt the user"
        result = next(m for m in agent.messages if m.get("name") == "enable_toolset")
        assert "already enabled" in result["content"]
        await agent.shutdown()

    async def test_a_missing_dependency_is_explained_not_prompted(self, store, bus, monkeypatch):
        from arcbot.tools.catalog import CATALOG

        monkeypatch.setattr(type(CATALOG["web"]), "missing_requirements", lambda self: ["ddgs"])
        agent = await build(store, bus, [
            [call("c1", "enable_toolset", toolset="web", reason="I need to search.")],
            [Chunk(text="ok")],
        ])
        asked = answer_with(agent, "allow")
        await agent.send("search the web")
        await asyncio.wait_for(agent._turn_task, timeout=30)
        assert not asked, "there is nothing to approve when the package is absent"
        result = next(m for m in agent.messages if m.get("name") == "enable_toolset")
        assert "pip install ddgs" in result["content"]
        await agent.shutdown()

    async def test_the_prompt_lists_disabled_capabilities(self, store, bus):
        """The model can only ask for what it knows exists."""
        store.settings.toolsets = ["core", "files"]
        agent = await build(store, bus, [[Chunk(text="hi")]])
        prompt = await agent._system_prompt()
        assert "switched off" in prompt
        assert "desktop:" in prompt
        assert "enable_toolset" in prompt
        assert "files:" not in prompt, "an enabled toolset should not be advertised as off"
        await agent.shutdown()


class TestPromptMatchesProviderMode:
    """Advertising a tool the provider does not have is worse than saying nothing:
    the model tries it, finds it missing, and routes around the switch instead."""

    def _block(self, mode: str, store) -> str:
        from arcbot.prompts import build_system_prompt

        store.settings.toolsets = ["core", "files"]
        prompt = build_system_prompt(
            store.settings, enabled_toolsets=["core", "files"], provider_mode=mode
        )
        return prompt.split("# Capabilities that are switched off")[1].split("\n# ")[0]

    def test_model_mode_offers_the_unlock_tool(self, store):
        block = self._block("model", store)
        assert "enable_toolset" in block
        assert "desktop" in block

    def test_agent_mode_never_mentions_a_tool_it_does_not_have(self, store):
        block = self._block("agent", store)
        assert "enable_toolset" not in block
        assert "sidebar" in block
        assert "defeats the switch" in block

    def test_both_modes_forbid_working_around_a_disabled_capability(self, store):
        for mode in ("model", "agent"):
            block = self._block(mode, store).lower()
            assert "do not" in block or "defeats" in block
