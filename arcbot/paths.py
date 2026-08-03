"""Filesystem locations ArcBot uses.

Config lives in the user's config dir (never inside a project), while per-project
state (sessions, memory, todos) lives in a ``.arcbot`` folder inside the
workspace so it travels with the project and can be git-ignored.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "arcbot"
#: Folder created inside a workspace for ArcBot's private per-project state.
STATE_DIRNAME = ".arcbot"


def _env_path(var: str) -> Path | None:
    raw = os.environ.get(var, "").strip()
    return Path(os.path.expanduser(raw)) if raw else None


def config_dir() -> Path:
    """Where global settings, credentials and logs live."""
    override = _env_path("ARCBOT_CONFIG_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return Path(base) / "ArcBot"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP_NAME


def data_dir() -> Path:
    """Where larger, regenerable artifacts (caches, logs) live."""
    override = _env_path("ARCBOT_DATA_DIR")
    if override:
        return override
    if sys.platform in ("win32", "darwin"):
        return config_dir()
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    return config_dir() / "config.json"


def credentials_file() -> Path:
    return config_dir() / "credentials.json"


def log_file() -> Path:
    return data_dir() / "arcbot.log"


def default_workspace() -> Path:
    return Path.home() / "ArcBot"


def state_dir(workspace: Path) -> Path:
    """Per-workspace private state directory (``<workspace>/.arcbot``)."""
    return Path(workspace) / STATE_DIRNAME


def ensure_state_dir(workspace: Path) -> Path:
    d = state_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    gitignore = d / ".gitignore"
    if not gitignore.exists():
        try:
            gitignore.write_text("*\n", encoding="utf-8")
        except OSError:
            pass
    return d
