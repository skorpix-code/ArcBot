"""Steer the agent to the right tool instead of the shell.

A model reaches for ``cat``, ``ls`` and ``grep`` out of habit, because that is
what its training data is full of.  But a shell command is the worst way for
ArcBot to do those things: the output is unstructured, the path is unchecked,
the result cannot be rendered as a diff or a file list, and the whole call needs
approval that a first-class tool would not.

So the shell tool checks first.  If a real tool covers what the command is
doing, the call comes back as a redirect naming that tool and its arguments —
which the model acts on immediately, because it is specific.

This is guidance, not a cage.  Anything the redirect table does not recognise
runs as normal, and ``run_command(force=True)`` bypasses it entirely for the
cases where the shell genuinely is the right answer.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .tools.catalog import CATALOG


@dataclass(frozen=True)
class Redirect:
    """A better way to do what the command was trying to do."""

    tool: str
    #: The toolset that provides it, so a disabled one can be requested.
    toolset: str
    #: Filled-in call the model can make, e.g. ``read_file(path="README.md")``.
    suggestion: str
    why: str = ""

    def message(self, enabled: set[str]) -> str:
        if self.toolset not in enabled and self.toolset != "always":
            entry = CATALOG.get(self.toolset)
            name = entry.name if entry else self.toolset
            return (
                f"Use the {name} capability for this instead of a shell command — it is "
                f"safer and gives you structured output. It is currently switched off: call "
                f"enable_toolset('{self.toolset}', reason) first, then use {self.tool}."
            )
        detail = f" {self.why}" if self.why else ""
        return (
            f"Use {self.tool} instead of a shell command.{detail}\n"
            f"Call it as: {self.suggestion}\n"
            f"If you genuinely need the shell for this, call run_command again with force=true."
        )


def _quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


#: Commands that always have a better tool, keyed by the executable name.
#: The value builds the redirect from the parsed argument list.
def _read(args: list[str]) -> Redirect | None:
    paths = [a for a in args if not a.startswith("-")]
    if not paths:
        return None
    return Redirect("read_file", "files", f"read_file(path={_quote(paths[0])})",
                    "It numbers the lines and keeps you inside the workspace.")


def _list(args: list[str]) -> Redirect | None:
    paths = [a for a in args if not a.startswith("-")]
    target = paths[0] if paths else "."
    recursive = any(a.startswith("-") and "R" in a for a in args)
    call = f"list_files(path={_quote(target)}" + (", recursive=true)" if recursive else ")")
    return Redirect("list_files", "files", call, "It skips build output and dependency folders.")


def _tree(args: list[str]) -> Redirect | None:
    paths = [a for a in args if not a.startswith("-")]
    return Redirect("directory_tree", "files",
                    f"directory_tree(path={_quote(paths[0] if paths else '.')})")


def _grep(args: list[str]) -> Redirect | None:
    positional = [a for a in args if not a.startswith("-")]
    if not positional:
        return None
    pattern = positional[0]
    where = positional[1] if len(positional) > 1 else "."
    return Redirect(
        "search_code", "files",
        f"search_code(query={_quote(pattern)}, path={_quote(where)})",
        "It returns file:line matches and skips binaries and vendor directories.",
    )


def _find(args: list[str]) -> Redirect | None:
    name = None
    for index, arg in enumerate(args):
        if arg in ("-name", "-iname") and index + 1 < len(args):
            name = args[index + 1]
            break
    return Redirect("find_files", "files",
                    f"find_files(pattern={_quote(name or '*')})")


def _remove(args: list[str]) -> Redirect | None:
    paths = [a for a in args if not a.startswith("-")]
    if not paths:
        return None
    recursive = any(a.startswith("-") and ("r" in a.lower()) for a in args)
    call = f"delete_path(path={_quote(paths[0])}" + (", recursive=true)" if recursive else ")")
    return Redirect("delete_path", "files", call,
                    "It refuses paths outside the workspace, which rm does not.")


def _move(args: list[str]) -> Redirect | None:
    paths = [a for a in args if not a.startswith("-")]
    if len(paths) < 2:
        return None
    return Redirect("move_path", "files",
                    f"move_path(source={_quote(paths[0])}, destination={_quote(paths[1])})")


def _copy(args: list[str]) -> Redirect | None:
    paths = [a for a in args if not a.startswith("-")]
    if len(paths) < 2:
        return None
    return Redirect("copy_path", "files",
                    f"copy_path(source={_quote(paths[0])}, destination={_quote(paths[1])})")


def _mkdir(args: list[str]) -> Redirect | None:
    paths = [a for a in args if not a.startswith("-")]
    if not paths:
        return None
    return Redirect("create_directory", "files", f"create_directory(path={_quote(paths[0])})")


def _curl(args: list[str]) -> Redirect | None:
    urls = [a for a in args if a.startswith(("http://", "https://"))]
    if not urls:
        return None
    return Redirect("read_webpage", "web", f"read_webpage(url={_quote(urls[0])})",
                    "It returns readable text instead of raw HTML. "
                    "For an API call use http_request; to save a file use download_file.")


def _ps(_args: list[str]) -> Redirect | None:
    return Redirect("list_processes", "system", "list_processes(sort_by=\"cpu\")")


def _df(_args: list[str]) -> Redirect | None:
    return Redirect("disk_usage", "system", "disk_usage()")


def _uname(_args: list[str]) -> Redirect | None:
    return Redirect("system_info", "system", "system_info()",
                    "It reports the OS, CPU, memory, GPU and desktop in one call.")


def _which(args: list[str]) -> Redirect | None:
    names = [a for a in args if not a.startswith("-")]
    if not names:
        return None
    return Redirect("which_program", "system", f"which_program(program={_quote(names[0])})")


def _diff(args: list[str]) -> Redirect | None:
    paths = [a for a in args if not a.startswith("-")]
    if len(paths) < 2:
        return None
    return Redirect("diff_files", "files",
                    f"diff_files(left={_quote(paths[0])}, right={_quote(paths[1])})")


def _clipboard(_args: list[str]) -> Redirect | None:
    return Redirect("read_clipboard", "desktop", "read_clipboard()")


def _notify(args: list[str]) -> Redirect | None:
    positional = [a for a in args if not a.startswith("-")]
    title = positional[0] if positional else "Notice"
    return Redirect("send_notification", "desktop",
                    f"send_notification(title={_quote(title)})")


def _screenshot(_args: list[str]) -> Redirect | None:
    return Redirect("take_screenshot", "desktop", "take_screenshot()")


def _git(args: list[str]) -> Redirect | None:
    if not args:
        return None
    sub = args[0]
    mapping = {
        "status": ("git_status", "git_status()"),
        "diff": ("git_diff", "git_diff()"),
        "log": ("git_log", "git_log()"),
        "branch": ("git_branch", "git_branch()"),
        "show": ("git_show", "git_show()"),
    }
    if sub not in mapping:
        return None
    tool, call = mapping[sub]
    return Redirect(tool, "git", call, "It returns a parsed summary rather than raw output.")


_HANDLERS = {
    "cat": _read, "bat": _read, "head": _read, "tail": _read, "less": _read, "more": _read,
    "type": _read,
    "ls": _list, "dir": _list,
    "tree": _tree,
    "grep": _grep, "rg": _grep, "ag": _grep, "ack": _grep,
    "find": _find, "fd": _find,
    "rm": _remove, "del": _remove,
    "mv": _move, "move": _move,
    "cp": _copy,
    "mkdir": _mkdir,
    "curl": _curl, "wget": _curl,
    "ps": _ps, "top": _ps, "htop": _ps,
    "df": _df, "du": _df,
    "uname": _uname, "hostnamectl": _uname, "lscpu": _uname, "free": _uname,
    "which": _which, "where": _which, "command": _which,
    "diff": _diff,
    "pbpaste": _clipboard, "wl-paste": _clipboard, "xclip": _clipboard,
    "notify-send": _notify,
    "grim": _screenshot, "scrot": _screenshot, "maim": _screenshot, "screencapture": _screenshot,
    "git": _git,
}

#: Anything with a shell operator is left alone — a pipeline is doing something
#: a single tool call cannot express, and rewriting it would be wrong.
_COMPOUND = re.compile(r"[|;&><]|\$\(|`|&&")


def find_redirect(command: str) -> Redirect | None:
    """The tool that should have been used, or ``None`` to let the shell run."""
    text = (command or "").strip()
    if not text or _COMPOUND.search(text):
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    # Skip leading environment assignments (FOO=bar cmd ...).
    while parts and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
        parts = parts[1:]
    if not parts:
        return None

    head = parts[0].rsplit("/", 1)[-1].lower()
    handler = _HANDLERS.get(head)
    if handler is None:
        return None
    try:
        return handler(parts[1:])
    except Exception:  # a redirect must never break the call it was inspecting
        return None
