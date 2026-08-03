"""Anthropic Messages API.

Credentials resolve in this order, so the zero-config paths work first:

1. an explicit API key (env var or ArcBot's credential store)
2. an ``ant auth login`` OAuth profile — the SDK reads it with no key at all

Adaptive thinking is on by default with summarised display, because a visible
reasoning stream is a large part of what makes an agent feel trustworthy.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..logging_setup import get_logger
from .base import Chunk, Provider, ProviderError, ToolCall, Usage

log = get_logger("provider.anthropic")

#: Models that take `thinking` / `effort` and reject sampling parameters.
_ADAPTIVE_THINKING_PREFIXES = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-mythos-5",
)

#: Rough per-model context windows for the compaction trigger.
_CONTEXT_WINDOWS = {
    "claude-haiku-4-5": 200_000,
}
_DEFAULT_CONTEXT = 1_000_000


class AnthropicProvider(Provider):
    mode = "model"
    label = "Claude"

    def __init__(
        self,
        model: str,
        *,
        api_key: str = "",
        auth_token: str = "",
        base_url: str = "",
        max_tokens: int = 16_000,
        effort: str = "high",
        thinking: bool = True,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError(
                "The `anthropic` package is not installed.",
                hint="pip install anthropic",
            ) from exc

        self._anthropic = anthropic
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.thinking = thinking
        self.context_window = _CONTEXT_WINDOWS.get(model, _DEFAULT_CONTEXT)

        kwargs: dict[str, Any] = {"timeout": 600.0, "max_retries": 3}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        elif auth_token:
            # OAuth profiles authenticate with a bearer token, not x-api-key.
            kwargs["auth_token"] = auth_token
        # With neither, the SDK resolves an `ant auth login` profile itself.
        self.client = anthropic.AsyncAnthropic(**kwargs)

    # ------------------------------------------------------------- utilities
    @property
    def _adaptive(self) -> bool:
        return self.model.startswith(_ADAPTIVE_THINKING_PREFIXES)

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
            }
            for t in tools
        ]

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Map ArcBot's OpenAI-shaped history onto Anthropic content blocks."""
        out: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content") or ""

            if role == "user":
                out.append({"role": "user", "content": content})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for call in message.get("tool_calls") or []:
                    try:
                        arguments = json.loads(call["function"]["arguments"] or "{}")
                    except (json.JSONDecodeError, TypeError, KeyError):
                        arguments = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": arguments,
                    })
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": str(content or "(no output)"),
                }
                if message.get("is_error"):
                    block["is_error"] = True
                # Consecutive tool results belong in one user turn.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    # ---------------------------------------------------------------- stream
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> AsyncIterator[Chunk]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._convert_messages(messages),
        }
        if system:
            request["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        converted = self._convert_tools(tools)
        if converted:
            request["tools"] = converted
        if self._adaptive:
            request["output_config"] = {"effort": self.effort}
            if self.thinking:
                request["thinking"] = {"type": "adaptive", "display": "summarized"}

        try:
            async with self.client.messages.stream(**request) as stream:
                pending: dict[int, ToolCall] = {}
                async for event in stream:
                    kind = getattr(event, "type", "")
                    if kind == "content_block_start":
                        block = event.content_block
                        if getattr(block, "type", "") == "tool_use":
                            pending[event.index] = ToolCall(block.id, block.name, "")
                    elif kind == "content_block_delta":
                        delta = event.delta
                        delta_type = getattr(delta, "type", "")
                        if delta_type == "text_delta":
                            yield Chunk(text=delta.text)
                        elif delta_type == "thinking_delta":
                            yield Chunk(thinking=getattr(delta, "thinking", "") or "")
                        elif delta_type == "input_json_delta":
                            call = pending.get(event.index)
                            if call is not None:
                                call.arguments += delta.partial_json or ""
                    elif kind == "content_block_stop":
                        call = pending.pop(event.index, None)
                        if call is not None:
                            yield Chunk(tool_calls=[call])

                final = await stream.get_final_message()
                usage = Usage(
                    input_tokens=getattr(final.usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(final.usage, "output_tokens", 0) or 0,
                    cache_read_tokens=getattr(final.usage, "cache_read_input_tokens", 0) or 0,
                )
                stop_reason = getattr(final, "stop_reason", "") or ""
                if stop_reason == "refusal":
                    details = getattr(final, "stop_details", None)
                    reason = getattr(details, "explanation", "") if details else ""
                    yield Chunk(
                        error=f"Claude declined this request. {reason}".strip(),
                        usage=usage,
                        done=True,
                        stop_reason=stop_reason,
                    )
                    return
                yield Chunk(usage=usage, done=True, stop_reason=stop_reason)

        except self._anthropic.AuthenticationError as exc:
            raise ProviderError(
                "Anthropic rejected the credentials.",
                hint="Check the API key, or run `ant auth login` and pick the OAuth option.",
            ) from exc
        except self._anthropic.RateLimitError as exc:
            raise ProviderError("Rate limited by Anthropic.", retryable=True) from exc
        except self._anthropic.NotFoundError as exc:
            raise ProviderError(
                f"Model {self.model!r} was not found.",
                hint="Pick a different model in Settings.",
            ) from exc
        except self._anthropic.APIConnectionError as exc:
            raise ProviderError("Could not reach the Anthropic API.", retryable=True) from exc
        except self._anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

    async def health(self) -> tuple[bool, str]:
        try:
            await self.client.messages.create(
                model=self.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True, "Connected."
        except Exception as exc:
            return False, str(exc)[:300]

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:
            pass
