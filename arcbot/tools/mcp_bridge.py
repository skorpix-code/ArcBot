"""Connect external MCP servers and expose their tools to the agent.

An MCP server is third-party code. ArcBot cannot sandbox it — a filesystem
server reads whatever its own config allows, regardless of ArcBot's workspace —
so every tool it provides is treated as state-changing and goes through the
permission engine like any other write.

Servers are connected in the background at startup: a broken or slow server
logs a warning and is skipped rather than delaying the app.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from .registry import ToolResult, ToolSpec

log = get_logger("tools.mcp")

#: Prefix that keeps MCP tool names from colliding with ArcBot's own.
PREFIX = "mcp"
#: A server that cannot start within this many seconds is skipped.
CONNECT_TIMEOUT = 25.0
#: Per-call ceiling; an MCP server has no other way to be interrupted.
CALL_TIMEOUT = 120.0


def mcp_available() -> bool:
    """Is the ``mcp`` package installed?"""
    import importlib.util

    return importlib.util.find_spec("mcp") is not None


@dataclass
class ServerStatus:
    name: str
    connected: bool = False
    tools: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "tools": self.tools,
            "error": self.error,
        }


def tool_name(server: str, tool: str) -> str:
    """``fetch`` + ``fetch_url`` -> ``mcp__fetch__fetch_url``."""
    safe_server = "".join(c if c.isalnum() else "_" for c in server)
    safe_tool = "".join(c if c.isalnum() or c == "_" else "_" for c in tool)
    return f"{PREFIX}__{safe_server}__{safe_tool}"


class _Connection:
    """One server, owning its own async context for its whole lifetime.

    The MCP client is built on anyio task groups, which refuse to be entered in
    one task and exited in another.  So each connection runs as a dedicated task
    that opens the transport, signals readiness, parks until asked to stop, and
    unwinds in the same task it started in.  Tool calls come from other tasks,
    which is fine — those are just messages over the open streams.
    """

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.status = ServerStatus(name)
        self.session: Any = None
        self.tools: list[Any] = []
        self.ready = asyncio.Event()
        self._stop = asyncio.Event()
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name=f"mcp:{self.name}")

    async def _run(self) -> None:
        try:
            async with self._transport() as streams:
                from mcp import ClientSession

                async with ClientSession(streams[0], streams[1]) as session:
                    await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT)
                    listed = await asyncio.wait_for(session.list_tools(), timeout=CONNECT_TIMEOUT)
                    self.session = session
                    self.tools = list(listed.tools)
                    self.status = ServerStatus(self.name, connected=True)
                    self.ready.set()
                    await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.status = ServerStatus(self.name, error=f"timed out after {CONNECT_TIMEOUT:.0f}s")
        except Exception as exc:  # any failure is a skipped server, not a crash
            self.status = ServerStatus(self.name, error=explain(exc))
        finally:
            self.session = None
            self.ready.set()

    def _transport(self):
        config = self.config
        if config.get("url"):
            from mcp.client.sse import sse_client

            return sse_client(config["url"], headers=config.get("headers") or None)

        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = config.get("command") or ""
        if not command:
            raise ValueError("no command or url configured")
        # Python servers run under our own interpreter so their imports match,
        # and so `python` resolves on Windows.
        if command in ("python", "python3", "py"):
            command = sys.executable
        elif not shutil.which(command):
            raise FileNotFoundError(f"{command!r} is not installed or not on PATH")
        return stdio_client(
            StdioServerParameters(
                command=command,
                args=list(config.get("args") or []),
                env={**os.environ, **(config.get("env") or {})},
            )
        )

    async def close(self) -> None:
        self._stop.set()
        task = self.task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass


class MCPBridge:
    """Owns the connections and registers each server's tools."""

    def __init__(self, registry) -> None:
        self.registry = registry
        self._connections: dict[str, _Connection] = {}
        self.status: dict[str, ServerStatus] = {}

    # ------------------------------------------------------------- lifecycle
    async def connect_all(self, servers: dict[str, dict[str, Any]]) -> dict[str, ServerStatus]:
        """Connect every enabled server in parallel, tolerating failures."""
        await self.close()
        enabled = {
            name: config for name, config in (servers or {}).items()
            if config.get("enabled", True)
        }
        if not enabled:
            return {}
        if not mcp_available():
            self.status = {
                name: ServerStatus(name, error="the `mcp` package is not installed")
                for name in enabled
            }
            return self.status

        for name, config in enabled.items():
            connection = _Connection(name, config)
            connection.start()
            self._connections[name] = connection

        await asyncio.gather(
            *(self._await_ready(c) for c in self._connections.values()),
            return_exceptions=True,
        )

        for name, connection in self._connections.items():
            if connection.session is not None:
                registered = []
                for tool in connection.tools:
                    spec = self._build_spec(name, connection, tool)
                    self.registry.register_dynamic(spec)
                    registered.append(spec.name)
                connection.status.tools = registered
                log.info("MCP server %r connected with %d tool(s).", name, len(registered))
            self.status[name] = connection.status
        return self.status

    @staticmethod
    async def _await_ready(connection: _Connection) -> None:
        try:
            await asyncio.wait_for(connection.ready.wait(), timeout=CONNECT_TIMEOUT + 5)
        except asyncio.TimeoutError:
            connection.status = ServerStatus(connection.name, error="did not become ready in time")

    def _build_spec(self, server: str, connection: _Connection, tool: Any) -> ToolSpec:
        schema = _first_attr(tool, "input_schema", "inputSchema") or {}
        description = (getattr(tool, "description", "") or "").strip()
        remote_name = tool.name

        async def call(**kwargs: Any) -> ToolResult:
            session = connection.session
            if session is None:
                return ToolResult.error(
                    f"The '{server}' MCP server is not connected. Reconnect it in Settings."
                )
            try:
                result = await asyncio.wait_for(
                    session.call_tool(remote_name, arguments=kwargs), timeout=CALL_TIMEOUT
                )
            except asyncio.TimeoutError:
                return ToolResult.error(
                    f"{server}/{remote_name} did not respond within {CALL_TIMEOUT:.0f}s."
                )
            except Exception as exc:  # server-side failure is data, not a crash
                return ToolResult.error(f"{server}/{remote_name} failed: {exc}")
            failed = bool(_first_attr(result, "is_error", "isError"))
            return ToolResult(not failed, _flatten(result))

        call.__name__ = tool_name(server, remote_name)
        return ToolSpec(
            name=tool_name(server, remote_name),
            description=(
                f"{description}\n\n(Provided by the '{server}' MCP server.)"
                if description else f"'{remote_name}' from the '{server}' MCP server."
            ),
            parameters=_sanitize_schema(schema),
            fn=call,
            toolset="mcp",
            # Third-party code outside ArcBot's sandbox: never treated as read-only.
            capability="write",
            title=f"{server}: {remote_name}",
            is_async=True,
        )

    async def close(self) -> None:
        self.registry.unregister_dynamic(f"{PREFIX}__")
        connections = list(self._connections.values())
        self._connections.clear()
        self.status.clear()
        for connection in connections:
            try:
                await connection.close()
            except Exception as exc:  # shutdown must never raise
                log.debug("MCP shutdown error for %r: %s", connection.name, exc)


def _first_attr(obj: Any, *names: str) -> Any:
    """Read whichever spelling this SDK version uses.

    The MCP Python SDK renamed several wire fields between v1 and v2
    (``inputSchema`` -> ``input_schema``, ``isError`` -> ``is_error``), and a
    silently-missed rename means tools get an empty schema and no arguments.
    """
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def explain(exc: BaseException, depth: int = 0) -> str:
    """A message the user can act on.

    The MCP client runs servers inside task groups, so a plain ``str(exc)``
    is usually "unhandled errors in a TaskGroup" — true, and useless. Unwrap to
    the first real cause instead.
    """
    nested = getattr(exc, "exceptions", None)
    if nested and depth < 4:
        return explain(nested[0], depth + 1)
    if isinstance(exc, FileNotFoundError) and exc.filename:
        return f"{exc.filename!r} is not installed or not on PATH"
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    if isinstance(exc, ImportError):
        return f"the server failed to start: {text}"[:220]
    return f"{type(exc).__name__}: {text}"[:220]


def _flatten(result: Any) -> str:
    """Turn an MCP tool result into plain text for the model."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
            continue
        kind = getattr(block, "type", "")
        if kind == "image":
            parts.append("[image returned — ArcBot cannot display it here]")
        elif kind == "resource":
            resource = getattr(block, "resource", None)
            parts.append(getattr(resource, "text", None) or f"[resource {getattr(resource, 'uri', '')}]")
    return "\n".join(parts).strip() or "(no output)"


def _sanitize_schema(schema: Any) -> dict[str, Any]:
    """Make a third-party schema safe to hand to any provider."""
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    cleaned = {
        "type": "object",
        "properties": schema.get("properties") or {},
        "required": [r for r in (schema.get("required") or []) if isinstance(r, str)],
    }
    if not isinstance(cleaned["properties"], dict):
        cleaned["properties"] = {}
    return cleaned


#: Servers offered as one-click presets in Settings.  Chosen because they are
#: widely used, need no API key, and are useful the moment they connect.
PRESETS: list[dict[str, Any]] = [
    {
        "id": "fetch",
        "name": "Fetch",
        "summary": "Fetch a URL and convert it to markdown.",
        "requires": "uvx",
        "config": {"command": "uvx", "args": ["mcp-server-fetch"]},
    },
    {
        "id": "time",
        "name": "Time",
        "summary": "Current time and timezone conversion.",
        "requires": "uvx",
        "config": {"command": "uvx", "args": ["mcp-server-time"]},
    },
    {
        "id": "sequential-thinking",
        "name": "Sequential thinking",
        "summary": "A scratchpad for working through hard problems step by step.",
        "requires": "npx",
        "config": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        },
    },
    {
        "id": "git",
        "name": "Git",
        "summary": "Read repository history, diffs and blame.",
        "requires": "uvx",
        "config": {"command": "uvx", "args": ["mcp-server-git"]},
    },
    {
        "id": "sqlite",
        "name": "SQLite",
        "summary": "Query a local SQLite database.",
        "requires": "uvx",
        "config": {"command": "uvx", "args": ["mcp-server-sqlite", "--db-path", "./data.db"]},
    },
]


def presets_payload() -> list[dict[str, Any]]:
    """Presets plus whether the machine can actually run each one."""
    return [
        {**preset, "available": shutil.which(preset["requires"]) is not None}
        for preset in PRESETS
    ]
