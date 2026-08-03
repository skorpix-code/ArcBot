"""Google Gemini via the official GenAI SDK.

Gemini's schema validator rejects several JSON-Schema keywords that ArcBot's
tool schemas legitimately contain, so schemas are sanitised on the way in.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ..logging_setup import get_logger
from .base import Chunk, Provider, ProviderError, ToolCall, Usage

log = get_logger("provider.gemini")

#: Keywords the Gemini function-declaration validator refuses.
_UNSUPPORTED = {
    "additionalProperties", "$schema", "$id", "$ref", "exclusiveMinimum",
    "exclusiveMaximum", "title", "default", "examples", "const",
}
_CONTEXT_WINDOWS = {"gemini-2.5-pro": 2_000_000, "gemini-2.5-flash": 1_000_000}


def _sanitize(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _sanitize(v) for k, v in schema.items() if k not in _UNSUPPORTED}
    if isinstance(schema, list):
        return [_sanitize(item) for item in schema]
    return schema


class GeminiProvider(Provider):
    mode = "model"
    label = "Gemini"

    def __init__(self, model: str, *, api_key: str = "", context_window: int = 0) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "The `google-genai` package is not installed.", hint="pip install google-genai"
            ) from exc
        if not api_key:
            raise ProviderError("Gemini needs an API key.", hint="Add GEMINI_API_KEY in Settings.")

        self._types = types
        self.model = model
        self.client = genai.Client(api_key=api_key)
        self.context_window = context_window or _CONTEXT_WINDOWS.get(model, 1_000_000)

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[Any] | None:
        if not tools:
            return None
        declarations = [
            {
                "name": t["name"],
                "description": (t.get("description") or "")[:1024],
                "parameters": _sanitize(t.get("parameters") or {"type": "object", "properties": {}}),
            }
            for t in tools
        ]
        return [self._types.Tool(function_declarations=declarations)]

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[Any]:
        types = self._types
        contents: list[Any] = []
        for message in messages:
            role = message.get("role")
            if role == "user":
                contents.append(types.Content(
                    role="user", parts=[types.Part(text=str(message.get("content") or ""))]
                ))
            elif role == "assistant":
                parts = []
                if message.get("content"):
                    parts.append(types.Part(text=str(message["content"])))
                for call in message.get("tool_calls") or []:
                    try:
                        args = json.loads(call["function"]["arguments"] or "{}")
                    except (json.JSONDecodeError, TypeError, KeyError):
                        args = {}
                    parts.append(types.Part.from_function_call(
                        name=call["function"]["name"], args=args
                    ))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                contents.append(types.Content(role="user", parts=[
                    types.Part.from_function_response(
                        name=message.get("name", "tool"),
                        response={"result": str(message.get("content") or "")},
                    )
                ]))
        return contents

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        system: str = "",
    ) -> AsyncIterator[Chunk]:
        config = self._types.GenerateContentConfig(
            tools=self._convert_tools(tools),
            system_instruction=system or None,
            temperature=0.0,
        )
        usage = Usage()
        try:
            stream = await self.client.aio.models.generate_content_stream(
                model=self.model,
                contents=self._convert_messages(messages),
                config=config,
            )
            async for event in stream:
                if getattr(event, "text", None):
                    yield Chunk(text=event.text)
                for call in getattr(event, "function_calls", None) or []:
                    yield Chunk(tool_calls=[ToolCall(
                        id=f"call_{uuid.uuid4().hex[:10]}",
                        name=call.name,
                        arguments=json.dumps(dict(call.args or {})),
                    )])
                metadata = getattr(event, "usage_metadata", None)
                if metadata:
                    usage.input_tokens = getattr(metadata, "prompt_token_count", 0) or 0
                    usage.output_tokens = getattr(metadata, "candidates_token_count", 0) or 0
        except Exception as exc:
            text = str(exc)
            if "API key" in text or "401" in text or "PERMISSION_DENIED" in text:
                raise ProviderError("Gemini rejected the API key.", hint="Check GEMINI_API_KEY.") from exc
            if "429" in text or "RESOURCE_EXHAUSTED" in text:
                raise ProviderError("Gemini rate limit reached.", retryable=True) from exc
            raise ProviderError(text[:400]) from exc

        yield Chunk(usage=usage, done=True, stop_reason="end_turn")

    async def health(self) -> tuple[bool, str]:
        try:
            await self.client.aio.models.generate_content(
                model=self.model, contents="hi"
            )
            return True, "Connected."
        except Exception as exc:
            return False, str(exc)[:300]
