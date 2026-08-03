"""The contract every model backend implements.

Two shapes of backend exist and both speak the same event stream:

* ``mode = "model"`` — the provider streams the model's output and ArcBot's own
  loop executes tools (OpenAI, Anthropic, Gemini, any local server).
* ``mode = "agent"`` — the provider *is* an agent that runs its own loop and
  reports what it did (Claude Code with a subscription login).

Because the stream vocabulary is identical, the UI, transcript and permission
plumbing are written once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Stream chunks
# --------------------------------------------------------------------------- #


@dataclass
class ToolCall:
    id: str
    name: str
    #: Raw JSON string as emitted by the model; parsed by the agent.
    arguments: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def merge(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cost_usd += other.cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "costUsd": round(self.cost_usd, 6),
        }


@dataclass
class Chunk:
    """One increment of a provider's response."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    #: Set when the provider ran a tool itself (agent-mode backends).
    tool_event: dict[str, Any] | None = None
    #: Terminal error; the agent surfaces it and ends the turn.
    error: str = ""
    #: True on the last chunk of a response.
    done: bool = False
    #: Provider-native reason the turn ended ("end_turn", "max_tokens", …).
    stop_reason: str = ""


class ProviderError(RuntimeError):
    """A provider failed in a way the user needs to act on (auth, quota, config)."""

    def __init__(self, message: str, *, hint: str = "", retryable: bool = False):
        super().__init__(message)
        self.hint = hint
        self.retryable = retryable


class Provider:
    """Base class.  Subclasses implement :meth:`stream`."""

    #: "model" or "agent" — see the module docstring.
    mode: str = "model"
    #: Advertised context window; used to decide when to compact history.
    context_window: int = 128_000
    #: Whether the backend accepts tool schemas at all.
    supports_tools: bool = True
    #: Human-readable name for the UI.
    label: str = "model"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator for type checkers

    async def complete(self, prompt: str, *, system: str = "") -> str:
        """One-shot text completion, outside any conversation.

        Used by features that need the model as a plain writer rather than as an
        agent — the tool builder, history compaction. Agent-mode providers
        override this so those features work on every backend rather than only
        the ones ArcBot drives itself.
        """
        text = ""
        async for chunk in self.stream([{"role": "user", "content": prompt}], [], system=system):
            text += chunk.text
            if chunk.error:
                raise ProviderError(chunk.error)
        return text

    async def close(self) -> None:
        """Release sockets/subprocesses.  Safe to call more than once."""

    async def health(self) -> tuple[bool, str]:
        """Cheap reachability probe used by onboarding and ``arcbot doctor``."""
        return True, "ok"

    def reset(self) -> None:
        """Drop any per-conversation state (called when the user clears the chat)."""
