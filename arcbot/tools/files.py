"""Filesystem and code tools, sandboxed to the workspace.

Every path goes through :meth:`ToolContext.path`, so a tool can never reach
outside the roots the user granted.  Edits are guarded by a read-before-write
check, which is the single highest-value protection against a model confidently
overwriting a file it has not seen.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import re
import shutil
from pathlib import Path

from ..native.fileedit import FileEditor
from .registry import ToolResult, ctx, tool

TOOLSET = "files"

#: Never walked during search or tree listings.
IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    ".next", ".nuxt", ".parcel-cache", "target", ".gradle", ".idea", ".vscode",
    "coverage", ".arcbot", ".DS_Store", ".terraform", "vendor", ".cache",
}
TEXT_SUFFIXES = {
    ".txt", ".md", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".bat", ".c", ".h", ".cpp", ".hpp", ".cc", ".rs", ".go", ".java",
    ".kt", ".rb", ".php", ".swift", ".sql", ".html", ".css", ".scss", ".vue",
    ".svelte", ".lua", ".r", ".jl", ".ex", ".exs", ".dart", ".zig", ".nix",
    ".gitignore", ".env.example", ".dockerignore", "Makefile", "Dockerfile",
}
MAX_READ_BYTES = 2_000_000


def _is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_SUFFIXES:
        return True
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
        return b"\x00" not in chunk
    except OSError:
        return False


def _read_text(path: Path) -> str:
    data = path.read_bytes()[:MAX_READ_BYTES]
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


@tool(toolset=TOOLSET, capability="read", title="Read {path}", preview_chars=1500)
def read_file(path: str, start_line: int = 1, max_lines: int = 0) -> ToolResult:
    """Read a text file from the workspace, with line numbers.

    Always read a file before editing it. For a large file, read the region you
    care about with start_line/max_lines rather than pulling in the whole thing.

    Args:
        path: File path, relative to the workspace or absolute inside it.
        start_line: First line to return (1-based).
        max_lines: How many lines to return; 0 means to the end of the file.
    """
    context = ctx()
    target = context.path(path)
    if not target.exists():
        return ToolResult.error(f"No such file: {context.rel(target)}")
    if target.is_dir():
        return ToolResult.error(f"{context.rel(target)} is a directory — use list_files.")
    if not _is_probably_text(target):
        size = target.stat().st_size
        return ToolResult.error(
            f"{context.rel(target)} is a binary file ({_human_size(size)}); it cannot be read as text."
        )

    text = _read_text(target)
    context.mark_read(target)
    lines = text.splitlines()
    total = len(lines)
    start = max(1, start_line)
    end = total if max_lines <= 0 else min(total, start + max_lines - 1)
    if start > total:
        return ToolResult.error(f"{context.rel(target)} has only {total} lines.")

    width = len(str(end))
    body = "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1))
    header = f"{context.rel(target)} ({total} lines"
    header += f", showing {start}-{end})" if (start > 1 or end < total) else ")"
    return ToolResult(
        True,
        context.clip(f"{header}\n{body}"),
        {"path": context.rel(target), "lines": total, "shown": [start, end]},
    )


@tool(toolset=TOOLSET, capability="read", title="List {path}")
def list_files(path: str = ".", pattern: str = "*", recursive: bool = False) -> ToolResult:
    """List the contents of a directory.

    Args:
        path: Directory to list, relative to the workspace.
        pattern: Optional glob filter such as '*.py'.
        recursive: Walk subdirectories too.
    """
    context = ctx()
    target = context.path(path)
    if not target.exists():
        return ToolResult.error(f"No such directory: {context.rel(target)}")
    if not target.is_dir():
        return ToolResult.error(f"{context.rel(target)} is a file, not a directory.")

    rows: list[str] = []
    count = 0
    iterator = target.rglob(pattern) if recursive else target.glob(pattern)
    for entry in sorted(iterator, key=lambda p: (p.is_file(), str(p).lower())):
        if any(part in IGNORE_DIRS for part in entry.relative_to(target).parts):
            continue
        count += 1
        if count > 500:
            rows.append("… (truncated at 500 entries; narrow the pattern)")
            break
        rel = entry.relative_to(target)
        if entry.is_dir():
            rows.append(f"  {rel}/")
        else:
            try:
                rows.append(f"  {rel}  ({_human_size(entry.stat().st_size)})")
            except OSError:
                rows.append(f"  {rel}")

    if not rows:
        return ToolResult(True, f"{context.rel(target)} is empty (or nothing matched {pattern!r}).")
    return ToolResult(True, f"{context.rel(target)}:\n" + "\n".join(rows), {"count": count})


@tool(toolset=TOOLSET, capability="read", title="Tree of {path}")
def directory_tree(path: str = ".", max_depth: int = 3) -> ToolResult:
    """Show a directory tree, skipping build output and dependency folders.

    Args:
        path: Root directory.
        max_depth: How many levels deep to descend.
    """
    context = ctx()
    root = context.path(path)
    if not root.is_dir():
        return ToolResult.error(f"{context.rel(root)} is not a directory.")

    lines: list[str] = [f"{root.name}/"]
    budget = 400

    def walk(directory: Path, prefix: str, depth: int) -> None:
        nonlocal budget
        if depth > max_depth or budget <= 0:
            return
        try:
            entries = sorted(
                (e for e in directory.iterdir() if e.name not in IGNORE_DIRS),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except OSError:
            return
        for index, entry in enumerate(entries):
            if budget <= 0:
                lines.append(f"{prefix}└── … (truncated)")
                return
            budget -= 1
            last = index == len(entries) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir():
                walk(entry, prefix + ("    " if last else "│   "), depth + 1)

    walk(root, "", 1)
    return ToolResult(True, context.clip("\n".join(lines)))


@tool(toolset=TOOLSET, capability="read", title="Find files: {pattern}")
def find_files(pattern: str, path: str = ".") -> ToolResult:
    """Find files by name pattern (glob), e.g. '**/*.test.ts' or 'README*'.

    Args:
        pattern: Glob pattern to match against paths.
        path: Directory to search from.
    """
    context = ctx()
    root = context.path(path)
    matches: list[str] = []
    normalised = pattern if any(c in pattern for c in "*?[") else f"**/*{pattern}*"

    for entry in root.rglob("*"):
        if len(matches) >= 300:
            break
        if not entry.is_file():
            continue
        rel = entry.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        if fnmatch.fnmatch(str(rel), normalised) or fnmatch.fnmatch(entry.name, normalised):
            matches.append(str(rel))

    if not matches:
        return ToolResult(True, f"No files matching {pattern!r} under {context.rel(root)}.")
    return ToolResult(
        True,
        f"{len(matches)} match(es) for {pattern!r}:\n" + "\n".join(f"  {m}" for m in sorted(matches)),
        {"matches": matches},
    )


@tool(toolset=TOOLSET, capability="read", title="Search: {query}", preview_chars=1500)
def search_code(
    query: str,
    path: str = ".",
    file_pattern: str = "*",
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 60,
) -> ToolResult:
    """Search file contents and return matching lines with their locations.

    This is the fastest way to orient yourself in unfamiliar code — prefer it
    over reading many files speculatively.

    Args:
        query: Text or regular expression to find.
        path: Directory to search in.
        file_pattern: Glob limiting which files are searched, e.g. '*.py'.
        regex: Treat query as a regular expression.
        case_sensitive: Match case exactly.
        max_results: Stop after this many matching lines.
    """
    context = ctx()
    root = context.path(path)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        needle = re.compile(query if regex else re.escape(query), flags)
    except re.error as exc:
        return ToolResult.error(f"Invalid regular expression: {exc}")

    hits: list[str] = []
    files_scanned = 0
    files_with_hits = set()

    for entry in root.rglob(file_pattern):
        if len(hits) >= max_results:
            break
        if not entry.is_file():
            continue
        rel = entry.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        if not _is_probably_text(entry):
            continue
        try:
            content = _read_text(entry)
        except OSError:
            continue
        files_scanned += 1
        for lineno, line in enumerate(content.splitlines(), 1):
            if needle.search(line):
                files_with_hits.add(str(rel))
                hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    break

    if not hits:
        return ToolResult(
            True, f"No matches for {query!r} in {files_scanned} file(s) under {context.rel(root)}."
        )
    header = f"{len(hits)} match(es) across {len(files_with_hits)} file(s):"
    return ToolResult(
        True,
        context.clip(header + "\n" + "\n".join(hits)),
        {"matches": len(hits), "files": sorted(files_with_hits)},
    )


@tool(toolset=TOOLSET, capability="read", title="Overview of {path}", preview_chars=2000)
def project_overview(path: str = ".") -> ToolResult:
    """Summarise a project: layout, language, entry points and key config files.

    Call this once at the start of any task in an unfamiliar codebase.

    Args:
        path: Project root.
    """
    context = ctx()
    root = context.path(path)
    if not root.is_dir():
        return ToolResult.error(f"{context.rel(root)} is not a directory.")

    markers = {
        "pyproject.toml": "Python (PEP 621)", "requirements.txt": "Python",
        "setup.py": "Python", "package.json": "Node.js", "deno.json": "Deno",
        "Cargo.toml": "Rust", "go.mod": "Go", "pom.xml": "Java (Maven)",
        "build.gradle": "Java/Kotlin (Gradle)", "Gemfile": "Ruby",
        "composer.json": "PHP", "CMakeLists.txt": "C/C++", "Dockerfile": "Docker",
        "docker-compose.yml": "Docker Compose", "Makefile": "Make",
    }
    found = [f"{name} — {label}" for name, label in markers.items() if (root / name).exists()]

    counts: dict[str, int] = {}
    total_files = 0
    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        if any(part in IGNORE_DIRS for part in entry.relative_to(root).parts):
            continue
        total_files += 1
        suffix = entry.suffix.lower() or "(no ext)"
        counts[suffix] = counts.get(suffix, 0) + 1
        if total_files > 20_000:
            break

    top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    try:
        top_dirs = sorted(
            d.name + "/" for d in root.iterdir() if d.is_dir() and d.name not in IGNORE_DIRS
        )[:20]
    except OSError:
        top_dirs = []

    sections = [f"Project: {root.name}  ({root})", f"Files: {total_files:,}"]
    if found:
        sections.append("Detected stack:\n  " + "\n  ".join(found))
    if top:
        sections.append("File types:\n  " + "\n  ".join(f"{ext}: {n}" for ext, n in top))
    if top_dirs:
        sections.append("Top-level directories:\n  " + "  ".join(top_dirs))

    for doc in ("README.md", "README.rst", "README.txt", "AGENTS.md", "CLAUDE.md"):
        candidate = root / doc
        if candidate.exists():
            head = _read_text(candidate)[:1200]
            sections.append(f"--- {doc} (first 1200 chars) ---\n{head}")
            break

    return ToolResult(True, context.clip("\n\n".join(sections)))


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


@tool(toolset=TOOLSET, capability="write", title="Write {path}")
def write_file(path: str, content: str) -> ToolResult:
    """Create a file, or replace its entire contents.

    For an existing file, prefer edit_file — a targeted replacement is safer and
    easier to review than a full rewrite.

    Args:
        path: File to write, relative to the workspace.
        content: The complete new file contents.
    """
    context = ctx()
    target = context.path(path)
    existed = target.exists()
    if existed:
        stale = context.staleness(target)
        if stale:
            return ToolResult.error(stale)

    target.parent.mkdir(parents=True, exist_ok=True)
    before = _read_text(target) if existed else ""
    target.write_text(content, encoding="utf-8")
    context.mark_read(target)

    diff = _diff(before, content, context.rel(target)) if existed else ""
    verb = "Updated" if existed else "Created"
    lines = content.count("\n") + 1
    return ToolResult(
        True,
        f"{verb} {context.rel(target)} ({lines} lines).",
        {"path": context.rel(target), "created": not existed, "diff": diff},
    )


@tool(toolset=TOOLSET, capability="write", title="Edit {path}")
def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
    """Replace an exact snippet of text in a file.

    old_text must match the file byte-for-byte, including indentation, and must
    be unique unless replace_all is set. Include a few surrounding lines to make
    it unambiguous. Read the file first.

    Args:
        path: File to edit.
        old_text: Exact text to find.
        new_text: Text to replace it with.
        replace_all: Replace every occurrence instead of requiring a unique match.
    """
    context = ctx()
    target = context.path(path)
    if not target.exists():
        return ToolResult.error(f"No such file: {context.rel(target)}. Use write_file to create it.")
    stale = context.staleness(target)
    if stale:
        return ToolResult.error(stale)
    if old_text == new_text:
        return ToolResult.error("old_text and new_text are identical — nothing to do.")

    content = _read_text(target)
    occurrences = content.count(old_text)
    if occurrences == 0:
        hint = _closest_hint(content, old_text)
        return ToolResult.error(
            f"That exact text is not in {context.rel(target)}.{hint} "
            f"Re-read the file and copy the snippet exactly, including whitespace."
        )
    if occurrences > 1 and not replace_all:
        return ToolResult.error(
            f"Found {occurrences} occurrences in {context.rel(target)}. Add surrounding "
            f"lines to make old_text unique, or set replace_all=true."
        )

    updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
    target.write_text(updated, encoding="utf-8")
    context.mark_read(target)
    return ToolResult(
        True,
        f"Edited {context.rel(target)} ({occurrences if replace_all else 1} replacement(s)).",
        {"path": context.rel(target), "diff": _diff(content, updated, context.rel(target))},
    )


@tool(toolset=TOOLSET, capability="write", title="Insert into {path}")
def insert_lines(path: str, after_line: int, content: str) -> ToolResult:
    """Insert text after a given line number (0 inserts at the top of the file).

    Args:
        path: File to modify.
        after_line: Insert after this 1-based line; 0 means the very beginning.
        content: Text to insert.
    """
    context = ctx()
    target = context.path(path)
    if not target.exists():
        return ToolResult.error(f"No such file: {context.rel(target)}")
    stale = context.staleness(target)
    if stale:
        return ToolResult.error(stale)
    before = _read_text(target)
    # ``after_line=0`` means "before line 1"; anything else inserts after that line.
    if after_line <= 0:
        result = FileEditor.insert_at_line(target, 1, content, mode="before")
    else:
        result = FileEditor.insert_at_line(target, after_line, content, mode="after")
    if not result.get("success"):
        return ToolResult.error(result.get("message", "Insert failed."))
    context.mark_read(target)
    return ToolResult(
        True,
        f"Inserted {content.count(chr(10)) + 1} line(s) into {context.rel(target)}.",
        {"diff": _diff(before, _read_text(target), context.rel(target))},
    )


@tool(toolset=TOOLSET, capability="write", title="Append to {path}")
def append_to_file(path: str, content: str) -> ToolResult:
    """Append text to the end of a file, creating it if needed.

    Args:
        path: File to append to.
        content: Text to append.
    """
    context = ctx()
    target = context.path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(content if content.startswith("\n") or not target.stat().st_size else "\n" + content)
    context.mark_read(target)
    return ToolResult(True, f"Appended {len(content)} characters to {context.rel(target)}.")


@tool(toolset=TOOLSET, capability="write", title="Create folder {path}")
def create_directory(path: str) -> ToolResult:
    """Create a directory, including any missing parents.

    Args:
        path: Directory to create.
    """
    context = ctx()
    target = context.path(path)
    target.mkdir(parents=True, exist_ok=True)
    return ToolResult(True, f"Directory ready: {context.rel(target)}")


@tool(toolset=TOOLSET, capability="write", title="Copy {source} → {destination}")
def copy_path(source: str, destination: str) -> ToolResult:
    """Copy a file or directory inside the workspace.

    Args:
        source: Path to copy from.
        destination: Path to copy to.
    """
    context = ctx()
    src, dst = context.path(source), context.path(destination)
    if not src.exists():
        return ToolResult.error(f"No such path: {context.rel(src)}")
    if dst.exists():
        return ToolResult.error(f"{context.rel(dst)} already exists; delete or rename it first.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return ToolResult(True, f"Copied {context.rel(src)} → {context.rel(dst)}")


@tool(toolset=TOOLSET, capability="write", title="Move {source} → {destination}")
def move_path(source: str, destination: str) -> ToolResult:
    """Move or rename a file or directory inside the workspace.

    Args:
        source: Path to move.
        destination: New path.
    """
    context = ctx()
    src, dst = context.path(source), context.path(destination)
    if not src.exists():
        return ToolResult.error(f"No such path: {context.rel(src)}")
    if dst.exists():
        return ToolResult.error(f"{context.rel(dst)} already exists.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    context.seen_files.pop(str(src), None)
    return ToolResult(True, f"Moved {context.rel(src)} → {context.rel(dst)}")


@tool(toolset=TOOLSET, capability="destructive", title="Delete {path}")
def delete_path(path: str, recursive: bool = False) -> ToolResult:
    """Delete a file, or a directory when recursive is set.

    Args:
        path: Path to delete.
        recursive: Required to delete a non-empty directory.
    """
    context = ctx()
    target = context.path(path)
    if not target.exists():
        return ToolResult.error(f"No such path: {context.rel(target)}")
    if target.resolve() == context.workspace:
        return ToolResult.error("Refusing to delete the workspace root.")

    if target.is_dir():
        if not recursive and any(target.iterdir()):
            return ToolResult.error(
                f"{context.rel(target)} is not empty. Set recursive=true to delete it and its contents."
            )
        shutil.rmtree(target) if recursive else target.rmdir()
    else:
        target.unlink()
    context.seen_files.pop(str(target), None)
    return ToolResult(True, f"Deleted {context.rel(target)}")


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #


@tool(toolset=TOOLSET, capability="read", title="Diff {left} ↔ {right}", preview_chars=1500)
def diff_files(left: str, right: str) -> ToolResult:
    """Show a unified diff between two files.

    Args:
        left: First file.
        right: Second file.
    """
    context = ctx()
    a, b = context.path(left), context.path(right)
    for path in (a, b):
        if not path.is_file():
            return ToolResult.error(f"No such file: {context.rel(path)}")
    diff = _diff(_read_text(a), _read_text(b), context.rel(a), context.rel(b))
    return ToolResult(True, context.clip(diff or "The files are identical."))


@tool(toolset=TOOLSET, capability="read", title="Info for {path}")
def file_info(path: str) -> ToolResult:
    """Report size, timestamps, permissions and a content hash for a path.

    Args:
        path: File or directory to inspect.
    """
    import datetime as _dt

    context = ctx()
    target = context.path(path)
    if not target.exists():
        return ToolResult.error(f"No such path: {context.rel(target)}")
    stat = target.stat()
    rows = [
        f"Path: {target}",
        f"Type: {'directory' if target.is_dir() else 'file'}",
        f"Size: {_human_size(stat.st_size)} ({stat.st_size:,} bytes)",
        f"Modified: {_dt.datetime.fromtimestamp(stat.st_mtime):%Y-%m-%d %H:%M:%S}",
        f"Permissions: {oct(stat.st_mode)[-3:]}",
    ]
    if target.is_file() and stat.st_size <= 50_000_000:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        rows.append(f"SHA-256: {digest}")
        if _is_probably_text(target):
            rows.append(f"Lines: {_read_text(target).count(chr(10)) + 1:,}")
    return ToolResult(True, "\n".join(rows))


def _diff(before: str, after: str, name: str, name_b: str | None = None) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name_b or name}",
        n=3,
    )
    return "".join(diff)


def _closest_hint(content: str, needle: str) -> str:
    """Point at the nearest line when an exact match fails — saves a retry."""
    first_line = needle.strip().splitlines()[0] if needle.strip() else ""
    if not first_line or len(first_line) < 4:
        return ""
    candidates = difflib.get_close_matches(first_line, content.splitlines(), n=1, cutoff=0.7)
    if not candidates:
        return ""
    return f" The closest line in the file is:\n  {candidates[0].strip()[:200]}\n"


# --------------------------------------------------------------------------- #
# Archives
# --------------------------------------------------------------------------- #

#: Formats ``shutil`` can create, mapped from the extension a user would type.
_ARCHIVE_FORMATS = {
    ".zip": "zip", ".tar": "tar", ".tar.gz": "gztar", ".tgz": "gztar",
    ".tar.bz2": "bztar", ".tbz2": "bztar", ".tar.xz": "xztar", ".txz": "xztar",
}


def _archive_format(name: str) -> str | None:
    lowered = name.lower()
    for suffix, fmt in sorted(_ARCHIVE_FORMATS.items(), key=lambda kv: -len(kv[0])):
        if lowered.endswith(suffix):
            return fmt
    return None


@tool(toolset=TOOLSET, capability="write", title="Archive → {destination}")
def create_archive(source: str, destination: str) -> ToolResult:
    """Compress a file or folder into a .zip or .tar.gz archive.

    Args:
        source: File or directory to compress.
        destination: Archive path, ending in .zip, .tar.gz, .tar.bz2 or .tar.xz.
    """
    context = ctx()
    src = context.path(source)
    dst = context.path(destination)
    if not src.exists():
        return ToolResult.error(f"No such path: {context.rel(src)}")
    if dst.exists():
        return ToolResult.error(f"{context.rel(dst)} already exists.")

    fmt = _archive_format(dst.name)
    if fmt is None:
        return ToolResult.error(
            f"Cannot tell the format from {dst.name!r}. "
            f"Use one of: {', '.join(sorted(_ARCHIVE_FORMATS))}"
        )
    # make_archive appends its own suffix, so build from the stem.
    suffix = next(s for s in sorted(_ARCHIVE_FORMATS, key=len, reverse=True)
                  if dst.name.lower().endswith(s))
    base = dst.parent / dst.name[: -len(suffix)]
    dst.parent.mkdir(parents=True, exist_ok=True)

    root = src if src.is_dir() else src.parent
    inner = None if src.is_dir() else src.name
    try:
        produced = Path(shutil.make_archive(str(base), fmt, root_dir=str(root), base_dir=inner))
    except (OSError, ValueError) as exc:
        return ToolResult.error(f"Could not create the archive: {exc}")
    if produced != dst:
        produced.rename(dst)
    return ToolResult(
        True,
        f"Created {context.rel(dst)} ({_human_size(dst.stat().st_size)}).",
        {"path": context.rel(dst), "bytes": dst.stat().st_size},
    )


@tool(toolset=TOOLSET, capability="write", title="Extract {archive}")
def extract_archive(archive: str, destination: str = ".") -> ToolResult:
    """Extract a .zip or .tar archive into the workspace.

    Args:
        archive: Archive file to extract.
        destination: Directory to extract into.
    """
    context = ctx()
    src = context.path(archive)
    dst = context.path(destination)
    if not src.is_file():
        return ToolResult.error(f"No such archive: {context.rel(src)}")
    dst.mkdir(parents=True, exist_ok=True)

    # An archive can carry ../ entries that escape the destination, so every
    # member is checked before anything is written.
    try:
        members = _archive_members(src)
    except (OSError, ValueError) as exc:
        return ToolResult.error(f"Could not read the archive: {exc}")

    resolved_dst = dst.resolve()
    for member in members:
        target = (dst / member).resolve()
        if target != resolved_dst and resolved_dst not in target.parents:
            return ToolResult.error(
                f"Refusing to extract: the archive contains an entry that escapes the "
                f"destination ({member!r})."
            )
    try:
        shutil.unpack_archive(str(src), str(dst))
    except (OSError, ValueError, shutil.ReadError) as exc:
        return ToolResult.error(f"Could not extract: {exc}")
    return ToolResult(
        True,
        f"Extracted {len(members)} entr{'y' if len(members) == 1 else 'ies'} "
        f"into {context.rel(dst)}.",
        {"path": context.rel(dst), "entries": len(members)},
    )


def _archive_members(path: Path) -> list[str]:
    import tarfile
    import zipfile

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as tf:
            return tf.getnames()
    raise ValueError("unsupported archive format (expected zip or tar)")


@tool(toolset=TOOLSET, capability="read", title="Inspect data in {path}", preview_chars=1800)
def read_data(path: str, rows: int = 10) -> ToolResult:
    """Summarise a CSV, JSON or JSONL file instead of dumping the whole thing.

    Use this for data files rather than read_file: it reports the shape, the
    columns or keys, and a sample — which is what you need to reason about the
    data, without spending the context window on it.

    Args:
        path: The data file to inspect.
        rows: How many sample rows to show.
    """
    import csv
    import io
    import json

    context = ctx()
    target = context.path(path)
    if not target.is_file():
        return ToolResult.error(f"No such file: {context.rel(target)}")

    sample = max(1, min(int(rows or 10), 50))
    suffix = target.suffix.lower()
    try:
        text = _read_text(target)
    except OSError as exc:
        return ToolResult.error(f"Could not read the file: {exc}")

    if suffix in (".json",):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return ToolResult.error(f"Not valid JSON: {exc}")
        return ToolResult(True, _describe_json(data, sample, context.rel(target)))

    if suffix in (".jsonl", ".ndjson"):
        records, bad = [], 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
        header = f"{context.rel(target)} — {len(records):,} records"
        if bad:
            header += f" ({bad} unparseable lines)"
        preview = json.dumps(records[:sample], indent=2, default=str)[:4000]
        keys = sorted({k for r in records[:200] if isinstance(r, dict) for k in r})
        body = f"Keys: {', '.join(keys[:40]) or '(not objects)'}\n\nFirst {sample}:\n{preview}"
        return ToolResult(True, f"{header}\n{body}")

    # Anything else is treated as delimited text.
    try:
        dialect = csv.Sniffer().sniff(text[:8000])
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    all_rows = list(reader)
    if not all_rows:
        return ToolResult(True, f"{context.rel(target)} is empty.")

    header_row = all_rows[0]
    data_rows = all_rows[1:]
    widths = [max(len(str(r[i])) if i < len(r) else 0
                  for r in all_rows[: sample + 1]) for i in range(len(header_row))]
    def line(row):
        return "  ".join(str(row[i] if i < len(row) else "").ljust(min(widths[i], 24))[:24]
                         for i in range(len(header_row)))

    body = [
        f"{context.rel(target)} — {len(data_rows):,} rows × {len(header_row)} columns",
        f"Columns: {', '.join(header_row[:40])}",
        "",
        line(header_row),
        "-" * min(sum(min(w, 24) + 2 for w in widths), 120),
    ]
    body += [line(row) for row in data_rows[:sample]]
    if len(data_rows) > sample:
        body.append(f"… and {len(data_rows) - sample:,} more rows")
    return ToolResult(True, context.clip("\n".join(body)),
                      {"rows": len(data_rows), "columns": header_row})


def _describe_json(data, sample: int, name: str) -> str:
    import json

    if isinstance(data, list):
        keys = sorted({k for item in data[:200] if isinstance(item, dict) for k in item})
        head = json.dumps(data[:sample], indent=2, default=str)[:4000]
        return (f"{name} — array of {len(data):,} items\n"
                f"Keys: {', '.join(keys[:40]) or '(not objects)'}\n\nFirst {sample}:\n{head}")
    if isinstance(data, dict):
        preview = json.dumps(data, indent=2, default=str)
        summary = f"{name} — object with {len(data)} keys\nKeys: {', '.join(list(data)[:40])}"
        return f"{summary}\n\n{preview[:4000]}" + ("\n… truncated." if len(preview) > 4000 else "")
    return f"{name} — a single {type(data).__name__} value: {json.dumps(data, default=str)[:1000]}"
