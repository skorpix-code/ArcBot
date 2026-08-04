"""The agent loop.

One turn is: build the prompt → stream the model → run whatever tools it asked
for → repeat until it answers or a guard stops it.  Everything observable is
emitted on the event bus, so the UI is a pure function of the event stream and
the loop has no idea a browser exists.

Providers come in two shapes (see :mod:`arcbot.providers.base`).  A *model*
backend hands back tool calls for ArcBot to run; an *agent* backend (Claude Code
with a subscription) runs its own loop and reports what it did.  Both land in
the same events, so the rest of the system does not care which is in use.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, ClassVar

from . import context as ctxmod
from .asks import AskBroker
from .config import ConfigStore, Settings
from .events import E, EventBus
from .guards import GuardDecision, LoopGuard
from .logging_setup import get_logger
from .permissions import PermissionEngine, Verdict
from .prompts import NUDGES, build_system_prompt
from .providers import Provider, ProviderError, build_provider
from .security import truncate_output
from .session import Session
from .terminal import TerminalManager
from .tools import ToolContext, ToolRegistry
from .tools import normalise as normalise_toolsets
from .tools.mcp_bridge import MCPBridge
from .tools.registry import ToolResult

log = get_logger("agent")

#: Providers with at least this context window get the terser system prompt.
_COMPACT_PROMPT_ABOVE = 400_000
#: Characters of a tool result echoed into the UI card.
_DEFAULT_PREVIEW = 800


class Agent:
    """Owns one workspace's conversation, tools and provider."""

    def __init__(self, store: ConfigStore, bus: EventBus):
        self.store = store
        self.bus = bus
        self.broker = AskBroker(bus)
        self.permissions = PermissionEngine(
            store.settings, self.broker, on_change=lambda: store.save()
        )
        self.registry = ToolRegistry()
        self.mcp = MCPBridge(self.registry)
        #: Live voice conversation, created only when the user starts one.
        self.voice: Any = None
        self.terminal = TerminalManager()
        self.provider: Provider | None = None
        self.session: Session | None = None

        self.messages: list[dict[str, Any]] = []
        self.usage_total = {"inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}
        self._turn_task: asyncio.Task | None = None
        self._cancelled = False
        self._ready = False
        self._tool_context: ToolContext | None = None
        self._started_at = time.monotonic()
        #: Set by the process owner (the CLI) so the agent can shut the app down.
        self.on_quit: Any = None
        #: Where the UI is served, for the agent's own situational awareness.
        self.host_url: str = ""
        #: Token the control bridge authenticates with, set by the web host.
        self.control_token: str = ""

    # ================================================================ lifecycle
    @property
    def settings(self) -> Settings:
        return self.store.settings

    @property
    def busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    async def start(self, session_id: str | None = None) -> None:
        """Bring the agent up: workspace, tools, terminal, provider."""
        settings = self.settings
        workspace = settings.workspace_path
        workspace.mkdir(parents=True, exist_ok=True)

        await self.bus.emit(E.STATUS, {"state": "starting", "detail": "Loading tools…"})

        wanted = normalise_toolsets(settings.toolsets)
        failures = self.registry.load(wanted)
        for toolset_id, error in failures.items():
            await self.bus.emit(E.NOTICE, {
                "level": "warn",
                "text": f"Toolset '{toolset_id}' could not load: {error}",
            })
        settings.toolsets = self.registry.enabled_toolsets
        await self._connect_mcp()

        self.terminal.configure(workspace, on_data=self._on_terminal_data)
        if "shell" in settings.toolsets:
            await self.terminal.start_shell()

        self.session = Session(workspace, session_id)
        if session_id:
            self.messages = ctxmod.normalise(self.session.messages())

        self._tool_context = ToolContext(
            settings=settings,
            bus=self.bus,
            permissions=self.permissions,
            terminal=self.terminal,
            enable_toolset=self._enable_toolset,
            request_quit=self._request_quit,
            open_settings=self._open_settings,
            describe_host=self.describe_host,
        )

        await self._build_provider()
        self._ready = self.provider is not None
        await self.emit_toolsets()
        await self.bus.emit(E.READY, {
            "sessionId": self.session.id,
            "workspace": str(workspace),
            "provider": settings.model.provider,
            "model": settings.model.model,
            "mode": self.provider.mode if self.provider else "model",
            "tools": [s.name for s in self.registry.specs()],
            "messages": _replayable(self.messages),
        })
        await self.bus.emit(E.STATUS, {"state": "ready" if self._ready else "unconfigured"})

    async def _build_provider(self) -> None:
        if self.provider is not None:
            await self.provider.close()
            self.provider = None
        try:
            # A delegated agent reaches ArcBot's controls through a local bridge,
            # so it needs this process's own address and token.
            if self.control_token:
                self.settings.model.options.update({
                    "control_url": self.host_url or "http://127.0.0.1:8765/",
                    "control_token": self.control_token,
                })
            self.provider = build_provider(self.settings, self.store)
        except ProviderError as exc:
            await self.bus.emit(E.ERROR, {
                "message": str(exc), "detail": exc.hint, "recoverable": True,
            })
        except Exception as exc:
            log.exception("Provider construction failed")
            await self.bus.emit(E.ERROR, {"message": f"Could not start the model: {exc}"})

    async def reload(self) -> None:
        """Re-apply settings after the user changes them."""
        await self.stop()
        settings = self.settings
        self.permissions.settings = settings
        if self._tool_context is not None:
            self._tool_context.settings = settings
        failures = self.registry.load(normalise_toolsets(settings.toolsets))
        settings.toolsets = self.registry.enabled_toolsets
        await self._connect_mcp()
        self.terminal.configure(settings.workspace_path)
        if "shell" in settings.toolsets:
            await self.terminal.start_shell()
        await self._build_provider()
        self._ready = self.provider is not None
        for toolset_id, error in failures.items():
            await self.bus.emit(E.NOTICE, {"level": "warn", "text": f"{toolset_id}: {error}"})
        await self.emit_toolsets()
        await self.bus.emit(E.STATUS, {"state": "ready" if self._ready else "unconfigured"})

    async def shutdown(self) -> None:
        await self.stop()
        self.broker.cancel_all()
        await self.mcp.close()
        await self.terminal.stop()
        if self.provider is not None:
            await self.provider.close()

    async def clear(self) -> None:
        """Start a fresh conversation in the same workspace."""
        await self.stop()
        self.messages = []
        self.usage_total = {"inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}
        if self.provider is not None:
            self.provider.reset()
        self.session = Session(self.settings.workspace_path)
        self.permissions.reset_session_rules()
        if self._tool_context is not None:
            self._tool_context.seen_files.clear()
        await self.bus.emit(E.READY, {
            "sessionId": self.session.id,
            "workspace": str(self.settings.workspace_path),
            "provider": self.settings.model.provider,
            "model": self.settings.model.model,
            "mode": self.provider.mode if self.provider else "model",
            "tools": [s.name for s in self.registry.specs()],
            "messages": [],
        })

    # ==================================================================== turns
    async def send(self, text: str) -> None:
        """Queue a user message and run a turn.  Ignored while one is running."""
        if self.busy:
            await self.bus.emit(E.NOTICE, {
                "level": "warn", "text": "Still working on the previous message.",
            })
            return
        if not self._ready or self.provider is None:
            await self.bus.emit(E.ERROR, {
                "message": "No model is configured yet.",
                "detail": "Open Settings and choose a provider.",
                "recoverable": True,
            })
            return
        self._cancelled = False
        self._turn_task = asyncio.create_task(self._run_turn(text))

    async def stop(self) -> None:
        """Cancel the running turn and any command it started."""
        self._cancelled = True
        if self.voice is not None:
            await self.voice.stop_speaking()
        self.broker.cancel_all("deny")
        await self.terminal.interrupt()
        task = self._turn_task
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._turn_task = None

    async def _run_turn(self, text: str) -> None:
        assert self.provider is not None and self.session is not None
        turn_id = uuid.uuid4().hex[:10]
        guard = LoopGuard(self.settings.limits)
        self.permissions.reset_session_rules()

        self.session.set_title(text)
        user_message = {"role": "user", "content": text}
        self.messages.append(user_message)
        self.session.append("message", user_message)

        await self.bus.emit(E.TURN_START, {"turnId": turn_id})
        await self.bus.emit(E.STATUS, {"state": "thinking"})

        reason = "completed"
        try:
            if self.provider.mode == "agent":
                reason = await self._run_delegated_turn(turn_id, guard)
            else:
                reason = await self._run_tool_loop(turn_id, guard)
        except asyncio.CancelledError:
            reason = "stopped"
            await self._seal_pending_tool_calls("Cancelled by the user.")
            raise
        except ProviderError as exc:
            reason = "provider-error"
            await self.bus.emit(E.ERROR, {
                "message": str(exc), "detail": exc.hint, "recoverable": True,
            })
        except Exception as exc:
            reason = "error"
            log.exception("Turn failed")
            await self.bus.emit(E.ERROR, {"message": f"Something went wrong: {exc}"})
        finally:
            await self._seal_pending_tool_calls("Turn ended before this tool ran.")
            if self.voice is not None:
                await self.voice.on_turn_end(self._last_answer())
            await self.bus.emit(E.TURN_END, {"turnId": turn_id, "reason": reason, **guard.summary()})
            await self.bus.emit(E.STATUS, {"state": "ready"})
            await self._emit_usage()

    # ------------------------------------------------------------- model mode
    async def _run_tool_loop(self, turn_id: str, guard: LoopGuard) -> str:
        assert self.provider is not None
        force_final = False

        while True:
            if self._cancelled:
                return "stopped"

            decision = guard.begin_step()
            if decision.stop:
                await self._notice(decision.reason)
                return decision.reason
            self._apply(decision, guard)
            force_final = force_final or decision.force_final

            await self.bus.emit(E.STEP, {
                "turnId": turn_id, "step": guard.step, "maxSteps": self.settings.limits.max_steps,
            })
            await self._maybe_compact()

            system = await self._system_prompt()
            tools = [] if force_final else self.registry.schemas()

            message_id = uuid.uuid4().hex[:10]
            await self.bus.emit(E.MESSAGE_START, {"id": message_id, "role": "assistant"})
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            calls: list[dict[str, Any]] = []
            failed = ""

            async for chunk in self.provider.stream(
                ctxmod.normalise(self.messages), tools, system=system
            ):
                if self._cancelled:
                    break
                if chunk.text:
                    text_parts.append(chunk.text)
                    await self.bus.emit(E.TEXT_DELTA, {"id": message_id, "text": chunk.text})
                    if self.voice is not None:
                        await self.voice.on_text("".join(text_parts))
                if chunk.thinking:
                    thinking_parts.append(chunk.thinking)
                    await self.bus.emit(E.THINKING_DELTA, {"id": message_id, "text": chunk.thinking})
                for call in chunk.tool_calls:
                    calls.append({"id": call.id, "name": call.name, "raw": call.arguments})
                if chunk.usage:
                    self._accumulate(chunk.usage)
                if chunk.error:
                    failed = chunk.error
                    break

            answer = "".join(text_parts)
            thinking = "".join(thinking_parts)
            await self.bus.emit(E.MESSAGE_END, {
                "id": message_id, "text": answer, "thinking": thinking,
            })

            if failed:
                await self.bus.emit(E.ERROR, {"message": failed, "recoverable": True})
                self._record_assistant(answer, [], thinking)
                return "provider-error"
            if self._cancelled:
                return "stopped"

            observation = guard.observe_response(has_text=bool(answer.strip()), has_tool_calls=bool(calls))
            if observation.stop:
                self._record_assistant(answer or "(no response)", [], thinking)
                await self._notice("The model stopped responding; ending the turn.")
                return observation.reason

            if not calls:
                if observation.nudge and not answer.strip():
                    self._record_assistant(answer, [], thinking)
                    self._apply(observation, guard)
                    continue
                self._record_assistant(answer, [], thinking)
                await self._remember_turn(answer)
                return "completed"

            # Parse and validate the calls before doing anything with them.
            prepared, invalid = self._prepare_calls(calls)
            batch_decision = guard.observe_tool_batch(prepared) if prepared else GuardDecision()

            self._record_assistant(answer, prepared, thinking)
            for entry in invalid:
                self._record_tool_result(entry["id"], entry["name"], entry["error"], is_error=True)

            for entry in prepared:
                if self._cancelled:
                    await self._seal_pending_tool_calls("Cancelled by the user.")
                    return "stopped"
                await self._execute(entry, guard)

            self._apply(batch_decision, guard)
            force_final = force_final or batch_decision.force_final
            await self.bus.emit(E.STATUS, {"state": "thinking"})

    def _prepare_calls(self, calls: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
        """Split raw model calls into runnable ones and immediate errors."""
        prepared: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for call in calls:
            name = call["name"]
            args, error = ctxmod.parse_arguments(call["raw"])
            spec = self.registry.get(name)
            if spec is None:
                invalid.append({"id": call["id"], "name": name, "error": self._explain_missing(name)})
                continue
            if error:
                invalid.append({
                    "id": call["id"], "name": name,
                    "error": f"Could not read the arguments — {error}. Send valid JSON.",
                })
                continue
            prepared.append({"id": call["id"], "name": name, "args": args, "spec": spec})
        return prepared, invalid

    def _explain_missing(self, name: str) -> str:
        """Turn a bad tool name into something the model can act on.

        The common case is not a hallucination — it is a real tool whose toolset
        the user has switched off, so the answer is to ask for it rather than to
        list alternatives.
        """
        from .tools.catalog import CATALOG, owning_toolset

        owner = owning_toolset(name)
        if owner is None:
            # Dynamically registered tools are not in the static catalog.
            loaded = self.registry.find_anywhere(name)
            owner = CATALOG.get(loaded.toolset) if loaded else None
        if owner is not None:
            missing = owner.missing_requirements()
            if missing:
                return (
                    f"'{name}' belongs to the '{owner.name}' toolset, which cannot run here: "
                    f"Python package(s) {', '.join(missing)} are not installed. Tell the user, "
                    f"and solve the task another way."
                )
            return (
                f"'{name}' belongs to the '{owner.id}' toolset, which is switched off. "
                f"Call enable_toolset('{owner.id}', reason) to ask the user to turn it on, "
                f"then continue."
            )
        available = ", ".join(sorted(s.name for s in self.registry.specs()))
        return (
            f"There is no tool called '{name}'. Use one of these instead: {available}"
        )

    async def _execute(self, entry: dict[str, Any], guard: LoopGuard) -> None:
        spec = entry["spec"]
        name, args, call_id = entry["name"], entry["args"], entry["id"]

        if guard.is_benched(name):
            self._record_tool_result(
                call_id, name,
                f"'{name}' has failed repeatedly and is disabled for this turn. Try another approach.",
                is_error=True,
            )
            return

        await self.bus.emit(E.TOOL_START, {
            "callId": call_id,
            "name": name,
            "toolset": spec.toolset,
            "title": spec.render_title(args),
            "args": _safe_args(args),
            "capability": spec.capability,
        })

        started = time.monotonic()
        # A self-gated tool asks for itself, with far better detail than a
        # generic "approve this tool" prompt could carry.
        verdict = (
            Verdict(True, "Gated by the tool itself.")
            if spec.self_gated
            else await self.permissions.check_tool(
                name, spec.capability, args, title=spec.render_title(args),
                detail=spec.description.split("\n")[0][:200],
            )
        )
        if not verdict.allowed:
            await self.bus.emit(E.TOOL_END, {
                "callId": call_id, "ok": False, "preview": verdict.reason,
                "elapsedMs": int((time.monotonic() - started) * 1000), "denied": True,
            })
            self._record_tool_result(call_id, name, verdict.reason, is_error=True)
            return

        assert self._tool_context is not None
        self._tool_context.call_id = call_id
        guard.record_call(name, args)
        try:
            result: ToolResult = await self.registry.call(
                name, args, self._tool_context,
                timeout=float(self.settings.limits.command_timeout * 4) if spec.capability == "exec" else 300.0,
            )
        except asyncio.CancelledError:
            await self.bus.emit(E.TOOL_END, {"callId": call_id, "ok": False, "preview": "Cancelled."})
            self._record_tool_result(call_id, name, "Cancelled by the user.", is_error=True)
            raise
        finally:
            self._tool_context.call_id = ""

        if result.ok:
            guard.record_success(name)
        else:
            self._apply(guard.record_failure(name, result.content), guard)

        content = truncate_output(result.content, self.settings.limits.max_tool_output)
        preview = content[: spec.preview_chars or _DEFAULT_PREVIEW]
        await self.bus.emit(E.TOOL_END, {
            "callId": call_id,
            "ok": result.ok,
            "preview": preview,
            "truncated": len(content) > len(preview),
            "elapsedMs": result.elapsed_ms or int((time.monotonic() - started) * 1000),
            "meta": _safe_args(result.meta),
        })
        self._record_tool_result(call_id, name, content, is_error=not result.ok)

    # ------------------------------------------------------------- agent mode
    async def _run_delegated_turn(self, turn_id: str, guard: LoopGuard) -> str:
        """Relay a provider that runs its own loop (Claude Code)."""
        assert self.provider is not None
        guard.begin_step()

        all_text: list[str] = []
        segment: list[str] = []
        thinking: list[str] = []
        open_calls: dict[str, dict[str, Any]] = {}
        failed = ""
        message_id: str | None = None

        async def open_message() -> str:
            nonlocal message_id
            if message_id is None:
                message_id = uuid.uuid4().hex[:10]
                await self.bus.emit(E.MESSAGE_START, {"id": message_id, "role": "assistant"})
            return message_id

        async def close_message() -> None:
            """Seal the current narration block so the next one lands *after* the
            tool it follows — the trace has to stay chronological to be useful."""
            nonlocal message_id
            if message_id is None:
                return
            await self.bus.emit(E.MESSAGE_END, {
                "id": message_id, "text": "".join(segment), "thinking": "".join(thinking),
            })
            message_id = None
            segment.clear()
            thinking.clear()

        async for chunk in self.provider.stream(
            ctxmod.normalise(self.messages), [], system=await self._system_prompt()
        ):
            if self._cancelled:
                break
            if chunk.text:
                current = await open_message()
                segment.append(chunk.text)
                all_text.append(chunk.text)
                await self.bus.emit(E.TEXT_DELTA, {"id": current, "text": chunk.text})
                if self.voice is not None:
                    await self.voice.on_text("".join(all_text))
            if chunk.thinking:
                current = await open_message()
                thinking.append(chunk.thinking)
                await self.bus.emit(E.THINKING_DELTA, {"id": current, "text": chunk.thinking})
            if chunk.usage:
                self._accumulate(chunk.usage)
            if chunk.tool_event:
                if chunk.tool_event.get("phase") == "start":
                    await close_message()
                    if all_text and not all_text[-1].endswith("\n"):
                        all_text.append("\n\n")
                await self._relay_tool_event(chunk.tool_event, open_calls)
            if chunk.error:
                failed = chunk.error
                break

        await close_message()
        answer = "".join(all_text).strip()
        for call_id in list(open_calls):
            await self.bus.emit(E.TOOL_END, {"callId": call_id, "ok": False, "preview": "(no result)"})

        if failed:
            await self.bus.emit(E.ERROR, {"message": failed, "recoverable": True})
            self._record_assistant(answer, [], "")
            return "provider-error"

        self._record_assistant(answer, [], "")
        await self._remember_turn(answer)
        return "stopped" if self._cancelled else "completed"

    async def _relay_tool_event(self, event: dict[str, Any], open_calls: dict[str, dict]) -> None:
        call_id = event.get("id") or uuid.uuid4().hex[:10]
        if event.get("phase") == "start":
            name = event.get("name", "tool")
            open_calls[call_id] = {"name": name, "at": time.monotonic()}
            await self.bus.emit(E.TOOL_START, {
                "callId": call_id,
                "name": name,
                "toolset": "claude-code",
                "title": _delegated_title(name, event.get("input") or {}),
                "args": _safe_args(event.get("input") or {}),
                "capability": _DELEGATED_CAPABILITY.get(name, "exec"),
            })
        else:
            started = open_calls.pop(call_id, {}).get("at", time.monotonic())
            content = str(event.get("content") or "")
            await self.bus.emit(E.TOOL_END, {
                "callId": call_id,
                "ok": bool(event.get("ok", True)),
                "preview": content[:1200],
                "truncated": len(content) > 1200,
                "elapsedMs": int((time.monotonic() - started) * 1000),
            })

    # ================================================================== helpers
    #: Nudges that mean "you are stuck", and so deserve the full situation report
    #: rather than a one-line reminder.
    _DIAGNOSTIC_NUDGES: ClassVar[set[str]] = {"repeat", "tool_failing", "no_action"}

    def _apply(self, decision: GuardDecision, guard: LoopGuard | None = None) -> None:
        if not decision.nudge:
            return
        text = NUDGES.get(decision.nudge, decision.nudge)
        if guard is not None and decision.nudge in self._DIAGNOSTIC_NUDGES:
            text = f"{text}\n\n{guard.diagnose()}"
        self.messages.append({"role": "user", "content": text})

    def _record_assistant(self, text: str, calls: list[dict], thinking: str = "") -> None:
        message: dict[str, Any] = {"role": "assistant", "content": text or ""}
        if calls:
            message["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": json.dumps(c["args"], default=str)},
                }
                for c in calls
            ]
        self.messages.append(message)
        if self.session:
            self.session.append("message", {**message, **({"thinking": thinking} if thinking else {})})

    def _record_tool_result(self, call_id: str, name: str, content: str, *, is_error: bool = False) -> None:
        message = {
            "role": "tool", "tool_call_id": call_id, "name": name,
            "content": content or "(no output)",
        }
        if is_error:
            message["is_error"] = True
        self.messages.append(message)
        if self.session:
            self.session.append("message", message)

    async def _seal_pending_tool_calls(self, reason: str) -> None:
        """Give every unanswered tool call a result so the history stays valid."""
        answered = {
            m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"
        }
        for message in self.messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if call.get("id") not in answered:
                    self._record_tool_result(
                        call["id"], call.get("function", {}).get("name", "tool"), reason, is_error=True
                    )
                    answered.add(call["id"])

    async def _system_prompt(self) -> str:
        settings = self.settings
        memory_context = ""
        plan_summary = ""
        if self._tool_context is not None:
            last_user = next(
                (m["content"] for m in reversed(self.messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)),
                "",
            )
            if "memory" in self.registry.enabled_toolsets and last_user:
                try:
                    memory_context = self._tool_context.memory.build_context(last_user, 1600)
                except Exception:
                    memory_context = ""
            try:
                plan_summary = self._tool_context.todos.summary()
            except Exception:
                plan_summary = ""

        window = self.provider.context_window if self.provider else 128_000
        return build_system_prompt(
            settings,
            enabled_toolsets=self.registry.enabled_toolsets,
            memory_context=memory_context,
            plan_summary=plan_summary,
            project_notes=self._project_notes(),
            provider_mode=self.provider.mode if self.provider else "model",
            compact=window >= _COMPACT_PROMPT_ABOVE,
        )

    def _project_notes(self) -> str:
        """Pick up AGENTS.md / CLAUDE.md so project conventions are respected."""
        for name in ("AGENTS.md", "CLAUDE.md", ".arcbot/NOTES.md"):
            candidate = self.settings.workspace_path / name
            try:
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
        return ""

    async def _maybe_compact(self) -> None:
        if self.provider is None:
            return
        system = await self._system_prompt()
        window = self.provider.context_window
        if not ctxmod.should_compact(self.messages, system, window, self.settings.limits.compact_at):
            return
        await self.bus.emit(E.STATUS, {"state": "compacting", "detail": "Summarising earlier context…"})
        self.messages, compacted = await ctxmod.compact(self.messages, self.provider, system=system)
        if compacted:
            await self.bus.emit(E.NOTICE, {
                "level": "info", "text": "Earlier conversation was summarised to free up context.",
            })
        await self.bus.emit(E.STATUS, {"state": "thinking"})

    async def _remember_turn(self, answer: str) -> None:
        """Nothing automatic — the model decides what is worth remembering.

        Kept as a hook so future policies (auto-summarising long sessions into
        episodic memory) have an obvious home.
        """
        return

    def _accumulate(self, usage) -> None:
        self.usage_total["inputTokens"] += usage.input_tokens
        self.usage_total["outputTokens"] += usage.output_tokens
        self.usage_total["costUsd"] += usage.cost_usd

    async def _emit_usage(self) -> None:
        window = self.provider.context_window if self.provider else 0
        payload = dict(self.usage_total)
        payload.update(
            ctxmod.context_usage(self.messages, await self._system_prompt(), window)
        )
        await self.bus.emit(E.USAGE, payload)

    def _last_answer(self) -> str:
        """The assistant text from this turn, for the voice session to finish speaking."""
        for message in reversed(self.messages):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"])
        return ""

    async def _notice(self, text: str, level: str = "warn") -> None:
        if text:
            await self.bus.emit(E.NOTICE, {"level": level, "text": text})

    async def _on_terminal_data(self, data: str) -> None:
        await self.bus.emit(E.TERMINAL_DATA, {"data": data})

    async def _enable_toolset(self, toolset_id: str) -> str | None:
        """Turn a toolset on mid-turn (called by the ``enable_toolset`` tool)."""
        error = self.registry.enable(toolset_id)
        if error:
            return error
        settings = self.settings
        if toolset_id not in settings.toolsets:
            settings.toolsets = normalise_toolsets([*settings.toolsets, toolset_id])
            self.store.save(settings)
        if toolset_id == "shell":
            self.terminal.configure(settings.workspace_path, on_data=self._on_terminal_data)
            await self.terminal.start_shell()
        await self.emit_toolsets()
        return None

    async def disable_toolset(self, toolset_id: str) -> None:
        self.registry.disable(toolset_id)
        settings = self.settings
        settings.toolsets = [t for t in settings.toolsets if t != toolset_id]
        self.store.save(settings)
        await self.emit_toolsets()

    async def _request_quit(self, reason: str) -> bool:
        """Ask whoever owns the process to stop.

        Returns False when nothing is listening — an embedded or test run — so
        the caller can tell the user the truth instead of claiming success.
        """
        if self.on_quit is None:
            return False
        await self.bus.emit(E.SHUTDOWN, {"reason": reason})
        await self.on_quit(reason)
        return True

    async def _open_settings(self, panel: str) -> None:
        await self.bus.emit(E.OPEN_SETTINGS, {"panel": panel})

    def describe_host(self) -> dict[str, Any]:
        """Live facts about this ArcBot process, for ``arcbot_status``."""
        custom = [s.name for s in self.registry.specs() if s.toolset == "custom"]
        return {
            "url": self.host_url,
            "uptimeSeconds": time.monotonic() - self._started_at,
            "provider": self.settings.model.provider,
            "providerMode": self.provider.mode if self.provider else None,
            "mcpServers": [n for n, s in self.mcp.status.items() if s.connected],
            "customTools": custom,
            "canQuit": self.on_quit is not None,
            "toolCount": len(self.registry.specs()),
            "sessionId": self.session.id if self.session else "",
        }

    async def _connect_mcp(self) -> None:
        """Attach configured MCP servers, reporting each one's outcome."""
        if "mcp" not in self.registry.enabled_toolsets:
            await self.mcp.close()
            return
        statuses = await self.mcp.connect_all(self.settings.mcp_servers)
        for status in statuses.values():
            if status.connected:
                await self.bus.emit(E.NOTICE, {
                    "level": "success",
                    "text": f"MCP server '{status.name}' connected ({len(status.tools)} tools).",
                })
            else:
                await self.bus.emit(E.NOTICE, {
                    "level": "warn",
                    "text": f"MCP server '{status.name}' did not connect: {status.error}",
                })

    async def emit_toolsets(self) -> None:
        from .tools.catalog import catalog_payload

        await self.bus.emit(E.TOOLSETS, {
            "available": catalog_payload(),
            "enabled": self.registry.enabled_toolsets,
            "failures": self.registry.failures,
            "mcp": [s.to_dict() for s in self.mcp.status.values()],
            "tools": [
                {"name": s.name, "toolset": s.toolset, "capability": s.capability,
                 "description": s.description.split("\n")[0][:160]}
                for s in sorted(self.registry.specs(), key=lambda s: (s.toolset, s.name))
            ],
        })


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _safe_args(args: Any, limit: int = 4000) -> Any:
    """Trim oversized values so a 2 MB file body never reaches the browser."""
    if not isinstance(args, dict):
        return args
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > limit:
            out[key] = value[:limit] + f"… (+{len(value) - limit:,} chars)"
        else:
            out[key] = value
    return out


def _replayable(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The subset of history the UI can render when reopening a session."""
    out = []
    for message in messages:
        role = message.get("role")
        if role == "user" and isinstance(message.get("content"), str):
            if not str(message["content"]).startswith("[system]"):
                out.append({"role": "user", "text": message["content"]})
        elif role == "assistant" and message.get("content"):
            out.append({"role": "assistant", "text": message["content"]})
    return out


#: What each Claude Code tool actually does, so the trace colours it correctly.
_DELEGATED_CAPABILITY = {
    "Read": "read", "Glob": "read", "Grep": "read", "NotebookRead": "read",
    "WebSearch": "network", "WebFetch": "network",
    "Write": "write", "Edit": "write", "NotebookEdit": "write", "TodoWrite": "read",
    "Bash": "exec", "BashOutput": "read", "KillShell": "exec", "Task": "exec",
}


def _delegated_title(name: str, args: dict[str, Any]) -> str:
    """Readable label for a tool Claude Code ran."""
    templates = {
        "Bash": "$ {command}",
        "Read": "Read {file_path}",
        "Write": "Write {file_path}",
        "Edit": "Edit {file_path}",
        "Glob": "Find {pattern}",
        "Grep": "Search {pattern}",
        "WebFetch": "Fetch {url}",
        "WebSearch": "Search: {query}",
        "TodoWrite": "Update plan",
        "Task": "Subagent: {description}",
    }
    template = templates.get(name)
    if not template:
        return name
    try:
        return template.format(**{k: str(v)[:80] for k, v in args.items()})
    except (KeyError, IndexError, ValueError):
        return name
