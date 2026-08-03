"""Shared services a tool body can reach via :func:`arcbot.tools.registry.ctx`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..events import E, EventBus
from ..paths import ensure_state_dir
from ..permissions import PermissionEngine
from ..security import resolve_in_roots, truncate_output


@dataclass
class ToolContext:
    settings: Settings
    bus: EventBus
    permissions: PermissionEngine
    #: Set by the agent for the duration of one tool call, so progress events
    #: can be attributed to the right card in the UI.
    call_id: str = ""
    terminal: Any = None
    #: Callback the ``core`` toolset uses to turn a toolset on mid-turn.
    enable_toolset: Callable[[str], Any] | None = None
    #: Asks the host to shut ArcBot down.  Set by whatever owns the process, so
    #: the agent can act on itself without knowing how it is being served.
    request_quit: Callable[[str], Any] | None = None
    #: Opens a settings panel in the user's browser.
    open_settings: Callable[[str], Any] | None = None
    #: Live facts about the running app, for ``arcbot_status``.
    describe_host: Callable[[], dict] | None = None
    #: ``{resolved path: mtime at read time}`` — powers the read-before-edit and
    #: stale-write guards, which catch the most common destructive tool mistake.
    seen_files: dict[str, float] = field(default_factory=dict)
    _memory: Any = field(default=None, repr=False)
    _todos: Any = field(default=None, repr=False)

    def mark_read(self, path: Path) -> None:
        try:
            self.seen_files[str(path)] = path.stat().st_mtime
        except OSError:
            self.seen_files[str(path)] = 0.0

    def staleness(self, path: Path) -> str | None:
        """Why an edit to *path* should be refused, or ``None`` if it is safe."""
        key = str(path)
        if not path.exists():
            return None
        if key not in self.seen_files:
            return (
                f"You have not read {path.name} in this session. Read it first so you "
                f"edit the current contents."
            )
        try:
            if path.stat().st_mtime > self.seen_files[key] + 1e-6:
                return (
                    f"{path.name} changed on disk since you read it. Read it again "
                    f"before editing so you do not overwrite someone else's change."
                )
        except OSError:
            return None
        return None

    # ------------------------------------------------------------------ paths
    @property
    def workspace(self) -> Path:
        return self.settings.workspace_path

    @property
    def roots(self) -> list[Path]:
        return self.permissions.roots

    @property
    def state_dir(self) -> Path:
        return ensure_state_dir(self.workspace)

    def path(self, user_path: str) -> Path:
        """Resolve a model-supplied path, refusing anything outside the sandbox."""
        return resolve_in_roots(user_path, self.roots)

    def rel(self, path: Path) -> str:
        """Workspace-relative display string (falls back to absolute)."""
        try:
            return str(Path(path).resolve().relative_to(self.workspace))
        except (ValueError, OSError):
            return str(path)

    # ----------------------------------------------------------------- limits
    def clip(self, text: str, limit: int | None = None) -> str:
        return truncate_output(text, limit or self.settings.limits.max_tool_output)

    # ------------------------------------------------------------- lazy state
    @property
    def memory(self):
        if self._memory is None:
            from ..memory import MemoryManager

            self._memory = MemoryManager(self.state_dir)
        return self._memory

    @property
    def todos(self):
        if self._todos is None:
            from ..native.todostore import TodoManager

            self._todos = TodoManager(self.workspace)
        return self._todos

    # ------------------------------------------------------------------ emits
    async def progress(self, chunk: str) -> None:
        """Stream partial output (terminal lines, download progress) to the UI."""
        if self.call_id:
            await self.bus.emit(E.TOOL_PROGRESS, {"callId": self.call_id, "chunk": chunk})

    async def notice(self, text: str, level: str = "info") -> None:
        await self.bus.emit(E.NOTICE, {"level": level, "text": text})

    async def push_todos(self) -> None:
        try:
            await self.bus.emit(E.TODOS, {"items": self.todos.list_tasks_raw()})
        except Exception:
            pass
