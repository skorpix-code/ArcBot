"""
Enhanced MCP Server for ArcBot Agent
=====================================
Cross-platform system agent with:
- Full filesystem interaction (sandboxed)
- Window management (Linux/macOS/Windows)
- Command execution with guardrails
- Todo/task management
- Persistent & temporary memory
- Code analysis tools
"""

import ast
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
from mcp.server.fastmcp import FastMCP

# Optional imports
try:
    from ddgs import DDGS

    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

# --- CONFIGURATION ---
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(
    os.environ.get("ARCBOT_BASE_DIR", os.path.expanduser("~/ArcBot_Workspace"))
)
MEMORY_DIR = BASE_DIR / ".arcbot"
TODO_FILE = ".arcbot/todo.json"
MEMORY_FILE = ".arcbot/memory.json"
TEMP_MEMORY_FILE = ".arcbot/temp_memory.json"

# Create the MCP Server instance
mcp = FastMCP("ArcBot_Agent")

# --- CONSTANTS ---
MAX_OUTPUT_LENGTH = 8000
COMMAND_TIMEOUT = 30
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
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "eggs",
    ".eggs",
    "*.egg-info",
    "__pypackages__",
    ".arcbot",
}

# Dangerous command patterns that require explicit confirmation
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\brmdir\b",
    r"\bformat\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bchmod\s+777\b",
    r"\bchown\b",
    r"\bsudo\b",
    r"\bsu\s",
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bsystemctl\b",
    r"\breg\s+(delete|add)\b",
    r"\bnet\s+(user|stop|start)\b",
    r"\btaskkill\b",
    r"\bkill\s+-9\b",
    r"\bpkill\b",
]


# --- DATA CLASSES ---
class TaskPriority(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


# --- LOGGING ---
def log(message: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] [SERVER]: {message}", file=sys.stderr)


# --- SECURITY ---
def get_secure_path(user_path: str) -> Path:
    """Resolves a path and ensures it is contained within BASE_DIR."""
    try:
        user_path = str(user_path).strip()
        if not user_path or user_path == ".":
            return BASE_DIR.resolve()
        if os.path.isabs(user_path):
            target_path = Path(user_path).resolve()
        else:
            target_path = (BASE_DIR / user_path).resolve()
        base_resolved = BASE_DIR.resolve()
        if not str(target_path).startswith(str(base_resolved)):
            raise ValueError(
                f"Security Error: Access denied to '{user_path}'. "
                f"You are restricted to '{BASE_DIR}'"
            )
        return target_path
    except Exception as e:
        log(f"Security check failed for path '{user_path}': {e}", "ERROR")
        raise


def sanitize_input(text: str, max_length: int = 10000) -> str:
    if not text:
        return ""
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text))
    return sanitized[:max_length]


def truncate_output(output: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    if len(output) <= max_length:
        return output
    half = max_length // 2
    return (
        output[:half]
        + f"\n\n... [Truncated {len(output) - max_length} chars] ...\n\n"
        + output[-half:]
    )


def is_dangerous_command(command: str) -> bool:
    """Check if a command matches dangerous patterns."""
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False


def command_accesses_outside_basedir(command: str) -> bool:
    """Basic heuristic to check if command tries to access paths outside BASE_DIR."""
    # Look for absolute paths that aren't within BASE_DIR
    abs_paths = re.findall(r"(?:^|\s)(/[^\s]+|[A-Z]:\\[^\s]+)", command)
    base_str = str(BASE_DIR.resolve())
    for p in abs_paths:
        try:
            resolved = str(Path(p).resolve())
            if not resolved.startswith(base_str):
                return True
        except Exception:
            pass
    return False


# --- SYSTEM DETECTION ---
class SystemDetector:
    """Cross-platform system information detection."""

    @staticmethod
    def get_os_info() -> Tuple[str, str, str, str]:
        system = platform.system()
        if system == "Linux":
            os_name = "Linux"
            os_version = ""
            for release_file in ["/etc/os-release", "/usr/lib/os-release"]:
                if os.path.exists(release_file):
                    try:
                        with open(release_file) as f:
                            content = f.read()
                            name_match = re.search(
                                r'^NAME="?(.+?)"?\s*$', content, re.MULTILINE
                            )
                            version_match = re.search(
                                r'^VERSION="?(.+?)"?\s*$', content, re.MULTILINE
                            )
                            if name_match:
                                os_name = name_match.group(1).strip('"')
                            if version_match:
                                os_version = version_match.group(1).strip('"')
                            break
                    except Exception:
                        pass
            if not os_version:
                os_version = platform.release()
        elif system == "Windows":
            os_name = "Windows"
            os_version = platform.version()
        elif system == "Darwin":
            os_name = "macOS"
            try:
                os_version = subprocess.check_output(
                    ["sw_vers", "-productVersion"], text=True
                ).strip()
            except Exception:
                os_version = platform.mac_ver()[0]
        else:
            os_name = system
            os_version = platform.version()

        architecture = platform.machine() or platform.processor()
        hostname = platform.node()
        return os_name, os_version, architecture, hostname

    @staticmethod
    def get_cpu_info() -> Dict[str, Any]:
        cpu_info = {
            "physical_cores": psutil.cpu_count(logical=False) or 1,
            "logical_cores": psutil.cpu_count(logical=True) or 1,
            "max_frequency": {},
            "current_frequency": {},
            "cpu_percent": 0,
            "model": "Unknown",
        }
        try:
            freq = psutil.cpu_freq()
            if freq:
                cpu_info["max_frequency"] = {"value": freq.max, "unit": "MHz"}
                cpu_info["current_frequency"] = {
                    "value": round(freq.current, 2),
                    "unit": "MHz",
                }
            cpu_info["cpu_percent"] = psutil.cpu_percent(interval=0.1)

            system = platform.system()
            if system == "Linux":
                try:
                    with open("/proc/cpuinfo") as f:
                        content = f.read()
                        model_match = re.search(r"model name\s*:\s*(.+)", content)
                        if model_match:
                            cpu_info["model"] = model_match.group(1).strip()
                except Exception:
                    pass
            elif system == "Darwin":
                try:
                    result = subprocess.run(
                        ["sysctl", "-n", "machdep.cpu.brand_string"],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        cpu_info["model"] = result.stdout.strip()
                except Exception:
                    pass
            elif system == "Windows":
                try:
                    result = subprocess.run(
                        ["wmic", "cpu", "get", "name"],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW")
                        else 0,
                    )
                    lines = result.stdout.strip().split("\n")
                    if len(lines) > 1:
                        cpu_info["model"] = lines[1].strip()
                except Exception:
                    pass
        except Exception as e:
            log(f"Error getting CPU info: {e}", "WARNING")
        return cpu_info

    @staticmethod
    def get_memory_info() -> Dict[str, Any]:
        try:
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()

            def format_bytes(bytes_val):
                for unit in ["B", "KB", "MB", "GB", "TB"]:
                    if bytes_val < 1024.0:
                        return {"value": round(bytes_val, 2), "unit": unit}
                    bytes_val /= 1024.0
                return {"value": round(bytes_val, 2), "unit": "PB"}

            return {
                "total": format_bytes(mem.total),
                "available": format_bytes(mem.available),
                "used": format_bytes(mem.used),
                "percent": mem.percent,
                "swap": {
                    "total": format_bytes(swap.total),
                    "used": format_bytes(swap.used),
                    "percent": swap.percent,
                },
            }
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def get_gpu_info() -> List[Dict[str, Any]]:
        gpus = []
        system = platform.system()
        try:
            if shutil.which("nvidia-smi"):
                result = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=name,memory.total,driver_version",
                        "--format=csv,noheader",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 3:
                            gpus.append(
                                {
                                    "vendor": "NVIDIA",
                                    "model": parts[0],
                                    "memory": parts[1],
                                    "driver": parts[2],
                                }
                            )

            if system == "Linux" and not gpus:
                if shutil.which("lspci"):
                    result = subprocess.run(
                        ["lspci"], capture_output=True, text=True, timeout=10
                    )
                    if result.returncode == 0:
                        for line in result.stdout.splitlines():
                            if "VGA" in line or "3D" in line or "Display" in line:
                                gpus.append(
                                    {
                                        "model": line.split(":")[-1].strip()
                                        if ":" in line
                                        else line.strip()
                                    }
                                )

            elif system == "Windows" and not gpus:
                try:
                    result = subprocess.run(
                        [
                            "powershell",
                            "-NoProfile",
                            "-Command",
                            "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW")
                        else 0,
                    )
                    if result.returncode == 0:
                        for line in result.stdout.strip().splitlines():
                            if line.strip():
                                gpus.append({"model": line.strip()})
                except Exception:
                    pass

            elif system == "Darwin" and not gpus:
                result = subprocess.run(
                    ["system_profiler", "SPDisplaysDataType"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    current_gpu = {}
                    for line in result.stdout.splitlines():
                        line = line.strip()
                        if "Chipset Model:" in line:
                            if current_gpu:
                                gpus.append(current_gpu)
                            current_gpu = {
                                "model": line.split(":")[1].strip(),
                                "vendor": "Apple",
                            }
                        elif "VRAM" in line:
                            current_gpu["memory"] = line.split(":")[1].strip()
                    if current_gpu:
                        gpus.append(current_gpu)

        except Exception as e:
            log(f"Error getting GPU info: {e}", "WARNING")
        return gpus if gpus else [{"info": "No GPU detected"}]

    @staticmethod
    def get_disk_info() -> List[Dict[str, Any]]:
        disks = []
        try:
            for partition in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(partition.mountpoint)
                    disks.append(
                        {
                            "device": partition.device,
                            "mountpoint": partition.mountpoint,
                            "fstype": partition.fstype,
                            "total_gb": round(usage.total / (1024**3), 2),
                            "used_gb": round(usage.used / (1024**3), 2),
                            "free_gb": round(usage.free / (1024**3), 2),
                            "percent": usage.percent,
                        }
                    )
                except (PermissionError, OSError):
                    continue
        except Exception as e:
            log(f"Error getting disk info: {e}", "WARNING")
        return disks

    @staticmethod
    def get_display_server() -> str:
        system = platform.system()
        if system != "Linux":
            return (
                "Quartz"
                if system == "Darwin"
                else ("DWM" if system == "Windows" else "Unknown")
            )

        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return "Hyprland"
        if os.environ.get("SWAYSOCK"):
            return "Sway"
        if os.environ.get("GNOME_DESKTOP_SESSION_ID"):
            return "GNOME"
        if os.environ.get("KDE_FULL_SESSION"):
            return "KDE"

        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type == "x11" or os.environ.get("DISPLAY"):
            return "X11"
        if session_type == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            return "Wayland"
        return "Unknown"


# ============================================================
# CROSS-PLATFORM WINDOW MANAGER
# ============================================================


class WindowManager:
    """Cross-platform window management with proper Windows support."""

    # Cache for Windows Add-Type to avoid duplicate loading
    _win32_types_loaded = False

    @staticmethod
    def _run_cmd(
        cmd_list: List[str], timeout: int = 10, input_text: str = None
    ) -> Tuple[str, str, int]:
        try:
            kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
                "input": input_text,
            }
            if platform.system() == "Windows" and hasattr(
                subprocess, "CREATE_NO_WINDOW"
            ):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(cmd_list, **kwargs)
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return "", "Command timed out", -1
        except FileNotFoundError:
            return "", f"Command not found: {cmd_list[0]}", -1
        except Exception as e:
            return "", str(e), -1

    @staticmethod
    def _run_powershell(script: str, timeout: int = 15) -> Tuple[str, str, int]:
        """Run PowerShell script with proper encoding and no window flash."""
        try:
            kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
            }
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                **kwargs,
            )
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        except Exception as e:
            return "", str(e), -1

    @staticmethod
    def get_display_server() -> str:
        return SystemDetector.get_display_server()

    # ========================================
    # WINDOW LISTING
    # ========================================

    @classmethod
    def list_windows(cls) -> List[Dict[str, Any]]:
        system = platform.system()
        windows = []
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    windows = cls._list_hyprland_windows()
                elif ds == "Sway":
                    windows = cls._list_sway_windows()
                else:
                    windows = cls._list_x11_windows()
            elif system == "Darwin":
                windows = cls._list_macos_windows()
            elif system == "Windows":
                windows = cls._list_windows_windows()
        except Exception as e:
            log(f"Error listing windows: {e}", "ERROR")
        return windows

    @classmethod
    def _list_hyprland_windows(cls) -> List[Dict[str, Any]]:
        stdout, _, code = cls._run_cmd(["hyprctl", "clients", "-j"])
        if code != 0 or not stdout:
            return []
        try:
            clients = json.loads(stdout)
            windows = []
            for c in clients:
                if c.get("mapped") and c.get("title"):
                    windows.append(
                        {
                            "id": c.get("address", "unknown"),
                            "title": c.get("title", ""),
                            "app": c.get("class", ""),
                            "workspace": str(c.get("workspace", {}).get("id", "")),
                            "workspace_name": c.get("workspace", {}).get("name", ""),
                            "monitor": str(c.get("monitor", "")),
                            "fullscreen": c.get("fullscreen", False),
                            "floating": c.get("floating", False),
                            "pinned": c.get("pinned", False),
                            "size": {
                                "width": c.get("size", [0, 0])[0],
                                "height": c.get("size", [0, 0])[1],
                            },
                            "position": {
                                "x": c.get("at", [0, 0])[0],
                                "y": c.get("at", [0, 0])[1],
                            },
                            "display_server": "Hyprland",
                        }
                    )
            return windows
        except json.JSONDecodeError:
            return []

    @classmethod
    def _list_sway_windows(cls) -> List[Dict[str, Any]]:
        stdout, _, code = cls._run_cmd(["swaymsg", "-t", "get_tree"])
        if code != 0 or not stdout:
            return []
        windows = []

        def find_windows(node, ws_id="", ws_name=""):
            if node.get("type") == "workspace":
                ws_id = str(node.get("id", ""))
                ws_name = node.get("name", "")
            if node.get("type") == "con" and node.get("name"):
                wp = node.get("window_properties", {})
                windows.append(
                    {
                        "id": str(node.get("id", "unknown")),
                        "title": node.get("name", ""),
                        "app": node.get("app_id", wp.get("class", "")),
                        "workspace": ws_id,
                        "workspace_name": ws_name,
                        "fullscreen": node.get("fullscreen", False),
                        "floating": node.get("type") == "floating_con",
                        "size": {
                            "width": node.get("rect", {}).get("width", 0),
                            "height": node.get("rect", {}).get("height", 0),
                        },
                        "position": {
                            "x": node.get("rect", {}).get("x", 0),
                            "y": node.get("rect", {}).get("y", 0),
                        },
                        "display_server": "Sway",
                    }
                )
            for child in node.get("nodes", []) + node.get("floating_nodes", []):
                find_windows(child, ws_id, ws_name)

        try:
            find_windows(json.loads(stdout))
            return windows
        except json.JSONDecodeError:
            return []

    @classmethod
    def _list_x11_windows(cls) -> List[Dict[str, Any]]:
        windows = []
        if shutil.which("wmctrl"):
            stdout, _, code = cls._run_cmd(["wmctrl", "-l", "-p", "-G"])
            if code == 0 and stdout:
                for line in stdout.splitlines():
                    parts = line.split(None, 7)
                    if len(parts) >= 8:
                        windows.append(
                            {
                                "id": parts[0],
                                "title": parts[7] if len(parts) > 7 else "",
                                "app": "",
                                "workspace": parts[1],
                                "workspace_name": f"Desktop {parts[1]}",
                                "size": {
                                    "width": int(parts[5]),
                                    "height": int(parts[6]),
                                },
                                "position": {"x": int(parts[3]), "y": int(parts[4])},
                                "pid": parts[2],
                                "display_server": "X11",
                            }
                        )
        return windows

    @classmethod
    def _list_macos_windows(cls) -> List[Dict[str, Any]]:
        scpt = """
        tell application "System Events"
            set output to ""
            repeat with p in (every process whose background only is false)
                try
                    set appName to name of p
                    set appFrontmost to frontmost of p
                    repeat with w in (every window of p)
                        set winTitle to name of w
                        try
                            set winPos to position of w
                            set winSize to size of w
                            set posX to item 1 of winPos
                            set posY to item 2 of winPos
                            set sizeW to item 1 of winSize
                            set sizeH to item 2 of winSize
                        on error
                            set posX to 0
                            set posY to 0
                            set sizeW to 0
                            set sizeH to 0
                        end try
                        set output to output & appName & "|||" & winTitle & "|||" & posX & "|||" & posY & "|||" & sizeW & "|||" & sizeH & "|||" & appFrontmost & linefeed
                    end repeat
                end try
            end repeat
            return output
        end tell
        """
        stdout, _, code = cls._run_cmd(["osascript", "-e", scpt], timeout=15)
        if code != 0 or not stdout:
            return []
        windows = []
        for line in stdout.splitlines():
            if "|||" in line:
                parts = line.split("|||")
                if len(parts) >= 7:
                    try:
                        windows.append(
                            {
                                "app": parts[0],
                                "title": parts[1],
                                "id": f"{parts[0]}:{parts[1]}",
                                "position": {
                                    "x": int(float(parts[2])),
                                    "y": int(float(parts[3])),
                                },
                                "size": {
                                    "width": int(float(parts[4])),
                                    "height": int(float(parts[5])),
                                },
                                "frontmost": parts[6].lower() == "true",
                                "display_server": "macOS",
                            }
                        )
                    except (ValueError, IndexError):
                        continue
        return windows

    @classmethod
    def _list_windows_windows(cls) -> List[Dict[str, Any]]:
        """List windows on Windows using Get-Process (no Add-Type needed for listing)."""
        ps_cmd = """
Get-Process | Where-Object {$_.MainWindowTitle -ne ""} | ForEach-Object {
    $h = $_.MainWindowHandle
    "$($_.ProcessName)|||$($_.MainWindowTitle)|||$($_.Id)|||$h"
}
"""
        stdout, stderr, code = cls._run_powershell(ps_cmd)
        if code != 0 or not stdout:
            return []
        windows = []
        for line in stdout.splitlines():
            if "|||" in line:
                parts = line.split("|||")
                if len(parts) >= 4:
                    windows.append(
                        {
                            "app": parts[0],
                            "title": parts[1],
                            "id": parts[3],
                            "pid": parts[2],
                            "display_server": "Windows",
                        }
                    )
        return windows

    # ========================================
    # WINDOWS HELPER - Shared Win32 Functions
    # ========================================

    @classmethod
    def _win32_window_action(
        cls, handle: str, action: str, **kwargs
    ) -> Tuple[bool, str]:
        """
        Unified Windows window action using a single PowerShell script
        that avoids duplicate Add-Type by using a unique class name per call.
        """
        uid = hashlib.md5(
            f"{action}{handle}{datetime.now().isoformat()}".encode()
        ).hexdigest()[:8]

        if action == "minimize":
            ps = f"""
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class W{uid} {{
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}}
"@
[W{uid}]::ShowWindow([IntPtr]::new({handle}), 6)
"OK"
"""
        elif action == "maximize":
            ps = f"""
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class W{uid} {{
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}}
"@
[W{uid}]::ShowWindow([IntPtr]::new({handle}), 3)
"OK"
"""
        elif action == "restore":
            ps = f"""
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class W{uid} {{
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}}
"@
[W{uid}]::ShowWindow([IntPtr]::new({handle}), 9)
"OK"
"""
        elif action == "focus":
            ps = f"""
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class W{uid} {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}}
"@
[W{uid}]::ShowWindow([IntPtr]::new({handle}), 9)
[W{uid}]::SetForegroundWindow([IntPtr]::new({handle}))
"OK"
"""
        elif action == "close":
            title = kwargs.get("title", "")
            ps = f"""
$proc = Get-Process | Where-Object {{$_.MainWindowHandle -eq {handle}}} | Select-Object -First 1
if ($proc) {{ $proc.CloseMainWindow() | Out-Null; "OK" }} else {{ "NOTFOUND" }}
"""
        elif action == "move":
            x, y = kwargs.get("x", 0), kwargs.get("y", 0)
            ps = f"""
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class W{uid} {{
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int X, int Y, int cx, int cy, uint f);
}}
"@
[W{uid}]::SetWindowPos([IntPtr]::new({handle}), [IntPtr]::Zero, {x}, {y}, 0, 0, 0x0005)
"OK"
"""
        elif action == "resize":
            w, h = kwargs.get("width", 800), kwargs.get("height", 600)
            ps = f"""
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class W{uid} {{
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int X, int Y, int cx, int cy, uint f);
}}
"@
[W{uid}]::SetWindowPos([IntPtr]::new({handle}), [IntPtr]::Zero, 0, 0, {w}, {h}, 0x0006)
"OK"
"""
        elif action == "move_resize":
            x, y = kwargs.get("x", 0), kwargs.get("y", 0)
            w, h = kwargs.get("width", 800), kwargs.get("height", 600)
            ps = f"""
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class W{uid} {{
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int X, int Y, int cx, int cy, uint f);
}}
"@
[W{uid}]::SetWindowPos([IntPtr]::new({handle}), [IntPtr]::Zero, {x}, {y}, {w}, {h}, 0x0000)
"OK"
"""
        elif action == "topmost":
            enable = kwargs.get("enable", True)
            after = "-1" if enable else "-2"
            ps = f"""
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class W{uid} {{
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int X, int Y, int cx, int cy, uint f);
}}
"@
[W{uid}]::SetWindowPos([IntPtr]::new({handle}), [IntPtr]::new({after}), 0, 0, 0, 0, 0x0003)
"OK"
"""
        else:
            return False, f"Unknown action: {action}"

        stdout, stderr, code = cls._run_powershell(ps)
        success = "OK" in stdout
        return success, "OK" if success else (stderr or "Failed")

    # ========================================
    # HELPER: Find window by query
    # ========================================

    @classmethod
    def _find_window(cls, window_query: str) -> Optional[Dict[str, Any]]:
        windows = cls.list_windows()
        q = window_query.lower()
        for w in windows:
            if q in w.get("title", "").lower() or q in w.get("app", "").lower():
                return w
        return None

    # ========================================
    # WORKSPACE MANAGEMENT
    # ========================================

    @classmethod
    def list_workspaces(cls) -> List[Dict[str, Any]]:
        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    return cls._list_hyprland_workspaces()
                elif ds == "Sway":
                    return cls._list_sway_workspaces()
                else:
                    return cls._list_x11_workspaces()
            elif system == "Darwin":
                return cls._list_macos_spaces()
            elif system == "Windows":
                return [
                    {
                        "id": "1",
                        "name": "Desktop 1",
                        "number": 1,
                        "windows": 0,
                        "active": True,
                    }
                ]
        except Exception as e:
            log(f"Error listing workspaces: {e}", "ERROR")
        return []

    @classmethod
    def _list_hyprland_workspaces(cls) -> List[Dict[str, Any]]:
        stdout, _, code = cls._run_cmd(["hyprctl", "workspaces", "-j"])
        if code != 0 or not stdout:
            return []
        try:
            data = json.loads(stdout)
            active_stdout, _, _ = cls._run_cmd(["hyprctl", "activeworkspace", "-j"])
            active_id = None
            if active_stdout:
                try:
                    active_id = json.loads(active_stdout).get("id")
                except Exception:
                    pass
            return [
                {
                    "id": str(ws.get("id", "")),
                    "name": ws.get("name", str(ws.get("id", ""))),
                    "number": ws.get("id", 0),
                    "windows": ws.get("windows", 0),
                    "monitor": ws.get("monitor", ""),
                    "active": ws.get("id") == active_id,
                }
                for ws in data
            ]
        except json.JSONDecodeError:
            return []

    @classmethod
    def _list_sway_workspaces(cls) -> List[Dict[str, Any]]:
        stdout, _, code = cls._run_cmd(["swaymsg", "-t", "get_workspaces"])
        if code != 0 or not stdout:
            return []
        try:
            data = json.loads(stdout)
            return [
                {
                    "id": str(ws.get("id", "")),
                    "name": ws.get("name", ""),
                    "number": ws.get("num", 0),
                    "windows": len(ws.get("floating_nodes", []))
                    + len(ws.get("nodes", [])),
                    "active": ws.get("focused", False),
                }
                for ws in data
            ]
        except json.JSONDecodeError:
            return []

    @classmethod
    def _list_x11_workspaces(cls) -> List[Dict[str, Any]]:
        if not shutil.which("wmctrl"):
            return []
        stdout, _, code = cls._run_cmd(["wmctrl", "-d"])
        if code != 0 or not stdout:
            return []
        workspaces = []
        for line in stdout.splitlines():
            parts = line.split(None, 11)
            if len(parts) >= 3:
                workspaces.append(
                    {
                        "id": parts[0],
                        "name": parts[-1] if len(parts) > 5 else f"Desktop {parts[0]}",
                        "number": int(parts[0]) if parts[0].isdigit() else 0,
                        "active": "*" in parts[1],
                    }
                )
        return workspaces

    @classmethod
    def _list_macos_spaces(cls) -> List[Dict[str, Any]]:
        if shutil.which("yabai"):
            stdout, _, code = cls._run_cmd(["yabai", "-m", "query", "--spaces"])
            if code == 0 and stdout:
                try:
                    return [
                        {
                            "id": str(s.get("id", "")),
                            "name": f"Space {s.get('index', '')}",
                            "number": s.get("index", 0),
                            "windows": len(s.get("windows", [])),
                            "active": s.get("has-focus", False),
                        }
                        for s in json.loads(stdout)
                    ]
                except Exception:
                    pass
        return [
            {
                "id": "1",
                "name": "Current Space",
                "number": 1,
                "windows": 0,
                "active": True,
            }
        ]

    # ========================================
    # WINDOW ACTIONS (Cross-Platform)
    # ========================================

    @classmethod
    def focus_window(cls, window_query: str) -> Dict[str, Any]:
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, _, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "focuswindow",
                            f"address:{target['id']}",
                        ]
                    )
                    result["success"] = code == 0
                elif ds == "Sway":
                    _, _, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "focus",
                        ]
                    )
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    _, _, code = cls._run_cmd(["wmctrl", "-a", window_query])
                    result["success"] = code == 0
            elif system == "Darwin":
                app = target.get("app", "")
                _, _, code = cls._run_cmd(
                    ["osascript", "-e", f'tell application "{app}" to activate']
                )
                result["success"] = code == 0
            elif system == "Windows":
                ok, msg = cls._win32_window_action(target["id"], "focus")
                result["success"] = ok
                result["message"] = msg
        except Exception as e:
            result["message"] = str(e)

        if result["success"]:
            result["message"] = f"Focused: {target.get('title', window_query)}"
        return result

    @classmethod
    def minimize_window(cls, window_query: str) -> Dict[str, Any]:
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, _, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "movetoworkspacesilent",
                            f"special:minimized,address:{target['id']}",
                        ]
                    )
                    result["success"] = code == 0
                elif ds == "Sway":
                    _, _, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "move",
                            "to",
                            "scratchpad",
                        ]
                    )
                    result["success"] = code == 0
                elif shutil.which("xdotool"):
                    _, _, code = cls._run_cmd(
                        ["xdotool", "windowminimize", target["id"]]
                    )
                    result["success"] = code == 0
            elif system == "Darwin":
                app = target.get("app", "")
                scpt = f'''tell application "System Events" to tell process "{app}" to set value of attribute "AXMinimized" of window 1 to true'''
                _, _, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
            elif system == "Windows":
                ok, msg = cls._win32_window_action(target["id"], "minimize")
                result["success"] = ok
        except Exception as e:
            result["message"] = str(e)

        if result["success"]:
            result["message"] = f"Minimized: {target.get('title', window_query)}"
        return result

    @classmethod
    def maximize_window(cls, window_query: str) -> Dict[str, Any]:
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, _, code = cls._run_cmd(
                        ["hyprctl", "dispatch", "fullscreen", "1"]
                    )
                    result["success"] = code == 0
                elif ds == "Sway":
                    _, _, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "fullscreen",
                            "toggle",
                        ]
                    )
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    _, _, code = cls._run_cmd(
                        [
                            "wmctrl",
                            "-i",
                            "-r",
                            target["id"],
                            "-b",
                            "toggle,maximized_vert,maximized_horz",
                        ]
                    )
                    result["success"] = code == 0
            elif system == "Darwin":
                app = target.get("app", "")
                scpt = f'''tell application "{app}" to activate
tell application "System Events" to tell process "{app}" to keystroke "f" using {{control down, command down}}'''
                _, _, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
            elif system == "Windows":
                ok, msg = cls._win32_window_action(target["id"], "maximize")
                result["success"] = ok
        except Exception as e:
            result["message"] = str(e)

        if result["success"]:
            result["message"] = f"Maximized: {target.get('title', window_query)}"
        return result

    @classmethod
    def close_window(cls, window_query: str) -> Dict[str, Any]:
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, _, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "closewindow",
                            f"address:{target['id']}",
                        ]
                    )
                    result["success"] = code == 0
                elif ds == "Sway":
                    _, _, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "kill",
                        ]
                    )
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    _, _, code = cls._run_cmd(["wmctrl", "-c", window_query])
                    result["success"] = code == 0
            elif system == "Darwin":
                app = target.get("app", "")
                scpt = f'''tell application "{app}" to activate
tell application "System Events" to keystroke "w" using command down'''
                _, _, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
            elif system == "Windows":
                ok, msg = cls._win32_window_action(
                    target["id"], "close", title=target.get("title", "")
                )
                result["success"] = ok
        except Exception as e:
            result["message"] = str(e)

        if result["success"]:
            result["message"] = f"Closed: {target.get('title', window_query)}"
        return result

    @classmethod
    def move_window(cls, window_query: str, x: int, y: int) -> Dict[str, Any]:
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, _, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "movewindowpixel",
                            f"exact {x} {y},address:{target['id']}",
                        ]
                    )
                    result["success"] = code == 0
                elif ds == "Sway":
                    _, _, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "move",
                            "position",
                            str(x),
                            str(y),
                        ]
                    )
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    _, _, code = cls._run_cmd(
                        ["wmctrl", "-i", "-r", target["id"], "-e", f"0,{x},{y},-1,-1"]
                    )
                    result["success"] = code == 0
            elif system == "Darwin":
                app = target.get("app", "")
                scpt = f'''tell application "System Events" to tell process "{app}" to set position of window 1 to {{{x}, {y}}}'''
                _, _, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
            elif system == "Windows":
                ok, msg = cls._win32_window_action(target["id"], "move", x=x, y=y)
                result["success"] = ok
        except Exception as e:
            result["message"] = str(e)

        if result["success"]:
            result["message"] = f"Moved to ({x}, {y})"
        return result

    @classmethod
    def resize_window(
        cls, window_query: str, width: int, height: int
    ) -> Dict[str, Any]:
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, _, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "resizewindowpixel",
                            f"exact {width} {height},address:{target['id']}",
                        ]
                    )
                    result["success"] = code == 0
                elif ds == "Sway":
                    _, _, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "resize",
                            "set",
                            str(width),
                            str(height),
                        ]
                    )
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    _, _, code = cls._run_cmd(
                        [
                            "wmctrl",
                            "-i",
                            "-r",
                            target["id"],
                            "-e",
                            f"0,-1,-1,{width},{height}",
                        ]
                    )
                    result["success"] = code == 0
            elif system == "Darwin":
                app = target.get("app", "")
                scpt = f'''tell application "System Events" to tell process "{app}" to set size of window 1 to {{{width}, {height}}}'''
                _, _, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
            elif system == "Windows":
                ok, msg = cls._win32_window_action(
                    target["id"], "resize", width=width, height=height
                )
                result["success"] = ok
        except Exception as e:
            result["message"] = str(e)

        if result["success"]:
            result["message"] = f"Resized to {width}x{height}"
        return result

    @classmethod
    def set_always_on_top(
        cls, window_query: str, enable: bool = True
    ) -> Dict[str, Any]:
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, _, code = cls._run_cmd(
                        ["hyprctl", "dispatch", "pin", f"address:{target['id']}"]
                    )
                    result["success"] = code == 0
                elif ds == "Sway":
                    _, _, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "sticky",
                            "toggle",
                        ]
                    )
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    action = "add" if enable else "remove"
                    _, _, code = cls._run_cmd(
                        ["wmctrl", "-i", "-r", target["id"], "-b", f"{action},above"]
                    )
                    result["success"] = code == 0
            elif system == "Windows":
                ok, msg = cls._win32_window_action(
                    target["id"], "topmost", enable=enable
                )
                result["success"] = ok
        except Exception as e:
            result["message"] = str(e)

        if result["success"]:
            result["message"] = f"Always-on-top {'enabled' if enable else 'disabled'}"
        return result

    @classmethod
    def snap_window(cls, window_query: str, direction: str) -> Dict[str, Any]:
        """Snap window to screen edge. Directions: left, right, top, bottom, center."""
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        system = platform.system()
        try:
            # Get screen dimensions
            monitors = cls.list_monitors()
            mon = (
                monitors[0]
                if monitors
                else {"width": 1920, "height": 1080, "x": 0, "y": 0}
            )
            sw, sh = mon["width"], mon["height"]
            sx, sy = mon.get("x", 0), mon.get("y", 0)

            snap_map = {
                "left": (sx, sy, sw // 2, sh),
                "right": (sx + sw // 2, sy, sw // 2, sh),
                "top": (sx, sy, sw, sh // 2),
                "bottom": (sx, sy + sh // 2, sw, sh // 2),
                "top_left": (sx, sy, sw // 2, sh // 2),
                "top_right": (sx + sw // 2, sy, sw // 2, sh // 2),
                "bottom_left": (sx, sy + sh // 2, sw // 2, sh // 2),
                "bottom_right": (sx + sw // 2, sy + sh // 2, sw // 2, sh // 2),
                "center": (sx + sw // 4, sy + sh // 4, sw // 2, sh // 2),
            }

            coords = snap_map.get(direction.lower())
            if not coords:
                result["message"] = f"Unknown direction: {direction}"
                return result

            x, y, w, h = coords

            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    addr = target["id"]
                    cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "movewindowpixel",
                            f"exact {x} {y},address:{addr}",
                        ]
                    )
                    _, _, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "resizewindowpixel",
                            f"exact {w} {h},address:{addr}",
                        ]
                    )
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    _, _, code = cls._run_cmd(
                        ["wmctrl", "-i", "-r", target["id"], "-e", f"0,{x},{y},{w},{h}"]
                    )
                    result["success"] = code == 0
            elif system == "Darwin":
                app = target.get("app", "")
                scpt = f'''tell application "System Events" to tell process "{app}"
set position of window 1 to {{{x}, {y}}}
set size of window 1 to {{{w}, {h}}}
end tell'''
                _, _, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
            elif system == "Windows":
                ok, msg = cls._win32_window_action(
                    target["id"], "move_resize", x=x, y=y, width=w, height=h
                )
                result["success"] = ok

        except Exception as e:
            result["message"] = str(e)

        if result["success"]:
            result["message"] = f"Snapped to {direction}"
        return result

    @classmethod
    def tile_windows(cls, layout: str = "horizontal") -> Dict[str, Any]:
        """Tile all visible windows in a layout: horizontal, vertical, grid."""
        windows = cls.list_windows()
        if not windows:
            return {"success": False, "message": "No windows to tile"}

        monitors = cls.list_monitors()
        mon = (
            monitors[0] if monitors else {"width": 1920, "height": 1080, "x": 0, "y": 0}
        )
        sw, sh = mon["width"], mon["height"]
        sx, sy = mon.get("x", 0), mon.get("y", 0)
        count = len(windows)

        positions = []
        if layout == "horizontal":
            w = sw // count
            for i in range(count):
                positions.append((sx + i * w, sy, w, sh))
        elif layout == "vertical":
            h = sh // count
            for i in range(count):
                positions.append((sx, sy + i * h, sw, h))
        elif layout == "grid":
            import math

            cols = math.ceil(math.sqrt(count))
            rows = math.ceil(count / cols)
            cw, ch = sw // cols, sh // rows
            for i in range(count):
                r, c = divmod(i, cols)
                positions.append((sx + c * cw, sy + r * ch, cw, ch))

        system = platform.system()
        successes = 0
        for win, (x, y, w, h) in zip(windows, positions):
            try:
                if system == "Linux":
                    ds = cls.get_display_server()
                    if ds == "Hyprland":
                        addr = win["id"]
                        cls._run_cmd(
                            [
                                "hyprctl",
                                "dispatch",
                                "movewindowpixel",
                                f"exact {x} {y},address:{addr}",
                            ]
                        )
                        cls._run_cmd(
                            [
                                "hyprctl",
                                "dispatch",
                                "resizewindowpixel",
                                f"exact {w} {h},address:{addr}",
                            ]
                        )
                        successes += 1
                    elif shutil.which("wmctrl"):
                        cls._run_cmd(
                            [
                                "wmctrl",
                                "-i",
                                "-r",
                                win["id"],
                                "-e",
                                f"0,{x},{y},{w},{h}",
                            ]
                        )
                        successes += 1
                elif system == "Darwin":
                    app = win.get("app", "")
                    scpt = f'''tell application "System Events" to tell process "{app}"
set position of window 1 to {{{x}, {y}}}
set size of window 1 to {{{w}, {h}}}
end tell'''
                    cls._run_cmd(["osascript", "-e", scpt])
                    successes += 1
                elif system == "Windows":
                    ok, _ = cls._win32_window_action(
                        win["id"], "move_resize", x=x, y=y, width=w, height=h
                    )
                    if ok:
                        successes += 1
            except Exception:
                continue

        return {
            "success": successes > 0,
            "message": f"Tiled {successes}/{count} windows ({layout})",
        }

    @classmethod
    def list_monitors(cls) -> List[Dict[str, Any]]:
        system = platform.system()
        monitors = []
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    stdout, _, code = cls._run_cmd(["hyprctl", "monitors", "-j"])
                    if code == 0 and stdout:
                        for m in json.loads(stdout):
                            monitors.append(
                                {
                                    "id": str(m.get("id", 0)),
                                    "name": m.get("name", ""),
                                    "width": m.get("width", 0),
                                    "height": m.get("height", 0),
                                    "x": m.get("x", 0),
                                    "y": m.get("y", 0),
                                    "primary": m.get("focused", False),
                                    "scale": m.get("scale", 1.0),
                                }
                            )
                elif ds == "Sway":
                    stdout, _, code = cls._run_cmd(["swaymsg", "-t", "get_outputs"])
                    if code == 0 and stdout:
                        for i, m in enumerate(json.loads(stdout)):
                            monitors.append(
                                {
                                    "id": str(i),
                                    "name": m.get("name", ""),
                                    "width": m.get("rect", {}).get("width", 0),
                                    "height": m.get("rect", {}).get("height", 0),
                                    "x": m.get("rect", {}).get("x", 0),
                                    "y": m.get("rect", {}).get("y", 0),
                                    "primary": m.get("focused", False),
                                    "scale": m.get("scale", 1.0),
                                }
                            )
                elif shutil.which("xrandr"):
                    stdout, _, code = cls._run_cmd(["xrandr", "--query"])
                    if code == 0:
                        for line in stdout.splitlines():
                            if " connected" in line:
                                res_match = re.search(
                                    r"(\d+)x(\d+)\+(\d+)\+(\d+)", line
                                )
                                if res_match:
                                    monitors.append(
                                        {
                                            "id": str(len(monitors)),
                                            "name": line.split()[0],
                                            "width": int(res_match.group(1)),
                                            "height": int(res_match.group(2)),
                                            "x": int(res_match.group(3)),
                                            "y": int(res_match.group(4)),
                                            "primary": "primary" in line,
                                            "scale": 1.0,
                                        }
                                    )

            elif system == "Darwin":
                stdout, _, code = cls._run_cmd(
                    ["system_profiler", "SPDisplaysDataType"], timeout=15
                )
                if code == 0 and stdout:
                    for line in stdout.splitlines():
                        if "Resolution:" in line:
                            rm = re.search(r"(\d+) x (\d+)", line)
                            if rm:
                                monitors.append(
                                    {
                                        "id": str(len(monitors)),
                                        "name": f"Display {len(monitors) + 1}",
                                        "width": int(rm.group(1)),
                                        "height": int(rm.group(2)),
                                        "x": 0,
                                        "y": 0,
                                        "primary": len(monitors) == 0,
                                        "scale": 1.0,
                                    }
                                )

            elif system == "Windows":
                ps = """
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Screen]::AllScreens | ForEach-Object {
    "$($_.DeviceName)|||$($_.Bounds.Width)|||$($_.Bounds.Height)|||$($_.Bounds.X)|||$($_.Bounds.Y)|||$($_.Primary)"
}"""
                stdout, _, code = cls._run_powershell(ps)
                if code == 0 and stdout:
                    for i, line in enumerate(stdout.splitlines()):
                        if "|||" in line:
                            parts = line.split("|||")
                            if len(parts) >= 6:
                                monitors.append(
                                    {
                                        "id": str(i),
                                        "name": parts[0],
                                        "width": int(parts[1]),
                                        "height": int(parts[2]),
                                        "x": int(parts[3]),
                                        "y": int(parts[4]),
                                        "primary": parts[5].lower() == "true",
                                        "scale": 1.0,
                                    }
                                )
        except Exception as e:
            log(f"Error listing monitors: {e}", "ERROR")

        return (
            monitors
            if monitors
            else [
                {
                    "id": "0",
                    "name": "Default",
                    "width": 1920,
                    "height": 1080,
                    "x": 0,
                    "y": 0,
                    "primary": True,
                    "scale": 1.0,
                }
            ]
        )


# ============================================================
# TODO MANAGER
# ============================================================


class TodoManager:
    def __init__(self, base_dir: Path):
        self.todo_path = base_dir / TODO_FILE
        self.todo_path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> Dict:
        if self.todo_path.exists():
            try:
                return json.loads(self.todo_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"tasks": [], "metadata": {"created": datetime.now().isoformat()}}

    def _save(self, data: Dict):
        data["metadata"]["last_updated"] = datetime.now().isoformat()
        self.todo_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add_task(
        self,
        title,
        description="",
        priority="medium",
        dependencies=None,
        assigned_files=None,
    ):
        data = self._load()
        now = datetime.now().isoformat()
        task = {
            "id": hashlib.md5(f"{title}{now}".encode()).hexdigest()[:12],
            "title": title,
            "description": description,
            "status": "pending",
            "priority": priority,
            "created_at": now,
            "updated_at": now,
            "dependencies": dependencies or [],
            "subtasks": [],
            "notes": [],
            "assigned_files": assigned_files or [],
        }
        data["tasks"].append(task)
        self._save(data)
        return task

    def update_task(self, task_id, **kwargs):
        data = self._load()
        for task in data["tasks"]:
            if task["id"] == task_id:
                for k, v in kwargs.items():
                    if v is not None:
                        if k == "add_note":
                            task["notes"].append(
                                {"content": v, "timestamp": datetime.now().isoformat()}
                            )
                        elif k == "add_subtask":
                            task["subtasks"].append(
                                {
                                    "id": f"{task_id}_s{len(task['subtasks'])}",
                                    "content": v,
                                    "completed": False,
                                }
                            )
                        else:
                            task[k] = v
                task["updated_at"] = datetime.now().isoformat()
                self._save(data)
                return task
        return None

    def delete_task(self, task_id):
        data = self._load()
        orig = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]
        if len(data["tasks"]) < orig:
            self._save(data)
            return True
        return False

    def get_task(self, task_id):
        for t in self._load()["tasks"]:
            if t["id"] == task_id:
                return t
        return None

    def list_tasks(self, status=None, priority=None, include_completed=True):
        tasks = self._load()["tasks"]
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        if priority:
            tasks = [t for t in tasks if t["priority"] == priority]
        if not include_completed:
            tasks = [t for t in tasks if t["status"] != "completed"]
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tasks.sort(key=lambda t: (order.get(t["priority"], 99), t["created_at"]))
        return tasks

    def get_summary(self):
        tasks = self._load()["tasks"]
        return {
            "total": len(tasks),
            "pending": sum(1 for t in tasks if t["status"] == "pending"),
            "in_progress": sum(1 for t in tasks if t["status"] == "in_progress"),
            "completed": sum(1 for t in tasks if t["status"] == "completed"),
            "blocked": sum(1 for t in tasks if t["status"] == "blocked"),
        }


# ============================================================
# MEMORY MANAGER
# ============================================================


class MemoryManager:
    """Persistent and temporary memory for the agent."""

    def __init__(self, base_dir: Path):
        self.persistent_path = base_dir / MEMORY_FILE
        self.temp_path = base_dir / TEMP_MEMORY_FILE
        self.persistent_path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self, path: Path) -> Dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"entries": {}}

    def _save(self, path: Path, data: Dict):
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def store(self, key: str, value: str, persistent: bool = True):
        path = self.persistent_path if persistent else self.temp_path
        data = self._load(path)
        data["entries"][key] = {
            "value": value,
            "stored_at": datetime.now().isoformat(),
            "type": "persistent" if persistent else "temporary",
        }
        self._save(path, data)

    def recall(self, key: str) -> Optional[str]:
        # Check persistent first, then temporary
        for path in [self.persistent_path, self.temp_path]:
            data = self._load(path)
            if key in data["entries"]:
                return data["entries"][key]["value"]
        return None

    def list_memories(self, persistent: bool = True) -> Dict[str, Any]:
        path = self.persistent_path if persistent else self.temp_path
        data = self._load(path)
        return {
            k: v["value"][:100] + ("..." if len(v["value"]) > 100 else "")
            for k, v in data["entries"].items()
        }

    def forget(self, key: str) -> bool:
        for path in [self.persistent_path, self.temp_path]:
            data = self._load(path)
            if key in data["entries"]:
                del data["entries"][key]
                self._save(path, data)
                return True
        return False

    def clear_temp(self) -> int:
        data = self._load(self.temp_path)
        count = len(data["entries"])
        self._save(self.temp_path, {"entries": {}})
        return count


# ============================================================
# FILE EDITOR
# ============================================================


class FileEditor:
    @staticmethod
    def read_lines(file_path: Path) -> List[str]:
        return file_path.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def write_lines(file_path: Path, lines: List[str]):
        file_path.write_text("\n".join(lines), encoding="utf-8")

    @classmethod
    def replace_lines(
        cls, file_path: Path, start_line: int, end_line: int, new_content: str
    ):
        lines = cls.read_lines(file_path)
        total = len(lines)
        start_line = max(1, start_line)
        end_line = min(total, end_line)
        if start_line > end_line:
            return {
                "success": False,
                "message": f"Invalid range: {start_line}-{end_line}",
            }
        replaced = lines[start_line - 1 : end_line]
        new_lines = new_content.splitlines()
        final = lines[: start_line - 1] + new_lines + lines[end_line:]
        cls.write_lines(file_path, final)
        return {
            "success": True,
            "message": f"Replaced lines {start_line}-{end_line}",
            "replaced_content": "\n".join(replaced),
            "lines_before": total,
            "lines_after": len(final),
        }

    @classmethod
    def insert_at_line(
        cls, file_path: Path, line_number: int, content: str, mode: str = "before"
    ):
        lines = cls.read_lines(file_path)
        total = len(lines)
        line_number = max(1, min(line_number, total + 1))
        idx = line_number - 1 if mode == "before" else line_number
        new_lines = content.splitlines()
        final = lines[:idx] + new_lines + lines[idx:]
        cls.write_lines(file_path, final)
        return {
            "success": True,
            "message": f"Inserted {len(new_lines)} lines at {line_number} ({mode})",
            "lines_before": total,
            "lines_after": len(final),
        }

    @classmethod
    def delete_lines(cls, file_path: Path, start_line: int, end_line: int):
        lines = cls.read_lines(file_path)
        total = len(lines)
        start_line = max(1, start_line)
        end_line = min(total, end_line)
        deleted = lines[start_line - 1 : end_line]
        final = lines[: start_line - 1] + lines[end_line:]
        cls.write_lines(file_path, final)
        return {
            "success": True,
            "message": f"Deleted lines {start_line}-{end_line}",
            "deleted_content": "\n".join(deleted),
            "lines_before": total,
            "lines_after": len(final),
        }

    @classmethod
    def find_and_replace(
        cls,
        file_path: Path,
        search: str,
        replace: str,
        use_regex=False,
        case_sensitive=True,
        replace_all=True,
    ):
        content = file_path.read_text(encoding="utf-8")
        if use_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(search, flags)
            count_func = lambda: len(pattern.findall(content))
            matches = count_func()
            if replace_all:
                new_content, count = pattern.subn(replace, content)
            else:
                new_content, count = pattern.subn(replace, content, count=1)
        else:
            if case_sensitive:
                matches = content.count(search)
                if replace_all:
                    new_content = content.replace(search, replace)
                    count = matches
                else:
                    new_content = content.replace(search, replace, 1)
                    count = min(1, matches)
            else:
                pattern = re.compile(re.escape(search), re.IGNORECASE)
                matches = len(pattern.findall(content))
                if replace_all:
                    new_content = pattern.sub(replace, content)
                    count = matches
                else:
                    new_content = pattern.sub(replace, content, count=1)
                    count = min(1, matches)

        if count > 0:
            file_path.write_text(new_content, encoding="utf-8")
        return {"success": True, "matches_found": matches, "replacements_made": count}


# Initialize managers
todo_manager = None
memory_manager = None


def get_todo_manager() -> TodoManager:
    global todo_manager
    if todo_manager is None:
        todo_manager = TodoManager(BASE_DIR)
    return todo_manager


def get_memory_manager() -> MemoryManager:
    global memory_manager
    if memory_manager is None:
        memory_manager = MemoryManager(BASE_DIR)
    return memory_manager


# ============================================================
# MCP TOOLS
# ============================================================

# --- SYSTEM ---


@mcp.tool()
def get_system_info() -> str:
    """Returns comprehensive system information including OS, CPU, memory, GPU, disk, and display server."""
    try:
        os_name, os_version, arch, hostname = SystemDetector.get_os_info()
        cpu = SystemDetector.get_cpu_info()
        mem = SystemDetector.get_memory_info()
        gpu = SystemDetector.get_gpu_info()
        disk = SystemDetector.get_disk_info()
        ds = SystemDetector.get_display_server()

        out = [
            f"=== SYSTEM INFO ===",
            f"OS: {os_name} {os_version} ({arch})",
            f"Host: {hostname} | Python: {platform.python_version()} | Display: {ds}",
            f"\nCPU: {cpu.get('model', 'Unknown')} ({cpu.get('logical_cores')} cores, {cpu.get('cpu_percent')}%)",
            f"RAM: {mem.get('total', {}).get('value', '?')} {mem.get('total', {}).get('unit', '')} ({mem.get('percent', '?')}% used)",
        ]
        for i, g in enumerate(gpu, 1):
            out.append(f"GPU {i}: {g.get('model', g.get('info', 'Unknown'))}")
        for d in disk:
            out.append(f"Disk: {d['device']} {d['total_gb']}GB ({d['percent']}% used)")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# --- WINDOW MANAGEMENT ---


@mcp.tool()
def window_list() -> str:
    """List all open windows with title, app, position, and size."""
    try:
        windows = WindowManager.list_windows()
        if not windows:
            return "No visible windows found."
        out = [f"=== OPEN WINDOWS ({WindowManager.get_display_server()}) ==="]
        for i, w in enumerate(windows, 1):
            size = w.get("size", {})
            pos = w.get("position", {})
            out.append(f"[{i}] {w.get('title', 'N/A')}")
            out.append(f"    App: {w.get('app', 'N/A')} | ID: {w.get('id', 'N/A')}")
            if size:
                out.append(
                    f"    Size: {size.get('width', 0)}x{size.get('height', 0)} | Pos: ({pos.get('x', 0)}, {pos.get('y', 0)})"
                )
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def window_focus(query: str) -> str:
    """Focus a window by title or app name substring match."""
    r = WindowManager.focus_window(query)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def window_minimize(query: str) -> str:
    """Minimize a window by title or app name."""
    r = WindowManager.minimize_window(query)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def window_maximize(query: str) -> str:
    """Maximize/toggle maximize for a window."""
    r = WindowManager.maximize_window(query)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def window_close(query: str) -> str:
    """Close a window gracefully by title or app name."""
    r = WindowManager.close_window(query)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def window_move(query: str, x: int, y: int) -> str:
    """Move a window to specific screen coordinates."""
    r = WindowManager.move_window(query, x, y)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def window_resize(query: str, width: int, height: int) -> str:
    """Resize a window to specific dimensions."""
    r = WindowManager.resize_window(query, width, height)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def window_snap(query: str, direction: str) -> str:
    """Snap a window to a screen edge. Directions: left, right, top, bottom, top_left, top_right, bottom_left, bottom_right, center."""
    r = WindowManager.snap_window(query, direction)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def window_always_on_top(query: str, enable: bool = True) -> str:
    """Set a window to always stay on top."""
    r = WindowManager.set_always_on_top(query, enable)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def window_tile(layout: str = "horizontal") -> str:
    """Tile all visible windows. Layouts: horizontal, vertical, grid."""
    r = WindowManager.tile_windows(layout)
    return f"{'✓' if r['success'] else '✗'} {r['message']}"


@mcp.tool()
def workspace_list() -> str:
    """List all workspaces/virtual desktops."""
    try:
        ws = WindowManager.list_workspaces()
        if not ws:
            return "No workspaces found."
        out = ["=== WORKSPACES ==="]
        for w in ws:
            active = " [ACTIVE]" if w.get("active") else ""
            out.append(
                f"  [{w.get('id')}] {w.get('name', 'Unnamed')}{active} ({w.get('windows', 0)} windows)"
            )
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def monitor_list() -> str:
    """List all connected monitors/displays."""
    try:
        monitors = WindowManager.list_monitors()
        out = ["=== MONITORS ==="]
        for m in monitors:
            primary = " [PRIMARY]" if m.get("primary") else ""
            out.append(
                f"  [{m['id']}] {m['name']}{primary}: {m['width']}x{m['height']} at ({m['x']}, {m['y']})"
            )
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# --- TODO ---


@mcp.tool()
def todo_add(
    title: str,
    description: str = "",
    priority: str = "medium",
    dependencies: str = "",
    assigned_files: str = "",
) -> str:
    """Add a task. Priority: low/medium/high/critical."""
    try:
        dep_list = (
            [d.strip() for d in dependencies.split(",") if d.strip()]
            if dependencies
            else []
        )
        file_list = (
            [f.strip() for f in assigned_files.split(",") if f.strip()]
            if assigned_files
            else []
        )
        if priority.lower() not in ["low", "medium", "high", "critical"]:
            priority = "medium"
        task = get_todo_manager().add_task(
            title, description, priority.lower(), dep_list, file_list
        )
        return f"✓ Task Created: [{task['id']}] {task['title']} ({task['priority']})"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def todo_update(
    task_id: str,
    status: str = "",
    title: str = "",
    priority: str = "",
    add_note: str = "",
    add_subtask: str = "",
) -> str:
    """Update a task. Status: pending/in_progress/completed/blocked/cancelled."""
    try:
        kwargs = {}
        if status:
            kwargs["status"] = status.lower()
        if title:
            kwargs["title"] = title
        if priority:
            kwargs["priority"] = priority.lower()
        if add_note:
            kwargs["add_note"] = add_note
        if add_subtask:
            kwargs["add_subtask"] = add_subtask
        task = get_todo_manager().update_task(task_id, **kwargs)
        if not task:
            return f"Task not found: {task_id}"
        return f"✓ Updated: [{task['id']}] {task['title']} -> {task['status']}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def todo_list(
    status: str = "", priority: str = "", include_completed: bool = True
) -> str:
    """List tasks with optional filtering."""
    try:
        mgr = get_todo_manager()
        tasks = mgr.list_tasks(status or None, priority or None, include_completed)
        if not tasks:
            return "No tasks found."
        summary = mgr.get_summary()
        icons = {
            "pending": "○",
            "in_progress": "◐",
            "completed": "●",
            "blocked": "⊘",
            "cancelled": "✕",
        }
        pri_icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        out = [
            f"=== TODO ({summary['total']} total, {summary['pending']} pending, {summary['in_progress']} active) ==="
        ]
        for t in tasks:
            out.append(
                f"{icons.get(t['status'], '○')} {pri_icons.get(t['priority'], '⚪')} [{t['id']}] {t['title']}"
            )
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def todo_get(task_id: str) -> str:
    """Get detailed info about a task."""
    task = get_todo_manager().get_task(task_id)
    if not task:
        return f"Task not found: {task_id}"
    out = [
        f"=== {task['title']} ===",
        f"ID: {task['id']} | Status: {task['status']} | Priority: {task['priority']}",
        f"Created: {task['created_at'][:19]}",
    ]
    if task["description"]:
        out.append(f"\n{task['description']}")
    if task["notes"]:
        out.append("\nNotes:")
        for n in task["notes"]:
            out.append(f"  [{n['timestamp'][:10]}] {n['content']}")
    if task["subtasks"]:
        out.append("\nSubtasks:")
        for s in task["subtasks"]:
            check = "✓" if s.get("completed") else "○"
            out.append(f"  {check} {s['content']}")
    return "\n".join(out)


@mcp.tool()
def todo_delete(task_id: str) -> str:
    """Delete a task."""
    return (
        f"✓ Deleted"
        if get_todo_manager().delete_task(task_id)
        else f"Not found: {task_id}"
    )


# --- MEMORY ---


@mcp.tool()
def memory_store(key: str, value: str, persistent: bool = True) -> str:
    """Store information in agent memory. Use persistent=True for long-term, False for session-only."""
    get_memory_manager().store(key, value, persistent)
    return f"✓ Stored '{key}' ({'persistent' if persistent else 'temporary'})"


@mcp.tool()
def memory_recall(key: str) -> str:
    """Recall stored information by key."""
    val = get_memory_manager().recall(key)
    return val if val else f"No memory found for key: '{key}'"


@mcp.tool()
def memory_list(persistent: bool = True) -> str:
    """List all stored memories."""
    memories = get_memory_manager().list_memories(persistent)
    if not memories:
        return "No memories stored."
    out = [f"=== {'Persistent' if persistent else 'Temporary'} Memory ==="]
    for k, v in memories.items():
        out.append(f"  [{k}]: {v}")
    return "\n".join(out)


@mcp.tool()
def memory_forget(key: str) -> str:
    """Remove a memory entry."""
    return f"✓ Forgotten" if get_memory_manager().forget(key) else f"Not found: '{key}'"


@mcp.tool()
def memory_clear_temp() -> str:
    """Clear all temporary memories."""
    count = get_memory_manager().clear_temp()
    return f"✓ Cleared {count} temporary memories"


# --- FILESYSTEM ---


@mcp.tool()
def list_content(sub_path: str = ".") -> str:
    """Lists the contents of a directory."""
    try:
        target = get_secure_path(sub_path)
        if not target.exists():
            return f"Error: '{sub_path}' does not exist."
        if not target.is_dir():
            return f"Error: '{sub_path}' is a file."
        items = []
        for item in target.iterdir():
            if item.is_dir():
                items.append(f"[DIR]  {item.name}/")
            else:
                size = item.stat().st_size
                s = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"
                items.append(f"[FILE] {item.name} ({s})")
        return "\n".join(sorted(items)) if items else f"'{sub_path}' is empty."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_directory_tree(sub_path: str = ".") -> str:
    """Returns a visual tree of a directory."""
    try:
        target = get_secure_path(sub_path)
        if not target.exists() or not target.is_dir():
            return f"Error: '{sub_path}' is not a valid directory."
        tree_lines = [f"{target.name}/"]
        file_count = dir_count = 0

        def build(d: Path, prefix=""):
            nonlocal file_count, dir_count
            try:
                items = sorted(
                    [i for i in d.iterdir() if i.name not in IGNORE_DIRS],
                    key=lambda x: (x.is_file(), x.name.lower()),
                )
            except PermissionError:
                tree_lines.append(f"{prefix}[ACCESS DENIED]")
                return
            for i, item in enumerate(items):
                last = i == len(items) - 1
                conn = "└── " if last else "├── "
                ext = "    " if last else "│   "
                if item.is_dir():
                    tree_lines.append(f"{prefix}{conn}{item.name}/")
                    dir_count += 1
                    build(item, prefix + ext)
                else:
                    tree_lines.append(f"{prefix}{conn}{item.name}")
                    file_count += 1

        build(target)
        tree_lines.append(f"\n{dir_count} dirs, {file_count} files")
        return truncate_output("\n".join(tree_lines))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_directory(path: str) -> str:
    """Creates a new directory."""
    try:
        target = get_secure_path(path)
        if target.exists():
            return f"Already exists: '{path}'"
        target.mkdir(parents=True, exist_ok=True)
        return f"✓ Created: {path}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def read_file(file_path: str) -> str:
    """Reads the content of a file."""
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: '{file_path}' not found."
        if not target.is_file():
            return f"Error: '{file_path}' is not a file."
        content = target.read_text(encoding="utf-8")
        return truncate_output(content)
    except UnicodeDecodeError:
        return f"Error: '{file_path}' is a binary file."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def read_file_segment(file_path: str, start_line: int, end_line: int) -> str:
    """Reads specific lines from a file (1-indexed)."""
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: '{file_path}' not found."
        lines = target.read_text(encoding="utf-8").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line)
        segment = lines[start - 1 : end]
        return "\n".join(f"{start + i:4d} | {line}" for i, line in enumerate(segment))
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def read_file_skeleton(file_path: str) -> str:
    """Returns the skeleton (classes, functions) of a Python file."""
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: '{file_path}' not found."
        if target.suffix != ".py":
            return "Error: Only .py files supported."
        tree = ast.parse(target.read_text(encoding="utf-8"))
        lines = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                lines.append(f"def {node.name}({', '.join(args)}):")
                if ast.get_docstring(node):
                    lines.append(f'    """{ast.get_docstring(node)}"""')
                lines.append("    ...\n")
            elif isinstance(node, ast.ClassDef):
                lines.append(f"class {node.name}:")
                if ast.get_docstring(node):
                    lines.append(f'    """{ast.get_docstring(node)}"""')
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = [a.arg for a in item.args.args]
                        lines.append(f"    def {item.name}({', '.join(args)}): ...")
                lines.append("")
        return "\n".join(lines) if lines else "No definitions found."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def write_file(file_path: str, content: str) -> str:
    """Writes content to a file. Creates parent dirs if needed."""
    try:
        target = get_secure_path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        sanitized = sanitize_input(content, max_length=10_000_000)
        target.write_text(sanitized, encoding="utf-8")
        return f"✓ Wrote {len(sanitized)} chars to {file_path}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def append_to_file(path: str, content: str) -> str:
    """Appends text to end of an existing file."""
    try:
        target = get_secure_path(path)
        if not target.exists():
            return f"Error: '{path}' not found."
        prefix = "\n" if target.stat().st_size > 0 else ""
        with open(target, "a", encoding="utf-8") as f:
            f.write(prefix + content)
        return f"✓ Appended to {path}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def edit_file(file_path: str, start_line: int, end_line: int, new_content: str) -> str:
    """Replace a range of lines in a file (1-indexed)."""
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: '{file_path}' not found."
        r = FileEditor.replace_lines(target, start_line, end_line, new_content)
        return f"{'✓' if r['success'] else '✗'} {r['message']} ({r.get('lines_before', 0)} -> {r.get('lines_after', 0)} lines)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def edit_insert(
    file_path: str, line_number: int, content: str, mode: str = "before"
) -> str:
    """Insert content before or after a specific line."""
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: '{file_path}' not found."
        r = FileEditor.insert_at_line(target, line_number, content, mode)
        return f"{'✓' if r['success'] else '✗'} {r['message']}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def edit_delete_lines(file_path: str, start_line: int, end_line: int) -> str:
    """Delete a range of lines from a file."""
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: '{file_path}' not found."
        r = FileEditor.delete_lines(target, start_line, end_line)
        return f"{'✓' if r['success'] else '✗'} {r['message']}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def edit_find_replace(
    file_path: str,
    search: str,
    replace: str,
    use_regex: bool = False,
    case_sensitive: bool = True,
    replace_all: bool = True,
) -> str:
    """Find and replace text in a file."""
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: '{file_path}' not found."
        r = FileEditor.find_and_replace(
            target, search, replace, use_regex, case_sensitive, replace_all
        )
        return f"✓ Found {r['matches_found']}, replaced {r['replacements_made']}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def search_files(pattern: str, sub_path: str = ".") -> str:
    """Search for a regex pattern across all files in a directory."""
    try:
        target = get_secure_path(sub_path)
        if not target.exists():
            return f"Error: '{sub_path}' not found."
        try:
            regex = re.compile(pattern)
        except re.error:
            return f"Invalid regex: '{pattern}'"
        results = []
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for file in files:
                fp = Path(root) / file
                try:
                    content = fp.read_text(encoding="utf-8", errors="ignore")
                    for i, line in enumerate(content.splitlines(), 1):
                        if regex.search(line):
                            results.append(
                                f"{fp.relative_to(target)}:{i}: {line.strip()}"
                            )
                            if len(results) >= 100:
                                results.append("... (truncated at 100)")
                                return "\n".join(results)
                except Exception:
                    continue
        return "\n".join(results) if results else f"No matches for '{pattern}'"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def copy_item(source_path: str, destination_path: str) -> str:
    """Copy a file or directory."""
    try:
        src = get_secure_path(source_path)
        dest = get_secure_path(destination_path)
        if not src.exists():
            return f"Error: Source '{source_path}' not found."
        if dest.exists():
            return f"Error: Destination '{destination_path}' already exists."
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        return f"✓ Copied '{source_path}' -> '{destination_path}'"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def move_item(source_path: str, destination_path: str) -> str:
    """Move or rename a file or directory."""
    try:
        src = get_secure_path(source_path)
        dest = get_secure_path(destination_path)
        if not src.exists():
            return f"Error: Source not found."
        if dest.exists():
            return f"Error: Destination already exists."
        shutil.move(str(src), str(dest))
        return f"✓ Moved '{source_path}' -> '{destination_path}'"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def delete_item(path: str) -> str:
    """Delete a file or directory."""
    try:
        target = get_secure_path(path)
        if not target.exists():
            return f"Error: '{path}' not found."
        if target == BASE_DIR:
            return "Error: Cannot delete the root workspace."
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return f"✓ Deleted: {path}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_metadata(path: str) -> str:
    """Get metadata for a file or directory."""
    try:
        target = get_secure_path(path)
        if not target.exists():
            return f"Error: '{path}' not found."
        s = target.stat()

        def fmt(sz):
            for u in ["B", "KB", "MB", "GB"]:
                if sz < 1024:
                    return f"{sz:.1f} {u}"
                sz /= 1024
            return f"{sz:.1f} TB"

        return (
            f"=== {target.name} ===\n"
            f"Type: {'Dir' if target.is_dir() else 'File'}\n"
            f"Size: {fmt(s.st_size)}\n"
            f"Modified: {datetime.fromtimestamp(s.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Permissions: {stat.filemode(s.st_mode)}\n"
            f"Path: {target}"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def open_in_desktop(path: str = ".") -> str:
    """Open a file/directory with the OS default application."""
    try:
        target = get_secure_path(path)
        if not target.exists():
            return f"Error: '{path}' not found."
        system = platform.system()
        if system == "Darwin":
            subprocess.run(["open", str(target)], check=True)
        elif system == "Windows":
            os.startfile(str(target))
        elif system == "Linux":
            if shutil.which("xdg-open"):
                subprocess.run(["xdg-open", str(target)], check=True)
            else:
                return "Error: xdg-open not found."
        return f"✓ Opened '{path}'"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def execute_command(command: str, timeout: int = 30) -> str:
    """Execute a terminal command in the workspace directory."""
    try:
        timeout = max(1, min(timeout, 120))
        if not command.strip():
            return "Error: Empty command."

        # Security checks
        if is_dangerous_command(command):
            return f"⚠️ DANGEROUS COMMAND DETECTED: '{command}'. This requires explicit user approval via the UI."
        if command_accesses_outside_basedir(command):
            return f"⚠️ SECURITY: Command appears to access paths outside the workspace. Denied."

        process = subprocess.run(
            command,
            shell=True,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        parts = []
        if process.stdout.strip():
            parts.append(f"STDOUT:\n{process.stdout.strip()}")
        if process.stderr.strip():
            parts.append(f"STDERR:\n{process.stderr.strip()}")
        if not parts:
            parts.append("(No output)")
        full = "\n\n".join(parts) + f"\n[Exit: {process.returncode}]"
        return truncate_output(full)
    except subprocess.TimeoutExpired:
        return f"Error: Timed out after {timeout}s."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """Search the internet."""
    if not HAS_DDGS:
        return "Error: duckduckgo-search package not installed."
    try:
        max_results = max(1, min(max_results, 10))
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(r)
        if not results:
            return f"No results for: '{query}'"
        out = [f"=== Search: '{query}' ==="]
        for i, r in enumerate(results, 1):
            out.append(f"\n[{i}] {r.get('title', 'No Title')}")
            out.append(f"    {r.get('href', 'No URL')}")
            out.append(f"    {r.get('body', '')[:150]}...")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# SMART WINDOW ORGANIZATION
# ============================================================


@mcp.tool()
def window_organize(layout: str, window_queries: str = "", gap: int = 0) -> str:
    """Organize specific or all windows into a smart layout. Returns positions used for verification.

    Args:
        layout: Layout type - 'side_by_side', 'grid', 'stack_left_right', 'main_plus_stack',
                'columns_2', 'columns_3', 'rows_2', 'rows_3', 'cascade'
        window_queries: Comma-separated window title/app substrings to organize. Empty = all windows.
        gap: Pixel gap between windows (default 0).

    Returns:
        Detailed report of each window's new position for verification.
    """
    try:
        all_windows = WindowManager.list_windows()
        if not all_windows:
            return "Error: No windows found."

        # Filter windows if queries provided
        if window_queries.strip():
            queries = [
                q.strip().lower() for q in window_queries.split(",") if q.strip()
            ]
            windows = []
            for q in queries:
                for w in all_windows:
                    if q in w.get("title", "").lower() or q in w.get("app", "").lower():
                        if w not in windows:
                            windows.append(w)
                            break
            if not windows:
                return f"Error: No windows matched queries: {window_queries}"
        else:
            windows = all_windows

        monitors = WindowManager.list_monitors()
        mon = (
            monitors[0] if monitors else {"width": 1920, "height": 1080, "x": 0, "y": 0}
        )
        sw, sh = mon["width"], mon["height"]
        sx, sy = mon.get("x", 0), mon.get("y", 0)

        # Reserve space for taskbar (approximate)
        taskbar_h = 40
        if platform.system() == "Windows":
            taskbar_h = 48
        elif platform.system() == "Darwin":
            taskbar_h = 25
            sy += taskbar_h  # macOS menu bar is at top
        sh -= taskbar_h

        count = len(windows)
        g = gap
        positions = []

        if layout == "side_by_side" or layout == "columns_2":
            # Two columns, split evenly
            cols = min(count, 2)
            per_col = (count + cols - 1) // cols
            cw = (sw - g * (cols + 1)) // cols
            for i, _ in enumerate(windows):
                col = i % cols
                row = i // cols
                rh = (sh - g * (per_col + 1)) // per_col
                x = sx + g + col * (cw + g)
                y = sy + g + row * (rh + g)
                positions.append((x, y, cw, rh))

        elif layout == "columns_3":
            cols = min(count, 3)
            per_col = (count + cols - 1) // cols
            cw = (sw - g * (cols + 1)) // cols
            for i, _ in enumerate(windows):
                col = i % cols
                row = i // cols
                rh = (sh - g * (per_col + 1)) // per_col
                x = sx + g + col * (cw + g)
                y = sy + g + row * (rh + g)
                positions.append((x, y, cw, rh))

        elif layout == "rows_2":
            rows = min(count, 2)
            per_row = (count + rows - 1) // rows
            rh = (sh - g * (rows + 1)) // rows
            for i, _ in enumerate(windows):
                row = i % rows
                col = i // rows
                cw = (sw - g * (per_row + 1)) // per_row
                x = sx + g + col * (cw + g)
                y = sy + g + row * (rh + g)
                positions.append((x, y, cw, rh))

        elif layout == "rows_3":
            rows = min(count, 3)
            per_row = (count + rows - 1) // rows
            rh = (sh - g * (rows + 1)) // rows
            for i, _ in enumerate(windows):
                row = i % rows
                col = i // rows
                cw = (sw - g * (per_row + 1)) // per_row
                x = sx + g + col * (cw + g)
                y = sy + g + row * (rh + g)
                positions.append((x, y, cw, rh))

        elif layout == "grid":
            import math

            cols = math.ceil(math.sqrt(count))
            rows = math.ceil(count / cols)
            cw = (sw - g * (cols + 1)) // cols
            rh = (sh - g * (rows + 1)) // rows
            for i in range(count):
                r, c = divmod(i, cols)
                x = sx + g + c * (cw + g)
                y = sy + g + r * (rh + g)
                positions.append((x, y, cw, rh))

        elif layout == "main_plus_stack":
            # First window takes left 60%, rest stacked on right 40%
            main_w = int((sw - g * 3) * 0.6)
            stack_w = sw - main_w - g * 3
            positions.append((sx + g, sy + g, main_w, sh - g * 2))
            stack_count = count - 1
            if stack_count > 0:
                stack_h = (sh - g * (stack_count + 1)) // stack_count
                for i in range(stack_count):
                    x = sx + main_w + g * 2
                    y = sy + g + i * (stack_h + g)
                    positions.append((x, y, stack_w, stack_h))

        elif layout == "stack_left_right":
            # Left stack and right stack
            half = (count + 1) // 2
            other = count - half
            lw = (sw - g * 3) // 2
            rw = lw
            lh = (sh - g * (half + 1)) // half if half > 0 else sh
            for i in range(half):
                positions.append((sx + g, sy + g + i * (lh + g), lw, lh))
            if other > 0:
                rh = (sh - g * (other + 1)) // other
                for i in range(other):
                    positions.append((sx + lw + g * 2, sy + g + i * (rh + g), rw, rh))

        elif layout == "cascade":
            cascade_offset = 30
            cw = int(sw * 0.6)
            ch = int(sh * 0.6)
            for i in range(count):
                x = sx + g + i * cascade_offset
                y = sy + g + i * cascade_offset
                positions.append((x, y, cw, ch))

        else:
            return f"Error: Unknown layout '{layout}'. Use: side_by_side, grid, main_plus_stack, stack_left_right, columns_2, columns_3, rows_2, rows_3, cascade"

        # Apply positions
        system = platform.system()
        report = [
            f"=== ORGANIZED {count} WINDOWS ({layout}) ===",
            f"Screen: {sw}x{sh} at ({sx},{sy})",
        ]
        successes = 0

        for win, (x, y, w, h) in zip(windows, positions):
            title = win.get("title", "?")[:40]
            try:
                if system == "Linux":
                    ds = WindowManager.get_display_server()
                    if ds == "Hyprland":
                        addr = win["id"]
                        # Unfloat first, then float and position
                        WindowManager._run_cmd(
                            ["hyprctl", "dispatch", "focuswindow", f"address:{addr}"]
                        )
                        WindowManager._run_cmd(
                            ["hyprctl", "dispatch", "togglefloating", f"address:{addr}"]
                        )
                        WindowManager._run_cmd(
                            [
                                "hyprctl",
                                "dispatch",
                                "movewindowpixel",
                                f"exact {x} {y},address:{addr}",
                            ]
                        )
                        WindowManager._run_cmd(
                            [
                                "hyprctl",
                                "dispatch",
                                "resizewindowpixel",
                                f"exact {w} {h},address:{addr}",
                            ]
                        )
                        successes += 1
                    elif ds == "Sway":
                        wid = win["id"]
                        WindowManager._run_cmd(
                            ["swaymsg", f'[con_id="{wid}"]', "floating", "enable"]
                        )
                        WindowManager._run_cmd(
                            [
                                "swaymsg",
                                f'[con_id="{wid}"]',
                                "move",
                                "position",
                                str(x),
                                str(y),
                            ]
                        )
                        WindowManager._run_cmd(
                            [
                                "swaymsg",
                                f'[con_id="{wid}"]',
                                "resize",
                                "set",
                                str(w),
                                str(h),
                            ]
                        )
                        successes += 1
                    elif shutil.which("wmctrl"):
                        # Remove maximized state first, then move+resize
                        WindowManager._run_cmd(
                            [
                                "wmctrl",
                                "-i",
                                "-r",
                                win["id"],
                                "-b",
                                "remove,maximized_vert,maximized_horz",
                            ]
                        )
                        WindowManager._run_cmd(
                            [
                                "wmctrl",
                                "-i",
                                "-r",
                                win["id"],
                                "-e",
                                f"0,{x},{y},{w},{h}",
                            ]
                        )
                        successes += 1
                elif system == "Darwin":
                    app = win.get("app", "")
                    scpt = f'''tell application "System Events" to tell process "{app}"
set position of window 1 to {{{x}, {y}}}
set size of window 1 to {{{w}, {h}}}
end tell'''
                    WindowManager._run_cmd(["osascript", "-e", scpt])
                    successes += 1
                elif system == "Windows":
                    # Restore first (un-maximize), then move+resize
                    WindowManager._win32_window_action(win["id"], "restore")
                    ok, _ = WindowManager._win32_window_action(
                        win["id"], "move_resize", x=x, y=y, width=w, height=h
                    )
                    if ok:
                        successes += 1
                report.append(f"  ✓ '{title}' -> ({x},{y}) {w}x{h}")
            except Exception as e:
                report.append(f"  ✗ '{title}' -> Error: {e}")

        report.append(f"\nResult: {successes}/{count} windows organized successfully.")
        report.append("Tip: Use window_list to verify current positions.")
        return "\n".join(report)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def screen_info() -> str:
    """Get detailed screen/display information including resolution, work area, and all monitors."""
    try:
        monitors = WindowManager.list_monitors()
        out = ["=== SCREEN INFO ==="]
        total_w = 0
        for m in monitors:
            primary = " [PRIMARY]" if m.get("primary") else ""
            out.append(f"Monitor {m['id']}{primary}: {m['name']}")
            out.append(f"  Resolution: {m['width']}x{m['height']}")
            out.append(f"  Position: ({m['x']}, {m['y']})")
            out.append(f"  Scale: {m.get('scale', 1.0)}")
            total_w += m["width"]

        if len(monitors) > 1:
            out.append(f"\nTotal desktop span: {total_w}px wide")

        # Get available work area (minus taskbar)
        system = platform.system()
        if system == "Windows":
            ps = """
Add-Type -AssemblyName System.Windows.Forms
$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
"$($wa.Width)|||$($wa.Height)|||$($wa.X)|||$($wa.Y)"
"""
            stdout, _, code = WindowManager._run_powershell(ps)
            if code == 0 and stdout and "|||" in stdout:
                parts = stdout.strip().split("|||")
                out.append(
                    f"\nWork area (minus taskbar): {parts[0]}x{parts[1]} at ({parts[2]},{parts[3]})"
                )

        out.append(
            f"\nPlatform: {platform.system()} ({WindowManager.get_display_server()})"
        )
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# CLIPBOARD TOOLS
# ============================================================


@mcp.tool()
def clipboard_read() -> str:
    """Read the current system clipboard text content."""
    try:
        system = platform.system()
        if system == "Windows":
            stdout, _, code = WindowManager._run_powershell("Get-Clipboard")
            if code == 0:
                return stdout if stdout else "(Clipboard is empty)"
        elif system == "Darwin":
            stdout, _, code = WindowManager._run_cmd(["pbpaste"])
            if code == 0:
                return stdout if stdout else "(Clipboard is empty)"
        elif system == "Linux":
            for cmd in [
                ["wl-paste"],
                ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
            ]:
                if shutil.which(cmd[0]):
                    stdout, _, code = WindowManager._run_cmd(cmd)
                    if code == 0:
                        return stdout if stdout else "(Clipboard is empty)"
            return "Error: No clipboard tool found. Install wl-paste, xclip, or xsel."
        return "Error: Unsupported platform."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def clipboard_write(text: str) -> str:
    """Write text to the system clipboard."""
    try:
        system = platform.system()
        if system == "Windows":
            escaped = text.replace("'", "''")
            stdout, _, code = WindowManager._run_powershell(
                f"Set-Clipboard -Value '{escaped}'"
            )
            if code == 0:
                return f"✓ Copied {len(text)} chars to clipboard"
        elif system == "Darwin":
            result = subprocess.run(
                ["pbcopy"], input=text, capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return f"✓ Copied {len(text)} chars to clipboard"
        elif system == "Linux":
            for cmd in [
                ["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ]:
                if shutil.which(cmd[0]):
                    result = subprocess.run(
                        cmd, input=text, capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        return f"✓ Copied {len(text)} chars to clipboard"
            return "Error: No clipboard tool found."
        return "Error: Unsupported platform."
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# NOTIFICATION TOOL
# ============================================================


@mcp.tool()
def notification_send(title: str, message: str, urgency: str = "normal") -> str:
    """Send a desktop notification. urgency: low, normal, critical."""
    try:
        system = platform.system()
        if system == "Linux":
            for cmd_name in ["notify-send", "kdialog"]:
                if shutil.which(cmd_name):
                    if cmd_name == "notify-send":
                        args = ["notify-send", f"--urgency={urgency}", title, message]
                    else:
                        args = [
                            "kdialog",
                            "--passivepopup",
                            message,
                            "5",
                            "--title",
                            title,
                        ]
                    _, _, code = WindowManager._run_cmd(args)
                    return (
                        "✓ Notification sent" if code == 0 else "Error: Failed to send"
                    )
            return "Error: No notification tool found. Install notify-send."
        elif system == "Darwin":
            scpt = f'display notification "{message}" with title "{title}"'
            _, _, code = WindowManager._run_cmd(["osascript", "-e", scpt])
            return "✓ Notification sent" if code == 0 else "Error: Failed"
        elif system == "Windows":
            ps = f'''
[void] [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms")
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = "{title}"
$n.BalloonTipText = "{message}"
$n.Visible = $True
$n.ShowBalloonTip(5000)
Start-Sleep 6
$n.Dispose()
'''
            _, _, code = WindowManager._run_powershell(ps, timeout=20)
            return "✓ Notification sent" if code == 0 else "Error: Failed"
        return "Error: Unsupported platform."
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# PROCESS MANAGEMENT
# ============================================================


@mcp.tool()
def process_list(filter_name: str = "") -> str:
    """List running processes, optionally filtered by name. Shows PID, name, CPU%, memory."""
    try:
        procs = []
        for p in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_percent", "status"]
        ):
            try:
                info = p.info
                if filter_name and filter_name.lower() not in info["name"].lower():
                    continue
                procs.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs.sort(key=lambda x: x.get("cpu_percent", 0) or 0, reverse=True)
        procs = procs[:50]

        out = [f"=== PROCESSES ({len(procs)} shown) ==="]
        out.append(f"{'PID':>8}  {'CPU%':>5}  {'MEM%':>5}  {'STATUS':<10}  NAME")
        for p in procs:
            out.append(
                f"{p['pid']:>8}  {(p.get('cpu_percent') or 0):>5.1f}  {(p.get('memory_percent') or 0):>5.1f}  {(p.get('status') or '?'):<10}  {p['name']}"
            )
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def process_kill(pid: int = 0, name: str = "") -> str:
    """Kill a process by PID or name. Requires either pid or name."""
    try:
        if pid > 0:
            p = psutil.Process(pid)
            pname = p.name()
            p.terminate()
            return f"✓ Terminated process {pid} ({pname})"
        elif name:
            killed = 0
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if name.lower() in p.info["name"].lower():
                        p.terminate()
                        killed += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return (
                f"✓ Terminated {killed} process(es) matching '{name}'"
                if killed
                else f"No processes found matching '{name}'"
            )
        return "Error: Provide either pid or name."
    except psutil.NoSuchProcess:
        return f"Error: Process {pid} not found."
    except psutil.AccessDenied:
        return f"Error: Access denied for process {pid}."
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# NETWORK TOOLS
# ============================================================


@mcp.tool()
def network_info() -> str:
    """Get network interface information including IP addresses and connection status."""
    try:
        out = ["=== NETWORK INFO ==="]
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()

        for iface, addr_list in addrs.items():
            is_up = stats.get(iface, None)
            status = "UP" if is_up and is_up.isup else "DOWN"
            speed = f" ({is_up.speed}Mbps)" if is_up and is_up.speed > 0 else ""
            out.append(f"\n{iface}: {status}{speed}")
            for addr in addr_list:
                family = str(addr.family).split(".")[-1]
                if "AF_INET" in family:
                    out.append(f"  IPv4: {addr.address}")
                elif "AF_INET6" in family and not addr.address.startswith("fe80"):
                    out.append(f"  IPv6: {addr.address}")

        # Connection stats
        counters = psutil.net_io_counters()

        def fmt_bytes(b):
            for u in ["B", "KB", "MB", "GB"]:
                if b < 1024:
                    return f"{b:.1f}{u}"
                b /= 1024
            return f"{b:.1f}TB"

        out.append(
            f"\nTraffic: Sent {fmt_bytes(counters.bytes_sent)}, Recv {fmt_bytes(counters.bytes_recv)}"
        )
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# URL & APP LAUNCHER
# ============================================================


@mcp.tool()
def open_url(url: str) -> str:
    """Open a URL in the default web browser."""
    try:
        import webbrowser

        webbrowser.open(url)
        return f"✓ Opened URL: {url}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def launch_app(app_name: str, args: str = "") -> str:
    """Launch an application by name. Works cross-platform. The app runs detached."""
    try:
        system = platform.system()
        if system == "Darwin":
            cmd = f'open -a "{app_name}"'
            if args:
                cmd += f" --args {args}"
            subprocess.Popen(cmd, shell=True)
        elif system == "Windows":
            cmd = f'start "" "{app_name}" {args}'
            subprocess.Popen(
                cmd,
                shell=True,
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0,
            )
        elif system == "Linux":
            cmd = f"{app_name} {args} &"
            subprocess.Popen(cmd, shell=True, start_new_session=True)
        return f"✓ Launched: {app_name}"
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# VOLUME CONTROL
# ============================================================


@mcp.tool()
def volume_get() -> str:
    """Get the current system volume level."""
    try:
        system = platform.system()
        if system == "Windows":
            ps = """
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume { int _0(); int _1(); int _2(); int _3(); int SetMasterVolumeLevelScalar(float fLevel, Guid pguidEventContext); int GetMasterVolumeLevelScalar(out float pfLevel); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppDevice); }
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface); }
"@ -ErrorAction SilentlyContinue
$null
"""
            # Simpler approach - use NirCmd or PowerShell Audio
            stdout, _, code = WindowManager._run_powershell(
                "Get-AudioDevice -PlaybackVolume 2>$null || Write-Output 'unavailable'"
            )
            if code == 0 and "unavailable" not in stdout:
                return f"Volume: {stdout.strip()}%"
            return "Volume control: Use the execute_command tool with platform-specific audio commands."

        elif system == "Darwin":
            stdout, _, code = WindowManager._run_cmd(
                ["osascript", "-e", "output volume of (get volume settings)"]
            )
            if code == 0:
                return f"Volume: {stdout.strip()}%"
        elif system == "Linux":
            for cmd in [
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                ["amixer", "get", "Master"],
            ]:
                if shutil.which(cmd[0]):
                    stdout, _, code = WindowManager._run_cmd(cmd)
                    if code == 0:
                        vol_match = re.search(r"(\d+)%", stdout)
                        if vol_match:
                            return f"Volume: {vol_match.group(1)}%"
                        return f"Volume output: {stdout[:200]}"
        return "Error: Could not determine volume."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def volume_set(level: int) -> str:
    """Set the system volume level (0-100)."""
    try:
        level = max(0, min(100, level))
        system = platform.system()
        if system == "Darwin":
            _, _, code = WindowManager._run_cmd(
                ["osascript", "-e", f"set volume output volume {level}"]
            )
            if code == 0:
                return f"✓ Volume set to {level}%"
        elif system == "Linux":
            if shutil.which("pactl"):
                _, _, code = WindowManager._run_cmd(
                    ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"]
                )
                if code == 0:
                    return f"✓ Volume set to {level}%"
            elif shutil.which("amixer"):
                _, _, code = WindowManager._run_cmd(
                    ["amixer", "set", "Master", f"{level}%"]
                )
                if code == 0:
                    return f"✓ Volume set to {level}%"
        elif system == "Windows":
            # Use nircmd if available, otherwise PowerShell
            if shutil.which("nircmd"):
                val = int(level * 655.35)
                _, _, code = WindowManager._run_cmd(
                    ["nircmd", "setsysvolume", str(val)]
                )
                if code == 0:
                    return f"✓ Volume set to {level}%"
            ps = f"""
$obj = new-object -com wscript.shell
$target = {level}
# This is approximate - sends volume up/down key presses
# For precise control, nircmd or AudioDeviceCmdlets is recommended
"Volume adjustment requested to {level}% - use nircmd for precise control"
"""
            return f"Windows volume: Install 'nircmd' for precise control, or use execute_command with 'nircmd setsysvolume {int(level * 655.35)}'."
        return "Error: No volume control method available."
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# DATE/TIME TOOL
# ============================================================


@mcp.tool()
def datetime_info() -> str:
    """Get current date, time, timezone, and uptime information."""
    try:
        now = datetime.now()
        import time

        boot = datetime.fromtimestamp(psutil.boot_time())
        uptime = now - boot
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)

        tz_name = time.tzname[0] if time.tzname else "Unknown"
        utc_offset = time.strftime("%z")

        return (
            f"=== DATE/TIME ===\n"
            f"Date: {now.strftime('%Y-%m-%d (%A)')}\n"
            f"Time: {now.strftime('%H:%M:%S')}\n"
            f"Timezone: {tz_name} (UTC{utc_offset[:3]}:{utc_offset[3:]})\n"
            f"Epoch: {int(now.timestamp())}\n"
            f"System uptime: {hours}h {minutes}m {seconds}s\n"
            f"Boot time: {boot.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================


@mcp.tool()
def env_get(variable: str) -> str:
    """Read an environment variable value."""
    val = os.environ.get(variable)
    if val is not None:
        return f"{variable}={val}"
    return f"Error: Environment variable '{variable}' is not set."


@mcp.tool()
def env_list(prefix: str = "") -> str:
    """List environment variables, optionally filtered by prefix."""
    try:
        items = sorted(os.environ.items())
        if prefix:
            items = [(k, v) for k, v in items if k.lower().startswith(prefix.lower())]
        if not items:
            return f"No environment variables found{' with prefix ' + prefix if prefix else ''}."
        out = [f"=== ENVIRONMENT ({len(items)} vars) ==="]
        for k, v in items[:50]:
            display_v = v[:80] + "..." if len(v) > 80 else v
            out.append(f"  {k}={display_v}")
        if len(items) > 50:
            out.append(f"  ... and {len(items) - 50} more")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# SCREENSHOT TOOL
# ============================================================


@mcp.tool()
def screenshot_take(save_path: str = "screenshot.png") -> str:
    """Take a screenshot and save it to the workspace. Returns the file path."""
    try:
        target = get_secure_path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        system = platform.system()

        if system == "Linux":
            for tool, cmd in [
                ("grim", ["grim", str(target)]),
                ("scrot", ["scrot", str(target)]),
                ("import", ["import", "-window", "root", str(target)]),
                ("gnome-screenshot", ["gnome-screenshot", "-f", str(target)]),
            ]:
                if shutil.which(tool):
                    _, _, code = WindowManager._run_cmd(cmd, timeout=10)
                    if code == 0 and target.exists():
                        return f"✓ Screenshot saved: {save_path} ({target.stat().st_size} bytes)"
            return "Error: No screenshot tool found. Install grim, scrot, or gnome-screenshot."
        elif system == "Darwin":
            _, _, code = WindowManager._run_cmd(["screencapture", "-x", str(target)])
            if code == 0 and target.exists():
                return f"✓ Screenshot saved: {save_path}"
        elif system == "Windows":
            ps = f'''
Add-Type -AssemblyName System.Windows.Forms
$screen = [System.Windows.Forms.Screen]::PrimaryScreen
$bmp = New-Object Drawing.Bitmap($screen.Bounds.Width, $screen.Bounds.Height)
$graphics = [Drawing.Graphics]::FromImage($bmp)
$graphics.CopyFromScreen($screen.Bounds.Location, [Drawing.Point]::Empty, $screen.Bounds.Size)
$bmp.Save("{str(target).replace(chr(92), "/")}")
$graphics.Dispose()
$bmp.Dispose()
"OK"
'''
            stdout, _, code = WindowManager._run_powershell(ps, timeout=15)
            if "OK" in stdout and target.exists():
                return f"✓ Screenshot saved: {save_path}"
        return "Error: Screenshot failed."
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# ADDITIONAL TOOLS - File & System Utilities
# ============================================================


@mcp.tool()
def find_files_pattern(
    pattern: str, directory: str = ".", max_results: int = 50
) -> str:
    """Find files matching a glob pattern recursively.

    Args:
        pattern: Glob pattern (e.g. '*.py', '**/*.json', 'test_*.py')
        directory: Directory to search in (relative to workspace)
        max_results: Maximum number of results
    """
    try:
        base = get_secure_path(directory)
        if not base.exists():
            return f"Error: Directory not found: {directory}"
        matches = []
        for p in base.rglob(pattern):
            if len(matches) >= max_results:
                break
            try:
                rel = p.relative_to(BASE_DIR)
                size = p.stat().st_size if p.is_file() else 0
                kind = "dir" if p.is_dir() else "file"
                matches.append(
                    f"  {kind}: {rel} ({size} bytes)"
                    if kind == "file"
                    else f"  {kind}: {rel}/"
                )
            except:
                pass
        if not matches:
            return f"No files matching '{pattern}' in {directory}"
        return f"Found {len(matches)} matches:\n" + "\n".join(matches)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def compress_files(paths: str, output: str = "archive.zip", format: str = "zip") -> str:
    """Compress files/directories into an archive.

    Args:
        paths: Comma-separated list of files/directories to compress
        output: Output archive filename
        format: 'zip' or 'tar.gz'
    """
    try:
        import tarfile
        import zipfile

        items = [p.strip() for p in paths.split(",") if p.strip()]
        resolved = [get_secure_path(p) for p in items]
        for r in resolved:
            if not r.exists():
                return f"Error: Not found: {r.relative_to(BASE_DIR)}"
        target = get_secure_path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        if format == "zip":
            with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as zf:
                for r in resolved:
                    if r.is_file():
                        zf.write(str(r), r.name)
                    elif r.is_dir():
                        for fp in r.rglob("*"):
                            if fp.is_file():
                                zf.write(str(fp), str(fp.relative_to(r.parent)))
            return f"Created zip: {output} ({target.stat().st_size} bytes)"
        elif format == "tar.gz":
            with tarfile.open(str(target), "w:gz") as tf:
                for r in resolved:
                    tf.add(str(r), arcname=r.name)
            return f"Created tar.gz: {output} ({target.stat().st_size} bytes)"
        return f"Error: Unknown format '{format}'. Use 'zip' or 'tar.gz'."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def extract_archive(archive: str, destination: str = ".") -> str:
    """Extract a zip or tar archive.

    Args:
        archive: Path to the archive file
        destination: Directory to extract to
    """
    try:
        import tarfile
        import zipfile

        src = get_secure_path(archive)
        dest = get_secure_path(destination)
        if not src.exists():
            return f"Error: Archive not found: {archive}"
        dest.mkdir(parents=True, exist_ok=True)
        if str(src).endswith(".zip"):
            with zipfile.ZipFile(str(src), "r") as zf:
                zf.extractall(str(dest))
                return f"Extracted {len(zf.namelist())} items to {destination}"
        elif str(src).endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar")):
            with tarfile.open(str(src), "r:*") as tf:
                tf.extractall(str(dest))
                return f"Extracted {len(tf.getnames())} items to {destination}"
        return "Error: Unsupported format. Use .zip, .tar.gz, .tgz, .tar.bz2, or .tar"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def file_hash(path: str, algorithm: str = "sha256") -> str:
    """Calculate the hash/checksum of a file.

    Args:
        path: File path
        algorithm: Hash algorithm - 'md5', 'sha1', 'sha256', 'sha512'
    """
    try:
        target = get_secure_path(path)
        if not target.is_file():
            return f"Error: File not found: {path}"
        h = hashlib.new(algorithm)
        with open(str(target), "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return f"{algorithm}: {h.hexdigest()}\nFile: {path} ({target.stat().st_size} bytes)"
    except ValueError:
        return (
            f"Error: Unknown algorithm '{algorithm}'. Use md5, sha1, sha256, or sha512."
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def file_info(path: str) -> str:
    """Get detailed information about a file or directory.

    Args:
        path: File or directory path
    """
    try:
        target = get_secure_path(path)
        if not target.exists():
            return f"Error: Not found: {path}"
        st = target.stat()
        info = [f"Path: {path}", f"Type: {'directory' if target.is_dir() else 'file'}"]
        info.append(f"Size: {st.st_size} bytes ({st.st_size / 1024:.1f} KB)")
        from datetime import datetime as dt

        info.append(f"Modified: {dt.fromtimestamp(st.st_mtime).isoformat()}")
        info.append(f"Created: {dt.fromtimestamp(st.st_ctime).isoformat()}")
        info.append(f"Permissions: {oct(st.st_mode)[-3:]}")
        if target.is_file():
            info.append(f"Extension: {target.suffix.lower() or '(none)'}")
            text_exts = {
                ".txt",
                ".py",
                ".js",
                ".ts",
                ".html",
                ".css",
                ".json",
                ".md",
                ".yaml",
                ".yml",
                ".toml",
                ".cfg",
                ".ini",
                ".sh",
                ".bat",
                ".c",
                ".cpp",
                ".h",
                ".java",
                ".rs",
                ".go",
                ".rb",
                ".php",
            }
            if target.suffix.lower() in text_exts:
                try:
                    with open(str(target), "r", errors="replace") as f:
                        info.append(f"Lines: {sum(1 for _ in f)}")
                except:
                    pass
        elif target.is_dir():
            files = list(target.iterdir())
            dirs = sum(1 for f in files if f.is_dir())
            info.append(
                f"Contents: {len(files)} items ({dirs} dirs, {len(files) - dirs} files)"
            )
        return "\n".join(info)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def disk_usage(path: str = ".") -> str:
    """Get disk usage information.

    Args:
        path: Path to check (default: workspace root)
    """
    try:
        target = get_secure_path(path)
        usage = shutil.disk_usage(str(target))
        total_gb = usage.total / (1024**3)
        used_gb = usage.used / (1024**3)
        free_gb = usage.free / (1024**3)
        pct = (usage.used / usage.total) * 100
        out = [
            f"Disk: {total_gb:.1f} GB total, {used_gb:.1f} GB used ({pct:.1f}%), {free_gb:.1f} GB free"
        ]
        if target.is_dir():
            ws_size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            out.append(f"Workspace folder: {ws_size / (1024**2):.1f} MB")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def service_check(service_name: str) -> str:
    """Check if a system service/process is running.

    Args:
        service_name: Name of the service or process to check
    """
    try:
        found = []
        for proc in psutil.process_iter(["pid", "name", "status", "memory_info"]):
            try:
                if service_name.lower() in proc.info["name"].lower():
                    mem = (
                        proc.info["memory_info"].rss / (1024 * 1024)
                        if proc.info["memory_info"]
                        else 0
                    )
                    found.append(
                        f"  PID {proc.info['pid']}: {proc.info['name']} ({proc.info['status']}) {mem:.1f}MB"
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if found:
            return f"'{service_name}' running ({len(found)}):\n" + "\n".join(found)
        return f"'{service_name}' is NOT running."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def env_variable_set(name: str, value: str, persist: bool = False) -> str:
    """Set an environment variable.

    Args:
        name: Variable name
        value: Variable value
        persist: If true, also save to .env file
    """
    try:
        os.environ[name] = value
        msg = f"Set {name}={value}"
        if persist:
            from dotenv import set_key as sk

            env_path = BASE_DIR / ".env"
            if not env_path.exists():
                env_path.touch()
            sk(str(env_path), name, value)
            msg += " (persisted)"
        return msg
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def text_search_replace(
    path: str, search: str, replace: str, regex: bool = False
) -> str:
    """Find and replace text in a file.

    Args:
        path: File path
        search: Text or regex pattern to find
        replace: Replacement text
        regex: Whether to use regex
    """
    try:
        target = get_secure_path(path)
        if not target.is_file():
            return f"Error: File not found: {path}"
        with open(str(target), "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if regex:
            new_content, count = re.subn(search, replace, content)
        else:
            count = content.count(search)
            new_content = content.replace(search, replace)
        if count == 0:
            return f"No matches for '{search}' in {path}"
        with open(str(target), "w", encoding="utf-8") as f:
            f.write(new_content)
        return f"Replaced {count} occurrence(s) in {path}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def batch_file_rename(
    directory: str, pattern: str, replacement: str, dry_run: bool = True
) -> str:
    """Rename multiple files using regex pattern matching.

    Args:
        directory: Directory containing files
        pattern: Regex pattern for filenames
        replacement: Replacement string (supports \\1 groups)
        dry_run: Preview only (set false to apply)
    """
    try:
        base = get_secure_path(directory)
        if not base.is_dir():
            return f"Error: Directory not found: {directory}"
        changes = []
        for item in sorted(base.iterdir()):
            if item.is_file():
                new_name = re.sub(pattern, replacement, item.name)
                if new_name != item.name:
                    changes.append((item, base / new_name))
        if not changes:
            return f"No files matched '{pattern}'"
        lines = [f"{'[DRY RUN] ' if dry_run else ''}{len(changes)} file(s):"]
        for old, new in changes:
            lines.append(f"  {old.name} -> {new.name}")
            if not dry_run:
                old.rename(new)
        if dry_run:
            lines.append("Set dry_run=false to apply.")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def http_request(url: str, method: str = "GET") -> str:
    """Make a simple HTTP request and return the response.

    Args:
        url: The URL to request
        method: HTTP method (GET, HEAD)
    """
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, method=method.upper())
        req.add_header("User-Agent", "ArcBot/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            out = [
                f"Status: {resp.status}",
                f"Content-Type: {resp.headers.get('Content-Type', '')}",
            ]
            if method.upper() == "HEAD":
                return "\n".join(out)
            ct = resp.headers.get("Content-Type", "")
            if any(t in ct for t in ["text", "json", "xml", "javascript"]):
                body = resp.read(50000).decode(errors="replace")
                out.append(f"\nBody ({len(body)} chars):\n{body[:5000]}")
            else:
                out.append("(Binary content)")
            return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# --- MAIN ---

if __name__ == "__main__":
    if not BASE_DIR.exists():
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Created workspace: {BASE_DIR}")

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    log("ArcBot MCP Server starting...")
    log(f"Workspace: {BASE_DIR}")
    log(f"Platform: {platform.system()} ({SystemDetector.get_display_server()})")
    mcp.run(transport="stdio")
