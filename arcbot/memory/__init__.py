"""ArcBot layered memory subsystem (pure-Python, zero heavy deps).

Memory types
------------
* ``SEMANTIC``   — durable facts & knowledge ("user's name is Alex", "project
  uses Python 3.10").
* ``EPISODIC``   — summaries of past events / sessions / completed tasks.
* ``PROCEDURAL`` — reusable how-to knowledge the agent has learned.
* ``PREFERENCE`` — explicit user preferences that should steer behaviour.
* ``WORKING``    — short-lived scratchpad for the current task (auto-expires).

Retrieval is a BM25 keyword ranker blended with importance, recency and a pin
boost — deliberately lightweight so it runs instantly on any consumer machine
and needs no model download.
"""

from .manager import MemoryManager
from .types import MemoryRecord, MemoryType

__all__ = ["MemoryManager", "MemoryRecord", "MemoryType"]
