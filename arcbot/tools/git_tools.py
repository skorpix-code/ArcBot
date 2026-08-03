"""Git, as tools rather than shell commands.

Every call is a fixed argument vector — no shell, no interpolation — so a
repository path or branch name can never turn into a command injection.  The
output is trimmed to what the model actually needs to reason about, because raw
``git log`` on a busy repository will happily eat a context window.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .registry import ToolResult, ctx, tool

TOOLSET = "git"

#: Long output is the norm in git; each tool keeps its own sane slice.
MAX_DIFF_CHARS = 20_000


def _git(args: list[str], cwd: Path, timeout: int = 30) -> tuple[bool, str]:
    """Run one git command, returning ``(ok, combined output)``."""
    binary = shutil.which("git")
    if not binary:
        return False, "git is not installed on this machine."
    try:
        proc = subprocess.run(
            [binary, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PATH": _path_env()},
        )
    except subprocess.TimeoutExpired:
        return False, f"git {args[0]} timed out."
    except OSError as exc:
        return False, str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()


def _path_env() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


def _repo() -> tuple[Path | None, str]:
    """The repository root for the workspace, or an explanation."""
    context = ctx()
    ok, output = _git(["rev-parse", "--show-toplevel"], context.workspace)
    if not ok:
        return None, (
            f"{context.workspace} is not inside a git repository. "
            f"Run `git init` with run_command if you want to create one."
        )
    return Path(output.splitlines()[0].strip()), ""


@tool(toolset=TOOLSET, capability="read", title="git status", preview_chars=1200)
def git_status() -> ToolResult:
    """Show the current branch and which files are staged, modified or untracked.

    Call this before committing, and after making changes, to see exactly what
    you have touched.
    """
    root, problem = _repo()
    if root is None:
        return ToolResult.error(problem)

    ok, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    ok2, porcelain = _git(["status", "--porcelain=v1", "--branch"], root)
    if not ok2:
        return ToolResult.error(porcelain)

    staged, modified, untracked = [], [], []
    for line in porcelain.splitlines():
        if line.startswith("##") or len(line) < 3:
            continue
        code, path = line[:2], line[3:]
        if code == "??":
            untracked.append(path)
        else:
            if code[0] not in " ?":
                staged.append(f"{code[0]} {path}")
            if code[1] not in " ?":
                modified.append(f"{code[1]} {path}")

    lines = [f"Branch: {branch if ok else 'unknown'}", f"Repository: {root}"]
    for label, group in (("Staged", staged), ("Modified", modified), ("Untracked", untracked)):
        if group:
            lines.append(f"\n{label} ({len(group)}):")
            lines.extend(f"  {item}" for item in group[:50])
            if len(group) > 50:
                lines.append(f"  … and {len(group) - 50} more")
    if not (staged or modified or untracked):
        lines.append("\nThe working tree is clean.")
    return ToolResult(True, "\n".join(lines),
                      {"branch": branch if ok else "", "staged": len(staged),
                       "modified": len(modified), "untracked": len(untracked)})


@tool(toolset=TOOLSET, capability="read", title="git diff", preview_chars=2000)
def git_diff(path: str = "", staged: bool = False, stat_only: bool = False) -> ToolResult:
    """Show what changed, as a unified diff.

    Read this before committing so you know exactly what you are about to record.
    For a large change set, start with stat_only to see the shape of it.

    Args:
        path: Limit the diff to one file or directory.
        staged: Show staged changes instead of unstaged ones.
        stat_only: Show only a per-file summary of insertions and deletions.
    """
    root, problem = _repo()
    if root is None:
        return ToolResult.error(problem)

    args = ["diff"]
    if staged:
        args.append("--cached")
    if stat_only:
        args.append("--stat")
    if path:
        args += ["--", path]

    ok, output = _git(args, root)
    if not ok:
        return ToolResult.error(output)
    if not output:
        return ToolResult(True, "No " + ("staged " if staged else "") + "changes.")
    if len(output) > MAX_DIFF_CHARS:
        return ToolResult(
            True,
            output[:MAX_DIFF_CHARS] + (
                f"\n\n… diff truncated at {MAX_DIFF_CHARS:,} characters. "
                f"Call git_diff again with stat_only=true, or with a path, to narrow it."
            ),
        )
    return ToolResult(True, output)


@tool(toolset=TOOLSET, capability="read", title="git log", preview_chars=1500)
def git_log(limit: int = 15, path: str = "", author: str = "") -> ToolResult:
    """Show recent commits: hash, author, date and subject.

    Args:
        limit: How many commits to show.
        path: Only commits touching this file or directory.
        author: Only commits by this author.
    """
    root, problem = _repo()
    if root is None:
        return ToolResult.error(problem)

    args = ["log", f"-{max(1, min(int(limit), 100))}", "--date=short",
            "--pretty=format:%h  %ad  %an  %s"]
    if author:
        args += ["--author", author]
    if path:
        args += ["--", path]

    ok, output = _git(args, root)
    if not ok:
        return ToolResult.error(output)
    return ToolResult(True, output or "No commits yet.")


@tool(toolset=TOOLSET, capability="read", title="git branches")
def git_branch() -> ToolResult:
    """List local branches and show which one is checked out."""
    root, problem = _repo()
    if root is None:
        return ToolResult.error(problem)
    ok, output = _git(["branch", "--list", "-vv"], root)
    if not ok:
        return ToolResult.error(output)
    return ToolResult(True, output or "No branches yet.")


@tool(toolset=TOOLSET, capability="read", title="git show {ref}", preview_chars=2000)
def git_show(ref: str = "HEAD") -> ToolResult:
    """Show one commit: its message and the changes it made.

    Args:
        ref: Commit hash, branch, tag or a reference like HEAD~2.
    """
    root, problem = _repo()
    if root is None:
        return ToolResult.error(problem)
    if ref.startswith("-"):
        return ToolResult.error("That does not look like a commit reference.")
    ok, output = _git(["show", "--stat", "--patch", ref], root)
    if not ok:
        return ToolResult.error(output)
    if len(output) > MAX_DIFF_CHARS:
        output = output[:MAX_DIFF_CHARS] + "\n\n… truncated."
    return ToolResult(True, output)


@tool(toolset=TOOLSET, capability="write", title="git add {paths}")
def git_stage(paths: str = ".") -> ToolResult:
    """Stage files for the next commit.

    Args:
        paths: Space-separated paths to stage; '.' stages everything changed.
    """
    root, problem = _repo()
    if root is None:
        return ToolResult.error(problem)
    targets = [p for p in paths.split() if not p.startswith("-")] or ["."]
    ok, output = _git(["add", "--", *targets], root)
    if not ok:
        return ToolResult.error(output)
    _, porcelain = _git(["diff", "--cached", "--name-only"], root)
    count = len([line for line in porcelain.splitlines() if line.strip()])
    return ToolResult(True, f"Staged {count} file(s).")


@tool(toolset=TOOLSET, capability="write", title="git commit")
def git_commit(message: str, stage_all: bool = False) -> ToolResult:
    """Record the staged changes as a commit.

    Check git_status and git_diff first so the message matches what is actually
    being committed. Write the message the way this repository writes them.

    Args:
        message: The commit message. Use the imperative mood, e.g. "add retry to the uploader".
        stage_all: Stage every modified tracked file first (does not add new files).
    """
    root, problem = _repo()
    if root is None:
        return ToolResult.error(problem)
    if not message.strip():
        return ToolResult.error("A commit needs a message.")

    args = ["commit", "-m", message.strip()]
    if stage_all:
        args.insert(1, "-a")
    ok, output = _git(args, root)
    if not ok:
        if "nothing to commit" in output.lower():
            return ToolResult.error("There is nothing staged to commit. Use git_stage first.")
        if "please tell me who you are" in output.lower():
            return ToolResult.error(
                "git does not know who you are in this repository. The user needs to set "
                "user.name and user.email before anything can be committed."
            )
        return ToolResult.error(output)
    return ToolResult(True, output)
