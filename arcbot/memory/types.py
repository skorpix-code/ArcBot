"""Data types for the memory subsystem."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    SEMANTIC = "semantic"        # durable facts / knowledge
    EPISODIC = "episodic"        # events, session/task summaries
    PROCEDURAL = "procedural"    # learned how-to knowledge
    PREFERENCE = "preference"    # explicit user preferences
    WORKING = "working"          # short-lived scratchpad (auto-expires)

    @classmethod
    def coerce(cls, value: Any) -> MemoryType:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except Exception:
            return cls.SEMANTIC


#: Default lifetime for WORKING memories (seconds) — one working day.
WORKING_TTL_SECONDS = 12 * 3600


@dataclass
class MemoryRecord:
    content: str
    mtype: MemoryType = MemoryType.SEMANTIC
    importance: float = 0.5          # 0..1 — how much this should influence recall
    keywords: list[str] = field(default_factory=list)
    source: str = "agent"            # "user" | "agent" | "system"
    metadata: dict[str, Any] = field(default_factory=dict)
    pinned: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    expires_at: float | None = None

    def __post_init__(self) -> None:
        self.mtype = MemoryType.coerce(self.mtype)
        try:
            self.importance = max(0.0, min(1.0, float(self.importance)))
        except (TypeError, ValueError):
            self.importance = 0.5

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def to_row(self) -> tuple:
        return (
            self.id,
            self.mtype.value,
            self.content,
            json.dumps(self.keywords),
            self.importance,
            1 if self.pinned else 0,
            self.created_at,
            self.last_accessed,
            self.access_count,
            self.source,
            json.dumps(self.metadata),
            self.expires_at,
        )

    @classmethod
    def from_row(cls, row: tuple) -> MemoryRecord:
        (rid, mtype, content, keywords, importance, pinned, created_at,
         last_accessed, access_count, source, meta, expires_at) = row
        try:
            kw = json.loads(keywords) if keywords else []
        except Exception:
            kw = []
        try:
            md = json.loads(meta) if meta else {}
        except Exception:
            md = {}
        return cls(
            content=content,
            mtype=MemoryType.coerce(mtype),
            importance=importance,
            keywords=kw,
            source=source or "agent",
            metadata=md,
            pinned=bool(pinned),
            id=rid,
            created_at=created_at,
            last_accessed=last_accessed,
            access_count=access_count or 0,
            expires_at=expires_at,
        )

    def summary(self, width: int = 120) -> str:
        text = self.content.replace("\n", " ").strip()
        if len(text) > width:
            text = text[: width - 1] + "…"
        pin = "📌" if self.pinned else "  "
        return f"{pin}[{self.mtype.value}·{self.importance:.1f}] {text}"
