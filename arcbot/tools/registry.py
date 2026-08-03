"""Tool declaration, JSON-schema generation and dispatch.

A tool is an ordinary Python function.  Its signature becomes the JSON schema
the model sees, and its docstring becomes the description — so there is exactly
one place to edit when a tool changes.

Toolsets are imported lazily: a user who never enables ``desktop`` never pays
for importing the window manager, and a machine without ``psutil`` still runs
everything else.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import re
import time
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import UnionType
from typing import (
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
)

from ..logging_setup import get_logger

log = get_logger("tools")

#: Set while a tool executes so tool bodies can reach shared services without
#: threading a context object through every signature.
_CONTEXT: ContextVar[Any] = ContextVar("arcbot_tool_context")


def ctx():
    """The :class:`arcbot.tools.context.ToolContext` for the running tool call."""
    try:
        return _CONTEXT.get()
    except LookupError as exc:  # pragma: no cover - programmer error
        raise RuntimeError("No tool context is active.") from exc


# --------------------------------------------------------------------------- #
# Declaration
# --------------------------------------------------------------------------- #


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    toolset: str
    capability: str = "read"
    #: ``str.format``-style label for the UI, e.g. ``"Read {path}"``.
    title: str = ""
    #: Tool results that are large by nature get a lower preview budget.
    preview_chars: int = 800
    #: True when the tool runs its own, more specific permission check.  The
    #: agent then skips the generic gate so the user sees one prompt carrying
    #: the real detail (the command and its risk) rather than two vague ones.
    self_gated: bool = False
    is_async: bool = False

    def render_title(self, args: dict[str, Any]) -> str:
        if not self.title:
            return self.name.replace("_", " ")
        try:
            return self.title.format(**{k: _short(v) for k, v in args.items()})
        except Exception:
            return self.name.replace("_", " ")

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _short(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


#: Populated at import time by every ``@tool``-decorated function.
_DECLARED: dict[str, ToolSpec] = {}


def tool(
    *,
    toolset: str,
    capability: str = "read",
    title: str = "",
    name: str | None = None,
    preview_chars: int = 800,
    self_gated: bool = False,
):
    """Register a function as a model-callable tool."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        description, param_docs = _parse_docstring(fn)
        spec = ToolSpec(
            name=tool_name,
            description=description,
            parameters=build_schema(fn, param_docs),
            fn=fn,
            toolset=toolset,
            capability=capability,
            title=title,
            preview_chars=preview_chars,
            self_gated=self_gated,
            is_async=inspect.iscoroutinefunction(fn),
        )
        if tool_name in _DECLARED and _DECLARED[tool_name].fn is not fn:
            log.warning("Tool %r redeclared; the later definition wins.", tool_name)
        _DECLARED[tool_name] = spec
        fn.__arcbot_tool__ = spec  # type: ignore[attr-defined]
        return fn

    return decorator


# --------------------------------------------------------------------------- #
# Schema generation
# --------------------------------------------------------------------------- #

_ARGS_HEADING = re.compile(r"^\s*(Args|Arguments|Parameters)\s*:\s*$", re.IGNORECASE)
_ARG_LINE = re.compile(r"^\s*(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)$")
_SECTION = re.compile(r"^\s*(Returns|Raises|Examples?|Notes?|Yields)\s*:\s*$", re.IGNORECASE)


def _parse_docstring(fn: Callable[..., Any]) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into a summary and per-argument help."""
    raw = inspect.getdoc(fn) or ""
    summary_lines: list[str] = []
    params: dict[str, str] = {}
    in_args = False
    current: str | None = None

    for line in raw.splitlines():
        if _ARGS_HEADING.match(line):
            in_args, current = True, None
            continue
        if _SECTION.match(line):
            in_args, current = False, None
            continue
        if in_args:
            match = _ARG_LINE.match(line)
            if match:
                current = match.group(1).lstrip("*")
                params[current] = match.group(2).strip()
            elif current and line.strip():
                params[current] += " " + line.strip()
            continue
        summary_lines.append(line)

    summary = "\n".join(summary_lines).strip()
    return summary or fn.__name__.replace("_", " "), params


_PRIMITIVES = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    dict: {"type": "object"},
    list: {"type": "array"},
    Any: {},
}


def _type_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}
    if annotation in _PRIMITIVES:
        return dict(_PRIMITIVES[annotation])

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Literal:
        values = list(args)
        kind = "string"
        if values and all(isinstance(v, bool) for v in values):
            kind = "boolean"
        elif values and all(isinstance(v, int) for v in values):
            kind = "integer"
        return {"type": kind, "enum": values}

    # ``Optional[X]`` and ``X | None`` have different origins — handle both, or
    # every PEP 604 annotation silently degrades to "string".
    if origin is Union or origin is UnionType:
        non_none = [a for a in args if a is not type(None)]
        if not non_none:
            return {"type": "string"}
        if len(non_none) == 1:
            return _type_schema(non_none[0])
        return {"anyOf": [_type_schema(a) for a in non_none]}

    if origin in (list, set, frozenset, Sequence, tuple):
        item = args[0] if args else str
        return {"type": "array", "items": _type_schema(item)}

    if origin is dict:
        return {"type": "object"}

    # Unresolved string annotations (PEP 563) and exotic types degrade to string.
    if isinstance(annotation, str):
        lowered = annotation.lower()
        for key, schema in (
            ("bool", {"type": "boolean"}),
            ("int", {"type": "integer"}),
            ("float", {"type": "number"}),
            ("list", {"type": "array", "items": {"type": "string"}}),
            ("dict", {"type": "object"}),
        ):
            if lowered.startswith(key) or lowered.startswith(f"optional[{key}"):
                return dict(schema)
    return {"type": "string"}


def build_schema(fn: Callable[..., Any], param_docs: dict[str, str]) -> dict[str, Any]:
    """Derive a JSON Schema object from a function signature."""
    try:
        hints = inspect.get_annotations(fn, eval_str=True)
    except Exception:
        hints = getattr(fn, "__annotations__", {}) or {}

    signature = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in signature.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        schema = _type_schema(hints.get(param_name, param.annotation))
        doc = param_docs.get(param_name)
        if doc:
            schema["description"] = doc
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        elif param.default is not None:
            schema["default"] = param.default
        properties[param_name] = schema

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class ToolResult:
    ok: bool
    content: str
    #: Extra structured payload for the UI (diffs, file lists, exit codes…).
    meta: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    @classmethod
    def error(cls, message: str, **meta: Any) -> ToolResult:
        return cls(False, message, meta)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class ToolRegistry:
    """Holds the *enabled* tools and dispatches calls to them."""

    def __init__(self) -> None:
        self._enabled_toolsets: list[str] = []
        self._loaded_modules: dict[str, Any] = {}
        self._failed: dict[str, str] = {}

    # ------------------------------------------------------------- lifecycle
    def load(self, toolset_ids: Sequence[str]) -> dict[str, str]:
        """Import the modules for *toolset_ids*.  Returns ``{id: error}`` failures."""
        from .catalog import CATALOG  # local import avoids a cycle

        errors: dict[str, str] = {}
        wanted = [t for t in dict.fromkeys(toolset_ids) if t in CATALOG]
        for toolset_id in wanted:
            entry = CATALOG[toolset_id]
            if toolset_id in self._loaded_modules:
                continue
            missing = entry.missing_requirements()
            if missing:
                errors[toolset_id] = f"missing Python package(s): {', '.join(missing)}"
                self._failed[toolset_id] = errors[toolset_id]
                continue
            try:
                module = importlib.import_module(f".{entry.module}", __package__)
                self._loaded_modules[toolset_id] = module
                self._failed.pop(toolset_id, None)
            except Exception as exc:
                log.warning("Toolset %r failed to load: %s", toolset_id, exc)
                errors[toolset_id] = str(exc)
                self._failed[toolset_id] = str(exc)

        self._enabled_toolsets = [t for t in wanted if t in self._loaded_modules]
        return errors

    def enable(self, toolset_id: str) -> str | None:
        """Turn one toolset on at runtime.  Returns an error string on failure."""
        errors = self.load([*self._enabled_toolsets, toolset_id])
        return errors.get(toolset_id)

    def disable(self, toolset_id: str) -> None:
        self._enabled_toolsets = [t for t in self._enabled_toolsets if t != toolset_id]

    @property
    def enabled_toolsets(self) -> list[str]:
        return list(self._enabled_toolsets)

    @property
    def failures(self) -> dict[str, str]:
        return dict(self._failed)

    # ----------------------------------------------------------------- query
    def specs(self) -> list[ToolSpec]:
        active = set(self._enabled_toolsets)
        return [s for s in _DECLARED.values() if s.toolset in active or s.toolset == "always"]

    def get(self, name: str) -> ToolSpec | None:
        spec = _DECLARED.get(name)
        if spec is None:
            return None
        if spec.toolset == "always" or spec.toolset in self._enabled_toolsets:
            return spec
        return None

    def find_anywhere(self, name: str) -> ToolSpec | None:
        """Look a tool up even if its toolset is currently disabled."""
        return _DECLARED.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [s.to_schema() for s in sorted(self.specs(), key=lambda s: s.name)]

    def register_dynamic(self, spec: ToolSpec) -> None:
        """Add a tool discovered at runtime (an external MCP server's tools)."""
        _DECLARED[spec.name] = spec

    def unregister_dynamic(self, prefix: str) -> None:
        for name in [n for n, s in _DECLARED.items() if n.startswith(prefix)]:
            _DECLARED.pop(name, None)

    # -------------------------------------------------------------- dispatch
    async def call(
        self,
        name: str,
        args: dict[str, Any],
        context: Any,
        *,
        timeout: float | None = None,
    ) -> ToolResult:
        spec = self.get(name)
        if spec is None:
            return ToolResult.error(f"Tool '{name}' is not available.")

        cleaned, problem = _coerce_args(spec, args)
        if problem:
            return ToolResult.error(f"Invalid arguments for {name}: {problem}")

        started = time.monotonic()
        token = _CONTEXT.set(context)
        try:
            # `to_thread` (not `run_in_executor`) for sync tools: it copies the
            # current context, and without that a sync tool cannot see `ctx()`.
            coro = spec.fn(**cleaned) if spec.is_async else asyncio.to_thread(spec.fn, **cleaned)
            raw = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        except asyncio.TimeoutError:
            return ToolResult.error(
                f"{name} timed out after {timeout:.0f}s. Try a narrower request.",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Tool %s failed", name, exc_info=True)
            return ToolResult.error(f"{type(exc).__name__}: {exc}")
        finally:
            _CONTEXT.reset(token)

        elapsed = int((time.monotonic() - started) * 1000)
        if isinstance(raw, ToolResult):
            raw.elapsed_ms = elapsed
            return raw
        return ToolResult(True, "" if raw is None else str(raw), {}, elapsed)


def _coerce_args(spec: ToolSpec, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Drop unknown keys and coerce loose JSON types.

    Small models routinely send ``"3"`` for an int or ``"true"`` for a bool.
    Coercing is far better UX than bouncing the call back as an error.
    """
    props: dict[str, Any] = spec.parameters.get("properties", {})
    required: list[str] = spec.parameters.get("required", [])
    if not isinstance(args, dict):
        return {}, "arguments must be a JSON object"

    out: dict[str, Any] = {}
    for key, value in args.items():
        if key not in props:
            continue
        expected = props[key].get("type")
        try:
            out[key] = _coerce(value, expected)
        except (TypeError, ValueError):
            out[key] = value

    missing = [r for r in required if r not in out]
    if missing:
        return out, f"missing required argument(s): {', '.join(missing)}"
    return out, None


def _coerce(value: Any, expected: str | None) -> Any:
    if expected is None or value is None:
        return value
    if expected == "string":
        return value if isinstance(value, str) else str(value)
    if expected == "integer":
        return value if isinstance(value, int) and not isinstance(value, bool) else int(str(value).strip())
    if expected == "number":
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else float(str(value).strip())
    if expected == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "y", "on")
    if expected == "array" and isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            import json

            return json.loads(stripped)
        return [p.strip() for p in stripped.split(",") if p.strip()]
    if expected == "object" and isinstance(value, str):
        import json

        return json.loads(value)
    return value
