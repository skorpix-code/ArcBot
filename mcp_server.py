import ast
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ddgs import DDGS
from mcp.server.fastmcp import FastMCP

# --- CONFIGURATION ---
# The strict directory the LLM is allowed to access
# Default= "/home/skorp/Programming/LLM_Coder_Dir"
BASE_DIR = Path("/home/skorp/Programming/LLM_Coder_Dir")

# Create the MCP Server instance
mcp = FastMCP("LLM_Coder_FileSystem")


# --- SECURITY HELPER ---
def get_secure_path(user_path: str) -> Path:
    """
    Resolves a path and ensures it is contained within BASE_DIR.
    Prevents directory traversal attacks (e.g., ../../etc/passwd).
    """
    # Handle absolute paths that start with the base dir, or relative paths
    if os.path.isabs(user_path):
        target_path = Path(user_path).resolve()
    else:
        target_path = (BASE_DIR / user_path).resolve()

    # Security check: Ensure the resolved path starts with the BASE_DIR
    if not str(target_path).startswith(str(BASE_DIR.resolve())):
        raise ValueError(
            f"Security Error: Access denied to {user_path}. You are restricted to {BASE_DIR}"
        )

    return target_path


def log(message: str):
    """
    Logs messages to stderr.
    CRITICAL: In stdio mode, stdout is used for JSON-RPC communication.
    Debug messages MUST go to stderr to avoid breaking the protocol.
    """
    print(f"[SERVER]: {message}", file=sys.stderr)


# --- ORIGINAL TOOLS ---


@mcp.tool()
def list_content(sub_path: str = ".") -> str:
    """
    Lists the contents of a directory.
    Args:
        sub_path: Relative path to list (defaults to root of allowable dir).
    """
    try:
        target = get_secure_path(sub_path)
        if not target.exists():
            return f"Error: Directory {sub_path} does not exist."

        log(f"Listing contents of {target}")

        items = []
        for item in target.iterdir():
            prefix = "[DIR]" if item.is_dir() else "[FILE]"
            items.append(f"{prefix} {item.name}")

        if not items:
            return "(Directory is empty)"

        return "\n".join(sorted(items))
    except Exception as e:
        log(f"Error listing content: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def get_directory_tree(sub_path: str = ".") -> str:
    """
    Returns a visual, recursive tree structure of a directory and its descendants.

    Use this tool to:
    1. Understand the overall project architecture at a glance.
    2. See where files are located relative to one another.
    3. Explore nested folder structures without multiple 'list_content' calls.

    Args:
        sub_path: The relative path to start the tree from (default is root ".").

    Returns:
        A string representation of the directory tree.
        Note: Automatically hides common noise directories like .git, __pycache__, node_modules.
    """
    try:
        target = get_secure_path(sub_path)

        if not target.exists():
            return f"Error: Directory '{sub_path}' does not exist."
        if not target.is_dir():
            return f"Error: '{sub_path}' is a file, not a directory. Use read_file to view it."

        # Define directories to ignore to prevent context flooding
        IGNORE_DIRS = {
            ".git",
            "__pycache__",
            "node_modules",
            "venv",
            ".env",
            ".vscode",
            ".idea",
            "dist",
            "build",
            ".DS_Store",
            "coverage",
        }

        tree_lines = []

        def build_tree(directory: Path, prefix: str = ""):
            # 1. Get all items
            try:
                items = list(directory.iterdir())
            except PermissionError:
                tree_lines.append(f"{prefix}[ACCESS DENIED]")
                return

            # 2. Filter out ignored items
            filtered_items = [item for item in items if item.name not in IGNORE_DIRS]

            # 3. Sort: Directories first, then Files. Both alphabetical.
            # Lambda logic: (is_file? (0=Dir, 1=File), lowercase_name)
            sorted_items = sorted(
                filtered_items, key=lambda x: (x.is_file(), x.name.lower())
            )

            count = len(sorted_items)

            for i, item in enumerate(sorted_items):
                # Determine if this is the last item in the current branch
                is_last = i == count - 1

                connector = "└── " if is_last else "├── "

                # Visual marker for directories vs files
                type_marker = "/" if item.is_dir() else ""

                line = f"{prefix}{connector}{item.name}{type_marker}"
                tree_lines.append(line)

                # 4. Recurse if it is a directory
                if item.is_dir():
                    # Calculate new prefix for children
                    # If we are last, children don't need the vertical bar │
                    extension = "    " if is_last else "│   "
                    build_tree(item, prefix + extension)

        # Start the tree build
        log(f"Building directory tree for: {target.name}")
        tree_lines.append(f"{target.name}/")  # Add the root folder name at top
        build_tree(target)

        if len(tree_lines) == 1:
            return f"{target.name}/ (Empty Directory)"

        return "\n".join(tree_lines)

    except Exception as e:
        log(f"Error building tree: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def create_dir(dir_path: str) -> str:
    """
    Creates a new subdirectory.
    Args:
        dir_path: The path of the directory to create.
    """
    try:
        target = get_secure_path(dir_path)
        if target.exists():
            return f"Directory {dir_path} already exists."

        target.mkdir(parents=True, exist_ok=True)
        log(f"Directory created at {target}")
        return f"Successfully created directory: {dir_path}"
    except Exception as e:
        log(f"Error creating directory: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def write_file(file_path: str, content: str) -> str:
    """
    Writes content to a file.
    IMPORTANT: This tool automatically creates any missing parent directories.

    Args:
        file_path: The specific path where the file should be saved (e.g., 'reports/notes.txt').
        content: The full string content to write to the file.
    """
    try:
        target = get_secure_path(file_path)

        # 1. AUTO-CREATE PARENT DIRECTORIES
        # This fixes the issue where the LLM fails because the folder doesn't exist yet.
        if not target.parent.exists():
            log(f"Parent directory missing. Creating: {target.parent}")
            target.parent.mkdir(parents=True, exist_ok=True)

        # 2. Write the file
        target.write_text(content, encoding="utf-8")
        log(f"File {target.name} written at {target}")

        return f"Successfully wrote to {file_path}"
    except Exception as e:
        log(f"Error writing file: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def append_to_file(path: str, content: str) -> str:
    """
    Appends text to the END of an existing file.

    Args:
        path: The relative path to the file (e.g., 'voila/report.txt').
        content: The text to append.
    """
    try:
        # We now use 'path' here to match the function argument
        target = get_secure_path(path)

        if not target.exists():
            return (
                f"Error: File {path} does not exist. Use write_file to create it first."
            )

        with open(target, "a", encoding="utf-8") as f:
            f.write("\n" + content)

        log(f"Appended to file: {target}")
        return f"Success: Appended content to {path}"
    except Exception as e:
        return str(e)


@mcp.tool()
def read_file(file_path: str) -> str:
    """
    Reads the content of a file.
    Args:
        file_path: The path to the file.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: File {file_path} not found."

        if not target.is_file():
            return f"Error: {file_path} is not a file."

        content = target.read_text(encoding="utf-8")
        log(f"Read content from {target}")
        return content
    except Exception as e:
        log(f"Error reading file: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def edit_file(file_path: str, start_line: int, end_line: int, new_content: str) -> str:
    """
    Edits a file as required.
    Replaces a specific range of lines in a file with new content.
    Lines are 1-indexed.

    Args:
        file_path: Path to the file.
        start_line: The first line number to replace (1-based).
        end_line: The last line number to replace (1-based).
        new_content: The new text to insert.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: File {file_path} not found."

        lines = target.read_text(encoding="utf-8").splitlines()
        total_lines = len(lines)

        if start_line < 1 or start_line > total_lines:
            return f"Error: start_line {start_line} is out of bounds (File has {total_lines} lines)."

        # Adjust for 0-based indexing
        start_idx = start_line - 1
        end_idx = end_line

        new_lines = new_content.splitlines()

        # Reconstruct file content
        final_lines = lines[:start_idx]
        final_lines.extend(new_lines)
        final_lines.extend(lines[end_idx:])

        target.write_text("\n".join(final_lines), encoding="utf-8")
        log(f"Edited file {target} lines {start_line}-{end_line}")
        return f"Successfully edited {file_path}"
    except Exception as e:
        log(f"Error editing file: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def copy_item(source_path: str, destination_path: str) -> str:
    """
    Copies a file or directory to a new location.

    Args:
        source_path: The path of the item to copy.
        destination_path: The new path. If copying a file, this can be a new directory or a full target path.
    """
    try:
        src = get_secure_path(source_path)
        dest = get_secure_path(destination_path)

        if not src.exists():
            return f"Error: Source {source_path} does not exist."

        if dest.exists():
            return f"Error: Destination {destination_path} already exists."

        # Ensure parent of destination exists
        if not dest.parent.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            # Copy Directory
            shutil.copytree(src, dest)
            log(f"Copied directory {src} to {dest}")
            return f"Successfully copied directory from {source_path} to {destination_path}"
        else:
            # Copy File (preserves metadata)
            shutil.copy2(src, dest)
            log(f"Copied file {src} to {dest}")
            return f"Successfully copied file from {source_path} to {destination_path}"

    except Exception as e:
        log(f"Error copying item: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def rename_item(path: str, new_name: str) -> str:
    """
    Renames a file or directory while keeping it in the same parent folder.

    Args:
        path: The relative path to the current item (e.g., 'data/old_name.txt').
        new_name: Just the new name (e.g., 'new_name.txt'). Do not include the full path.
    """
    try:
        target = get_secure_path(path)

        if not target.exists():
            return f"Error: {path} not found."

        # Construct the new path within the SAME parent directory
        new_target = target.parent / new_name

        # Security check: Ensure the new name doesn't resolve outside (e.g., new_name="../evil")
        # We re-verify the full path using get_secure_path logic implicitly by checking parents
        if not str(new_target.resolve()).startswith(str(BASE_DIR.resolve())):
            return "Error: Invalid new name causes path traversal."

        if new_target.exists():
            return f"Error: A file/folder with the name '{new_name}' already exists in this directory."

        target.rename(new_target)
        log(f"Renamed {target.name} to {new_name}")
        return f"Successfully renamed {target.name} to {new_name}"

    except Exception as e:
        log(f"Error renaming item: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def get_metadata(path: str) -> str:
    """
    Retrieves detailed metadata about a file or directory.
    Returns: Size, Created/Modified timestamps, and Permissions.
    """
    try:
        target = get_secure_path(path)
        if not target.exists():
            return f"Error: {path} not found."

        stat_info = target.stat()

        # Helper to format timestamps
        def fmt_time(ts):
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        # Helper to format size
        def fmt_size(size_bytes):
            for unit in ["B", "KB", "MB", "GB"]:
                if size_bytes < 1024.0:
                    return f"{size_bytes:.2f} {unit}"
                size_bytes /= 1024.0
            return f"{size_bytes:.2f} TB"

        # Determine type
        item_type = "Directory" if target.is_dir() else "File"

        # Get permissions (human readable-ish)
        mode = stat.filemode(stat_info.st_mode)

        info = [
            f"--- Metadata for: {target.name} ---",
            f"Path: {path}",
            f"Type: {item_type}",
            f"Size: {fmt_size(stat_info.st_size)}",
            f"Created: {fmt_time(stat_info.st_ctime)}",
            f"Modified: {fmt_time(stat_info.st_mtime)}",
            f"Permissions: {mode}",
            f"Absolute Path: {target}",
        ]

        return "\n".join(info)

    except Exception as e:
        log(f"Error getting metadata: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def open_in_desktop(path: str = ".") -> str:
    """
    Opens the file or directory using the operating system's default application.
    - If a directory: Opens it in the File Explorer / Finder.
    - If a file: Opens it in the default viewer (e.g., Preview, Notepad, Browser).

    Args:
        path: The path to open (default is the current root).
    """
    try:
        target = get_secure_path(path)
        if not target.exists():
            return f"Error: {path} not found."

        # Detect OS and select the correct opener command
        system_platform = platform.system()

        if system_platform == "Darwin":  # macOS
            cmd = ["open", str(target)]
        elif system_platform == "Windows":  # Windows
            cmd = ["start", str(target)]
        elif system_platform == "Linux":  # Linux
            cmd = ["xdg-open", str(target)]
        else:
            return f"Error: Unsupported OS for this action: {system_platform}"

        # Execute command
        # For Windows 'start', we need shell=True
        use_shell = system_platform == "Windows"

        subprocess.run(cmd, check=True, shell=use_shell)

        log(f"Opened {target} in desktop environment")
        return f"Successfully opened {path} in your default desktop viewer."

    except FileNotFoundError:
        return (
            "Error: System command for opening files (xdg-open/open/start) not found."
        )
    except Exception as e:
        log(f"Error opening item: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def open_file_with_editor(file_path: str) -> str:
    """
    Opens the specific file in VS Code along with the base project folder.
    Args:
        file_path: The specific file to focus on.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: File {file_path} does not exist."

        # Command: code <folder_path> <file_path>
        cmd = ["code", str(BASE_DIR), str(target)]

        subprocess.run(cmd, check=True)
        log(f"Opened VS Code for {target}")
        return f"VS Code opened successfully for {file_path}"
    except FileNotFoundError:
        return "Error: 'code' command not found. Is VS Code installed and in your PATH?"
    except Exception as e:
        log(f"Error opening editor: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def delete_file(file_path: str) -> str:
    """
    Permanently deletes a file.
    Args:
        file_path: The path to the file to delete.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: File {file_path} not found."

        if not target.is_file():
            return f"Error: {file_path} is a directory. Use delete_dir instead."

        target.unlink()
        log(f"Deleted file {target}")
        return f"Successfully deleted file: {file_path}"
    except Exception as e:
        log(f"Error deleting file: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def delete_dir(dir_path: str) -> str:
    """
    Permanently deletes a directory and all its contents (Recursive delete).
    Args:
        dir_path: The path to the directory to delete.
    """
    try:
        target = get_secure_path(dir_path)
        if not target.exists():
            return f"Error: Directory {dir_path} not found."

        if not target.is_dir():
            return f"Error: {dir_path} is not a directory."

        # Prevent deleting the root base dir accidentally via the tool
        if target == BASE_DIR:
            return "Error: You cannot delete the root Project directory itself."

        shutil.rmtree(target)
        log(f"Deleted directory {target}")
        return f"Successfully deleted directory: {dir_path}"
    except Exception as e:
        log(f"Error deleting directory: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def move_item(source_path: str, destination_path: str) -> str:
    """
    Moves or renames a file or directory.
    Args:
        source_path: The current path of the item.
        destination_path: The new path (must be within the allowed directory).
    """
    try:
        # Secure both paths
        src = get_secure_path(source_path)
        dest = get_secure_path(destination_path)

        if not src.exists():
            return f"Error: Source {source_path} does not exist."

        if dest.exists():
            return f"Error: Destination {destination_path} already exists."

        # Ensure destination parent exists
        if not dest.parent.exists():
            return f"Error: Parent directory of destination {destination_path} does not exist."

        shutil.move(str(src), str(dest))
        log(f"Moved {src} to {dest}")
        return f"Successfully moved {source_path} to {destination_path}"
    except Exception as e:
        log(f"Error moving item: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def search_files(pattern: str, sub_path: str = ".") -> str:
    """
    Searches for a regex pattern or text in files within the directory.
    Mimics 'grep'. Useful for finding function definitions or usages.
    """
    try:
        target = get_secure_path(sub_path)
        if not target.exists():
            return f"Error: Path {sub_path} does not exist."

        # Using grep is fast and efficient
        # -r: recursive, -n: line number, -I: ignore binary files
        # We explicitly exclude .git directory
        cmd = ["grep", "-rnI", "--exclude-dir=.git", pattern, str(target)]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,  # Security timeout
        )

        if not result.stdout:
            return f"No matches found for pattern: '{pattern}'"

        # Limit output to prevent context overflow (e.g., first 2000 chars)
        output = result.stdout
        if len(output) > 2000:
            output = output[:2000] + "\n... (Output truncated)"

        return output
    except Exception as e:
        log(f"Error searching files: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
def read_file_skeleton(file_path: str) -> str:
    """
    Returns the skeleton (classes, functions, docstrings) of a Python file.
    Hides the implementation details to save context window.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: File {file_path} not found."

        source = target.read_text(encoding="utf-8")
        tree = ast.parse(source)

        lines = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Get function signature
                args = [a.arg for a in node.args.args]
                lines.append(f"def {node.name}({', '.join(args)}):")
                if ast.get_docstring(node):
                    lines.append(f'    """{ast.get_docstring(node)}"""')
                lines.append("    # ... implementation hidden ...\n")

            elif isinstance(node, ast.ClassDef):
                lines.append(f"class {node.name}:")
                if ast.get_docstring(node):
                    lines.append(f'    """{ast.get_docstring(node)}"""')

                # Get methods inside class
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = [a.arg for a in item.args.args]
                        lines.append(f"    def {item.name}({', '.join(args)}):")
                        lines.append("        # ... implementation ...")
                lines.append("")

        return "\n".join(lines) if lines else "No definitions found (script or empty)."

    except Exception as e:
        return f"Error parsing file: {e}"


@mcp.tool()
def read_file_segment(file_path: str, start_line: int, end_line: int) -> str:
    """
    Reads a specific range of lines from a file. 1-indexed.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: {file_path} not found."

        lines = target.read_text(encoding="utf-8").splitlines()

        # Clamp values
        start = max(1, start_line)
        end = min(len(lines), end_line)

        segment = lines[start - 1 : end]

        # Add line numbers to helping debugging
        numbered_segment = []
        for i, line in enumerate(segment):
            numbered_segment.append(f"{start + i} | {line}")

        return "\n".join(numbered_segment)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """
    Searches the internet for a given query and returns relevant results.
    Args:
        query: The search term or question.
        max_results: Maximum number of results to return (default 5, max 10).
    """
    try:
        log(f"Searching web for: {query}")
        results = []

        # specific backend "api" is often more stable for automation than html
        with DDGS() as ddgs:
            # ddgs.text returns a generator of results
            search_gen = ddgs.text(query, max_results=max_results)

            for r in search_gen:
                results.append(r)

        if not results:
            return "No results found."

        # Format the output to be highly readable for the LLM
        formatted_output = [f"--- Search Results for '{query}' ---"]
        for i, res in enumerate(results, 1):
            title = res.get("title", "No Title")
            href = res.get("href", "No URL")
            body = res.get("body", "No Description")

            entry = f"Result {i}:\nTitle: {title}\nURL: {href}\nSnippet: {body}\n"
            formatted_output.append(entry)

        return "\n".join(formatted_output)

    except Exception as e:
        log(f"Error during web search: {e}")
        return f"Error searching the web: {str(e)}"


# -------- TAB MANAGEMENT HELPER FUNCTIONS --------


def _get_linux_display_server():
    """
    Detects the Linux display server/compositor.
    Returns: 'hyprland', 'sway', 'x11', or 'wayland_generic'
    """
    # Check specific environment variables first
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if os.environ.get("SWAYSOCK"):
        return "sway"

    # Check session type
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "x11":
        return "x11"
    elif session_type == "wayland":
        # Fallback if specific compositor env vars aren't found
        return "wayland_generic"

    return "unknown"


def _run_cmd(cmd_list):
    """Runs a subprocess command and returns stdout string."""
    try:
        res = subprocess.run(cmd_list, capture_output=True, text=True)
        return res.stdout.strip()
    except Exception as e:
        return ""


# -------- TAB MANAGEMENT TOOLS --------


@mcp.tool()
def list_open_windows() -> str:
    """
    Lists all visible, top-level open windows.
    Supports Windows, macOS, Linux (X11, Hyprland, Sway).
    """
    system = platform.system()

    try:
        if system == "Linux":
            server = _get_linux_display_server()

            if server == "hyprland":
                # Hyprland: Use hyprctl clients -j
                json_out = _run_cmd(["hyprctl", "clients", "-j"])
                if not json_out:
                    return "No windows found or hyprctl failed."

                try:
                    clients = json.loads(json_out)
                    output = []
                    for c in clients:
                        # mapped=True ensures it's visible/active
                        if c.get("mapped"):
                            output.append(
                                f"Address: {c['address']} | Title: {c['title']} | App: {c['class']}"
                            )
                    return "\n".join(output)
                except json.JSONDecodeError:
                    return "Error parsing hyprctl JSON."

            elif server == "sway":
                # Sway: Use swaymsg -t get_tree
                json_out = _run_cmd(["swaymsg", "-t", "get_tree"])
                if not json_out:
                    return "No windows found or swaymsg failed."

                output = []

                def find_windows(node):
                    if node.get("type") == "con" and node.get("name"):
                        output.append(
                            f"ID: {node['id']} | Title: {node['name']} | App: {node.get('app_id', 'N/A')}"
                        )
                    for child in node.get("nodes", []):
                        find_windows(child)
                    for child in node.get("floating_nodes", []):
                        find_windows(child)

                try:
                    tree = json.loads(json_out)
                    find_windows(tree)
                    return "\n".join(output)
                except json.JSONDecodeError:
                    return "Error parsing swaymsg JSON."

            elif server == "x11":
                # X11: Use wmctrl
                if not shutil.which("wmctrl"):
                    return "Error: 'wmctrl' not found. Install with: sudo apt install wmctrl"

                res = subprocess.run(
                    ["wmctrl", "-l", "-p"], capture_output=True, text=True
                )
                output = []
                for line in res.stdout.splitlines():
                    parts = line.split(maxsplit=4)
                    if len(parts) >= 5:
                        output.append(
                            f"ID: {parts[0]} | PID: {parts[2]} | Title: {parts[4]}"
                        )
                return "\n".join(output)

            else:
                return "Unsupported Linux environment. (Only X11, Hyprland, and Sway are fully supported)"

        elif system == "Windows":
            # PowerShell: Get-Process with MainWindowTitle
            ps_cmd = (
                'Get-Process | Where-Object {$_.MainWindowTitle -ne ""} | '
                "Select-Object Id, ProcessName, MainWindowTitle | Format-Table -HideTableHeaders"
            )
            res = subprocess.run(
                ["powershell", "-Command", ps_cmd], capture_output=True, text=True
            )
            return res.stdout.strip()

        elif system == "Darwin":
            # macOS: AppleScript via System Events (Filtering for visible apps)
            scpt = """
            tell application "System Events"
                set output to ""
                repeat with p in (every process whose background only is false)
                    try
                        repeat with w in (every window of p)
                            set output to output & "App: " & (name of p) & " | Title: " & (name of w) & "\n"
                        end repeat
                    end try
                end repeat
                return output
            end tell
            """
            res = subprocess.run(
                ["osascript", "-e", scpt], capture_output=True, text=True
            )
            return res.stdout.strip()

        else:
            return f"Unsupported OS: {system}"

    except Exception as e:
        return f"Error listing windows: {e}"


@mcp.tool()
def focus_window(window_title_fragment: str) -> str:
    """
    Brings a window to the front based on a partial match of its title or app name.
    Args:
        window_title_fragment: Part of the window name (e.g., 'Chrome', 'Terminal').
    """
    system = platform.system()
    try:
        if system == "Linux":
            server = _get_linux_display_server()

            if server == "hyprland":
                # Hyprland: focuswindow title:<regex>
                # We use regex to match the fragment case-insensitively
                cmd = f"hyprctl dispatch focuswindow title:.*{window_title_fragment}.*"
                _run_cmd(cmd.split())
                return f"Hyprland: Attempted to focus window matching '{window_title_fragment}'"

            elif server == "sway":
                # Sway: [title="..."] focus
                cmd = ["swaymsg", f'[title="(?i).*{window_title_fragment}.*"] focus']
                res = _run_cmd(cmd)
                return f"Sway: Focus result: {res}"

            elif server == "x11":
                if not shutil.which("wmctrl"):
                    return "Error: 'wmctrl' missing."
                # wmctrl -a activates the window
                subprocess.run(["wmctrl", "-a", window_title_fragment])
                return f"X11: Focused window matching '{window_title_fragment}'"

            return "Linux environment not supported for focus actions."

        elif system == "Darwin":
            # macOS: Iterate processes to find the specific window to raise
            scpt = f"""
            tell application "System Events"
                repeat with p in (every process whose background only is false)
                    repeat with w in (every window of p)
                        if name of w contains "{window_title_fragment}" then
                            tell p to set frontmost to true
                            return "Focused: " & name of w
                        end if
                    end repeat
                end repeat

                -- Fallback: try to activate by app name if window not found
                try
                    tell application "{window_title_fragment}" to activate
                    return "Activated App: {window_title_fragment}"
                on error
                    return "Window/App not found."
                end try
            end tell
            """
            res = subprocess.run(
                ["osascript", "-e", scpt], capture_output=True, text=True
            )
            return res.stdout.strip()

        elif system == "Windows":
            # Windows: Use WScript.Shell AppActivate
            # Note: This has limitations if the window is stubborn, but it's the standard non-DLL way.
            ps_code = f"""
            $w = New-Object -ComObject wscript.shell;
            if ($w.AppActivate('{window_title_fragment}')) {{
                Write-Output "Success"
            }} else {{
                Write-Output "Failed"
            }}
            """
            res = subprocess.run(
                ["powershell", "-Command", ps_code], capture_output=True, text=True
            )
            return f"Focus attempt result: {res.stdout.strip()}"

    except Exception as e:
        return f"Error focusing window: {e}"


@mcp.tool()
def close_window(window_title_fragment: str) -> str:
    """
    Closes a window gracefully (like clicking 'X').
    Falls back to killing the process if graceful close isn't supported.
    """
    system = platform.system()
    try:
        if system == "Linux":
            server = _get_linux_display_server()

            if server == "hyprland":
                # Hyprland: closewindow title:<regex>
                cmd = f"hyprctl dispatch closewindow title:.*{window_title_fragment}.*"
                _run_cmd(cmd.split())
                return f"Hyprland: Closed window matching '{window_title_fragment}'"

            elif server == "sway":
                cmd = ["swaymsg", f'[title="(?i).*{window_title_fragment}.*"] kill']
                res = _run_cmd(cmd)
                return f"Sway: Closed window. Output: {res}"

            elif server == "x11":
                if not shutil.which("wmctrl"):
                    return "Error: 'wmctrl' missing."
                # wmctrl -c closes gracefully
                subprocess.run(["wmctrl", "-c", window_title_fragment])
                return f"X11: Sent close signal to '{window_title_fragment}'"

        elif system == "Darwin":
            # macOS: Try to close specific window via System Events (Cmd+W equivalent)
            scpt = f"""
            tell application "System Events"
                repeat with p in (every process whose background only is false)
                    repeat with w in (every window of p)
                        if name of w contains "{window_title_fragment}" then
                            tell p
                                set frontmost to true -- Bring to front to ensure close works
                                tell w to click (button 1 of w) -- Usually the red 'X'
                            end tell
                            return "Closed window: " & name of w
                        end if
                    end repeat
                end repeat
                return "Window not found to close."
            end tell
            """
            res = subprocess.run(
                ["osascript", "-e", scpt], capture_output=True, text=True
            )
            return res.stdout.strip()

        elif system == "Windows":
            # Windows: Try CloseMainWindow() first (Graceful), else Stop-Process (Kill)
            ps_code = f"""
            $proc = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{window_title_fragment}*"}} | Select-Object -First 1
            if ($proc) {{
                $proc.CloseMainWindow() | Out-Null
                Write-Output "Sent close signal to: $($proc.MainWindowTitle)"
            }} else {{
                Write-Output "No matching process found."
            }}
            """
            res = subprocess.run(
                ["powershell", "-Command", ps_code], capture_output=True, text=True
            )
            return res.stdout.strip()

        return "Not supported."
    except Exception as e:
        return f"Error closing window: {e}"


@mcp.tool()
def execute_command(command: str) -> str:
    """
    Executes a terminal command.
    Restrictions:
    1. Non-interactive commands only (no input allowed).
    2. Times out after 15 seconds.
    3. Output is captured and returned.

    Args:
        command: The shell command to run (e.g., 'ls -la', 'pytest').
    """
    try:
        # Security: Log the attempt
        log(f"Received command request: {command}")

        # Security: Prevent escaping the shell context trivially,
        # though functionality requires shell=True for pipes/chaining.
        # The main security is the User Confirmation in the Client.

        # Run command
        # timeout=15 ensures we don't hang on interactive commands like 'python' or 'nano'
        # cwd=BASE_DIR ensures operations happen in the project folder
        process = subprocess.run(
            command,
            shell=True,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=15,
        )

        stdout = process.stdout.strip()
        stderr = process.stderr.strip()

        output_parts = []
        if stdout:
            output_parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            output_parts.append(f"STDERR:\n{stderr}")
        if not output_parts:
            output_parts.append("(Command executed with no output)")

        return_code_msg = f"\n[Return Code: {process.returncode}]"

        full_output = "\n\n".join(output_parts) + return_code_msg

        # Log result size
        log(f"Command finished. Output length: {len(full_output)}")
        return full_output

    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 15 seconds. Ensure the command is non-interactive."
    except Exception as e:
        log(f"Error executing command: {e}")
        return f"Error: {str(e)}"


if __name__ == "__main__":
    # Ensure the base directory exists before starting
    if not BASE_DIR.exists():
        print(
            f"[SERVER INIT]: Warning: Base directory {BASE_DIR} did not exist. Creating it...",
            file=sys.stderr,
        )
        BASE_DIR.mkdir(parents=True, exist_ok=True)

    log("Server starting in stdio mode...")
    mcp.run(transport="stdio")
