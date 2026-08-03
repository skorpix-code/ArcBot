"""Conversation history: repair, budgeting and compaction.

A long agent run will always outgrow its context window.  The strategy here is
cheap and predictable: estimate tokens, keep the most recent exchanges intact,
and fold everything older into a summary the model writes itself.  If the model
cannot be reached for a summary, fall back to dropping the oldest turns — losing
detail is survivable; a hard context-overflow error is not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from .logging_setup import get_logger

log = get_logger("context")

#: Characters per token — deliberately pessimistic so we compact early.
CHARS_PER_TOKEN = 3.6
#: Exchanges always kept verbatim, however long the conversation gets.
KEEP_RECENT_MESSAGES = 20


def estimate_tokens(messages: Sequence[dict[str, Any]], system: str = "") -> int:
    total = len(system)
    for message in messages:
        content = message.get("content")
        total += len(content) if isinstance(content, str) else len(json.dumps(content, default=str))
        for call in message.get("tool_calls") or []:
            total += len(json.dumps(call, default=str))
    return int(total / CHARS_PER_TOKEN) + 4 * len(messages)


def repair_json(raw: str) -> str:
    """Best-effort cleanup of tool arguments from a model that fumbled the JSON."""
    if not raw or not raw.strip():
        return "{}"
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    text = re.sub(r",\s*([}\]])", r"\1", text)          # trailing commas
    if text.count("{") > text.count("}"):
        text += "}" * (text.count("{") - text.count("}"))
    if text.count("[") > text.count("]"):
        text += "]" * (text.count("[") - text.count("]"))
    return text or "{}"


def parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """Parse tool-call arguments, returning ``(args, error)``."""
    if isinstance(raw, dict):
        return raw, None
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        try:
            parsed = json.loads(repair_json(raw))
        except (json.JSONDecodeError, TypeError):
            return {}, "the arguments were not valid JSON"
    if not isinstance(parsed, dict):
        return {}, "the arguments must be a JSON object"
    return parsed, None


def normalise(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a history no provider can choke on.

    Guarantees: content is never ``None``; every ``tool`` message follows the
    assistant turn that requested it; no tool call is left without a result.
    An unmatched tool call makes several APIs hard-fail, and it happens
    routinely when a turn is cancelled mid-flight.
    """
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")

        if role in ("user", "system"):
            out.append({"role": role, "content": message.get("content") or ""})
            index += 1
            continue

        if role == "assistant":
            calls = message.get("tool_calls") or []
            content = message.get("content") or ""
            if not calls:
                if content:
                    out.append({"role": "assistant", "content": content})
                index += 1
                continue

            # Collect the tool results that follow this assistant turn.
            results: dict[str, dict[str, Any]] = {}
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].get("role") == "tool":
                result = messages[cursor]
                results[result.get("tool_call_id", "")] = result
                cursor += 1

            kept_calls = [c for c in calls if c.get("id") in results]
            assistant: dict[str, Any] = {"role": "assistant", "content": content}
            if kept_calls:
                assistant["tool_calls"] = kept_calls
            if kept_calls or content:
                out.append(assistant)
            for call in kept_calls:
                result = results[call["id"]]
                out.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call.get("function", {}).get("name", ""),
                    "content": str(result.get("content") or "(no output)"),
                    **({"is_error": True} if result.get("is_error") else {}),
                })
            index = cursor
            continue

        index += 1  # orphaned tool message

    # A conversation must not begin with an assistant turn.
    while out and out[0].get("role") == "assistant":
        out.pop(0)
    return out


def _split_for_compaction(messages: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Split into ``(older, recent)`` on a user-turn boundary."""
    if len(messages) <= KEEP_RECENT_MESSAGES:
        return [], messages
    boundary = len(messages) - KEEP_RECENT_MESSAGES
    while boundary < len(messages) and messages[boundary].get("role") != "user":
        boundary += 1
    if boundary >= len(messages):
        boundary = max(0, len(messages) - KEEP_RECENT_MESSAGES)
    return messages[:boundary], messages[boundary:]


def transcript_for_summary(messages: Sequence[dict[str, Any]], limit: int = 24_000) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        content = str(message.get("content") or "")[:1500]
        if role == "tool":
            lines.append(f"[tool result] {content[:600]}")
        elif role == "assistant":
            calls = ", ".join(
                c.get("function", {}).get("name", "?") for c in message.get("tool_calls") or []
            )
            lines.append(f"[assistant] {content}" + (f" (called: {calls})" if calls else ""))
        else:
            lines.append(f"[{role}] {content}")
    text = "\n".join(lines)
    return text[-limit:] if len(text) > limit else text


SUMMARY_INSTRUCTION = """\
Summarise the conversation so far so that you can carry on working with no other \
context. Write it for yourself, not for the user.

Cover, in this order:
1. What the user actually asked for, in their words.
2. Decisions made and constraints agreed.
3. Files created or changed, with paths, and what changed in each.
4. Commands run that mattered, and what they showed.
5. What is done, what is still outstanding, and the immediate next step.

Be specific — exact paths, names and values. Omit anything that no longer \
affects what happens next."""


async def compact(
    messages: list[dict[str, Any]],
    provider,
    *,
    system: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """Fold older history into a summary.  Returns ``(messages, compacted)``."""
    older, recent = _split_for_compaction(messages)
    if not older:
        return messages, False

    summary = ""
    try:
        summary = await provider.complete(
            f"{SUMMARY_INSTRUCTION}\n\n---\n\n{transcript_for_summary(older)}",
            system="You write precise handover notes.",
        )
    except Exception as exc:
        log.warning("Could not summarise history (%s); dropping the oldest turns instead.", exc)

    if summary.strip():
        marker = {
            "role": "user",
            "content": (
                "[Earlier conversation, summarised to save context]\n\n"
                f"{summary.strip()}\n\n"
                "[End of summary — continue from here.]"
            ),
        }
        return normalise([marker, *recent]), True

    log.info("Compaction fell back to truncation (%d messages dropped).", len(older))
    return normalise(recent), True


def should_compact(
    messages: Sequence[dict[str, Any]],
    system: str,
    context_window: int,
    threshold: float,
) -> bool:
    if context_window <= 0:
        return False
    used = estimate_tokens(messages, system)
    return used > context_window * max(0.3, min(threshold, 0.95))


def context_usage(
    messages: Sequence[dict[str, Any]], system: str, context_window: int
) -> dict[str, Any]:
    used = estimate_tokens(messages, system)
    return {
        "estimatedTokens": used,
        "contextWindow": context_window,
        "contextPct": round(min(100.0, used / context_window * 100), 1) if context_window else 0.0,
    }
