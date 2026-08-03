"""Self-awareness, tool-over-command steering, and getting unstuck.

These cover the behaviours that decide whether the agent can handle a task it
was not explicitly designed for: knowing what it is, reaching for the right
tool, and changing approach when something fails.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from arcbot.agent import Agent
from arcbot.asks import AskResult
from arcbot.guards import LoopGuard
from arcbot.providers.base import Chunk, Provider, ToolCall, Usage
from arcbot.redirects import find_redirect


class ScriptedProvider(Provider):
    mode = "model"
    context_window = 200_000

    def __init__(self, script):
        self.script = script
        self.calls = 0
        self.systems: list[str] = []

    async def stream(self, messages, tools, *, system=""):
        self.systems.append(system)
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        for chunk in step:
            yield chunk
        yield Chunk(usage=Usage(5, 5), done=True, stop_reason="end_turn")


def call(call_id, name, **args):
    return Chunk(tool_calls=[ToolCall(call_id, name, json.dumps(args))])


async def build(store, bus, script, *, answer: str = "allow") -> Agent:
    agent = Agent(store, bus)
    await agent.start()
    agent.provider = ScriptedProvider(script)
    agent._ready = True

    async def auto_answer(kind, payload, *, default="deny", timeout=None):
        agent.asked.append({"kind": kind, **payload})
        return AskResult(answer)

    agent.asked = []          # type: ignore[attr-defined]
    agent.broker.ask = auto_answer
    return agent


def tool_result(agent: Agent, name: str) -> str:
    results = [m["content"] for m in agent.messages
               if m.get("role") == "tool" and m.get("name") == name]
    return results[-1] if results else ""


# --------------------------------------------------------------------------- #
# Tool over command
# --------------------------------------------------------------------------- #


class TestRedirectMapping:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("cat README.md", "read_file"),
            ("head -20 src/main.py", "read_file"),
            ("ls -la", "list_files"),
            ("tree src", "directory_tree"),
            ("grep -rn TODO src", "search_code"),
            ("rg pattern .", "search_code"),
            ("find . -name '*.py'", "find_files"),
            ("rm -rf build", "delete_path"),
            ("mv a.txt b.txt", "move_path"),
            ("cp a.txt b.txt", "copy_path"),
            ("mkdir -p out/sub", "create_directory"),
            ("curl https://example.com", "read_webpage"),
            ("ps aux", "list_processes"),
            ("df -h", "disk_usage"),
            ("uname -a", "system_info"),
            ("which python3", "which_program"),
            ("git status", "git_status"),
            ("git log --oneline", "git_log"),
        ],
    )
    def test_common_commands_point_at_a_tool(self, command, expected):
        redirect = find_redirect(command)
        assert redirect is not None, command
        assert redirect.tool == expected

    @pytest.mark.parametrize(
        "command",
        [
            "npm install",
            "pytest -q",
            "python setup.py build",
            "cat a.txt | grep b",          # a pipeline is doing more than one tool can
            "ls > listing.txt",            # redirection is not a listing
            "make && make install",
            "docker compose up -d",
            "git rebase -i HEAD~3",        # no tool covers this subcommand
            "./configure --prefix=/usr",
        ],
    )
    def test_real_command_work_is_left_alone(self, command):
        assert find_redirect(command) is None, command

    def test_the_suggestion_is_a_call_the_model_can_copy(self):
        redirect = find_redirect("cat src/app.py")
        assert redirect.suggestion == 'read_file(path="src/app.py")'

    def test_quotes_in_a_path_are_escaped_not_dropped(self):
        redirect = find_redirect('cat "we\'ird\\"name.txt"')
        assert redirect is not None
        # The inner quote survives as an escape, so the suggestion stays valid.
        assert '\\"' in redirect.suggestion
        assert redirect.suggestion.startswith('read_file(path="')
        assert redirect.suggestion.endswith('")')

    def test_a_disabled_toolset_is_requested_rather_than_named(self):
        redirect = find_redirect("curl https://example.com")
        message = redirect.message(enabled={"files", "shell"})
        assert "enable_toolset('web'" in message
        assert "read_webpage" in message

    def test_an_enabled_toolset_gets_the_direct_suggestion(self):
        redirect = find_redirect("curl https://example.com")
        message = redirect.message(enabled={"files", "shell", "web"})
        assert "read_webpage" in message
        assert "force=true" in message, "the escape hatch must be discoverable"


@pytest.mark.asyncio
class TestRedirectInTheLoop:
    async def test_a_covered_command_is_redirected_not_run(self, store, bus, workspace):
        (workspace / "note.txt").write_text("hello")
        agent = await build(store, bus, [
            [call("c1", "run_command", command="cat note.txt")],
            [call("c2", "read_file", path="note.txt")],
            [Chunk(text="It says hello.")],
        ])
        await agent.send("what is in note.txt")
        await asyncio.wait_for(agent._turn_task, timeout=30)

        redirect = tool_result(agent, "run_command")
        assert "read_file" in redirect
        assert 'read_file(path="note.txt")' in redirect
        assert "hello" in tool_result(agent, "read_file")
        await agent.shutdown()

    async def test_force_runs_it_anyway(self, store, bus, workspace):
        (workspace / "note.txt").write_text("hello")
        agent = await build(store, bus, [
            [call("c1", "run_command", command="cat note.txt", force=True)],
            [Chunk(text="done")],
        ])
        await agent.send("cat it anyway")
        await asyncio.wait_for(agent._turn_task, timeout=45)
        assert "hello" in tool_result(agent, "run_command")
        await agent.shutdown()

    async def test_an_uncovered_command_still_runs(self, store, bus):
        agent = await build(store, bus, [
            [call("c1", "run_command", command="echo hello-from-shell")],
            [Chunk(text="done")],
        ])
        await agent.send("echo something")
        await asyncio.wait_for(agent._turn_task, timeout=45)
        assert "hello-from-shell" in tool_result(agent, "run_command")
        await agent.shutdown()


# --------------------------------------------------------------------------- #
# Self-awareness
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestSelfAwareness:
    async def test_status_describes_the_app_not_the_machine(self, store, bus, workspace):
        agent = await build(store, bus, [
            [call("c1", "arcbot_status")],
            [Chunk(text="ok")],
        ])
        await agent.send("what are you")
        await asyncio.wait_for(agent._turn_task, timeout=30)
        report = tool_result(agent, "arcbot_status")
        assert "local application" in report
        assert str(workspace) in report
        assert "trusted" in report.lower()
        await agent.shutdown()

    async def test_quit_asks_the_host_to_stop(self, store, bus):
        stopped: list[str] = []

        agent = await build(store, bus, [
            [call("c1", "quit_arcbot", reason="you asked me to")],
            [Chunk(text="bye")],
        ])

        async def on_quit(reason: str) -> None:
            stopped.append(reason)

        agent.on_quit = on_quit
        await agent.send("close yourself")
        await asyncio.wait_for(agent._turn_task, timeout=30)
        assert stopped == ["you asked me to"]
        assert "shutting down" in tool_result(agent, "quit_arcbot").lower()
        await agent.shutdown()

    async def test_quit_says_so_when_there_is_no_host_to_stop(self, store, bus):
        agent = await build(store, bus, [
            [call("c1", "quit_arcbot")],
            [Chunk(text="ok")],
        ])
        agent.on_quit = None
        await agent.send("close yourself")
        await asyncio.wait_for(agent._turn_task, timeout=30)
        assert "close the ArcBot window" in tool_result(agent, "quit_arcbot")
        assert "shutting down" not in tool_result(agent, "quit_arcbot").lower()
        await agent.shutdown()

    async def test_open_settings_reaches_the_ui(self, store, bus, recorder):
        agent = await build(store, bus, [
            [call("c1", "open_settings", panel="trust", reason="raise the trust level")],
            [Chunk(text="ok")],
        ])
        await agent.send("I want you to run commands")
        await asyncio.wait_for(agent._turn_task, timeout=30)
        panels = [d for k, d in recorder if k == "open.settings"]
        assert panels and panels[0]["panel"] == "trust"
        await agent.shutdown()

    async def test_the_prompt_explains_what_arcbot_is(self, store, bus):
        agent = await build(store, bus, [[Chunk(text="hi")]])
        prompt = await agent._system_prompt()
        assert "local app" in prompt or "local application" in prompt
        assert "not a terminal session" in prompt
        assert "quit_arcbot" in prompt
        await agent.shutdown()

    async def test_the_prompt_indexes_tools_by_intent(self, store, bus):
        store.settings.toolsets = ["core", "files", "git"]
        agent = await build(store, bus, [[Chunk(text="hi")]])
        prompt = await agent._system_prompt()
        assert "Reaching for the right tool" in prompt
        assert "Understand a repository" in prompt
        assert "Control the desktop" not in prompt, "a disabled toolset must not be indexed"
        await agent.shutdown()

    async def test_the_prompt_says_to_prefer_tools(self, store, bus):
        agent = await build(store, bus, [[Chunk(text="hi")]])
        prompt = await agent._system_prompt()
        assert "Use the tool, not the shell" in prompt
        await agent.shutdown()


# --------------------------------------------------------------------------- #
# Getting unstuck
# --------------------------------------------------------------------------- #


class TestDiagnosis:
    def test_it_reports_what_was_tried_and_what_failed(self, store):
        guard = LoopGuard(store.settings.limits)
        guard.begin_step()
        guard.record_call("read_file", {"path": "a"})
        guard.record_call("read_file", {"path": "b"})
        guard.record_failure("read_file", "No such file: b")
        guard.record_call("run_command", {"command": "ls"})

        report = guard.diagnose()
        assert "read_file×2" in report
        assert "run_command" in report
        assert "No such file: b" in report
        assert "step 1 of" in report
        assert "change approach" in report

    def test_it_names_benched_tools(self, store):
        store.settings.limits.tool_failure_limit = 2
        guard = LoopGuard(store.settings.limits)
        guard.record_failure("web_search", "network down")
        guard.record_failure("web_search", "network down")
        assert guard.is_benched("web_search")
        assert "web_search" in guard.diagnose()

    def test_it_is_honest_when_nothing_has_run(self, store):
        assert "not called any tool" in LoopGuard(store.settings.limits).diagnose()


@pytest.mark.asyncio
class TestUnsticking:
    async def test_a_repeated_call_gets_the_full_situation_report(self, store, bus, workspace):
        (workspace / "f.txt").write_text("x")
        agent = await build(store, bus, [
            [call("c1", "read_file", path="f.txt")],
            [call("c2", "read_file", path="f.txt")],
            [call("c3", "read_file", path="f.txt")],
            [Chunk(text="stopping")],
        ])
        await agent.send("read it")
        await asyncio.wait_for(agent._turn_task, timeout=30)

        nudges = [str(m["content"]) for m in agent.messages
                  if m.get("role") == "user" and str(m.get("content", "")).startswith("[system]")]
        assert nudges
        combined = "\n".join(nudges)
        assert "Step back" in combined
        assert "read_file" in combined
        assert "Budget: step" in combined
        await agent.shutdown()

    async def test_a_failing_tool_reports_its_actual_error(self, store, bus):
        agent = await build(store, bus, [
            [call("c1", "read_file", path="missing1.txt")],
            [call("c2", "read_file", path="missing2.txt")],
            [call("c3", "read_file", path="missing3.txt")],
            [call("c4", "read_file", path="missing4.txt")],
            [Chunk(text="giving up")],
        ])
        await agent.send("read them")
        await asyncio.wait_for(agent._turn_task, timeout=30)
        nudges = "\n".join(str(m["content"]) for m in agent.messages
                           if m.get("role") == "user" and str(m.get("content", "")).startswith("[system]"))
        assert "No such file" in nudges, "the diagnosis must carry the real error"
        await agent.shutdown()
