"""Terminal tools.

``run_command`` is the agent's most powerful tool, so it is also the most
carefully gated: every invocation is risk-classified, routed through the
permission engine, streamed live to the UI, and — when a program stops to ask a
question — handed back to the model (or auto-answered) instead of hanging.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from ..interactive import summarize_for_model
from ..redirects import find_redirect
from ..security import Risk, classify_command
from .registry import ToolResult, ctx, tool

TOOLSET = "shell"

#: Short, safe replies the autopilot may send without asking again.
_SAFE_ANSWERS = {"y", "yes", "n", "no", ""}


@tool(
    toolset=TOOLSET,
    capability="exec",
    title="$ {command}",
    preview_chars=2000,
    self_gated=True,      # asks with the command and its risk, not a generic prompt
)
async def run_command(
    command: str, timeout: int = 0, reason: str = "", force: bool = False
) -> ToolResult:
    """Run a shell command in the workspace and return its output.

    Reach for this only when no tool covers the job. Reading, listing, searching,
    editing, deleting, fetching a URL, inspecting the machine — all have proper
    tools that are safer, return structured results and need less approval. If
    you call a command one of those covers, you will be redirected to it.

    The user sees the command and its live output. Commands above their trust
    level need approval, so keep each call to one clear purpose.

    If a command pauses for input (a [Y/n] prompt, a menu, "press enter"), you
    will be told what it is asking — call run_command again with just the answer
    (for example "y", "2", or "" for Enter).

    Args:
        command: The command line to run, or the answer to a paused prompt.
        timeout: Seconds before the command is killed; 0 uses the configured default.
        reason: One short sentence on why this command is needed, shown in the approval prompt.
        force: Run it even if a tool covers the same job. Only set this when the tool genuinely cannot do what you need.
    """
    context = ctx()
    terminal = context.terminal
    if terminal is None:
        return ToolResult.error("No terminal is attached to this session.")

    text = (command or "").strip()
    answering = terminal.has_live_command

    if not text and not answering:
        return ToolResult.error("Empty command.")

    # A purpose-built tool beats a shell command whenever one exists: the result
    # is structured, the path is sandboxed, and it needs less approval.
    if not answering and not force:
        redirect = find_redirect(text)
        if redirect is not None:
            enabled = set(context.settings.toolsets) | {"always"}
            return ToolResult(False, redirect.message(enabled),
                              {"redirectedTo": redirect.tool})

    # An answer to a paused program is judged as an answer, not as a new command.
    if answering:
        if text.lower() in _SAFE_ANSWERS or text.isdigit():
            if not context.settings.permissions.autopilot_prompts:
                verdict = await context.permissions.check_command(f"[answer] {text}", context=reason)
                if not verdict.allowed:
                    return ToolResult.error(verdict.reason)
        else:
            verdict = await context.permissions.check_command(text, context=reason or "answer to a prompt")
            if not verdict.allowed:
                return ToolResult.error(verdict.reason)
        await context.progress(f"\r\n\x1b[36m» {text}\x1b[0m\r\n")
    else:
        verdict = await context.permissions.check_command(text, context=reason)
        if not verdict.allowed:
            return ToolResult.error(verdict.reason)
        await context.progress(f"\r\n\x1b[1;32m$\x1b[0m {text}\r\n")

    limit = timeout if timeout and timeout > 0 else context.settings.limits.command_timeout
    outcome = await terminal.run(text, timeout=float(limit))
    output = context.clip(outcome.output.strip())

    if outcome.status == "paused":
        prompt_text = summarize_for_model(outcome.prompt) if outcome.prompt else (
            "The process is waiting for input. Call run_command again with just the answer."
        )
        await context.progress("\r\n\x1b[33m[waiting for input]\x1b[0m\r\n")
        return ToolResult(
            True,
            f"COMMAND PAUSED — it is waiting for input.\n\nOutput so far:\n{output}\n\n{prompt_text}",
            {"paused": True, "prompt": outcome.prompt.to_dict() if outcome.prompt else None},
        )

    if outcome.status == "timeout":
        return ToolResult(
            False,
            f"Command timed out after {limit}s and was stopped.\n\nOutput so far:\n{output}",
            {"timedOut": True},
        )
    if outcome.status == "error":
        return ToolResult.error(outcome.output or "The command could not be started.")

    code = outcome.exit_code
    await context.progress(f"\r\n\x1b[90m[exit {code}]\x1b[0m\r\n")
    header = f"Exit code: {code}"
    body = output or "(no output)"
    return ToolResult(code == 0, f"{header}\n\n{body}", {"exitCode": code})


@tool(toolset=TOOLSET, capability="read", title="Check: {command}")
def explain_command(command: str) -> ToolResult:
    """Check how risky a command is before proposing it, without running it.

    Useful when you are unsure whether something needs the user's approval, or
    when the user asks what a command would do.

    Args:
        command: The command line to assess.
    """
    context = ctx()
    risk = classify_command(command, context.roots)
    lines = [
        f"Command: {command}",
        f"Risk: {risk.level.label}",
        "Why: " + ("; ".join(risk.reasons) or "no notable patterns"),
    ]
    if risk.outside_paths:
        lines.append("Touches outside the workspace: " + ", ".join(risk.outside_paths))
    if risk.level >= Risk.BLOCKED:
        lines.append("This command is blocked and will never run.")
    return ToolResult(True, "\n".join(lines), risk.to_dict())


@tool(toolset=TOOLSET, capability="exec", title="Stop running command", self_gated=True)
async def stop_command() -> ToolResult:
    """Interrupt the command that is currently running or paused.

    Use this when a command is stuck, is waiting on something that will not
    arrive, or was started by mistake.
    """
    context = ctx()
    if context.terminal is None:
        return ToolResult.error("No terminal is attached.")
    stopped = await context.terminal.interrupt()
    return ToolResult(True, "Sent an interrupt to the running command." if stopped
                      else "Nothing is running right now.")


@tool(toolset=TOOLSET, capability="exec", title="Run Python", preview_chars=2000)
async def run_python(code: str, timeout: int = 60) -> ToolResult:
    """Run a short Python script and return whatever it prints.

    Use this for anything computational — parsing, arithmetic, reshaping data,
    checking a hypothesis — instead of chaining shell utilities. It runs in the
    workspace with the same interpreter as ArcBot, so the standard library is
    available. Print what you want to see; nothing is returned automatically.

    Args:
        code: The Python source to run.
        timeout: Seconds before it is stopped.
    """
    context = ctx()
    source = (code or "").strip()
    if not source:
        return ToolResult.error("No code given.")

    verdict = await context.permissions.check_command(
        f"python (inline script, {len(source)} chars)", context="running a Python snippet"
    )
    if not verdict.allowed:
        return ToolResult.error(verdict.reason)

    script = context.state_dir / "scratch.py"
    try:
        script.write_text(source, encoding="utf-8")
    except OSError as exc:
        return ToolResult.error(f"Could not write the script: {exc}")

    await context.progress(f"\r\n\x1b[1;32m$\x1b[0m python {script.name}\r\n")
    limit = max(1, min(int(timeout or 60), 600))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(context.workspace),
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return ToolResult(False, f"The script did not finish within {limit}s and was stopped.")
    except OSError as exc:
        return ToolResult.error(f"Could not run Python: {exc}")
    finally:
        script.unlink(missing_ok=True)

    output = context.clip(raw.decode(errors="replace").strip())
    if proc.returncode == 0:
        return ToolResult(True, output or "(the script printed nothing)", {"exitCode": 0})
    return ToolResult(False, f"Python exited with code {proc.returncode}.\n\n{output}",
                      {"exitCode": proc.returncode})
