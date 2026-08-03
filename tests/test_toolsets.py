"""Toolset declaration, lazy loading and schema generation."""

from __future__ import annotations

import importlib
from typing import Literal

import pytest

from arcbot.tools.catalog import ALWAYS_ON, CATALOG, TOOL_OWNER, normalise, owning_toolset
from arcbot.tools.registry import ToolRegistry, build_schema

#: Toolsets whose tools are discovered at runtime, not declared up front.
DYNAMIC = {"mcp", "custom"}


def test_every_toolset_declares_its_module_and_copy():
    for entry in CATALOG.values():
        assert entry.name and entry.summary, entry.id
        if entry.id not in DYNAMIC:
            assert entry.tools, f"{entry.id} declares no tools"
        if not entry.always_on:
            assert entry.caution or entry.id in ("memory",), f"{entry.id} has no caution copy"


@pytest.mark.parametrize("toolset_id", list(CATALOG))
def test_declared_tools_match_the_module(toolset_id):
    """The catalog is the only place a *disabled* toolset's tools are known.

    If it drifts from the module, the one-click-enable flow silently breaks, so
    the two are asserted equal here rather than trusted.
    """
    entry = CATALOG[toolset_id]
    if toolset_id in DYNAMIC:
        pytest.skip(f"{toolset_id} discovers its tools at runtime")
    if entry.missing_requirements():
        pytest.skip(f"{toolset_id} needs {entry.missing_requirements()}")

    from arcbot.tools import registry as reg

    importlib.import_module(f"arcbot.tools.{entry.module}")
    actual = {name for name, spec in reg._DECLARED.items() if spec.toolset in (toolset_id, "always")}
    if toolset_id != "core":
        actual = {name for name, spec in reg._DECLARED.items() if spec.toolset == toolset_id}
    assert actual == set(entry.tools), (
        f"{toolset_id}: catalog and module disagree.\n"
        f"  only in module:  {sorted(actual - set(entry.tools))}\n"
        f"  only in catalog: {sorted(set(entry.tools) - actual)}"
    )


def test_tool_owner_lookup_works_without_importing():
    assert owning_toolset("list_windows").id == "desktop"
    assert owning_toolset("run_command").id == "shell"
    assert owning_toolset("not_a_tool") is None


def test_no_tool_name_is_claimed_by_two_toolsets():
    seen: dict[str, str] = {}
    for entry in CATALOG.values():
        for name in entry.tools:
            assert name not in seen, f"{name} claimed by {seen[name]} and {entry.id}"
            seen[name] = entry.id
    assert len(seen) == len(TOOL_OWNER)


class TestNormalise:
    def test_unknown_ids_are_dropped(self):
        assert "nonsense" not in normalise(["files", "nonsense"])

    def test_always_on_toolsets_are_forced_in(self):
        result = normalise(["files"])
        for required in ALWAYS_ON:
            assert required in result

    def test_duplicates_collapse(self):
        assert normalise(["files", "files", "shell"]).count("files") == 1

    def test_empty_input_still_yields_the_always_on_set(self):
        assert normalise([]) == ALWAYS_ON


class TestLazyLoading:
    def test_only_requested_toolsets_are_exposed(self):
        registry = ToolRegistry()
        registry.load(["core", "files"])
        names = {spec.name for spec in registry.specs()}
        assert "read_file" in names
        assert "run_command" not in names
        assert "list_windows" not in names

    def test_a_toolset_can_be_enabled_at_runtime(self):
        registry = ToolRegistry()
        registry.load(["core"])
        assert registry.get("run_command") is None
        assert registry.enable("shell") is None
        assert registry.get("run_command") is not None

    def test_disabling_hides_the_tools_again(self):
        registry = ToolRegistry()
        registry.load(["core", "shell"])
        registry.disable("shell")
        assert registry.get("run_command") is None

    def test_a_missing_dependency_reports_rather_than_raises(self, monkeypatch):
        entry = CATALOG["web"]
        monkeypatch.setattr(type(entry), "missing_requirements", lambda self: ["ddgs"])
        registry = ToolRegistry()
        errors = registry.load(["core", "web"])
        assert "web" in errors and "ddgs" in errors["web"]
        assert "web" not in registry.enabled_toolsets


def sample_tool(name: str, count: int = 3, flag: bool = False, tags: list[str] | None = None):
    """A stand-in with the same annotation style the real tool modules use."""


def sample_literal(mode: Literal["a", "b"] = "a"):
    """Pick a mode."""


class TestSchemaGeneration:
    def test_signature_becomes_a_json_schema(self):
        schema = build_schema(sample_tool, {"name": "What to call it.", "count": "How many."})
        props = schema["properties"]
        assert schema["required"] == ["name"]
        assert props["name"]["type"] == "string"
        assert props["count"]["type"] == "integer"
        assert props["count"]["default"] == 3
        assert props["flag"]["type"] == "boolean"
        assert props["tags"]["type"] == "array"
        assert props["name"]["description"] == "What to call it."

    def test_literal_becomes_an_enum(self):
        schema = build_schema(sample_literal, {})
        assert schema["properties"]["mode"]["enum"] == ["a", "b"]

    def test_every_registered_tool_has_a_usable_schema(self):
        registry = ToolRegistry()
        registry.load(list(CATALOG))
        specs = registry.specs()
        assert len(specs) > 20
        for spec in specs:
            assert spec.description.strip(), f"{spec.name} has no description"
            schema = spec.parameters
            assert schema["type"] == "object"
            for name, prop in schema["properties"].items():
                assert "type" in prop or "anyOf" in prop, f"{spec.name}.{name} has no type"
            for required in schema["required"]:
                assert required in schema["properties"]


def test_the_default_toolsets_match_the_catalog():
    """A fresh install and the onboarding wizard must recommend the same set."""
    from arcbot.config import Settings
    from arcbot.tools.catalog import DEFAULT_TOOLSETS

    assert Settings().toolsets == DEFAULT_TOOLSETS
    assert set(ALWAYS_ON) <= set(DEFAULT_TOOLSETS)
