"""Logging that never leaks secrets and never corrupts a stdio transport."""

from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

from .paths import ensure_dir, log_file

_CONFIGURED = False

# Patterns whose *value* must never reach a log file or the UI.
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    # key=value / "key": "value" forms for anything that smells like a secret
    re.compile(
        r"(?i)\b(api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|password|passwd|secret)\b\s*[:=]\s*[\"']?([^\s\"',]{6,})"
    ),
]


def redact(text: str) -> str:
    """Replace anything that looks like a credential with ``«redacted»``."""
    if not text:
        return text or ""
    out = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}=«redacted»", out)
        else:
            out = pattern.sub("«redacted»", out)
    return out


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(str(record.msg))
            if record.args:
                record.args = tuple(redact(str(a)) for a in record.args)
        except Exception:
            pass
        return True


def setup_logging(level: str | None = None, *, stderr: bool = True) -> None:
    """Configure root logging once.  Console output goes to **stderr** so that
    stdout stays clean for stdio-based transports."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    lvl = (level or os.environ.get("ARCBOT_LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger("arcbot")
    root.setLevel(getattr(logging, lvl, logging.INFO))
    root.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    if stderr:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        console.addFilter(_RedactingFilter())
        root.addHandler(console)

    try:
        path = log_file()
        ensure_dir(path.parent)
        file_handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(_RedactingFilter())
        root.addHandler(file_handler)
    except OSError:
        pass  # read-only home: console logging is enough


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"arcbot.{name}")
