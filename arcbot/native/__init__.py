"""Platform-specific integrations (window managers, hardware, file editing).

These modules shell out to whatever the host actually provides — hyprctl,
swaymsg, wmctrl, yabai, PowerShell — and degrade gracefully when a tool is
missing, so the same tool surface works on Hyprland, GNOME, macOS and Windows.
"""

from __future__ import annotations

from ..logging_setup import get_logger

_log = get_logger("native")


def log(message: str, level: str = "INFO") -> None:
    """Compatibility shim for the platform modules' logging calls."""
    _log.log(
        {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}.get(str(level).upper(), 20),
        "%s",
        message,
    )
