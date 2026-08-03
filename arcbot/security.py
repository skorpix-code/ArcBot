"""Path sandboxing and shell-command risk classification.

Nothing here executes anything — it only judges.  The permission engine turns
these judgements into allow / ask / deny decisions, and the UI shows the
reasons so the user always knows *why* something needed approval.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


class SandboxError(PermissionError):
    """Raised when a path escapes every permitted root."""


# --------------------------------------------------------------------------- #
# Path sandbox
# --------------------------------------------------------------------------- #

#: Files/dirs that are never readable or writable by tools, wherever they live.
#: These hold credentials whose exfiltration would be the worst outcome of a
#: prompt-injection attack, so they are denied even inside the workspace.
_SENSITIVE_NAMES = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
    ".netrc", "_netrc", ".pgpass", ".git-credentials", "id_rsa", "id_ed25519",
    "credentials.json", "shadow", "sudoers",
}
_SENSITIVE_DIR_PARTS = {".ssh", ".gnupg", ".aws", ".gcloud", ".azure", ".kube"}


def _resolve(path: Path) -> Path:
    """``resolve()`` that works for not-yet-existing paths on every platform."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(str(path)))


def is_within(child: Path, parent: Path) -> bool:
    child, parent = _resolve(child), _resolve(parent)
    return child == parent or parent in child.parents


def is_sensitive(path: Path) -> bool:
    resolved = _resolve(path)
    if resolved.name in _SENSITIVE_NAMES:
        return True
    parts = set(resolved.parts)
    if parts & _SENSITIVE_DIR_PARTS:
        return True
    # Any .env that is not the project's own is treated as sensitive.
    return resolved.name.startswith(".env.") and "example" not in resolved.name


def resolve_in_roots(
    user_path: str,
    roots: Sequence[Path],
    *,
    allow_sensitive: bool = False,
) -> Path:
    """Resolve *user_path* and guarantee it stays inside one of *roots*.

    Relative paths are joined onto the first root (the workspace).  Absolute
    paths are accepted only when they already point inside a permitted root.
    """
    if not roots:
        raise SandboxError("No workspace configured.")
    primary = _resolve(Path(roots[0]))
    raw = str(user_path or "").strip()
    if not raw or raw in (".", "./"):
        return primary

    candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
    target = candidate if candidate.is_absolute() else (primary / candidate)
    target = _resolve(target)

    if not any(is_within(target, _resolve(Path(r))) for r in roots):
        raise SandboxError(
            f"'{user_path}' is outside the allowed workspace. "
            f"Allowed roots: {', '.join(str(_resolve(Path(r))) for r in roots)}"
        )
    if not allow_sensitive and is_sensitive(target):
        raise SandboxError(
            f"'{user_path}' looks like a credential store; access is blocked for safety."
        )
    return target


def truncate_output(text: str, max_length: int) -> str:
    """Keep the head and tail — the middle of a long output is rarely the point."""
    if text is None:
        return ""
    if len(text) <= max_length:
        return text
    half = max(200, max_length // 2)
    dropped = len(text) - (half * 2)
    return f"{text[:half]}\n\n… [{dropped:,} characters truncated] …\n\n{text[-half:]}"


def sanitize_text(text: str, max_length: int = 100_000) -> str:
    """Strip control characters that would corrupt the UI or a terminal."""
    if not text:
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text))
    return cleaned[:max_length]


# --------------------------------------------------------------------------- #
# Command risk classification
# --------------------------------------------------------------------------- #


class Risk(IntEnum):
    SAFE = 0        # read-only / trivially reversible
    MODERATE = 1    # writes state, installs software, network egress
    HIGH = 2        # destructive, privileged, or system-altering
    BLOCKED = 3     # catastrophic; never runs, whatever the mode

    @property
    def label(self) -> str:
        return self.name.title()


@dataclass
class CommandRisk:
    level: Risk
    reasons: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    #: Path-like arguments that resolve outside the workspace.
    outside_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "level": int(self.level),
            "label": self.level.label,
            "reasons": self.reasons,
            "categories": self.categories,
            "outsidePaths": self.outside_paths,
        }


# (pattern, level, category, human reason)
_RULES: list[tuple] = [
    # ---- BLOCKED: catastrophic, no legitimate agent use ---------------------
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", Risk.BLOCKED, "fork-bomb", "Shell fork bomb"),
    (r"\brm\b[^|;&]*\s(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b[^|;&]*\s(/|/\*|~|~/|\$HOME)\s*$",
     Risk.BLOCKED, "wipe-root", "Recursive delete of the entire home or root tree"),
    (r"\bmkfs(\.\w+)?\b", Risk.BLOCKED, "format", "Formats a filesystem"),
    (r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|disk|hd|mmcblk)", Risk.BLOCKED, "disk-overwrite",
     "Writes raw data directly to a disk device"),
    (r">\s*/dev/(sd|nvme|disk|hd|mmcblk)\w*", Risk.BLOCKED, "disk-overwrite",
     "Redirects output onto a raw disk device"),
    (r"\bchmod\s+-R\s+0*777\s+/\s*$", Risk.BLOCKED, "permissions", "Makes the entire root tree world-writable"),
    (r"\b(shred|wipefs)\b", Risk.BLOCKED, "destroy", "Irrecoverably destroys data"),

    # ---- HIGH: destructive or privileged ------------------------------------
    (r"\brm\b[^|;&]*\s-[a-zA-Z]*r[a-zA-Z]*f?\b", Risk.HIGH, "recursive-delete", "Recursive delete"),
    (r"\b(sudo|doas|runas|pkexec)\b", Risk.HIGH, "privilege", "Runs with elevated privileges"),
    (r"^\s*su\b", Risk.HIGH, "privilege", "Switches user"),
    (r"\bchmod\s+(-R\s+)?0*777\b", Risk.HIGH, "permissions", "Makes files world-writable"),
    (r"\bchown\b", Risk.HIGH, "permissions", "Changes file ownership"),
    (r"\b(reboot|shutdown|halt|poweroff)\b", Risk.HIGH, "power", "Reboots or powers off the machine"),
    (r"\bsystemctl\s+(stop|disable|mask|start|enable|restart|daemon-reload)\b",
     Risk.HIGH, "service", "Changes a system service"),
    (r"\bservice\s+\w+\s+(stop|start|restart)\b", Risk.HIGH, "service", "Changes a system service"),
    (r"\breg\s+(delete|add)\b", Risk.HIGH, "registry", "Edits the Windows registry"),
    (r"\bnet\s+(user|localgroup)\b", Risk.HIGH, "accounts", "Modifies user accounts"),
    (r"\b(taskkill|pkill|killall)\b|\bkill\s+-9\b", Risk.HIGH, "process-kill", "Force-kills processes"),
    (r"\b(mv|cp|install)\b[^\n]*\s/(bin|sbin|etc|usr|boot|lib|lib64|sys|dev|proc)\b",
     Risk.HIGH, "system-write", "Writes into a protected system directory"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+[^\n]*--force(-with-lease)?\b)",
     Risk.HIGH, "git-destructive", "Destructive git operation (can lose work)"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|fish|python\d?)\b",
     Risk.HIGH, "pipe-to-shell", "Downloads and executes a remote script"),
    (r"\biptables\b|\bufw\s+(allow|deny|disable)\b|\bfirewall-cmd\b",
     Risk.HIGH, "firewall", "Changes firewall rules"),
    (r"\bcrontab\b|\bat\s+now\b|\bschtasks\b", Risk.HIGH, "scheduler", "Schedules background jobs"),
    (r"\bhistory\s+-c\b|\btruncate\b[^\n]*bash_history", Risk.HIGH, "anti-forensics",
     "Clears shell history"),

    # ---- MODERATE: ordinary state changes -----------------------------------
    (r"\b(apt|apt-get|dnf|yum|pacman|zypper|apk|brew|choco|winget|snap|flatpak)\b",
     Risk.MODERATE, "package-install", "System package manager"),
    (r"\b(pip3?|pipx|uv|npm|pnpm|yarn|bun|cargo|go|gem|composer|dotnet)\s+"
     r"(install|add|i|get|remove|uninstall|remove)\b",
     Risk.MODERATE, "package-install", "Installs or removes project packages"),
    (r"\b(docker|podman|kubectl|helm)\b", Risk.MODERATE, "container", "Container / orchestration command"),
    (r"\brm\b", Risk.MODERATE, "delete", "Deletes files"),
    (r"\b(mv|move|rename)\b", Risk.MODERATE, "move", "Moves or renames files"),
    (r"\bgit\s+(push|commit|merge|rebase|checkout|switch|stash|tag)\b",
     Risk.MODERATE, "git", "Changes git state"),
    (r"\b(curl|wget|Invoke-WebRequest|iwr|nc|ncat|ssh|scp|rsync|ftp)\b",
     Risk.MODERATE, "network", "Network access"),
    (r"\bkill\b", Risk.MODERATE, "process-kill", "Signals a process"),
    (r"(?<![0-9>])>{1,2}\s*[^\s|&;]+", Risk.MODERATE, "file-write", "Writes to a file via redirect"),
    (r"\b(tee|truncate|sed\s+-i|perl\s+-i)\b", Risk.MODERATE, "file-write", "Edits files in place"),
    (r"\bnpm\s+run\b|\bmake\b|\bpytest\b|\bcargo\s+(run|build|test)\b|\bgradle\b|\bmvn\b",
     Risk.MODERATE, "build", "Runs a build or test task"),
]

_READ_ONLY_COMMANDS = {
    "ls", "dir", "cat", "bat", "type", "pwd", "echo", "whoami", "date", "df", "du",
    "ps", "top", "htop", "head", "tail", "grep", "rg", "ag", "find", "fd", "which",
    "where", "wc", "uname", "hostname", "env", "printenv", "stat", "file", "tree",
    "id", "uptime", "free", "lscpu", "lsblk", "nproc", "man", "help", "true",
}
_READ_ONLY_SUBCOMMANDS = {
    "git": {"status", "log", "diff", "branch", "show", "remote", "config", "blame",
            "describe", "rev-parse", "ls-files", "shortlog"},
    "npm": {"ls", "list", "view", "outdated", "why"},
    "docker": {"ps", "images", "logs", "inspect", "version", "info"},
    "kubectl": {"get", "describe", "logs", "version"},
    "systemctl": {"status", "list-units", "is-active", "is-enabled", "cat", "show"},
    "pip": {"list", "show", "freeze"},
    "uv": {"tree", "version"},
    "cargo": {"tree", "check"},
    "go": {"version", "env", "list"},
}

#: Splits a command line into independently-classified segments.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;|\n")


def _segments(command: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_SPLIT.split(command or "") if s.strip()]


def _is_read_only_segment(segment: str) -> bool:
    try:
        parts = shlex.split(segment, posix=os.name != "nt")
    except ValueError:
        return False
    if not parts:
        return False
    # Skip leading VAR=value assignments.
    while parts and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
        parts = parts[1:]
    if not parts:
        return False
    head = Path(parts[0]).name.lower()
    if head in _READ_ONLY_COMMANDS:
        return True
    subs = _READ_ONLY_SUBCOMMANDS.get(head)
    return bool(subs and len(parts) > 1 and parts[1].lower() in subs)


_PATH_TOKEN = re.compile(r"(?:^|\s)(~?/[^\s'\"|;&]+|[A-Za-z]:[\\/][^\s'\"|;&]+)")


def paths_outside(command: str, roots: Iterable[Path]) -> list[str]:
    """Absolute paths in *command* that fall outside every permitted root.

    Advisory only — a shell can always ``cd`` elsewhere — but it catches the
    common accidental case and is worth surfacing in the approval dialog.
    """
    resolved_roots = [_resolve(Path(r)) for r in roots]
    out: list[str] = []
    for match in _PATH_TOKEN.findall(command or ""):
        try:
            candidate = _resolve(Path(os.path.expanduser(match)))
        except Exception:
            continue
        outside = not any(is_within(candidate, root) for root in resolved_roots)
        if outside and str(candidate) not in out:
            out.append(str(candidate))
    return out


def classify_command(command: str, roots: Iterable[Path] | None = None) -> CommandRisk:
    """Return a structured risk assessment for a shell command line."""
    cmd = (command or "").strip()
    if not cmd:
        return CommandRisk(Risk.SAFE, ["Empty command"])

    reasons: list[str] = []
    categories: list[str] = []
    level = Risk.SAFE

    for pattern, rule_level, category, reason in _RULES:
        if re.search(pattern, cmd, re.IGNORECASE):
            level = max(level, rule_level)
            if reason not in reasons:
                reasons.append(reason)
            if category not in categories:
                categories.append(category)

    if level == Risk.SAFE:
        segs = _segments(cmd)
        if segs and all(_is_read_only_segment(s) for s in segs):
            reasons.append("Read-only inspection command")
        else:
            level = Risk.MODERATE
            reasons.append("Unrecognised command — treated as state-changing")
            categories.append("unknown")

    outside = paths_outside(cmd, roots) if roots else []
    if outside:
        level = max(level, Risk.HIGH)
        reasons.append(f"Touches {len(outside)} path(s) outside the workspace")
        if "outside-workspace" not in categories:
            categories.append("outside-workspace")

    return CommandRisk(level, reasons, categories, outside)


def matches_prefix(command: str, prefixes: Iterable[str]) -> bool:
    """True when *command* starts with one of the user's saved allow/deny rules.

    Matching is on whole tokens so ``git s`` never matches ``git status``, and
    a rule only ever applies to a single command (no ``&&`` chaining past it).
    """
    cmd = " ".join((command or "").split())
    for prefix in prefixes:
        rule = " ".join((prefix or "").split())
        if not rule:
            continue
        if cmd == rule:
            return True
        if cmd.startswith(rule + " ") and not _SEGMENT_SPLIT.search(cmd):
            return True
    return False
