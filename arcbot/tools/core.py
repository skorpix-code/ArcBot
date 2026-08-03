"""Always-available tools: planning, asking the user, and unlocking capabilities.

These three exist so the agent is never stuck.  If it needs a capability it
does not have, it asks for it; if it needs a fact only the user knows, it asks
for that; and for anything longer than a couple of steps it writes a plan the
user can watch.
"""

from __future__ import annotations

from typing import Literal

from ..events import Ask
from .catalog import CATALOG
from .registry import ToolResult, ctx, tool

TOOLSET = "always"


@tool(
    toolset=TOOLSET,
    capability="read",
    title="Plan: {steps}",
    preview_chars=1200,
)
async def write_plan(steps: list[str]) -> ToolResult:
    """Record the plan for a multi-step task, replacing any previous plan.

    Call this once at the start of anything that needs more than two steps, then
    keep it current with update_plan. The user sees this plan live, so write the
    steps as short, concrete outcomes ("run the test suite", not "step 3").
    Skip it entirely for single-step requests.

    Args:
        steps: Ordered list of short step descriptions.
    """
    context = ctx()
    items = context.todos.replace(steps)
    await context.push_todos()
    return ToolResult(True, context.todos.summary(), {"items": items})


@tool(toolset=TOOLSET, capability="read", title="Update plan")
async def update_plan(
    task: str,
    status: Literal["pending", "in_progress", "done", "blocked"],
    note: str = "",
) -> ToolResult:
    """Move one plan step to a new status. Mark a step in_progress before you start
    it and done the moment it is finished, so the user can follow along.

    Args:
        task: The step's id or its exact title.
        status: New status for the step.
        note: Optional one-line detail (e.g. why a step is blocked).
    """
    context = ctx()
    item = context.todos.set_status(task, status, note)
    if item is None:
        return ToolResult.error(
            f"No plan step matches '{task}'. Current plan:\n{context.todos.summary()}"
        )
    await context.push_todos()
    return ToolResult(True, context.todos.summary(), {"items": context.todos.list_tasks_raw()})


@tool(toolset=TOOLSET, capability="read", title="Ask: {question}")
async def ask_user(question: str, options: list[str] | None = None) -> ToolResult:
    """Ask the user a question and wait for their answer.

    Use this when a decision is genuinely theirs — an ambiguous requirement, a
    missing credential, or a choice between approaches with different tradeoffs.
    Do not use it for things you can determine yourself by reading files or
    running a read-only command.

    Args:
        question: The question, phrased so it can be answered in one line.
        options: Optional list of suggested answers to offer as buttons.
    """
    context = ctx()
    kind = Ask.CHOICE if options else Ask.INPUT
    result = await context.permissions.broker.ask(
        kind,
        {"question": question, "options": list(options or [])},
        default="answer",
    )
    if result.timed_out:
        return ToolResult.error(
            "The user did not answer. Continue with your best judgement and say "
            "clearly which assumption you made."
        )
    answer = (result.value or "").strip()
    if not answer:
        return ToolResult.error("The user declined to answer; proceed with your best judgement.")
    return ToolResult(True, f"The user answered: {answer}")


@tool(toolset=TOOLSET, capability="read", title="Enable {toolset}")
async def enable_toolset(toolset: str, reason: str) -> ToolResult:
    """Request a capability that is currently switched off.

    If a task needs tools you do not have — running commands, browsing the web,
    controlling windows — call this instead of giving up or improvising. The user
    gets a one-click prompt, and if they accept, the new tools become available
    immediately and you should carry straight on with the task.

    Args:
        toolset: Which capability to unlock. One of: files, shell, web, system, desktop, memory.
        reason: One sentence on why the task needs it, shown to the user.
    """
    context = ctx()
    key = (toolset or "").strip().lower()
    entry = CATALOG.get(key)
    if entry is None:
        available = ", ".join(k for k in CATALOG if k != "core")
        return ToolResult.error(f"No toolset called '{toolset}'. Available: {available}.")
    if entry.id in context.settings.toolsets:
        return ToolResult(True, f"'{entry.name}' is already enabled.")

    missing = entry.missing_requirements()
    if missing:
        return ToolResult.error(
            f"'{entry.name}' needs Python package(s) {', '.join(missing)}, which are not "
            f"installed. Tell the user to run: pip install {' '.join(missing)}"
        )

    result = await context.permissions.broker.ask(
        Ask.TOOLSET,
        {
            "toolset": entry.id,
            "name": entry.name,
            "summary": entry.summary,
            "caution": entry.caution,
            "reason": reason,
        },
        default="deny",
    )
    if not result.approved:
        return ToolResult.error(
            f"The user declined to enable '{entry.name}'. Do not ask again this turn — "
            f"solve the task another way, or explain what you cannot do without it."
        )

    if context.enable_toolset is None:
        return ToolResult.error("Toolsets cannot be changed in this context.")
    error = await context.enable_toolset(entry.id)
    if error:
        return ToolResult.error(f"Could not enable '{entry.name}': {error}")

    await context.notice(f"{entry.name} enabled.", "success")
    return ToolResult(
        True,
        f"'{entry.name}' is now enabled and its tools are available. Continue with the task.",
        {"toolset": entry.id},
    )


@tool(toolset=TOOLSET, capability="read", title="Check ArcBot", preview_chars=1600)
async def arcbot_status() -> ToolResult:
    """Report what ArcBot is and how it is currently configured.

    Call this when the user asks about you, about what you can do, or when a
    task depends on your own setup — your workspace, your trust level, which
    capabilities are on, or which model is driving you. It is faster and more
    accurate than inspecting the machine with shell commands.
    """
    context = ctx()
    settings = context.settings
    host = context.describe_host() if context.describe_host else {}

    enabled = list(settings.toolsets)
    off = [e.id for e in CATALOG.values()
           if e.id not in enabled and not e.always_on and e.available]

    lines = [
        "ArcBot is a local application running on this machine. You are the agent inside it.",
        "The user sees a web interface: a live trace of everything you do, a plan panel, "
        "capability switches, a terminal panel, and approval prompts.",
        "",
        f"Workspace: {settings.workspace_path}",
        f"Trust level: {settings.permissions.mode}",
        f"Model: {settings.model.model or '(provider default)'} via {settings.model.provider}",
        f"Capabilities on: {', '.join(enabled) or 'none'}",
        f"Capabilities off: {', '.join(off) or 'none'}",
    ]
    if host.get("url"):
        lines.append(f"Serving at: {host['url']}")
    if host.get("uptimeSeconds") is not None:
        lines.append(f"Running for: {int(host['uptimeSeconds'])}s")
    if host.get("mcpServers"):
        lines.append(f"MCP servers: {', '.join(host['mcpServers'])}")
    if host.get("customTools"):
        lines.append(f"Tools the user built: {', '.join(host['customTools'])}")
    if host.get("canQuit"):
        lines.append("You can shut ArcBot down yourself with quit_arcbot.")
    return ToolResult(True, "\n".join(lines), host)


@tool(toolset=TOOLSET, capability="write", title="Quit ArcBot")
async def quit_arcbot(reason: str = "") -> ToolResult:
    """Shut ArcBot down.

    Use this when the user asks you to close, quit, exit or stop ArcBot. Finish
    anything outstanding first — once this runs, the app is gone and you cannot
    reply again. Say goodbye in the same turn, before calling it.

    Args:
        reason: One short line explaining why, shown to the user as it closes.
    """
    context = ctx()
    if context.request_quit is None:
        return _no_host()
    await context.notice(reason or "Shutting down, as asked.", "info")
    stopping = await context.request_quit(reason or "the agent was asked to quit")
    if not stopping:
        return _no_host()
    return ToolResult(True, "ArcBot is shutting down. Do not call any more tools.")


def _no_host() -> ToolResult:
    return ToolResult.error(
        "Nothing is listening for a shutdown in this session, so ArcBot cannot close "
        "itself from here. Tell the user to close the ArcBot window instead."
    )


@tool(toolset=TOOLSET, capability="read", title="Open settings: {panel}")
async def open_settings(
    panel: Literal["model", "trust", "tools", "mcp"] = "tools",
    reason: str = "",
) -> ToolResult:
    """Open a settings panel in the user's browser so they can change something.

    Use this when a task needs a change only the user can make — a different
    trust level, an API key, a capability you cannot request yourself. It opens
    the right panel for them rather than describing where to click.

    Args:
        panel: Which panel to show. model, trust, tools or mcp.
        reason: One sentence on what they should change, shown above the panel.
    """
    context = ctx()
    if context.open_settings is None:
        return ToolResult.error("No user interface is attached to this session.")
    await context.open_settings(panel)
    if reason:
        await context.notice(reason, "info")
    return ToolResult(True, f"Opened the {panel} settings panel for the user.")
