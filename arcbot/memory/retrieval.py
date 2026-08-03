"""Lightweight lexical retrieval: tokenizer + BM25, no dependencies.

BM25 gives us solid keyword relevance without any model download.  On a desktop
agent the corpus is at most a few thousand short memories, so ranking in pure
Python is instant.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

_STOPWORDS = frozenset(["a", "an", "the", "this", "that", "these", "those", "and", "or", "but", "if", "then", "else", "for", "to", "of", "in", "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its", "it's", "i", "you", "he", "she", "they", "we", "me", "my", "your", "our", "their", "his", "her", "do", "does", "did", "done", "have", "has", "had", "will", "would", "can", "could", "should", "may", "might", "must", "not", "no", "nor", "so", "than", "too", "very", "just", "about", "into", "over", "under", "again", "further", "once", "here", "there", "all", "any", "both", "each", "few", "more", "most", "other", "some", "such", "only", "own", "same", "s", "t", "can", "don", "now"])

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _stem(token: str) -> str:
    """Very small, fast suffix stripper — not linguistically perfect, but it
    collapses obvious inflections (running/runs -> run) well enough for recall."""
    for suffix in ("ing", "edly", "ered", " ", "ies", "ied"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            break
    for suffix in ("ed", "ly", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            break
    return token


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if raw in _STOPWORDS or len(raw) < 2:
            continue
        out.append(_stem(raw))
    return out


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    """Pick the most salient distinct tokens from *text* (frequency-ordered)."""
    counts: dict[str, int] = {}
    for tok in tokenize(text):
        counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [tok for tok, _ in ranked[:limit]]


class BM25:
    """Classic Okapi BM25 over an in-memory corpus of token lists."""

    __slots__ = ("avgdl", "b", "df", "docs", "idf", "k1", "n")

    def __init__(self, corpus: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = [list(d) for d in corpus]
        self.n = len(self.docs)
        self.df: dict[str, int] = {}
        for doc in self.docs:
            for term in set(doc):
                self.df[term] = self.df.get(term, 0) + 1
        self.idf: dict[str, float] = {}
        for term, freq in self.df.items():
            # BM25+ style idf, always positive.
            self.idf[term] = math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
        total = sum(len(d) for d in self.docs)
        self.avgdl = (total / self.n) if self.n else 0.0

    def score(self, query_tokens: Iterable[str], index: int) -> float:
        doc = self.docs[index]
        if not doc:
            return 0.0
        dl = len(doc)
        freqs: dict[str, int] = {}
        for term in doc:
            freqs[term] = freqs.get(term, 0) + 1
        score = 0.0
        denom_const = self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
        for term in query_tokens:
            f = freqs.get(term, 0)
            if not f:
                continue
            idf = self.idf.get(term, 0.0)
            score += idf * (f * (self.k1 + 1)) / (f + denom_const)
        return score

    def rank(self, query_tokens: Sequence[str]) -> list[tuple[int, float]]:
        scores = [(i, self.score(query_tokens, i)) for i in range(self.n)]
        scores.sort(key=lambda kv: kv[1], reverse=True)
        return scores
