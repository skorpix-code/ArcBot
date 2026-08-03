"""The layered memory orchestrator.

``MemoryManager`` is the single object the rest of ArcBot talks to.  It handles
storage, near-duplicate merging, blended recall ranking, and — most importantly
for small local models — :meth:`build_context`, which produces a compact block
of the most relevant memories to inject into the prompt automatically so the
model never has to *remember to remember*.
"""

from __future__ import annotations

import builtins
import time
from collections.abc import Sequence
from pathlib import Path

from .retrieval import BM25, extract_keywords, tokenize
from .store import MemoryStore
from .types import WORKING_TTL_SECONDS, MemoryRecord, MemoryType

_HALF_LIFE_DAYS = 14.0  # recency weight halves every two weeks


def _recency_weight(created_at: float) -> float:
    age_days = max(0.0, (time.time() - created_at) / 86400.0)
    return 0.5 ** (age_days / _HALF_LIFE_DAYS)


def _similarity(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)  # Jaccard


class MemoryManager:
    def __init__(self, state_dir: Path):
        self.store = MemoryStore(Path(state_dir) / "memory.db")
        self.store.purge_expired()

    # ------------------------------------------------------------------ write
    def remember(
        self,
        content: str,
        mtype: MemoryType | str = MemoryType.SEMANTIC,
        importance: float = 0.5,
        keywords: builtins.list[str] | None = None,
        source: str = "agent",
        metadata: dict | None = None,
        pinned: bool = False,
        dedup: bool = True,
    ) -> MemoryRecord:
        content = (content or "").strip()
        if not content:
            raise ValueError("Cannot store empty memory")
        mtype = MemoryType.coerce(mtype)
        kw = keywords or extract_keywords(content)

        if dedup:
            existing = self._find_duplicate(content, mtype, kw)
            if existing is not None:
                # Reinforce rather than duplicate.
                existing.importance = min(1.0, max(existing.importance, importance) + 0.05)
                existing.last_accessed = time.time()
                existing.access_count += 1
                if pinned:
                    existing.pinned = True
                if metadata:
                    existing.metadata.update(metadata)
                self.store.upsert(existing)
                return existing

        rec = MemoryRecord(
            content=content,
            mtype=mtype,
            importance=importance,
            keywords=kw,
            source=source,
            metadata=metadata or {},
            pinned=pinned,
        )
        if mtype == MemoryType.WORKING:
            rec.expires_at = time.time() + WORKING_TTL_SECONDS
        self.store.upsert(rec)
        return rec

    def _find_duplicate(
        self, content: str, mtype: MemoryType, kw: builtins.list[str], threshold: float = 0.82
    ) -> MemoryRecord | None:
        target = tokenize(content)
        for rec in self.store.all(mtype=mtype, limit=400):
            if _similarity(target, tokenize(rec.content)) >= threshold:
                return rec
        return None

    def forget(self, mem_id: str) -> bool:
        return self.store.delete(mem_id)

    def forget_matching(self, query: str, limit: int = 1) -> int:
        hits = self.recall(query, k=limit, touch=False)
        removed = 0
        for rec in hits:
            if self.store.delete(rec.id):
                removed += 1
        return removed

    def pin(self, mem_id: str, pinned: bool = True) -> bool:
        rec = self.store.get(mem_id)
        if not rec:
            return False
        rec.pinned = pinned
        self.store.upsert(rec)
        return True

    def clear(self, mtype: MemoryType | str | None = None) -> int:
        mt = MemoryType.coerce(mtype) if mtype else None
        return self.store.clear(mt)

    # ------------------------------------------------------------------- read
    def recall(
        self,
        query: str,
        k: int = 6,
        types: Sequence[MemoryType] | None = None,
        touch: bool = True,
    ) -> builtins.list[MemoryRecord]:
        """Return the *k* memories most relevant to *query*, blending lexical
        relevance with importance, recency and pinning."""
        records: list[MemoryRecord] = []
        if types:
            for mt in types:
                records.extend(self.store.all(mtype=MemoryType.coerce(mt)))
        else:
            records = self.store.all()
        if not records:
            return []

        q_tokens = tokenize(query)
        corpus = [tokenize(f"{r.content} {' '.join(r.keywords)}") for r in records]
        bm25 = BM25(corpus)
        raw = [bm25.score(q_tokens, i) for i in range(len(records))] if q_tokens else [0.0] * len(records)
        max_raw = max(raw) or 1.0

        scored = []
        for i, rec in enumerate(records):
            lexical = raw[i] / max_raw
            score = (
                0.60 * lexical
                + 0.20 * rec.importance
                + 0.15 * _recency_weight(rec.created_at)
                + (0.25 if rec.pinned else 0.0)
            )
            scored.append((score, rec))
        scored.sort(key=lambda kv: kv[0], reverse=True)

        top = [rec for score, rec in scored[:k] if score > 0.02]
        if touch:
            for rec in top:
                self.store.touch(rec.id)
        return top

    def list(self, mtype: MemoryType | str | None = None, limit: int = 100) -> builtins.list[MemoryRecord]:
        mt = MemoryType.coerce(mtype) if mtype else None
        return self.store.all(mtype=mt, limit=limit)

    def stats(self) -> dict[str, int]:
        counts = self.store.count()
        counts["total"] = sum(counts.values())
        return counts

    # ---------------------------------------------------------------- context
    def build_context(self, query: str, budget_chars: int = 1600) -> str:
        """Compact block of the most relevant memories for prompt injection.

        Always leads with pinned/preference memories (they steer behaviour),
        then the best query-relevant recalls, staying under *budget_chars* so a
        small context window isn't overwhelmed.
        """
        self.store.purge_expired()
        chosen: list[MemoryRecord] = []
        seen: set = set()

        # 1) Pinned + preferences always come first.
        priority = self.store.all(mtype=MemoryType.PREFERENCE)
        priority += [r for r in self.store.all() if r.pinned]
        for rec in priority:
            if rec.id not in seen:
                seen.add(rec.id)
                chosen.append(rec)

        # 2) Query-relevant recalls.
        for rec in self.recall(query, k=8, touch=True):
            if rec.id not in seen:
                seen.add(rec.id)
                chosen.append(rec)

        if not chosen:
            return ""

        lines: list[str] = []
        used = 0
        for rec in chosen:
            line = rec.summary(width=200)
            if used + len(line) > budget_chars and lines:
                break
            lines.append(line)
            used += len(line) + 1

        header = "RELEVANT MEMORY (recalled automatically — use it, don't restate it):"
        return header + "\n" + "\n".join(lines)

    def close(self) -> None:
        self.store.close()
