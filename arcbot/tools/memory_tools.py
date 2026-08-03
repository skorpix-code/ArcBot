"""Long-term memory.

Relevant memories are injected into every turn automatically, so these tools are
mostly about *writing* — the agent should record durable facts as it learns them
rather than re-discovering them next session.
"""

from __future__ import annotations

from typing import Literal

from ..memory import MemoryType
from .registry import ToolResult, ctx, tool

TOOLSET = "memory"

Kind = Literal["semantic", "episodic", "procedural", "preference", "working"]


@tool(toolset=TOOLSET, capability="write", title="Remember: {content}")
def remember(
    content: str,
    kind: Kind = "semantic",
    importance: float = 0.6,
    pinned: bool = False,
) -> ToolResult:
    """Store something worth recalling in a future session.

    Write a memory when you learn a durable fact about the user, their machine
    or their project; when the user states a preference (importance 0.9, and
    consider pinning it); when you work out a reusable procedure; or when you
    finish a task worth summarising (kind="episodic"). Do not store things the
    code or git history already records.

    Args:
        content: The fact, written so it still makes sense with no other context.
        kind: semantic (facts), preference (how the user wants things done), procedural (how-to), episodic (what happened), working (scratch, expires within a day).
        importance: 0.0-1.0. Use 0.9 or above for explicit user preferences.
        pinned: Always include this memory in context, regardless of relevance.
    """
    context = ctx()
    text = (content or "").strip()
    if len(text) < 3:
        return ToolResult.error("Nothing to remember — content is empty.")
    record = context.memory.remember(
        content=text,
        mtype=kind,
        importance=max(0.0, min(float(importance), 1.0)),
        source="agent",
        pinned=bool(pinned),
    )
    return ToolResult(True, f"Remembered ({kind}): {text[:150]}", {"id": record.id})


@tool(toolset=TOOLSET, capability="read", title="Recall: {query}", preview_chars=1200)
def recall(query: str, limit: int = 6, kind: str = "") -> ToolResult:
    """Search long-term memory.

    Relevant memories are already injected each turn, so reach for this only
    when you need to look something specific up.

    Args:
        query: What to search for.
        limit: Maximum number of memories to return.
        kind: Optionally restrict to one kind (semantic, preference, procedural, episodic, working).
    """
    context = ctx()
    types = [MemoryType.coerce(kind)] if kind else None
    results = context.memory.recall(query, k=max(1, min(int(limit), 25)), types=types)
    if not results:
        return ToolResult(True, f"Nothing remembered about {query!r}.")
    lines = [f"• [{r.mtype.value}] {r.content}" for r in results]
    return ToolResult(True, f"{len(results)} memory/memories for {query!r}:\n" + "\n".join(lines))


@tool(toolset=TOOLSET, capability="read", title="List memories")
def list_memories(kind: str = "", limit: int = 30) -> ToolResult:
    """List stored memories.

    Args:
        kind: Optionally restrict to one kind.
        limit: Maximum number to return.
    """
    context = ctx()
    records = context.memory.list(mtype=kind or None, limit=max(1, min(int(limit), 200)))
    if not records:
        return ToolResult(True, "Memory is empty.")
    lines = [
        f"• [{r.mtype.value}]{' 📌' if r.pinned else ''} {r.content[:180]}" for r in records
    ]
    return ToolResult(True, context.clip("\n".join(lines)), {"count": len(records)})


@tool(toolset=TOOLSET, capability="write", title="Forget: {query}")
def forget(query: str, limit: int = 1) -> ToolResult:
    """Delete memories matching a query. Use when the user says something is wrong or outdated.

    Args:
        query: Text identifying the memories to remove.
        limit: How many matching memories to delete at most.
    """
    context = ctx()
    removed = context.memory.forget_matching(query, limit=max(1, int(limit)))
    if not removed:
        return ToolResult(True, f"Nothing matched {query!r}; nothing was deleted.")
    return ToolResult(True, f"Forgot {removed} memory/memories matching {query!r}.")


@tool(toolset=TOOLSET, capability="read", title="Memory stats")
def memory_stats() -> ToolResult:
    """Report how many memories are stored, broken down by kind."""
    stats = ctx().memory.stats()
    lines = [f"{key}: {value}" for key, value in sorted(stats.items())]
    return ToolResult(True, "\n".join(lines) or "Memory is empty.")


@tool(toolset=TOOLSET, capability="destructive", title="Clear {kind} memories")
def clear_memories(kind: Kind = "working") -> ToolResult:
    """Delete every memory of one kind.

    Use this only when the user explicitly asks to wipe a category — for
    targeted removal use forget instead. Clearing 'working' is routine
    housekeeping; clearing any other kind loses durable knowledge.

    Args:
        kind: Which kind to clear.
    """
    removed = ctx().memory.clear(kind)
    return ToolResult(True, f"Cleared {removed} '{kind}' memory/memories.")
