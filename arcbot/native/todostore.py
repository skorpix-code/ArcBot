"""A deliberately tiny task board.

Multi-step work goes wrong when the model loses the thread, so ArcBot keeps a
plan it can re-read every step.  The schema is intentionally minimal — id,
title, status, note — because richer schemas measurably confuse small models.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ..paths import ensure_state_dir

STATUSES = ("pending", "in_progress", "done", "blocked")


class TodoManager:
    def __init__(self, workspace: Path):
        self.path = ensure_state_dir(Path(workspace)) / "plan.json"

    # ------------------------------------------------------------- storage
    def _load(self) -> List[Dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("items", data if isinstance(data, list) else [])
            return [i for i in items if isinstance(i, dict) and i.get("id")]
        except (OSError, json.JSONDecodeError, AttributeError):
            return []

    def _save(self, items: List[Dict]) -> None:
        payload = {"items": items, "updated": time.time()}
        try:
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    # --------------------------------------------------------------- public
    def list_tasks_raw(self) -> List[Dict]:
        return self._load()

    def replace(self, titles: List[str]) -> List[Dict]:
        """Set the whole plan at once — the normal way a turn starts."""
        items = [
            {"id": uuid.uuid4().hex[:8], "title": t.strip(), "status": "pending", "note": ""}
            for t in titles
            if t and t.strip()
        ]
        self._save(items)
        return items

    def add(self, title: str) -> Dict:
        items = self._load()
        item = {"id": uuid.uuid4().hex[:8], "title": title.strip(), "status": "pending", "note": ""}
        items.append(item)
        self._save(items)
        return item

    def set_status(self, task_id: str, status: str, note: str = "") -> Optional[Dict]:
        if status not in STATUSES:
            return None
        items = self._load()
        for item in items:
            if item["id"] == task_id or item["title"].lower() == task_id.lower():
                item["status"] = status
                if note:
                    item["note"] = note
                self._save(items)
                return item
        return None

    def clear(self) -> None:
        self._save([])

    def summary(self) -> str:
        items = self._load()
        if not items:
            return "(no plan yet)"
        marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]"}
        lines = [f"{marks.get(i['status'], '[ ]')} {i['title']}" + (f" — {i['note']}" if i.get("note") else "")
                 for i in items]
        done = sum(1 for i in items if i["status"] == "done")
        return f"Plan ({done}/{len(items)} done):\n" + "\n".join(lines)
