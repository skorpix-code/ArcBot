"""Build your own tools, in your own words.

The user describes what they want; the configured model writes a tool module;
ArcBot validates it and shows the code **before anything is saved or run**.

Three rules make this defensible:

1. Nothing is written to disk or executed as a tool until the user has seen the
   code and clicked save.  Generation alone has no side effects.
2. Validation happens in a throwaway namespace, so a broken or hostile module
   fails at check time rather than at agent time.
3. A saved tool declares a capability like any other, so the permission engine
   gates it — a custom tool that shells out still asks in Guarded mode.

The static scan below reports what a tool *can* do rather than blocking it.
A blocklist on generated Python is trivially bypassed and would give false
confidence; an honest capability summary next to the code is worth more.
"""

from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .paths import config_dir, ensure_dir

log = get_logger("toolbuilder")

#: Where approved tools live.  Global, so a tool you build is available in
#: every workspace.
def tools_dir() -> Path:
    return ensure_dir(config_dir() / "tools")


VALID_NAME = re.compile(r"^[a-z][a-z0-9_]{2,39}$")

CAPABILITIES = ("read", "network", "write", "exec", "system", "destructive")

#: What the model is told to produce.  Deliberately narrow: one file, one tool,
#: no imports beyond the standard library unless the user asked for them.
CONTRACT = '''\
You write a single Python module that defines exactly one ArcBot tool.

Output ONLY the code. No prose, no markdown fences, no explanation.

The module must look exactly like this shape:

```
"""One line describing the tool."""

from arcbot.tools.registry import ToolResult, ctx, tool


@tool(toolset="custom", capability="read", title="Short label {arg}")
def my_tool_name(arg: str, count: int = 3) -> ToolResult:
    """What the tool does, in one or two sentences for the model that calls it.

    Args:
        arg: What this argument is for.
        count: What this one is for.
    """
    return ToolResult(True, "result text the model will read")
```

Rules:
- Exactly one function decorated with @tool. Its name is lowercase with
  underscores, and describes an action.
- `toolset="custom"` always.
- `capability` is one of: read, network, write, exec, system, destructive.
  Pick the most powerful thing the tool actually does — the user's trust level
  uses this to decide whether to ask before running it.
- Every parameter is annotated (str, int, float, bool, list[str], or a
  Literal[...] of allowed values) and documented under `Args:`. The signature
  and docstring become the schema the model sees, so they must be accurate.
- Return `ToolResult(ok: bool, content: str)`. `content` is text the model
  reads, so make it informative. Use `ToolResult.error("why")` for failures.
- Handle failure. Never let an exception escape; catch it and return an error
  result explaining what went wrong.
- For file paths, resolve with `ctx().path(user_path)` — that keeps the tool
  inside the user's workspace. Never use bare `open()` on a caller-supplied path.
- Standard library only, unless the request clearly requires a third-party
  package. If it does, import it inside the function and return a helpful
  error result when the import fails.
- Keep it under 80 lines.
'''


@dataclass
class ToolDraft:
    """A generated-but-not-yet-saved tool."""

    code: str
    name: str = ""
    description: str = ""
    capability: str = "read"
    parameters: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    #: Non-blocking observations about what the code can reach.
    notes: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems and bool(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "capability": self.capability,
            "parameters": self.parameters,
            "problems": self.problems,
            "notes": self.notes,
            "valid": self.valid,
        }


# --------------------------------------------------------------------------- #
# Static reading
# --------------------------------------------------------------------------- #

#: Module -> what reaching for it means, in the user's terms.
_REACH = {
    "subprocess": "runs other programs on your machine",
    "os": "uses operating-system functions",
    "shutil": "copies, moves or deletes files",
    "socket": "opens network connections",
    "urllib": "makes web requests",
    "requests": "makes web requests",
    "httpx": "makes web requests",
    "http": "makes web requests",
    "smtplib": "sends email",
    "ftplib": "transfers files over FTP",
    "sqlite3": "reads or writes a database",
    "ctypes": "calls native system libraries",
    "pickle": "deserialises Python objects (unsafe with untrusted data)",
    "webbrowser": "opens a browser",
}
_DANGEROUS_CALLS = {
    "eval": "evaluates code from a string",
    "exec": "executes code from a string",
    "compile": "compiles code from a string",
    "__import__": "imports a module chosen at runtime",
}


def describe_reach(tree: ast.AST) -> list[str]:
    """Plain-language notes about what a module can touch."""
    notes: list[str] = []

    def add(text: str) -> None:
        if text not in notes:
            notes.append(text)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _REACH:
                    add(_REACH[root])
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _REACH:
                add(_REACH[root])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _DANGEROUS_CALLS:
                add(_DANGEROUS_CALLS[node.func.id])
            elif node.func.id == "open":
                add("opens files directly")
    return notes


def _extract_decorator(tree: ast.Module) -> tuple[ast.FunctionDef | None, list[str]]:
    """Find the single ``@tool``-decorated function."""
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(_is_tool_decorator(d) for d in node.decorator_list)
    ]
    if not functions:
        return None, ["No function is decorated with @tool."]
    if len(functions) > 1:
        names = ", ".join(f.name for f in functions)
        return None, [f"A tool module must define exactly one tool, but found: {names}."]
    return functions[0], []


def _is_tool_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (isinstance(target, ast.Name) and target.id == "tool") or (
        isinstance(target, ast.Attribute) and target.attr == "tool"
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate(code: str) -> ToolDraft:
    """Check a tool module without installing it.

    The module is executed in a throwaway registry so a bad import or a broken
    annotation surfaces here, not the first time the agent calls it.
    """
    draft = ToolDraft(code=(code or "").strip())
    if not draft.code:
        draft.problems.append("The code is empty.")
        return draft

    try:
        tree = ast.parse(draft.code)
    except SyntaxError as exc:
        draft.problems.append(f"Syntax error on line {exc.lineno}: {exc.msg}")
        return draft

    function, problems = _extract_decorator(tree)
    draft.problems.extend(problems)
    draft.notes = describe_reach(tree)
    if function is None:
        return draft

    if not VALID_NAME.match(function.name):
        draft.problems.append(
            f"'{function.name}' is not a usable tool name — use lowercase letters, "
            f"digits and underscores, starting with a letter."
        )

    # Execute against a scratch copy of the registry so nothing leaks into the
    # live tool table — and so re-checking an *already loaded* tool (which the
    # editor does every keystroke) still sees a fresh registration.
    from .tools import registry as reg

    saved = dict(reg._DECLARED)
    reg._DECLARED.pop(function.name, None)
    try:
        namespace: dict[str, Any] = {"__name__": f"arcbot_custom_{function.name}"}
        exec(compile(draft.code, f"<custom:{function.name}>", "exec"), namespace)
        spec = reg._DECLARED.get(function.name)
    except Exception as exc:
        draft.problems.append(f"The module failed to load: {type(exc).__name__}: {exc}")
        return draft
    finally:
        reg._DECLARED.clear()
        reg._DECLARED.update(saved)

    if spec is None:
        draft.problems.append(
            "The @tool decorator did not register anything. Check it is imported from "
            "arcbot.tools.registry and applied directly to the function."
        )
        return draft

    if spec.toolset != "custom":
        draft.problems.append(f'The decorator must say toolset="custom", not "{spec.toolset}".')
    if spec.capability not in CAPABILITIES:
        draft.problems.append(
            f"'{spec.capability}' is not a capability. Use one of: {', '.join(CAPABILITIES)}."
        )

    from .tools.catalog import TOOL_OWNER

    owner = TOOL_OWNER.get(spec.name)
    if owner and owner != "custom":
        draft.problems.append(
            f"'{spec.name}' is already the name of a built-in tool in the '{owner}' "
            f"toolset. Choose a different name."
        )

    draft.name = spec.name
    draft.description = spec.description
    draft.capability = spec.capability
    draft.parameters = spec.parameters
    return draft


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _path_for(name: str) -> Path:
    if not VALID_NAME.match(name or ""):
        raise ValueError(f"{name!r} is not a valid tool name.")
    return tools_dir() / f"{name}.py"


def save(draft: ToolDraft) -> Path:
    """Write an approved tool to disk.  Refuses anything that did not validate."""
    if not draft.valid:
        raise ValueError("; ".join(draft.problems) or "the tool did not validate")
    path = _path_for(draft.name)
    header = (
        f"# Built with ArcBot's tool builder on {time.strftime('%Y-%m-%d')}.\n"
        f"# Edit freely — it is re-validated every time it loads.\n"
    )
    path.write_text(header + draft.code.rstrip() + "\n", encoding="utf-8")
    log.info("Saved custom tool %s", path)
    return path


def delete(name: str) -> bool:
    try:
        path = _path_for(name)
    except ValueError:
        return False
    if path.is_file():
        path.unlink()
        return True
    return False


def read(name: str) -> str:
    path = _path_for(name)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def list_custom() -> list[dict[str, Any]]:
    """Every saved tool, with whether it currently loads."""
    out: list[dict[str, Any]] = []
    for path in sorted(tools_dir().glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            code = path.read_text(encoding="utf-8")
        except OSError as exc:
            out.append({"name": path.stem, "valid": False, "problems": [str(exc)]})
            continue
        draft = validate(code)
        out.append({
            "name": draft.name or path.stem,
            "file": path.name,
            "description": draft.description.split("\n")[0][:200],
            "capability": draft.capability,
            "valid": draft.valid,
            "problems": draft.problems,
            "notes": draft.notes,
            "updated": path.stat().st_mtime,
        })
    return out


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #


async def generate(description: str, provider, *, existing: str = "") -> ToolDraft:
    """Ask the configured model for a tool, then validate what comes back.

    Generation has no side effects: the result is a draft the user reviews.
    """
    request = f"Write an ArcBot tool that does this:\n\n{description.strip()}"
    if existing:
        request += (
            f"\n\nThe user is editing this existing tool — keep what still applies:\n\n{existing}"
        )

    try:
        code = await provider.complete(request, system=CONTRACT)
    except Exception as exc:  # a provider failure is a problem to show, not a crash
        draft = ToolDraft(code="")
        draft.problems.append(f"The model could not be reached: {exc}")
        return draft

    return validate(strip_fences(code))


def strip_fences(text: str) -> str:
    """Models wrap code in fences however firmly you ask them not to."""
    cleaned = (text or "").strip()
    if "```" in cleaned:
        blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", cleaned, re.S)
        if blocks:
            cleaned = max(blocks, key=len)
        else:
            cleaned = cleaned.replace("```python", "").replace("```py", "").replace("```", "")
    return cleaned.strip()


def example_requests() -> list[str]:
    """Starter prompts for the builder, chosen to show the range."""
    return [
        "Look up a word in the dictionary and return its definition",
        "Count how many lines of each file type are in a folder",
        "Check whether a website is up and how long it took to respond",
        "Convert an amount between two currencies using a public API",
        "Read the front matter from every markdown file in a folder",
    ]


def to_json(draft: ToolDraft) -> str:
    return json.dumps(draft.to_dict())
