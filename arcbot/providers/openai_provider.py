"""Any OpenAI-compatible endpoint: OpenAI, Ollama, LM Studio, vLLM, OpenRouter…

This is the workhorse for local models, so it is deliberately forgiving: some
servers stream reasoning on a non-standard field, some emit tool-call ids late,
and some cannot do tool calls at all.  All three cases are handled rather than
crashed on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..logging_setup import get_logger
from .base import Chunk, Provider, ProviderError, ToolCall, Usage

log = get_logger("provider.openai")

#: Local models are often slow to first token — be patient rather than wrong.
REQUEST_TIMEOUT = 900.0


class OpenAIProvider(Provider):
    mode = "model"
    label = "OpenAI-compatible"

    def __init__(
        self,
        model: str,
        *,
        api_key: str = "",
        base_url: str = "",
        context_window: int = 128_000,
        temperature: float | None = 0.0,
        max_tokens: int = 0,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError("The `openai` package is not installed.", hint="pip install openai") from exc

        self.model = model
        self.context_window = context_window
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncOpenAI(
            api_key=api_key or "not-needed",   # local servers ignore this
            base_url=base_url or None,
            timeout=REQUEST_TIMEOUT,
            max_retries=2,
        )

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
                if message.get("tool_calls"):
                    entry["tool_calls"] = message["tool_calls"]
                out.append(entry)
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id", ""),
                    "content": str(message.get("content") or "(no output)"),
                })
            else:
                out.append({"role": "user", "content": message.get("content") or ""})
        return out

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> AsyncIterator[Chunk]:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages, system),
            "stream": True,
        }
        converted = self._convert_tools(tools)
        if converted:
            request["tools"] = converted
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if self.max_tokens:
            request["max_tokens"] = self.max_tokens
        try:
            request["stream_options"] = {"include_usage": True}
            stream = await self.client.chat.completions.create(**request)
        except Exception as exc:
            if "stream_options" in str(exc):
                request.pop("stream_options", None)
                try:
                    stream = await self.client.chat.completions.create(**request)
                except Exception as retry_exc:
                    raise _translate(retry_exc, self.model) from retry_exc
            else:
                raise _translate(exc, self.model) from exc

        buffers: dict[int, dict[str, str]] = {}
        usage = Usage()
        stop_reason = ""

        try:
            async for event in stream:
                if getattr(event, "usage", None):
                    usage.input_tokens = getattr(event.usage, "prompt_tokens", 0) or 0
                    usage.output_tokens = getattr(event.usage, "completion_tokens", 0) or 0
                if not getattr(event, "choices", None):
                    continue
                choice = event.choices[0]
                stop_reason = getattr(choice, "finish_reason", "") or stop_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if reasoning:
                    yield Chunk(thinking=str(reasoning))
                if getattr(delta, "content", None):
                    yield Chunk(text=delta.content)

                for call in getattr(delta, "tool_calls", None) or []:
                    index = getattr(call, "index", 0) or 0
                    slot = buffers.setdefault(index, {"id": "", "name": "", "args": ""})
                    if getattr(call, "id", None):
                        slot["id"] = call.id
                    function = getattr(call, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            slot["name"] = function.name
                        if getattr(function, "arguments", None):
                            slot["args"] += function.arguments
        except Exception as exc:
            raise _translate(exc, self.model) from exc

        calls = [
            ToolCall(slot["id"] or f"call_{index}", slot["name"], slot["args"])
            for index, slot in sorted(buffers.items())
            if slot["name"]
        ]
        if calls:
            yield Chunk(tool_calls=calls)
        yield Chunk(usage=usage, done=True, stop_reason=stop_reason)

    async def health(self) -> tuple[bool, str]:
        try:
            await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            return True, "Connected."
        except Exception as exc:
            return False, str(exc)[:300]

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:
            pass


def _translate(exc: Exception, model: str) -> ProviderError:
    text = str(exc)
    lowered = text.lower()
    if "connection" in lowered or "connect" in lowered or "refused" in lowered:
        return ProviderError(
            "Could not reach the model server.",
            hint="Is LM Studio / Ollama running, and is the base URL correct?",
            retryable=True,
        )
    if "401" in text or "unauthorized" in lowered or "api key" in lowered:
        return ProviderError("The server rejected the API key.", hint="Check the key in Settings.")
    if "404" in text or "not found" in lowered:
        return ProviderError(
            f"Model {model!r} was not found on this server.",
            hint="Pick a model the server actually has loaded.",
        )
    if "429" in text or "rate limit" in lowered:
        return ProviderError("Rate limited.", retryable=True)
    if "context" in lowered and "length" in lowered:
        return ProviderError(
            "The conversation exceeded the model's context window.",
            hint="Start a new chat, or pick a model with a larger context window.",
        )
    return ProviderError(text[:400] or "The model server returned an error.")
