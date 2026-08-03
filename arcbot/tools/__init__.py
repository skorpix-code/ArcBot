"""ArcBot's tool layer.

Tools are plain Python functions grouped into *toolsets* the user switches on
and off.  Nothing here is imported until its toolset is enabled, so a minimal
install stays fast and a missing optional dependency only disables one group.
"""

from .catalog import ALWAYS_ON, CATALOG, DEFAULT_TOOLSETS, catalog_payload, normalise
from .context import ToolContext
from .registry import ToolRegistry, ToolResult, ToolSpec, ctx, tool

__all__ = [
    "ALWAYS_ON",
    "CATALOG",
    "DEFAULT_TOOLSETS",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "catalog_payload",
    "ctx",
    "normalise",
    "tool",
]
