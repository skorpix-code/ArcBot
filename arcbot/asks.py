"""Round-trip questions from the agent to the human.

The agent awaits a future; the transport resolves it when the user clicks.  If
no UI is attached, or the user walks away, the ask times out and resolves to its
declared default so the agent can never deadlock.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import E, EventBus
from .logging_setup import get_logger

log = get_logger("asks")

#: How long an unanswered question waits before falling back to its default.
DEFAULT_TIMEOUT = 900.0


def spoken_form(kind: str, payload: dict[str, Any]) -> str:
    """Turn a question into something worth hearing.

    Deliberately short: the screen already shows the command, the diff and the
    risk badge.  Spoken aloud, all that matters is what is being asked for, so
    the listener knows to look.
    """
    from .events import Ask

    if kind == Ask.INPUT:
        return str(payload.get("question") or "")
    if kind == Ask.COMMAND:
        return f"Can I run {payload.get('command') or 'a command'}?"
    if kind == Ask.TOOL:
        return f"Can I {payload.get('title') or payload.get('tool') or 'do that'}?"
    if kind == Ask.TOOLSET:
        return f"I need the {payload.get('name') or 'a'} capability. Turn it on?"
    if kind == Ask.PATH:
        return f"Can I use {payload.get('path') or 'a folder outside the workspace'}?"
    return "I need your approval to continue."


@dataclass
class AskResult:
    decision: str                     # "allow" | "always" | "deny" | "never" | "answer"
    value: str | None = None       # free-text answer, or chosen option
    timed_out: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.decision in ("allow", "always", "answer")

    @property
    def persist(self) -> bool:
        return self.decision in ("always", "never")


class AskBroker:
    """Tracks in-flight questions and routes answers back to their waiter."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._pending: dict[str, asyncio.Future] = {}
        #: Reads a question aloud.  Set while voice mode is on — without it, a
        #: hands-free user would sit in silence waiting for approval they never
        #: heard was needed.
        self.narrate: Any = None

    @property
    def pending_ids(self) -> list[str]:
        return list(self._pending)

    async def ask(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        default: str = "deny",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> AskResult:
        """Emit an ``ask`` event and wait for the user's answer."""
        ask_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[ask_id] = future

        await self.bus.emit(E.ASK, {"askId": ask_id, "kind": kind, **payload})
        if self.narrate is not None:
            try:
                await self.narrate(spoken_form(kind, payload))
            except Exception as exc:      # never let speech block a question
                log.debug("Could not speak the question: %s", exc)
        try:
            if timeout and timeout > 0:
                result = await asyncio.wait_for(future, timeout=timeout)
            else:
                result = await future
        except asyncio.TimeoutError:
            log.info("Ask %s (%s) timed out; defaulting to %r.", ask_id, kind, default)
            result = AskResult(default, timed_out=True)
        except asyncio.CancelledError:
            await self.bus.emit(E.ASK_RESOLVED, {"askId": ask_id, "decision": "cancelled"})
            raise
        finally:
            self._pending.pop(ask_id, None)

        await self.bus.emit(
            E.ASK_RESOLVED,
            {"askId": ask_id, "decision": result.decision, "timedOut": result.timed_out},
        )
        return result

    def resolve(self, ask_id: str, decision: str, value: str | None = None,
                data: dict[str, Any] | None = None) -> bool:
        """Called by the transport when the user answers.  Returns True if it landed."""
        future = self._pending.get(ask_id)
        if future is None or future.done():
            return False
        future.set_result(AskResult(decision, value, data=data or {}))
        return True

    def cancel_all(self, decision: str = "deny") -> None:
        """Resolve every outstanding question — used on stop/disconnect."""
        for ask_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(AskResult(decision, timed_out=False))
            self._pending.pop(ask_id, None)
