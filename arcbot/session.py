"""Chat persistence.

Transcripts are append-only JSONL inside the workspace's ``.arcbot/sessions``
folder — cheap to write, trivial to tail, and readable with ``cat`` if anything
ever goes wrong.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .paths import ensure_state_dir

log = get_logger("session")


class Session:
    """One conversation, backed by a JSONL file."""

    def __init__(self, workspace: Path, session_id: str | None = None):
        self.workspace = Path(workspace)
        self.id = session_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.dir = ensure_state_dir(self.workspace) / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{self.id}.jsonl"
        self.title = ""
        self.created_at = time.time()

    # ------------------------------------------------------------------ write
    def append(self, kind: str, payload: dict[str, Any]) -> None:
        record = {"t": round(time.time(), 3), "kind": kind, **payload}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            log.debug("Could not append to the transcript: %s", exc)

    def set_title(self, text: str) -> None:
        if self.title:
            return
        cleaned = " ".join((text or "").split())[:70]
        if cleaned:
            self.title = cleaned
            self.append("meta", {"title": cleaned})

    # ------------------------------------------------------------------- read
    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return iter(())
        def rows() -> Iterator[dict[str, Any]]:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        return rows()

    def messages(self) -> list[dict[str, Any]]:
        """Rebuild the model-facing history from the transcript."""
        history: list[dict[str, Any]] = []
        for row in self.read():
            if row.get("kind") == "message":
                message = {k: v for k, v in row.items() if k not in ("t", "kind")}
                if message.get("role"):
                    history.append(message)
        return history


def list_sessions(workspace: Path, limit: int = 50) -> list[dict[str, Any]]:
    """Recent conversations, newest first — powers the history sidebar."""
    directory = ensure_state_dir(Path(workspace)) / "sessions"
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        title, turns = "", 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("title") and not title:
                        title = row["title"]
                    if row.get("kind") == "message" and row.get("role") == "user":
                        turns += 1
                        if not title:
                            title = " ".join(str(row.get("content", "")).split())[:70]
        except OSError:
            continue
        out.append({
            "id": path.stem,
            "title": title or "(untitled)",
            "turns": turns,
            "updated": path.stat().st_mtime,
        })
    return out


def delete_session(workspace: Path, session_id: str) -> bool:
    directory = ensure_state_dir(Path(workspace)) / "sessions"
    target = directory / f"{Path(session_id).name}.jsonl"
    if target.is_file() and target.parent == directory:
        target.unlink()
        return True
    return False
