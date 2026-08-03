"""Give a delegated agent access to ArcBot's own controls.

A provider that runs its own loop (Claude Code) brings its own tools, so it
cannot see ``arcbot_status``, ``enable_toolset``, ``ask_user`` or
``quit_arcbot`` — the very tools that let an agent reason about, and act on, its
own situation.  Without them it improvises: it works around a switched-off
capability, or tells the user to press Ctrl+C in a terminal that is not there.

The fix is to hand it those tools the way it already accepts tools: as an MCP
server.  This module *is* that server.  It is launched by the provider as a
short-lived subprocess and calls straight back into the running ArcBot over its
own authenticated local API, so there is one implementation of each control
rather than two that can drift.

Run as: ``python -m arcbot.agentbridge`` with ``ARCBOT_URL`` and
``ARCBOT_TOKEN`` in the environment.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

TIMEOUT = 900.0  # a question to the user can sit unanswered for a long time


def _call(action: str, payload: dict[str, Any]) -> str:
    """POST one control action back to the ArcBot that spawned us."""
    base = os.environ.get("ARCBOT_URL", "").rstrip("/")
    token = os.environ.get("ARCBOT_TOKEN", "")
    if not base or not token:
        return "ArcBot's control API is not reachable from here."

    request = urllib.request.Request(
        f"{base}/api/agent/{action}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-arcbot-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        return f"ArcBot refused that: {detail or exc.reason}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"Could not reach ArcBot: {exc}"
    return str(body.get("result", "")) or "Done."


def build_server():
    """Construct the MCP server exposing ArcBot's controls."""
    from mcp.server.mcpserver import MCPServer

    app = MCPServer("arcbot")

    @app.tool()
    def arcbot_status() -> str:
        """Report what ArcBot is and how it is currently configured.

        Call this when the user asks about you, about what you can do, or when a
        task depends on your own setup — the workspace, the trust level, which
        capabilities are on, which model is driving you. Faster and more accurate
        than inspecting the machine with shell commands.
        """
        return _call("status", {})

    @app.tool()
    def enable_capability(capability: str, reason: str) -> str:
        """Ask the user to switch on a capability that is currently off.

        If a task needs something you do not have — desktop control, web access,
        the system tools — call this instead of working around it with shell
        commands. The user gets a one-click prompt; if they accept, tell them it
        is on and carry on with the task.

        Args:
            capability: One of files, shell, web, system, desktop, memory, git.
            reason: One sentence on why the task needs it, shown to the user.
        """
        return _call("enable", {"toolset": capability, "reason": reason})

    @app.tool()
    def ask_the_user(question: str) -> str:
        """Ask the user a question and wait for their answer.

        For decisions that are genuinely theirs: an ambiguous requirement, a
        missing credential, a choice between approaches with different tradeoffs.
        Not for things you can find out yourself.

        Args:
            question: The question, phrased so it can be answered in one line.
        """
        return _call("ask", {"question": question})

    @app.tool()
    def quit_arcbot(reason: str = "") -> str:
        """Close ArcBot.

        Use this when the user asks you to close, quit, exit or stop ArcBot.
        Say goodbye in the same turn before calling it — once it runs, the app is
        gone and you cannot reply again.

        Args:
            reason: One short line explaining why, shown to the user as it closes.
        """
        return _call("quit", {"reason": reason})

    @app.tool()
    def open_settings(panel: str = "tools", reason: str = "") -> str:
        """Open a settings panel in the user's browser.

        Use this when a task needs a change only the user can make — a different
        trust level, an API key, a capability you cannot request yourself.

        Args:
            panel: model, trust, tools or mcp.
            reason: One sentence on what they should change.
        """
        return _call("settings", {"panel": panel, "reason": reason})

    return app


#: The tool names this server exposes, so the provider can allow-list them.
TOOL_NAMES = ("arcbot_status", "enable_capability", "ask_the_user", "quit_arcbot", "open_settings")


def main() -> int:
    try:
        build_server().run()
    except Exception as exc:  # the host logs stderr; never crash noisily
        print(f"arcbot bridge failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
