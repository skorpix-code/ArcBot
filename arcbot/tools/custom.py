"""Loads the tools the user built themselves.

Each approved tool is a small Python module in the user's config directory.
Loading executes it — which is the point, and why nothing lands here without
an explicit save in the tool builder.

A module that fails to load is skipped with a warning rather than taking the
whole toolset down, so one broken tool never blocks the others.
"""

from __future__ import annotations

from ..logging_setup import get_logger

log = get_logger("tools.custom")

TOOLSET = "custom"


def load_custom_tools() -> dict[str, str]:
    """Import every saved tool.  Returns ``{file: error}`` for the ones that failed."""
    from ..toolbuilder import tools_dir

    failures: dict[str, str] = {}
    directory = tools_dir()
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            code = path.read_text(encoding="utf-8")
            namespace = {"__name__": f"arcbot_custom_{path.stem}", "__file__": str(path)}
            exec(compile(code, str(path), "exec"), namespace)
        except Exception as exc:
            failures[path.name] = f"{type(exc).__name__}: {exc}"
            log.warning("Custom tool %s failed to load: %s", path.name, exc)
    return failures


#: Executed on import, which is when the toolset is enabled.
LOAD_FAILURES = load_custom_tools()
