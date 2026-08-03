"""Claude, without an API key — through an authenticated Claude Code CLI.

This is the "sign in on the website" path.  It does **not** reverse-engineer any
login: it drives the official ``claude`` binary, which the user has already
signed into with their Anthropic account, exactly as the editor extensions do.
Billing, rate limits and model access all follow that account's plan.

Claude Code runs its own agent loop, so this provider reports in *agent* mode:
it streams the assistant's text plus the tool calls Claude Code executed, and
ArcBot renders them with the same UI as its own tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..agentbridge import TOOL_NAMES as AGENT_CONTROL_TOOLS
from ..logging_setup import get_logger
from .auth import claude_cli_path
from .base import Chunk, Provider, ProviderError, Usage

log = get_logger("provider.claude-code")

#: ArcBot permission mode -> Claude Code's own permission mode.
_PERMISSION_MODE = {
    "plan": "plan",
    "guarded": "default",
    "trusted": "acceptEdits",
    "full": "bypassPermissions",
}

#: Claude Code tools grouped by the ArcBot toolset that owns them.
_TOOLSET_TOOLS = {
    "shell": ["Bash", "BashOutput", "KillShell"],
    "files": ["Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep"],
    "web": ["WebSearch", "WebFetch"],
}

#: Tools each trust level grants up front.  Claude Code cannot show its own
#: approval prompt in headless mode, so anything not granted here is refused
#: rather than queued — the allow-list *is* the trust level.
_GRANTS: dict[str, dict[str, list[str]]] = {
    "plan": {"files": ["Read", "Glob", "Grep"], "web": ["WebSearch", "WebFetch"]},
    "guarded": {
        "files": ["Read", "Glob", "Grep"],
        "web": ["WebSearch", "WebFetch"],
        "shell": [],          # commands need approval, which headless cannot ask for
    },
    "trusted": {
        "files": ["Read", "Glob", "Grep", "Write", "Edit", "NotebookEdit"],
        "web": ["WebSearch", "WebFetch"],
        "shell": ["Bash", "BashOutput", "KillShell"],
    },
    "full": {},               # bypassPermissions grants everything
}

#: Name the control bridge registers under, which fixes the tool prefix
#: Claude Code generates for it.
CONTROL_SERVER = "arcbot"

#: Always available regardless of toolset — they only affect Claude Code's own
#: bookkeeping, never the user's machine.  ArcBot's own controls are here too:
#: an agent must always be able to ask about itself, request a capability and
#: talk to the user, whatever the trust level.
_ALWAYS_ALLOWED = ["TodoWrite", "Task"] + [
    f"mcp__{CONTROL_SERVER}__{name}" for name in AGENT_CONTROL_TOOLS
]

#: Reasonable default when the CLI does not tell us the context window.
_CONTEXT_WINDOW = 200_000


class ClaudeCodeProvider(Provider):
    mode = "agent"
    label = "Claude Code"
    supports_tools = False   # it brings its own

    def __init__(
        self,
        model: str = "",
        *,
        workspace: str = "",
        permission_mode: str = "guarded",
        enabled_toolsets: list[str] | None = None,
        allow_commands: list[str] | None = None,
        control_url: str = "",
        control_token: str = "",
        extra_args: list[str] | None = None,
    ) -> None:
        binary = claude_cli_path()
        if not binary:
            raise ProviderError(
                "Claude Code is not installed.",
                hint="Install it with `npm install -g @anthropic-ai/claude-code`, "
                     "then run `claude` once and sign in.",
            )
        self.binary = binary
        self.model = model
        self.workspace = workspace or os.getcwd()
        self.permission_mode = permission_mode
        self.enabled_toolsets = list(enabled_toolsets or [])
        self.allow_commands = list(allow_commands or [])
        #: Where the bridge calls back to reach ArcBot's own controls.
        self.control_url = control_url
        self.control_token = control_token
        self.extra_args = list(extra_args or [])
        self._config_path: Path | None = None
        self.context_window = _CONTEXT_WINDOW
        self._session_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------------ argv
    def _build_argv(self, prompt: str, system: str) -> list[str]:
        argv = [
            self.binary,
            "--print", prompt,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode", _PERMISSION_MODE.get(self.permission_mode, "default"),
        ]
        if self.model:
            argv += ["--model", self.model]
        if self._session_id:
            argv += ["--resume", self._session_id]
        if system:
            argv += ["--append-system-prompt", system]

        # Anything the user switched off is refused outright…
        disallowed = [
            tool
            for toolset, tools in _TOOLSET_TOOLS.items()
            if toolset not in self.enabled_toolsets
            for tool in tools
        ]
        if disallowed:
            argv += ["--disallowedTools", ",".join(sorted(set(disallowed)))]

        # ArcBot's own controls, handed over as an MCP server so a delegated
        # agent can reason about and act on its own situation.
        config = self._control_config()
        if config is not None:
            argv += ["--mcp-config", str(config)]

        # …and what remains is granted according to the trust level.
        allowed = self.allowed_tools()
        if allowed:
            argv += ["--allowedTools", ",".join(allowed)]
        return argv + self.extra_args

    def _control_config(self) -> Path | None:
        """Write the `--mcp-config` that points at ArcBot's control bridge."""
        if not (self.control_url and self.control_token):
            return None
        if self._config_path is not None and self._config_path.exists():
            return self._config_path
        payload = {
            "mcpServers": {
                CONTROL_SERVER: {
                    "command": sys.executable,
                    "args": ["-m", "arcbot.agentbridge"],
                    "env": {
                        "ARCBOT_URL": self.control_url,
                        "ARCBOT_TOKEN": self.control_token,
                        "PYTHONPATH": os.pathsep.join(sys.path[:1] + [p for p in sys.path[1:] if p]),
                    },
                }
            }
        }
        fd, name = tempfile.mkstemp(suffix=".json", prefix="arcbot-mcp-", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        self._config_path = Path(name)
        return self._config_path

    def allowed_tools(self) -> list[str]:
        """Translate ArcBot's trust level and saved rules into Claude Code grants.

        ``full`` returns nothing because ``bypassPermissions`` already covers
        everything; adding an allow-list there would only narrow it.
        """
        if self.permission_mode == "full":
            return []
        grants = _GRANTS.get(self.permission_mode, _GRANTS["guarded"])
        allowed = list(_ALWAYS_ALLOWED)
        for toolset, tools in grants.items():
            if toolset in self.enabled_toolsets:
                allowed.extend(tools)
        # A command the user chose to always allow should apply here too, so the
        # two provider modes honour the same saved rules.  Plan mode is exempt:
        # it promises read-only, and a saved rule must not quietly break that.
        if "shell" in self.enabled_toolsets and self.permission_mode != "plan":
            for rule in self.allow_commands:
                cleaned = " ".join((rule or "").split())
                if cleaned and '"' not in cleaned:
                    allowed.append(f"Bash({cleaned}:*)")
        return sorted(set(allowed))

    # ---------------------------------------------------------------- stream
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> AsyncIterator[Chunk]:
        prompt = _latest_user_text(messages)
        if not prompt:
            yield Chunk(done=True, stop_reason="end_turn")
            return

        argv = self._build_argv(prompt, system)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=self.workspace,
                env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "arcbot"},
            )
        except (OSError, FileNotFoundError) as exc:
            raise ProviderError(f"Could not start Claude Code: {exc}") from exc

        self._process = process
        usage = Usage()
        stop_reason = ""
        saw_output = False

        try:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text or not text.startswith("{"):
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    continue

                for chunk in self._translate(event):
                    if chunk.text or chunk.thinking or chunk.tool_event:
                        saw_output = True
                    if chunk.usage:
                        usage.merge(chunk.usage)
                    if chunk.stop_reason:
                        stop_reason = chunk.stop_reason
                    if chunk.error:
                        yield chunk
                        return
                    if not chunk.done:
                        yield chunk

            await process.wait()
            if process.returncode not in (0, None):
                stderr = (await process.stderr.read()).decode(errors="replace")[:600] if process.stderr else ""
                if not saw_output:
                    raise ProviderError(
                        _explain_exit(process.returncode, stderr),
                        hint="Run `claude` in a terminal to check you are signed in.",
                    )
                log.warning("Claude Code exited %s: %s", process.returncode, stderr)
        except asyncio.CancelledError:
            await self._kill()
            raise
        finally:
            self._process = None

        yield Chunk(usage=usage, done=True, stop_reason=stop_reason or "end_turn")

    def _translate(self, event: dict[str, Any]) -> list[Chunk]:
        """Map one Claude Code stream-json object onto ArcBot chunks."""
        kind = event.get("type")

        if kind == "system" and event.get("subtype") == "init":
            self._session_id = event.get("session_id") or self._session_id
            return []

        # Incremental text/thinking while a message is being written.
        if kind == "stream_event":
            inner = event.get("event") or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta":
                    return [Chunk(text=delta.get("text", ""))]
                if delta.get("type") == "thinking_delta":
                    return [Chunk(thinking=delta.get("thinking", ""))]
            return []

        if kind == "assistant":
            chunks: list[Chunk] = []
            for block in (event.get("message") or {}).get("content") or []:
                block_type = block.get("type")
                if block_type == "tool_use":
                    chunks.append(Chunk(tool_event={
                        "phase": "start",
                        "id": block.get("id", ""),
                        "name": block.get("name", "tool"),
                        "input": block.get("input") or {},
                    }))
                # Text arrived already via stream_event deltas, so it is skipped
                # here to avoid printing everything twice.
            return chunks

        if kind == "user":
            chunks = []
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result":
                    chunks.append(Chunk(tool_event={
                        "phase": "end",
                        "id": block.get("tool_use_id", ""),
                        "ok": not block.get("is_error"),
                        "content": _flatten(block.get("content")),
                    }))
            return chunks

        if kind == "result":
            self._session_id = event.get("session_id") or self._session_id
            raw_usage = event.get("usage") or {}
            usage = Usage(
                input_tokens=int(raw_usage.get("input_tokens") or 0),
                output_tokens=int(raw_usage.get("output_tokens") or 0),
                cache_read_tokens=int(raw_usage.get("cache_read_input_tokens") or 0),
                cost_usd=float(event.get("total_cost_usd") or 0.0),
            )
            denials = event.get("permission_denials") or []
            if event.get("is_error"):
                message = event.get("result") or event.get("error") or "Claude Code reported an error."
                return [Chunk(error=str(message)[:600], usage=usage, done=True)]
            chunks = [Chunk(usage=usage, stop_reason=event.get("stop_reason") or "end_turn", done=True)]
            if denials:
                names = ", ".join(sorted({d.get("tool_name", "?") for d in denials}))
                chunks.insert(0, Chunk(text=(
                    f"\n\n_Claude Code was blocked from using: {names}. "
                    f"Raise the trust level in Settings if you want these allowed._"
                )))
            return chunks

        return []

    async def complete(self, prompt: str, *, system: str = "") -> str:
        """Run a one-shot, tool-free prompt.

        Deliberately not `--resume`: this is a standalone question, and mixing it
        into the user's conversation would poison the agent's context.
        """
        argv = [
            self.binary, "--print", prompt,
            "--output-format", "text",
            "--permission-mode", "plan",
            "--disallowedTools",
            "Bash,BashOutput,KillShell,Read,Write,Edit,NotebookEdit,Glob,Grep,"
            "WebSearch,WebFetch,Task,TodoWrite",
        ]
        if self.model:
            argv += ["--model", self.model]
        if system:
            argv += ["--append-system-prompt", system]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=self.workspace,
                env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "arcbot"},
            )
            out, err = await asyncio.wait_for(process.communicate(), timeout=300)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ProviderError(f"Claude Code did not answer: {exc}") from exc
        if process.returncode != 0:
            raise ProviderError(
                _explain_exit(process.returncode, err.decode(errors="replace")),
                hint="Run `claude` in a terminal to check you are signed in.",
            )
        return out.decode(errors="replace")

    # ---------------------------------------------------------------- control
    async def _kill(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass

    async def close(self) -> None:
        await self._kill()
        if self._config_path is not None:
            self._config_path.unlink(missing_ok=True)
            self._config_path = None

    def reset(self) -> None:
        self._session_id = None

    async def health(self) -> tuple[bool, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            text = output.decode(errors="replace").strip()
            if process.returncode == 0:
                return True, text.splitlines()[0] if text else "Claude Code is available."
            return False, text[:200] or "claude --version failed."
        except (asyncio.TimeoutError, OSError) as exc:
            return False, str(exc)


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or json.dumps(block)[:400])
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _explain_exit(code: int | None, stderr: str) -> str:
    lowered = (stderr or "").lower()
    if "not logged in" in lowered or "authentication" in lowered or "unauthorized" in lowered:
        return "Claude Code is not signed in. Run `claude` in a terminal and use /login."
    if "usage limit" in lowered or "rate limit" in lowered:
        return "Your Claude plan's usage limit has been reached. Try again later, or use an API key."
    return f"Claude Code exited with code {code}." + (f" {stderr.strip()[:300]}" if stderr.strip() else "")
