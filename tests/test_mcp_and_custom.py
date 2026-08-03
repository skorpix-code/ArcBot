"""External MCP servers and user-built tools.

Both add third-party or generated code to the agent's tool surface, so the
tests here focus on the boundaries: what is gated, what is rejected, and what
happens when something is broken.
"""

from __future__ import annotations

import textwrap

import pytest

from arcbot import toolbuilder as tb
from arcbot.tools.mcp_bridge import (
    MCPBridge,
    ServerStatus,
    _sanitize_schema,
    explain,
    presets_payload,
    tool_name,
)
from arcbot.tools.registry import ToolRegistry

GOOD_TOOL = textwrap.dedent('''
    """Count words."""

    from arcbot.tools.registry import ToolResult, ctx, tool


    @tool(toolset="custom", capability="read", title="Count words")
    def count_words(text: str, unique_only: bool = False) -> ToolResult:
        """Count the words in a piece of text.

        Args:
            text: The text to count.
            unique_only: Count distinct words instead of every word.
        """
        words = text.split()
        return ToolResult(True, f"{len(set(words)) if unique_only else len(words)} words")
''')


@pytest.fixture(autouse=True)
def isolated_tools(tmp_path, monkeypatch):
    """Keep every test's saved tools in its own directory."""
    directory = tmp_path / "tools"
    directory.mkdir()
    monkeypatch.setattr(tb, "tools_dir", lambda: directory)
    return directory


# --------------------------------------------------------------------------- #
# Tool builder
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_a_good_tool_yields_a_usable_schema(self):
        draft = tb.validate(GOOD_TOOL)
        assert draft.valid, draft.problems
        assert draft.name == "count_words"
        assert draft.capability == "read"
        assert set(draft.parameters["properties"]) == {"text", "unique_only"}
        assert draft.parameters["required"] == ["text"]

    @pytest.mark.parametrize(
        ("label", "code"),
        [
            ("empty", ""),
            ("syntax error", "def broken(:"),
            ("no decorator", "def plain():\n    return 1"),
            ("two tools", GOOD_TOOL + GOOD_TOOL.replace("count_words", "other")),
            ("wrong toolset", GOOD_TOOL.replace('toolset="custom"', 'toolset="files"')),
            ("bad capability", GOOD_TOOL.replace('capability="read"', 'capability="root"')),
            ("built-in name", GOOD_TOOL.replace("count_words", "read_file")),
            ("missing import", GOOD_TOOL.replace("import ToolResult", "import ToolResult\nimport nope_xyz")),
        ],
    )
    def test_bad_tools_are_rejected_with_a_reason(self, label, code):
        draft = tb.validate(code)
        assert not draft.valid, label
        assert draft.problems and draft.problems[0].strip(), label

    def test_validation_never_leaks_into_the_live_registry(self):
        from arcbot.tools import registry as reg

        before = set(reg._DECLARED)
        tb.validate(GOOD_TOOL)
        assert set(reg._DECLARED) == before

    def test_revalidating_an_already_loaded_tool_still_works(self):
        """The editor re-checks on every keystroke, including for saved tools."""
        from arcbot.tools import registry as reg

        draft = tb.validate(GOOD_TOOL)
        tb.save(draft)
        registry = ToolRegistry()
        registry.load(["core", "custom"])
        assert "count_words" in reg._DECLARED
        assert tb.validate(GOOD_TOOL).valid, "a loaded tool must still validate"

    def test_risky_code_is_described_rather_than_blocked(self):
        risky = GOOD_TOOL.replace(
            "    words = text.split()",
            "    import subprocess\n    subprocess.run(['ls'])\n    words = text.split()",
        )
        draft = tb.validate(risky)
        assert draft.valid, "the scan reports; it does not block"
        assert any("runs other programs" in note for note in draft.notes)

    def test_network_access_is_described(self):
        code = GOOD_TOOL.replace("    words = text.split()",
                                 "    import urllib.request\n    words = text.split()")
        assert any("web requests" in note for note in tb.validate(code).notes)


class TestPersistence:
    def test_save_load_call_delete(self, isolated_tools):
        draft = tb.validate(GOOD_TOOL)
        path = tb.save(draft)
        assert path.exists() and path.parent == isolated_tools

        listed = tb.list_custom()
        assert [t["name"] for t in listed] == ["count_words"]
        assert listed[0]["valid"]

        registry = ToolRegistry()
        assert not registry.load(["core", "custom"])
        spec = registry.get("count_words")
        assert spec is not None and spec.toolset == "custom"

        assert tb.delete("count_words")
        assert not tb.delete("count_words")

    def test_an_invalid_draft_is_never_written(self):
        with pytest.raises(ValueError):
            tb.save(tb.validate("def broken(:"))
        assert not tb.list_custom()

    def test_a_broken_file_does_not_stop_the_others(self, isolated_tools):
        tb.save(tb.validate(GOOD_TOOL))
        (isolated_tools / "broken.py").write_text("not python ((")
        import importlib

        from arcbot.tools import custom as custom_mod

        importlib.reload(custom_mod)
        assert "broken.py" in custom_mod.LOAD_FAILURES
        registry = ToolRegistry()
        registry.load(["core", "custom"])
        assert registry.get("count_words") is not None

    @pytest.mark.parametrize("name", ["../escape", "with space", "", "UPPER", "x"])
    def test_path_traversal_and_bad_names_are_refused(self, name):
        assert not tb.delete(name)
        with pytest.raises(ValueError):
            tb._path_for(name)


class TestFenceStripping:
    @pytest.mark.parametrize("wrapped", [
        "```python\nprint(1)\n```",
        "```\nprint(1)\n```",
        "Here you go:\n```python\nprint(1)\n```\nHope that helps.",
    ])
    def test_markdown_fences_are_removed(self, wrapped):
        assert tb.strip_fences(wrapped).strip() == "print(1)"

    def test_bare_code_is_untouched(self):
        assert tb.strip_fences("print(1)") == "print(1)"


@pytest.mark.asyncio
class TestGeneration:
    async def test_a_provider_failure_becomes_a_problem_not_a_crash(self):
        class Broken:
            async def complete(self, prompt, *, system=""):
                raise RuntimeError("model is down")

        draft = await tb.generate("do a thing", Broken())
        assert not draft.valid
        assert "model is down" in draft.problems[0]

    async def test_a_fenced_response_still_validates(self):
        class Fenced:
            async def complete(self, prompt, *, system=""):
                return f"Sure!\n```python\n{GOOD_TOOL}\n```"

        draft = await tb.generate("count words", Fenced())
        assert draft.valid, draft.problems
        assert draft.name == "count_words"


# --------------------------------------------------------------------------- #
# MCP bridge
# --------------------------------------------------------------------------- #


class TestMCPNaming:
    def test_names_are_namespaced_and_safe(self):
        assert tool_name("fetch", "get_url") == "mcp__fetch__get_url"
        assert tool_name("my server", "do-it") == "mcp__my_server__do_it"

    def test_a_server_cannot_shadow_a_built_in(self):
        from arcbot.tools.catalog import TOOL_OWNER

        assert tool_name("x", "read_file") not in TOOL_OWNER


class TestMCPSchema:
    def test_a_missing_schema_becomes_an_empty_object(self):
        assert _sanitize_schema(None) == {"type": "object", "properties": {}}
        assert _sanitize_schema("nonsense")["properties"] == {}

    def test_properties_and_required_survive(self):
        schema = _sanitize_schema({
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "$schema": "http://json-schema.org/draft-07/schema#",
        })
        assert schema["properties"] == {"url": {"type": "string"}}
        assert schema["required"] == ["url"]

    def test_non_string_required_entries_are_dropped(self):
        assert _sanitize_schema({"required": ["ok", 5, None]})["required"] == ["ok"]


class TestMCPErrors:
    def test_a_task_group_error_is_unwrapped_to_its_cause(self):
        inner = FileNotFoundError(2, "not found")
        inner.filename = "npx"
        group = Exception("unhandled errors in a TaskGroup")
        group.exceptions = [inner]
        assert "npx" in explain(group)

    def test_a_plain_error_reads_normally(self):
        assert explain(ValueError("bad url")) == "ValueError: bad url"

    def test_an_import_error_says_the_server_failed_to_start(self):
        assert "server failed to start" in explain(ImportError("no module"))


@pytest.mark.asyncio
class TestMCPLifecycle:
    async def test_no_servers_is_a_no_op(self):
        bridge = MCPBridge(ToolRegistry())
        assert await bridge.connect_all({}) == {}
        await bridge.close()

    async def test_a_disabled_server_is_skipped(self):
        bridge = MCPBridge(ToolRegistry())
        result = await bridge.connect_all({"x": {"command": "echo", "enabled": False}})
        assert result == {}
        await bridge.close()

    async def test_a_missing_binary_reports_instead_of_raising(self):
        bridge = MCPBridge(ToolRegistry())
        status = await bridge.connect_all(
            {"ghost": {"command": "definitely-not-a-real-binary-xyz", "args": []}}
        )
        assert not status["ghost"].connected
        assert "not installed" in status["ghost"].error
        await bridge.close()

    async def test_closing_removes_every_registered_tool(self):
        registry = ToolRegistry()
        registry.load(["core"])
        bridge = MCPBridge(registry)
        from arcbot.tools.registry import ToolSpec

        registry.register_dynamic(ToolSpec(
            name="mcp__x__y", description="d", parameters={"type": "object", "properties": {}},
            fn=lambda: None, toolset="mcp",
        ))
        assert registry.find_anywhere("mcp__x__y") is not None
        await bridge.close()
        assert registry.find_anywhere("mcp__x__y") is None


def test_presets_report_whether_they_can_run():
    presets = presets_payload()
    assert presets
    for preset in presets:
        assert preset["id"] and preset["name"] and preset["summary"]
        assert isinstance(preset["available"], bool)
        assert preset["config"].get("command") or preset["config"].get("url")


def test_server_status_serialises_for_the_ui():
    payload = ServerStatus("x", connected=True, tools=["a"]).to_dict()
    assert payload == {"name": "x", "connected": True, "tools": ["a"], "error": ""}
