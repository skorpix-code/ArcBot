"""Typed settings plus an atomic, permission-aware JSON store.

Two files, deliberately separated:

* ``config.json``      — everything safe to read, share and diff.
* ``credentials.json`` — API keys only, written with ``0600``.

Nothing here imports the agent, so both the CLI and the web app can read
configuration without pulling in the runtime.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .paths import config_file, credentials_file, default_workspace, ensure_dir

log = get_logger("config")

CONFIG_VERSION = 3


def _default_toolsets() -> list[str]:
    """The catalog's recommended set, imported late to avoid a cycle."""
    from .tools.catalog import DEFAULT_TOOLSETS

    return list(DEFAULT_TOOLSETS)


def load_dotenv(path: Path | None = None) -> int:
    """Read a ``.env`` file into the environment without overriding it.

    Deliberately tiny and dependency-free — this only exists so containers and
    CI can drop a file next to the app; a real environment variable always wins.
    Returns how many variables were set.
    """
    target = path or Path.cwd() / ".env"
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0

    count = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not key.isidentifier() or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            os.environ[key] = value
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Settings model
# --------------------------------------------------------------------------- #


@dataclass
class ModelSettings:
    """Which brain to use.  ``provider`` is a key from ``providers.catalog``."""

    provider: str = ""
    model: str = ""
    base_url: str = ""
    #: Free-form per-provider knobs (e.g. ``{"claude_code_permission_mode": "default"}``).
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.model)


@dataclass
class PermissionSettings:
    """How much the agent may do without asking.

    * ``plan``     — read-only reconnaissance; no writes, no commands.
    * ``guarded``  — auto-approve read-only actions, ask for everything else.
    * ``trusted``  — also auto-approve ordinary writes; ask before risky actions.
    * ``full``     — auto-approve everything except statically blocked commands.
    """

    mode: str = "guarded"
    #: Command prefixes the user chose to always allow, e.g. ``["git status"]``.
    allow_commands: list[str] = field(default_factory=list)
    #: Command prefixes to always refuse.
    deny_commands: list[str] = field(default_factory=list)
    #: Tool names the user chose to always allow.
    allow_tools: list[str] = field(default_factory=list)
    #: Extra directories (outside the workspace) the agent may read/write.
    extra_roots: list[str] = field(default_factory=list)
    #: Auto-answer safe interactive prompts (y/n, menu choice) of approved commands.
    autopilot_prompts: bool = True

    MODES = ("plan", "guarded", "trusted", "full")


@dataclass
class LimitSettings:
    """Guard rails that keep a weak model from spinning forever."""

    #: Hard cap on model round-trips inside one user turn.
    max_steps: int = 40
    #: Wall-clock budget for one user turn, in seconds (0 = unlimited).
    max_turn_seconds: int = 1800
    #: Identical tool calls tolerated before the agent is told to stop repeating.
    repeat_limit: int = 2
    #: Consecutive failures of the same tool before it is benched for the turn.
    tool_failure_limit: int = 3
    #: Per-command timeout for non-interactive shell execution.
    command_timeout: int = 120
    #: Characters of a single tool result kept before middle-truncation.
    max_tool_output: int = 16_000
    #: Fraction of the model's context window at which history is compacted.
    compact_at: float = 0.75


@dataclass
class UISettings:
    theme: str = "dark"
    accent: str = "ember"
    reduce_motion: bool = False
    show_thinking: bool = True
    sound: bool = False


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    #: Open the browser automatically on start.
    open_browser: bool = True


@dataclass
class Settings:
    version: int = CONFIG_VERSION
    onboarded: bool = False
    workspace: str = field(default_factory=lambda: str(default_workspace()))
    model: ModelSettings = field(default_factory=ModelSettings)
    #: Enabled toolset ids.  Derived from the catalog so this default and the
    #: onboarding wizard's recommendation can never drift apart.
    toolsets: list[str] = field(default_factory=lambda: _default_toolsets())
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    limits: LimitSettings = field(default_factory=LimitSettings)
    ui: UISettings = field(default_factory=UISettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    #: External MCP servers, ``{name: {command, args, env, enabled}}``.
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---------------------------------------------------------------- helpers
    @property
    def workspace_path(self) -> Path:
        raw = os.path.expanduser(os.path.expandvars(self.workspace or ""))
        return Path(raw).resolve() if raw else default_workspace().resolve()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        return _build(cls, data or {})


#: Field name -> nested dataclass.  Annotations are strings under PEP 563, so
#: the mapping is explicit rather than derived from ``f.type``.
_NESTED: dict[str, Any] = {
    "model": ModelSettings,
    "permissions": PermissionSettings,
    "limits": LimitSettings,
    "ui": UISettings,
    "server": ServerSettings,
}


def _build(cls, data: dict[str, Any]):
    """Rebuild a (possibly nested) dataclass, ignoring unknown keys.

    Tolerant on purpose: a config written by a newer version, or hand-edited
    with a typo, degrades to defaults instead of crashing the app at startup.
    """
    known = {f.name for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for name, value in (data or {}).items():
        if name not in known:
            continue
        nested = _NESTED.get(name) if cls is Settings else None
        if nested is not None and isinstance(value, dict):
            kwargs[name] = _build(nested, value)
        else:
            kwargs[name] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:  # a value of the wrong shape entirely
        log.warning("Ignoring malformed %s settings (%s).", cls.__name__, exc)
        return cls()


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _atomic_write(path: Path, text: str, *, private: bool = False) -> None:
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if private:
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ConfigStore:
    """Loads/saves :class:`Settings` and the separate credential file."""

    def __init__(self, path: Path | None = None, creds_path: Path | None = None):
        self.path = path or config_file()
        self.creds_path = creds_path or credentials_file()
        self._settings: Settings | None = None
        self._creds: dict[str, str] | None = None

    # ------------------------------------------------------------- settings
    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = self.load()
        return self._settings

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Config unreadable (%s); starting from defaults.", exc)
            return Settings()
        settings = Settings.from_dict(data)
        settings.version = CONFIG_VERSION
        return settings

    def save(self, settings: Settings | None = None) -> Settings:
        if settings is not None:
            self._settings = settings
        current = self.settings
        current.version = CONFIG_VERSION
        _atomic_write(self.path, json.dumps(current.to_dict(), indent=2, sort_keys=False))
        return current

    def update(self, **changes: Any) -> Settings:
        settings = self.settings
        for key, value in changes.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        return self.save(settings)

    # ---------------------------------------------------------- credentials
    def _load_creds(self) -> dict[str, str]:
        if self._creds is None:
            try:
                self._creds = json.loads(self.creds_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._creds = {}
        return self._creds

    def get_secret(self, key: str) -> str:
        """Environment wins over the stored file so CI/containers can inject."""
        env = os.environ.get(key, "").strip()
        if env:
            return env
        return str(self._load_creds().get(key, "") or "")

    def set_secret(self, key: str, value: str) -> None:
        creds = self._load_creds()
        if value:
            creds[key] = value
        else:
            creds.pop(key, None)
        self._creds = creds
        _atomic_write(self.creds_path, json.dumps(creds, indent=2), private=True)

    def has_secret(self, key: str) -> bool:
        return bool(self.get_secret(key))

    def secret_keys(self) -> list[str]:
        keys = set(self._load_creds())
        return sorted(keys)


#: Process-wide store.  The web app and CLI share it so a settings change made
#: in one surface is visible to the other without a restart.
STORE = ConfigStore()
