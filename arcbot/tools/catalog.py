"""What toolsets exist, what they cost, and what they need to work.

This is the source of truth for the onboarding picker and the settings panel,
so the copy here is written for a human choosing capabilities — not for a
developer reading code.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolsetEntry:
    id: str
    module: str
    name: str
    summary: str
    #: One line explaining the risk the user accepts by enabling it.
    caution: str = ""
    icon: str = "tool"
    #: Third-party imports the module needs.
    requires: tuple = ()
    #: Recommended in the onboarding wizard's default selection.
    default: bool = False
    #: Cannot be turned off.
    always_on: bool = False
    examples: tuple = ()
    #: The tool names this toolset provides.  Declared here (rather than derived
    #: from the module) so a disabled toolset can still be *named* when the model
    #: guesses one of its tools — the whole one-click-enable flow depends on it.
    #: ``test_catalog`` asserts this stays in sync with the module.
    tools: tuple = ()

    def missing_requirements(self) -> list[str]:
        return [m for m in self.requires if importlib.util.find_spec(m) is None]

    @property
    def available(self) -> bool:
        return not self.missing_requirements()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "caution": self.caution,
            "icon": self.icon,
            "default": self.default,
            "alwaysOn": self.always_on,
            "available": self.available,
            "missing": self.missing_requirements(),
            "examples": list(self.examples),
            "tools": list(self.tools),
        }


CATALOG: dict[str, ToolsetEntry] = {
    "core": ToolsetEntry(
        id="core",
        module="core",
        name="Core",
        summary="Plan work, ask you questions, unlock capabilities, and control ArcBot itself.",
        icon="sparkle",
        always_on=True,
        default=True,
        examples=("plan a multi-step task", "ask for a missing detail"),
        tools=(
            "write_plan",
            "update_plan",
            "ask_user",
            "enable_toolset",
            "arcbot_status",
            "quit_arcbot",
            "open_settings",
        ),
    ),
    "files": ToolsetEntry(
        id="files",
        module="files",
        name="Files & code",
        summary="Read, write, edit and search files inside your workspace.",
        caution="Writes are limited to the workspace folder you pick.",
        icon="file",
        default=True,
        examples=("refactor this module", "find every TODO", "summarise this repo"),
        tools=(
            "read_file",
            "list_files",
            "directory_tree",
            "find_files",
            "search_code",
            "project_overview",
            "write_file",
            "edit_file",
            "insert_lines",
            "append_to_file",
            "create_directory",
            "copy_path",
            "move_path",
            "delete_path",
            "diff_files",
            "file_info",
            "create_archive",
            "extract_archive",
            "read_data",
        ),
    ),
    "shell": ToolsetEntry(
        id="shell",
        module="shell",
        name="Terminal",
        summary="Run shell commands and Python snippets, with live output.",
        caution="Commands are classified by risk; you approve anything above your comfort level.",
        icon="terminal",
        default=True,
        examples=("run the tests", "install dependencies", "check git status"),
        tools=(
            "run_command",
            "explain_command",
            "stop_command",
            "run_python",
        ),
    ),
    "git": ToolsetEntry(
        id="git",
        module="git_tools",
        name="Git",
        summary="Read history, diffs and branches, stage files and commit.",
        caution="Committing changes your repository; reading never does.",
        icon="branch",
        default=True,
        examples=("what changed since yesterday?", "commit this with a good message"),
        tools=(
            "git_status",
            "git_diff",
            "git_log",
            "git_branch",
            "git_show",
            "git_stage",
            "git_commit",
        ),
    ),
    "web": ToolsetEntry(
        id="web",
        module="web",
        name="Web",
        summary="Search the web, read pages as clean text, and download files.",
        caution="Sends your queries to a search engine and fetches remote content.",
        icon="globe",
        requires=("ddgs",),
        default=True,
        examples=("look up the current API docs", "summarise this URL"),
        tools=(
            "web_search",
            "read_webpage",
            "download_file",
            "http_request",
        ),
    ),
    "system": ToolsetEntry(
        id="system",
        module="system",
        name="System",
        summary="Inspect hardware, processes, disks, network and environment.",
        caution="Read-only by default; killing a process always asks first.",
        icon="cpu",
        requires=("psutil",),
        examples=("what's eating my RAM?", "is port 8080 in use?"),
        tools=(
            "system_info",
            "list_processes",
            "kill_process",
            "disk_usage",
            "network_info",
            "environment_info",
            "which_program",
            "service_status",
            "ping_host",
            "resolve_host",
        ),
    ),
    "desktop": ToolsetEntry(
        id="desktop",
        module="desktop",
        name="Desktop control",
        summary="Manage windows and workspaces, screenshots, clipboard, volume, notifications.",
        caution="Can move your windows and type on your behalf. Works on Hyprland, Sway, X11, macOS and Windows.",
        icon="layout",
        examples=("tile my editor and browser", "move this window to workspace 3"),
        tools=(
            "list_windows",
            "active_window",
            "control_window",
            "move_window",
            "resize_window",
            "snap_window",
            "tile_windows",
            "list_workspaces",
            "move_window_to_workspace",
            "switch_workspace",
            "screen_info",
            "take_screenshot",
            "read_clipboard",
            "write_clipboard",
            "send_notification",
            "open_in_desktop",
            "media_control",
            "battery_status",
            "lock_screen",
            "get_brightness",
            "set_brightness",
            "launch_app",
            "type_text",
            "press_keys",
        ),
    ),
    "mcp": ToolsetEntry(
        id="mcp",
        module="mcp_bridge",
        name="MCP servers",
        summary="Tools from external Model Context Protocol servers you connect.",
        caution="Third-party code that ArcBot cannot sandbox — its tools always ask before running.",
        icon="plug",
        requires=("mcp",),
        examples=("fetch a URL as markdown", "query a SQLite database"),
    ),
    "custom": ToolsetEntry(
        id="custom",
        module="custom",
        name="Your own tools",
        summary="Tools you built yourself in the tool builder.",
        caution="Runs code you reviewed and approved, with the capability you granted it.",
        icon="wrench",
        examples=("check my calendar", "post to my team's webhook"),
    ),
    "memory": ToolsetEntry(
        id="memory",
        module="memory_tools",
        name="Long-term memory",
        summary="Remember facts, preferences and past work across sessions.",
        caution="Stored locally in your workspace; nothing is uploaded.",
        icon="brain",
        default=True,
        examples=("remember that I prefer tabs", "what did we decide last week?"),
        tools=(
            "remember",
            "recall",
            "list_memories",
            "forget",
            "memory_stats",
            "clear_memories",
        ),
    ),
}

#: Toolsets a fresh install starts with.
DEFAULT_TOOLSETS: list[str] = [t.id for t in CATALOG.values() if t.default]
#: Toolsets that cannot be disabled.
ALWAYS_ON: list[str] = [t.id for t in CATALOG.values() if t.always_on]
#: ``tool name -> toolset id`` for every toolset, loaded or not.
TOOL_OWNER: dict[str, str] = {
    name: entry.id for entry in CATALOG.values() for name in entry.tools
}


def owning_toolset(tool_name: str) -> ToolsetEntry | None:
    """Which toolset provides *tool_name*, even if it was never imported."""
    owner = TOOL_OWNER.get(tool_name)
    return CATALOG.get(owner) if owner else None


def normalise(toolset_ids) -> list[str]:
    """Drop unknown ids and force the always-on ones back in."""
    seen = [t for t in dict.fromkeys(toolset_ids or []) if t in CATALOG]
    for required in ALWAYS_ON:
        if required not in seen:
            seen.insert(0, required)
    return seen


def catalog_payload() -> list[dict]:
    return [entry.to_dict() for entry in CATALOG.values()]
