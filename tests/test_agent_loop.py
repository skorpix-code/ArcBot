"""The agent loop and its guard rails.

Every test drives the real loop with a scripted provider, so a regression in
tool dispatch, history repair or loop detection fails here rather than in front
of a user.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from arcbot.agent import Agent
from arcbot.context import normalise
from arcbot.providers.base import Chunk, Provider, ToolCall, Usage


class ScriptedProvider(Provider):
    """Replays a fixed list of model responses, one per step."""

    mode = "model"
    context_window = 200_000

    def __init__(self, script: list[list[Chunk]]):
        self.script = script
        self.calls = 0
        self.tools_offered: list[list[str]] = []

    async def stream(self, messages, tools, *, system=""):
        self.tools_offered.append([t["name"] for t in tools])
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        for chunk in step:
            yield chunk
        yield Chunk(usage=Usage(10, 5), done=True, stop_reason="end_turn")


def call(call_id: str, name: str, **args) -> Chunk:
    return Chunk(tool_calls=[ToolCall(call_id, name, json.dumps(args))])


async def build(store, bus, script) -> Agent:
    agent = Agent(store, bus)
    await agent.start()
    agent.provider = ScriptedProvider(script)
    agent._ready = True
    return agent


async def run_turn(agent: Agent, text: str, timeout: float = 30.0) -> None:
    await agent.send(text)
    await asyncio.wait_for(agent._turn_task, timeout=timeout)


def tool_results(agent: Agent, name: str) -> list[str]:
    return [m["content"] for m in agent.messages if m.get("role") == "tool" and m.get("name") == name]


@pytest.mark.asyncio
class TestBasicLoop:
    async def test_a_plain_answer_ends_the_turn(self, store, bus, recorder):
        agent = await build(store, bus, [[Chunk(text="Hello there.")]])
        await run_turn(agent, "hi")
        assert agent.provider.calls == 1
        assert [k for k, _ in recorder].count("turn.end") == 1
        assert agent.messages[-1]["content"] == "Hello there."
        await agent.shutdown()

    async def test_a_tool_call_runs_and_feeds_the_result_back(self, store, bus, workspace):
        agent = await build(store, bus, [
            [call("c1", "write_file", path="a.txt", content="body")],
            [Chunk(text="Written.")],
        ])
        await run_turn(agent, "make a file")
        assert (workspace / "a.txt").read_text() == "body"
        assert "Created a.txt" in tool_results(agent, "write_file")[0]
        await agent.shutdown()

    async def test_tool_events_reach_the_ui(self, store, bus, recorder):
        agent = await build(store, bus, [
            [call("c1", "write_plan", steps=["one", "two"])],
            [Chunk(text="done")],
        ])
        await run_turn(agent, "plan it")
        starts = [d for k, d in recorder if k == "tool.start"]
        ends = [d for k, d in recorder if k == "tool.end"]
        assert len(starts) == len(ends) == 1
        assert starts[0]["name"] == "write_plan"
        assert starts[0]["title"]
        assert ends[0]["ok"]
        await agent.shutdown()

    async def test_thinking_is_streamed_separately_from_the_answer(self, store, bus, recorder):
        agent = await build(store, bus, [[Chunk(thinking="pondering"), Chunk(text="answer")]])
        await run_turn(agent, "think")
        assert any(k == "thinking.delta" for k, _ in recorder)
        assert any(k == "text.delta" for k, _ in recorder)
        await agent.shutdown()


@pytest.mark.asyncio
class TestGuardRails:
    async def test_repeating_a_tool_call_forces_a_final_answer(self, store, bus, workspace):
        (workspace / "f.txt").write_text("x")
        agent = await build(store, bus, [
            [call("c1", "read_file", path="f.txt")],
            [call("c2", "read_file", path="f.txt")],
            [call("c3", "read_file", path="f.txt")],
            [call("c4", "read_file", path="f.txt")],
            [Chunk(text="Stopping.")],
        ])
        await run_turn(agent, "read it")
        nudges = [
            m["content"] for m in agent.messages
            if m.get("role") == "user" and str(m.get("content", "")).startswith("[system]")
        ]
        assert any("do NOT call it again" in n or "already have" in n for n in nudges)
        # The final step must have been offered no tools at all.
        assert agent.provider.tools_offered[-1] == []
        await agent.shutdown()

    async def test_the_step_budget_is_enforced(self, store, bus, workspace):
        store.settings.limits.max_steps = 4
        agent = await build(store, bus, [[call(f"c{i}", "list_files", path=".") for i in range(1)]])
        await run_turn(agent, "loop forever")
        assert agent.provider.calls <= 5
        await agent.shutdown()

    async def test_an_empty_response_does_not_loop_forever(self, store, bus):
        agent = await build(store, bus, [[Chunk(text="")]])
        await run_turn(agent, "say nothing")
        assert agent.provider.calls <= 4
        await agent.shutdown()

    async def test_a_failing_tool_gets_benched(self, store, bus):
        agent = await build(store, bus, [
            [call("c1", "read_file", path="missing.txt")],
            [call("c2", "read_file", path="missing2.txt")],
            [call("c3", "read_file", path="missing3.txt")],
            [call("c4", "read_file", path="missing4.txt")],
            [Chunk(text="giving up")],
        ])
        await run_turn(agent, "read missing files")
        results = tool_results(agent, "read_file")
        assert any("disabled for this turn" in r for r in results)
        await agent.shutdown()


@pytest.mark.asyncio
class TestErrorHandling:
    async def test_an_unknown_tool_returns_the_tool_list(self, store, bus):
        agent = await build(store, bus, [
            [call("c1", "teleport", target="mars")],
            [Chunk(text="ok")],
        ])
        await run_turn(agent, "teleport")
        result = tool_results(agent, "teleport")[0]
        assert "no tool called 'teleport'" in result.lower()
        assert "read_file" in result
        await agent.shutdown()

    async def test_a_disabled_toolset_points_at_enable_toolset(self, store, bus):
        agent = await build(store, bus, [
            [call("c1", "list_windows")],
            [Chunk(text="ok")],
        ])
        await run_turn(agent, "arrange windows")
        result = tool_results(agent, "list_windows")[0]
        assert "enable_toolset" in result
        assert "desktop" in result
        await agent.shutdown()

    async def test_malformed_arguments_are_reported_not_crashed(self, store, bus):
        agent = await build(store, bus, [
            [Chunk(tool_calls=[ToolCall("c1", "read_file", "{not json at all")])],
            [Chunk(text="ok")],
        ])
        await run_turn(agent, "read")
        assert "arguments" in tool_results(agent, "read_file")[0].lower()
        await agent.shutdown()

    async def test_loose_argument_types_are_coerced(self, store, bus, workspace):
        (workspace / "n.txt").write_text("1\n2\n3\n4\n5\n")
        agent = await build(store, bus, [
            # start_line/max_lines sent as strings, as small models often do
            [Chunk(tool_calls=[ToolCall("c1", "read_file",
                                        '{"path": "n.txt", "start_line": "2", "max_lines": "2"}')])],
            [Chunk(text="ok")],
        ])
        await run_turn(agent, "read part")
        assert "2|" in tool_results(agent, "read_file")[0]
        await agent.shutdown()

    async def test_a_provider_error_ends_the_turn_cleanly(self, store, bus, recorder):
        agent = await build(store, bus, [[Chunk(error="model exploded")]])
        await run_turn(agent, "hi")
        errors = [d for k, d in recorder if k == "error"]
        assert errors and "exploded" in errors[-1]["message"]
        assert any(k == "turn.end" for k, _ in recorder)
        await agent.shutdown()


@pytest.mark.asyncio
class TestSandboxInTheLoop:
    async def test_writes_outside_the_workspace_are_refused(self, store, bus, tmp_path):
        outside = tmp_path / "outside.txt"
        agent = await build(store, bus, [
            [call("c1", "write_file", path=str(outside), content="pwned")],
            [Chunk(text="blocked")],
        ])
        await run_turn(agent, "write outside")
        assert not outside.exists()
        assert "outside the allowed workspace" in tool_results(agent, "write_file")[0]
        await agent.shutdown()

    async def test_editing_a_file_requires_reading_it_first(self, store, bus, workspace):
        (workspace / "code.py").write_text("value = 1\n")
        agent = await build(store, bus, [
            [call("c1", "edit_file", path="code.py", old_text="value = 1", new_text="value = 2")],
            [Chunk(text="ok")],
        ])
        await run_turn(agent, "edit it")
        assert (workspace / "code.py").read_text() == "value = 1\n"
        assert "have not read" in tool_results(agent, "edit_file")[0]
        await agent.shutdown()

    async def test_editing_works_after_reading(self, store, bus, workspace):
        (workspace / "code.py").write_text("value = 1\n")
        agent = await build(store, bus, [
            [call("c1", "read_file", path="code.py")],
            [call("c2", "edit_file", path="code.py", old_text="value = 1", new_text="value = 2")],
            [Chunk(text="ok")],
        ])
        await run_turn(agent, "edit it")
        assert (workspace / "code.py").read_text() == "value = 2\n"
        await agent.shutdown()


@pytest.mark.asyncio
class TestHistoryIntegrity:
    async def test_every_tool_call_ends_up_with_a_result(self, store, bus, workspace):
        agent = await build(store, bus, [
            [call("c1", "list_files", path="."), call("c2", "project_overview", path=".")],
            [Chunk(text="done")],
        ])
        await run_turn(agent, "look around")
        pending = set()
        for message in normalise(agent.messages):
            for tc in message.get("tool_calls") or []:
                pending.add(tc["id"])
            if message.get("role") == "tool":
                pending.discard(message["tool_call_id"])
        assert not pending
        await agent.shutdown()

    async def test_normalise_drops_an_orphaned_tool_result(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
            {"role": "assistant", "content": "hello"},
        ]
        out = normalise(history)
        assert [m["role"] for m in out] == ["user", "assistant"]

    async def test_normalise_drops_a_tool_call_with_no_result(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "working",
             "tool_calls": [{"id": "x", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}]},
        ]
        out = normalise(history)
        assert "tool_calls" not in out[-1]

    async def test_history_never_starts_with_an_assistant_turn(self):
        out = normalise([{"role": "assistant", "content": "leading"}, {"role": "user", "content": "hi"}])
        assert out[0]["role"] == "user"
