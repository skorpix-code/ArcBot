"""
Enhanced MCP Server for LLM Coder FileSystem
=============================================
Comprehensive window management features added:
- Workspace management (list, switch, move windows)
- Window states (minimize, maximize, fullscreen, always-on-top)
- Window positioning (move, resize, snap, center)
- Window arrangement (tile, cascade, grid)
- Multi-monitor support
- Browser tab management
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
from ddgs import DDGS
from mcp.server.fastmcp import FastMCP

# --- CONFIGURATION ---
BASE_DIR = Path("/home/skorp/Programming/LLM_Coder_Dir")

# Create the MCP Server instance
mcp = FastMCP("LLM_Coder_FileSystem_Enhanced")

# --- CONSTANTS ---
TODO_FILE = ".agent_todo.json"
MAX_OUTPUT_LENGTH = 5000
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
}


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


class WindowState(Enum):
    NORMAL = "normal"
    MINIMIZED = "minimized"
    MAXIMIZED = "maximized"
    FULLSCREEN = "fullscreen"


class SnapDirection(Enum):
    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    CENTER = "center"


@dataclass
class TodoTask:
    id: str
    title: str
    description: str
    status: str
    priority: str
    created_at: str
    updated_at: str
    dependencies: List[str]
    subtasks: List[str]
    notes: List[str]
    assigned_files: List[str]


@dataclass
class SystemInfo:
    os_name: str
    os_version: str
    architecture: str
    hostname: str
    python_version: str
    cpu_info: Dict[str, Any]
    memory_info: Dict[str, Any]
    gpu_info: List[Dict[str, Any]]
    disk_info: List[Dict[str, Any]]
    display_server: str
    environment: Dict[str, str]


@dataclass
class MonitorInfo:
    id: str
    name: str
    width: int
    height: int
    x: int
    y: int
    primary: bool
    scale: float


@dataclass
class WorkspaceInfo:
    id: str
    name: str
    number: int
    windows: int
    active: bool


# --- LOGGING ---
def log(message: str, level: str = "INFO"):
    """Enhanced logging with timestamps and levels."""
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
    """Sanitize user input to prevent injection and limit size."""
    if not text:
        return ""
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text))
    return sanitized[:max_length]


def truncate_output(output: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """Truncate output to prevent context overflow."""
    if len(output) <= max_length:
        return output
    return (
        output[:max_length]
        + f"\n\n... [Output truncated. Total length: {len(output)} characters]"
    )


# --- SYSTEM DETECTION ---
class SystemDetector:
    """Cross-platform system information detection."""

    @staticmethod
    def get_os_info() -> Tuple[str, str, str, str]:
        """Get OS name, version, architecture, and hostname."""
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
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                ) as key:
                    os_version = (
                        f"{winreg.QueryValueEx(key, 'ProductName')[0]} "
                        f"{winreg.QueryValueEx(key, 'DisplayVersion')[0]}"
                    )
            except Exception:
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
        """Get CPU information."""
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
                cpu_info["max_frequency"] = {
                    "value": freq.max,
                    "unit": "MHz" if freq.max < 10000 else "GHz",
                }
                cpu_info["current_frequency"] = {
                    "value": round(freq.current, 2),
                    "unit": "MHz" if freq.current < 1000 else "GHz",
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
                        ["wmic", "cpu", "get", "name"], capture_output=True, text=True
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
        """Get memory information."""
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
            log(f"Error getting memory info: {e}", "WARNING")
            return {"error": str(e)}

    @staticmethod
    def get_gpu_info() -> List[Dict[str, Any]]:
        """Get GPU information (cross-platform)."""
        gpus = []
        system = platform.system()

        try:
            if system == "Linux":
                if shutil.which("lspci"):
                    result = subprocess.run(
                        ["lspci"], capture_output=True, text=True, timeout=10
                    )
                    if result.returncode == 0:
                        for line in result.stdout.splitlines():
                            if "VGA" in line or "3D" in line or "Display" in line:
                                gpus.append(
                                    {
                                        "vendor": line.split(":")[0].strip()
                                        if ":" in line
                                        else "Unknown",
                                        "model": line.split(":")[-1].strip()
                                        if ":" in line
                                        else line.strip(),
                                        "driver": "Unknown",
                                    }
                                )

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

            elif system == "Windows":
                result = subprocess.run(
                    ["wmic", "path", "win32_VideoController", "get", "name,adapterram"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    for line in lines[1:]:
                        if line.strip():
                            parts = line.split()
                            if parts:
                                name = (
                                    " ".join(parts[:-1]) if len(parts) > 1 else parts[0]
                                )
                                memory = parts[-1] if parts[-1].isdigit() else "Unknown"
                                gpus.append(
                                    {
                                        "vendor": "Unknown",
                                        "model": name,
                                        "memory": f"{int(memory) // (1024 * 1024)} MB"
                                        if memory.isdigit()
                                        else memory,
                                    }
                                )

            elif system == "Darwin":
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
                        elif "Vendor:" in line:
                            current_gpu["vendor"] = line.split(":")[1].strip()
                    if current_gpu:
                        gpus.append(current_gpu)

        except subprocess.TimeoutExpired:
            log("GPU detection timed out", "WARNING")
        except Exception as e:
            log(f"Error getting GPU info: {e}", "WARNING")

        return gpus if gpus else [{"error": "No GPU detected or detection failed"}]

    @staticmethod
    def get_disk_info() -> List[Dict[str, Any]]:
        """Get disk information."""
        disks = []
        try:
            partitions = psutil.disk_partitions()
            for partition in partitions:
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
        """Detect the display server/compositor on Linux."""
        system = platform.system()

        if system != "Linux":
            if system == "Darwin":
                return "Quartz"
            elif system == "Windows":
                return "DWM"
            return "Unknown"

        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return "Hyprland"
        if os.environ.get("SWAYSOCK"):
            return "Sway"
        if os.environ.get("GNOME_DESKTOP_SESSION_ID"):
            return "GNOME"
        if os.environ.get("KDE_FULL_SESSION"):
            return "KDE"

        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type == "x11":
            return "X11"
        elif session_type == "wayland":
            wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
            if wayland_display:
                return "Wayland"

        if os.environ.get("DISPLAY"):
            return "X11"

        if os.environ.get("WAYLAND_DISPLAY"):
            return "Wayland"

        return "Unknown"

    @classmethod
    def get_full_system_info(cls) -> SystemInfo:
        """Get complete system information."""
        os_name, os_version, arch, hostname = cls.get_os_info()

        return SystemInfo(
            os_name=os_name,
            os_version=os_version,
            architecture=arch,
            hostname=hostname,
            python_version=platform.python_version(),
            cpu_info=cls.get_cpu_info(),
            memory_info=cls.get_memory_info(),
            gpu_info=cls.get_gpu_info(),
            disk_info=cls.get_disk_info(),
            display_server=cls.get_display_server(),
            environment={
                "SHELL": os.environ.get("SHELL", "Unknown"),
                "TERM": os.environ.get("TERM", "Unknown"),
                "EDITOR": os.environ.get("EDITOR", "Unknown"),
                "LANG": os.environ.get("LANG", "Unknown"),
                "HOME": os.environ.get("HOME", "Unknown"),
            },
        )


# ============================================================
# ENHANCED WINDOW MANAGER
# ============================================================


class WindowManager:
    """Comprehensive cross-platform window management."""

    @staticmethod
    def _run_cmd(
        cmd_list: List[str], timeout: int = 10, input_text: str = None
    ) -> Tuple[str, str, int]:
        """Run a command and return stdout, stderr, returncode."""
        try:
            result = subprocess.run(
                cmd_list,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input_text,
            )
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return "", "Command timed out", -1
        except FileNotFoundError:
            return "", f"Command not found: {cmd_list[0]}", -1
        except Exception as e:
            return "", str(e), -1

    @staticmethod
    def _run_shell_cmd(command: str, timeout: int = 10) -> Tuple[str, str, int]:
        """Run a shell command and return stdout, stderr, returncode."""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=timeout
            )
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return "", "Command timed out", -1
        except Exception as e:
            return "", str(e), -1

    @staticmethod
    def get_display_server() -> str:
        """Get current display server."""
        return SystemDetector.get_display_server()

    # ========================================
    # WINDOW LISTING
    # ========================================

    @classmethod
    def list_windows(cls) -> List[Dict[str, Any]]:
        """List all visible windows with detailed information."""
        system = platform.system()
        windows = []

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    windows = cls._list_hyprland_windows()
                elif display_server == "Sway":
                    windows = cls._list_sway_windows()
                elif display_server in ["X11", "Wayland", "GNOME", "KDE"]:
                    windows = cls._list_x11_windows()
                else:
                    windows = (
                        cls._list_hyprland_windows()
                        or cls._list_sway_windows()
                        or cls._list_x11_windows()
                    )

            elif system == "Darwin":
                windows = cls._list_macos_windows()

            elif system == "Windows":
                windows = cls._list_windows_windows()

        except Exception as e:
            log(f"Error listing windows: {e}", "ERROR")

        return windows

    @classmethod
    def _list_hyprland_windows(cls) -> List[Dict[str, Any]]:
        """List windows using Hyprland's hyprctl."""
        stdout, stderr, code = cls._run_cmd(["hyprctl", "clients", "-j"])

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
        """List windows using Sway's swaymsg."""
        stdout, stderr, code = cls._run_cmd(["swaymsg", "-t", "get_tree"])

        if code != 0 or not stdout:
            return []

        windows = []

        def find_windows(node, workspace_id="", workspace_name=""):
            # Check if this is a workspace
            if node.get("type") == "workspace":
                workspace_id = str(node.get("id", ""))
                workspace_name = node.get("name", "")

            if node.get("type") == "con" and node.get("name"):
                window_props = node.get("window_properties", {})
                windows.append(
                    {
                        "id": str(node.get("id", "unknown")),
                        "title": node.get("name", ""),
                        "app": node.get("app_id", window_props.get("class", "")),
                        "workspace": workspace_id,
                        "workspace_name": workspace_name,
                        "fullscreen": node.get("fullscreen", False),
                        "floating": node.get("type") == "floating_con",
                        "pinned": node.get("sticky", False),
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

            for child in node.get("nodes", []):
                find_windows(child, workspace_id, workspace_name)
            for child in node.get("floating_nodes", []):
                find_windows(child, workspace_id, workspace_name)

        try:
            tree = json.loads(stdout)
            find_windows(tree)
            return windows
        except json.JSONDecodeError:
            return []

    @classmethod
    def _list_x11_windows(cls) -> List[Dict[str, Any]]:
        """List windows using wmctrl and xdotool for X11."""
        windows = []

        # Use wmctrl for window list
        if shutil.which("wmctrl"):
            stdout, stderr, code = cls._run_cmd(["wmctrl", "-l", "-p", "-G"])

            if code == 0 and stdout:
                for line in stdout.splitlines():
                    parts = line.split(None, 7)
                    if len(parts) >= 8:
                        window_id = parts[0]
                        desktop = parts[1]
                        pid = parts[2]
                        x, y, w, h = (
                            int(parts[3]),
                            int(parts[4]),
                            int(parts[5]),
                            int(parts[6]),
                        )
                        title = parts[7] if len(parts) > 7 else ""

                        # Get window state using xdotool if available
                        state = "normal"
                        if shutil.which("xdotool"):
                            state_out, _, _ = cls._run_cmd(
                                ["xdotool", "getwindowfocus", "getwindowgeometry"]
                            )

                        windows.append(
                            {
                                "id": window_id,
                                "title": title,
                                "app": "",
                                "workspace": desktop,
                                "workspace_name": f"Desktop {desktop}",
                                "fullscreen": False,
                                "floating": False,
                                "pinned": False,
                                "size": {"width": w, "height": h},
                                "position": {"x": x, "y": y},
                                "pid": pid,
                                "display_server": "X11",
                            }
                        )

        return windows

    @classmethod
    def _list_macos_windows(cls) -> List[Dict[str, Any]]:
        """List windows on macOS using AppleScript."""
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

        stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt], timeout=15)

        if code != 0 or not stdout:
            return []

        windows = []
        for line in stdout.splitlines():
            if "|||" in line:
                parts = line.split("|||")
                if len(parts) >= 7:
                    windows.append(
                        {
                            "app": parts[0],
                            "title": parts[1],
                            "id": f"{parts[0]}:{parts[1]}",
                            "position": {"x": int(parts[2]), "y": int(parts[3])},
                            "size": {"width": int(parts[4]), "height": int(parts[5])},
                            "frontmost": parts[6].lower() == "true",
                            "display_server": "macOS",
                        }
                    )

        return windows

    @classmethod
    def _list_windows_windows(cls) -> List[Dict[str, Any]]:
        """List windows on Windows using PowerShell."""
        ps_cmd = """
        Add-Type @"
            using System;
            using System.Runtime.InteropServices;
            public class Win32 {
                [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
                [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
                [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr hWnd);
            }
"@
        Get-Process | Where-Object {$_.MainWindowTitle -ne ""} | ForEach-Object {
            $handle = $_.MainWindowHandle
            $isMinimized = [Win32]::IsIconic($handle)
            $isMaximized = [Win32]::IsZoomed($handle)
            "$($_.ProcessName)|||$($_.MainWindowTitle)|||$($_.Id)|||$handle|||$isMinimized|||$isMaximized"
        }
        """

        stdout, stderr, code = cls._run_cmd(
            ["powershell", "-NoProfile", "-Command", ps_cmd], timeout=15
        )

        if code != 0 or not stdout:
            return []

        windows = []
        for line in stdout.splitlines():
            if "|||" in line:
                parts = line.split("|||")
                if len(parts) >= 6:
                    windows.append(
                        {
                            "app": parts[0],
                            "title": parts[1],
                            "id": parts[3],
                            "pid": parts[2],
                            "minimized": parts[4].lower() == "true",
                            "maximized": parts[5].lower() == "true",
                            "display_server": "Windows",
                        }
                    )

        return windows

    # ========================================
    # WORKSPACE MANAGEMENT
    # ========================================

    @classmethod
    def list_workspaces(cls) -> List[Dict[str, Any]]:
        """List all workspaces/desktops."""
        system = platform.system()
        workspaces = []

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    workspaces = cls._list_hyprland_workspaces()
                elif display_server == "Sway":
                    workspaces = cls._list_sway_workspaces()
                else:
                    workspaces = cls._list_x11_workspaces()

            elif system == "Darwin":
                workspaces = cls._list_macos_spaces()

            elif system == "Windows":
                workspaces = cls._list_windows_desktops()

        except Exception as e:
            log(f"Error listing workspaces: {e}", "ERROR")

        return workspaces

    @classmethod
    def _list_hyprland_workspaces(cls) -> List[Dict[str, Any]]:
        """List Hyprland workspaces."""
        stdout, stderr, code = cls._run_cmd(["hyprctl", "workspaces", "-j"])

        if code != 0 or not stdout:
            return []

        try:
            data = json.loads(stdout)
            workspaces = []

            # Get active workspace
            active_stdout, _, _ = cls._run_cmd(["hyprctl", "activeworkspace", "-j"])
            active_id = None
            if active_stdout:
                try:
                    active_data = json.loads(active_stdout)
                    active_id = active_data.get("id")
                except:
                    pass

            for ws in data:
                workspaces.append(
                    {
                        "id": str(ws.get("id", "")),
                        "name": ws.get("name", str(ws.get("id", ""))),
                        "number": ws.get("id", 0),
                        "windows": ws.get("windows", 0),
                        "monitor": ws.get("monitor", ""),
                        "active": ws.get("id") == active_id,
                    }
                )

            return workspaces
        except json.JSONDecodeError:
            return []

    @classmethod
    def _list_sway_workspaces(cls) -> List[Dict[str, Any]]:
        """List Sway workspaces."""
        stdout, stderr, code = cls._run_cmd(["swaymsg", "-t", "get_workspaces"])

        if code != 0 or not stdout:
            return []

        try:
            data = json.loads(stdout)
            workspaces = []

            for ws in data:
                workspaces.append(
                    {
                        "id": str(ws.get("id", "")),
                        "name": ws.get("name", ""),
                        "number": ws.get("num", 0),
                        "windows": len(ws.get("floating_nodes", []))
                        + len(ws.get("nodes", [])),
                        "monitor": ws.get("output", ""),
                        "active": ws.get("focused", False),
                        "urgent": ws.get("urgent", False),
                    }
                )

            return workspaces
        except json.JSONDecodeError:
            return []

    @classmethod
    def _list_x11_workspaces(cls) -> List[Dict[str, Any]]:
        """List X11 desktops using wmctrl."""
        if not shutil.which("wmctrl"):
            return []

        stdout, stderr, code = cls._run_cmd(["wmctrl", "-d"])

        if code != 0 or not stdout:
            return []

        workspaces = []
        for line in stdout.splitlines():
            parts = line.split(None, 11)
            if len(parts) >= 3:
                desktop_id = parts[0]
                active = "*" in parts[1]
                name = parts[-1] if len(parts) > 5 else f"Desktop {desktop_id}"

                workspaces.append(
                    {
                        "id": desktop_id,
                        "name": name,
                        "number": int(desktop_id) if desktop_id.isdigit() else 0,
                        "windows": 0,
                        "active": active,
                    }
                )

        return workspaces

    @classmethod
    def _list_macos_spaces(cls) -> List[Dict[str, Any]]:
        """List macOS spaces (Mission Control spaces)."""
        scpt = """
        tell application "System Events"
            try
                -- Get spaces using yabai if available, otherwise return current space info
                return "Space management requires yabai or Rectangle app on macOS"
            on error
                return "macOS spaces require third-party tools"
            end try
        end tell
        """

        # macOS doesn't have built-in AppleScript space management
        # Check for yabai
        if shutil.which("yabai"):
            stdout, stderr, code = cls._run_cmd(["yabai", "-m", "query", "--spaces"])
            if code == 0 and stdout:
                try:
                    data = json.loads(stdout)
                    workspaces = []
                    for space in data:
                        workspaces.append(
                            {
                                "id": str(space.get("id", "")),
                                "name": f"Space {space.get('index', '')}",
                                "number": space.get("index", 0),
                                "windows": len(space.get("windows", [])),
                                "active": space.get("has-focus", False),
                                "display": str(space.get("display", "")),
                            }
                        )
                    return workspaces
                except:
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

    @classmethod
    def _list_windows_desktops(cls) -> List[Dict[str, Any]]:
        """List Windows virtual desktops."""
        ps_cmd = """
        Add-Type @"
            using System;
            using System.Runtime.InteropServices;
            using System.Text;
            public class VDesk {
                [DllImport("user32.dll")] public static extern IntPtr GetDesktopWindow();
            }
"@
        # Windows 10+ virtual desktops require COM interface
        # Return basic info
        "1|||Desktop 1|||true"
        """

        stdout, stderr, code = cls._run_cmd(
            ["powershell", "-NoProfile", "-Command", ps_cmd], timeout=10
        )

        return [
            {"id": "1", "name": "Desktop 1", "number": 1, "windows": 0, "active": True}
        ]

    # ========================================
    # SWITCH WORKSPACE
    # ========================================

    @classmethod
    def switch_workspace(cls, workspace_id: str) -> Dict[str, Any]:
        """Switch to a specific workspace."""
        system = platform.system()
        result = {"success": False, "message": ""}

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    stdout, stderr, code = cls._run_cmd(
                        ["hyprctl", "dispatch", "workspace", workspace_id]
                    )
                    result["success"] = code == 0
                    result["message"] = "Switched workspace" if code == 0 else stderr

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
                        ["swaymsg", "workspace", f"number {workspace_id}"]
                    )
                    result["success"] = code == 0
                    result["message"] = "Switched workspace" if code == 0 else stderr

                else:
                    if shutil.which("wmctrl"):
                        stdout, stderr, code = cls._run_cmd(
                            ["wmctrl", "-s", workspace_id]
                        )
                        result["success"] = code == 0
                        result["message"] = (
                            "Switched workspace" if code == 0 else stderr
                        )
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                # macOS requires Ctrl+Arrow keys or yabai
                if shutil.which("yabai"):
                    stdout, stderr, code = cls._run_cmd(
                        ["yabai", "-m", "space", "--focus", workspace_id]
                    )
                    result["success"] = code == 0
                    result["message"] = "Switched space" if code == 0 else stderr
                else:
                    result["message"] = "macOS space switching requires yabai"

            elif system == "Windows":
                # Windows virtual desktop switching
                ps_cmd = f"""
                Add-Type @"
                    using System;
                    using System.Runtime.InteropServices;
                    public class VDesk {{
                        [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, int dwExtraInfo);
                    }}
"@
                # Use Win+Ctrl+Left/Right or Win+Ctrl+Number
                """
                result["message"] = (
                    "Windows virtual desktop switching requires additional setup"
                )

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error switching workspace: {e}", "ERROR")

        return result

    # ========================================
    # MOVE WINDOW TO WORKSPACE
    # ========================================

    @classmethod
    def move_window_to_workspace(
        cls, window_query: str, workspace_id: str, follow: bool = False
    ) -> Dict[str, Any]:
        """Move a window to a different workspace."""
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        # Find the window
        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    cmd = [
                        "hyprctl",
                        "dispatch",
                        "movetoworkspace",
                        f"{workspace_id},address:{address}",
                    ]
                    if follow:
                        cmd = [
                            "hyprctl",
                            "dispatch",
                            "movetoworkspacesilent",
                            f"{workspace_id},address:{address}",
                        ]
                    stdout, stderr, code = cls._run_cmd(cmd)
                    result["success"] = code == 0
                    result["message"] = (
                        f"Moved window to workspace {workspace_id}"
                        if code == 0
                        else stderr
                    )

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "move",
                            "container",
                            "to",
                            "workspace",
                            workspace_id,
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        f"Moved window to workspace {workspace_id}"
                        if code == 0
                        else stderr
                    )

                else:
                    if shutil.which("wmctrl"):
                        window_id = target_window.get("id", "")
                        stdout, stderr, code = cls._run_cmd(
                            ["wmctrl", "-i", "-r", window_id, "-t", workspace_id]
                        )
                        result["success"] = code == 0
                        result["message"] = (
                            f"Moved window to desktop {workspace_id}"
                            if code == 0
                            else stderr
                        )
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                if shutil.which("yabai"):
                    # Get window ID from yabai
                    stdout, stderr, code = cls._run_cmd(
                        ["yabai", "-m", "query", "--windows"]
                    )
                    if code == 0:
                        try:
                            yabai_windows = json.loads(stdout)
                            for yw in yabai_windows:
                                if (
                                    window_query.lower() in yw.get("app", "").lower()
                                    or window_query.lower()
                                    in yw.get("title", "").lower()
                                ):
                                    win_id = yw.get("id")
                                    cls._run_cmd(
                                        [
                                            "yabai",
                                            "-m",
                                            "window",
                                            str(win_id),
                                            "--space",
                                            workspace_id,
                                        ]
                                    )
                                    result["success"] = True
                                    result["message"] = (
                                        f"Moved window to space {workspace_id}"
                                    )
                                    break
                        except:
                            pass
                else:
                    result["message"] = (
                        "macOS requires yabai for window workspace management"
                    )

            elif system == "Windows":
                result["message"] = (
                    "Windows requires additional tools for this operation"
                )

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error moving window to workspace: {e}", "ERROR")

        return result

    # ========================================
    # WINDOW STATE MANAGEMENT
    # ========================================

    @classmethod
    def minimize_window(cls, window_query: str) -> Dict[str, Any]:
        """Minimize a window."""
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    # Hyprland doesn't have minimize, use movetoworkspacesilent
                    address = target_window.get("id", "")
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "movetoworkspacesilent",
                            f"special:minimized,address:{address}",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = "Window hidden" if code == 0 else stderr

                elif display_server == "Sway":
                    # Sway: move to scratchpad
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "move",
                            "to",
                            "scratchpad",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        "Window moved to scratchpad" if code == 0 else stderr
                    )

                else:
                    if shutil.which("xdotool"):
                        # Use xdotool to minimize
                        window_id = target_window.get("id", "")
                        stdout, stderr, code = cls._run_cmd(
                            ["xdotool", "windowminimize", window_id]
                        )
                        result["success"] = code == 0
                        result["message"] = "Window minimized" if code == 0 else stderr
                    else:
                        result["message"] = "xdotool not found"

            elif system == "Darwin":
                app_name = target_window.get("app", "")
                scpt = f'''
                tell application "System Events"
                    tell process "{app_name}"
                        set value of attribute "AXMinimized" of window 1 to true
                    end tell
                end tell
                '''
                stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = "Window minimized" if code == 0 else stderr

            elif system == "Windows":
                handle = target_window.get("id", "")
                ps_cmd = f"""
                Add-Type @"
                    using System;
                    using System.Runtime.InteropServices;
                    public class Win32 {{
                        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
                    }}
"@
                [Win32]::ShowWindow([IntPtr]::new({handle}), 6)
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_cmd]
                )
                result["success"] = code == 0
                result["message"] = "Window minimized" if code == 0 else stderr

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error minimizing window: {e}", "ERROR")

        return result

    @classmethod
    def maximize_window(cls, window_query: str, toggle: bool = True) -> Dict[str, Any]:
        """Maximize or unmaximize a window."""
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    action = "togglemaximize" if toggle else "maximize"
                    stdout, stderr, code = cls._run_cmd(
                        ["hyprctl", "dispatch", action, f"address:{address}"]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        "Window maximized/unmaximized" if code == 0 else stderr
                    )

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "fullscreen",
                            "toggle",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        "Window fullscreen toggled" if code == 0 else stderr
                    )

                else:
                    if shutil.which("wmctrl"):
                        window_id = target_window.get("id", "")
                        stdout, stderr, code = cls._run_cmd(
                            [
                                "wmctrl",
                                "-i",
                                "-r",
                                window_id,
                                "-b",
                                "toggle,maximized_vert,maximized_horz",
                            ]
                        )
                        result["success"] = code == 0
                        result["message"] = (
                            "Window maximized/unmaximized" if code == 0 else stderr
                        )
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                app_name = target_window.get("app", "")
                scpt = f'''
                tell application "{app_name}"
                    activate
                    delay 0.2
                end tell
                tell application "System Events"
                    tell process "{app_name}"
                        keystroke "f" using {{control down, command down}}
                    end tell
                end tell
                '''
                stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = "Fullscreen toggled" if code == 0 else stderr

            elif system == "Windows":
                handle = target_window.get("id", "")
                ps_cmd = f"""
                Add-Type @"
                    using System;
                    using System.Runtime.InteropServices;
                    public class Win32 {{
                        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
                    }}
"@
                [Win32]::ShowWindow([IntPtr]::new({handle}), 3)
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_cmd]
                )
                result["success"] = code == 0
                result["message"] = "Window maximized" if code == 0 else stderr

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error maximizing window: {e}", "ERROR")

        return result

    @classmethod
    def toggle_fullscreen(cls, window_query: str) -> Dict[str, Any]:
        """Toggle fullscreen for a window."""
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    stdout, stderr, code = cls._run_cmd(
                        ["hyprctl", "dispatch", "fullscreen", "0", f"address:{address}"]
                    )
                    result["success"] = code == 0
                    result["message"] = "Fullscreen toggled" if code == 0 else stderr

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "fullscreen",
                            "toggle",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = "Fullscreen toggled" if code == 0 else stderr

                else:
                    if shutil.which("wmctrl"):
                        window_id = target_window.get("id", "")
                        stdout, stderr, code = cls._run_cmd(
                            ["wmctrl", "-i", "-r", window_id, "-b", "toggle,fullscreen"]
                        )
                        result["success"] = code == 0
                        result["message"] = (
                            "Fullscreen toggled" if code == 0 else stderr
                        )
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                app_name = target_window.get("app", "")
                scpt = f'''
                tell application "{app_name}"
                    activate
                end tell
                tell application "System Events"
                    tell process "{app_name}"
                        keystroke "f" using {{control down, command down}}
                    end tell
                end tell
                '''
                stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = "Fullscreen toggled" if code == 0 else stderr

            elif system == "Windows":
                handle = target_window.get("id", "")
                ps_cmd = f"""
                Add-Type @"
                    using System;
                    using System.Runtime.InteropServices;
                    public class Win32 {{
                        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
                    }}
"@
                [Win32]::ShowWindow([IntPtr]::new({handle}), 3)
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_cmd]
                )
                result["success"] = code == 0
                result["message"] = "Fullscreen toggled" if code == 0 else stderr

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error toggling fullscreen: {e}", "ERROR")

        return result

    @classmethod
    def set_always_on_top(
        cls, window_query: str, enable: bool = True
    ) -> Dict[str, Any]:
        """Set window always on top (pin)."""
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    action = "pin" if enable else "unpin"
                    stdout, stderr, code = cls._run_cmd(
                        ["hyprctl", "dispatch", action, f"address:{address}"]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        f"Window pin set to {enable}" if code == 0 else stderr
                    )

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "sticky",
                            "toggle" if enable else "disable",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        f"Window sticky set to {enable}" if code == 0 else stderr
                    )

                else:
                    if shutil.which("wmctrl"):
                        window_id = target_window.get("id", "")
                        action = "add" if enable else "remove"
                        stdout, stderr, code = cls._run_cmd(
                            ["wmctrl", "-i", "-r", window_id, "-b", f"{action},above"]
                        )
                        result["success"] = code == 0
                        result["message"] = (
                            f"Window always on top set to {enable}"
                            if code == 0
                            else stderr
                        )
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                app_name = target_window.get("app", "")
                scpt = f'''
                tell application "System Events"
                    tell process "{app_name}"
                        set value of attribute "AXFloating" of window 1 to {str(enable).lower()}
                    end tell
                end tell
                '''
                stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = (
                    f"Window always on top set to {enable}" if code == 0 else stderr
                )

            elif system == "Windows":
                handle = target_window.get("id", "")
                ps_cmd = f"""
                Add-Type @"
                    using System;
                    using System.Runtime.InteropServices;
                    public class Win32 {{
                        [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
                        public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
                        public static readonly IntPtr HWND_NOTOPMOST = new IntPtr(-2);
                    }}
"@
                $handle = [IntPtr]::new({handle})
                if ({str(enable).lower()}) {{
                    [Win32]::SetWindowPos($handle, [Win32]::HWND_TOPMOST, 0, 0, 0, 0, 0x0003)
                }} else {{
                    [Win32]::SetWindowPos($handle, [Win32]::HWND_NOTOPMOST, 0, 0, 0, 0, 0x0003)
                }}
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_cmd]
                )
                result["success"] = code == 0
                result["message"] = (
                    f"Window always on top set to {enable}" if code == 0 else stderr
                )

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error setting always on top: {e}", "ERROR")

        return result

    # ========================================
    # WINDOW POSITIONING
    # ========================================

    @classmethod
    def move_window_position(cls, window_query: str, x: int, y: int) -> Dict[str, Any]:
        """Move a window to specific coordinates."""
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "movewindowpixel",
                            f"exact {x} {y},address:{address}",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        f"Window moved to ({x}, {y})" if code == 0 else stderr
                    )

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
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
                    result["message"] = (
                        f"Window moved to ({x}, {y})" if code == 0 else stderr
                    )

                else:
                    if shutil.which("wmctrl"):
                        window_id = target_window.get("id", "")
                        stdout, stderr, code = cls._run_cmd(
                            ["wmctrl", "-i", "-r", window_id, "-e", f"0,{x},{y},-1,-1"]
                        )
                        result["success"] = code == 0
                        result["message"] = (
                            f"Window moved to ({x}, {y})" if code == 0 else stderr
                        )
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                app_name = target_window.get("app", "")
                scpt = f'''
                tell application "System Events"
                    tell process "{app_name}"
                        set position of window 1 to {{{x}, {y}}}
                    end tell
                end tell
                '''
                stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = (
                    f"Window moved to ({x}, {y})" if code == 0 else stderr
                )

            elif system == "Windows":
                handle = target_window.get("id", "")
                ps_cmd = f"""
                Add-Type @"
                    using System;
                    using System.Runtime.InteropServices;
                    public class Win32 {{
                        [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
                    }}
"@
                [Win32]::SetWindowPos([IntPtr]::new({handle}), [IntPtr]::Zero, {x}, {y}, 0, 0, 0x0001)
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_cmd]
                )
                result["success"] = code == 0
                result["message"] = (
                    f"Window moved to ({x}, {y})" if code == 0 else stderr
                )

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error moving window: {e}", "ERROR")

        return result

    @classmethod
    def resize_window(
        cls, window_query: str, width: int, height: int
    ) -> Dict[str, Any]:
        """Resize a window to specific dimensions."""
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "resizewindowpixel",
                            f"exact {width} {height},address:{address}",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        f"Window resized to {width}x{height}" if code == 0 else stderr
                    )

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
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
                    result["message"] = (
                        f"Window resized to {width}x{height}" if code == 0 else stderr
                    )

                else:
                    if shutil.which("wmctrl"):
                        window_id = target_window.get("id", "")
                        stdout, stderr, code = cls._run_cmd(
                            [
                                "wmctrl",
                                "-i",
                                "-r",
                                window_id,
                                "-e",
                                f"0,-1,-1,{width},{height}",
                            ]
                        )
                        result["success"] = code == 0
                        result["message"] = (
                            f"Window resized to {width}x{height}"
                            if code == 0
                            else stderr
                        )
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                app_name = target_window.get("app", "")
                scpt = f'''
                tell application "System Events"
                    tell process "{app_name}"
                        set size of window 1 to {{{width}, {height}}}
                    end tell
                end tell
                '''
                stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = (
                    f"Window resized to {width}x{height}" if code == 0 else stderr
                )

            elif system == "Windows":
                handle = target_window.get("id", "")
                ps_cmd = f"""
                Add-Type @"
                    using System;
                    using System.Runtime.InteropServices;
                    public class Win32 {{
                        [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
                    }}
"@
                [Win32]::SetWindowPos([IntPtr]::new({handle}), [IntPtr]::Zero, 0, 0, {width}, {height}, 0x0001)
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_cmd]
                )
                result["success"] = code == 0
                result["message"] = (
                    f"Window resized to {width}x{height}" if code == 0 else stderr
                )

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error resizing window: {e}", "ERROR")

        return result

    @classmethod
    def snap_window(cls, window_query: str, direction: str) -> Dict[str, Any]:
        """Snap a window to a screen edge/corner.

        Directions: left, right, top, bottom, top_left, top_right, bottom_left, bottom_right, center
        """
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    # Map direction to Hyprland submap
                    direction_map = {
                        "left": "l",
                        "right": "r",
                        "top": "u",
                        "bottom": "d",
                        "top_left": "ul",
                        "top_right": "ur",
                        "bottom_left": "dl",
                        "bottom_right": "dr",
                        "center": "c",
                    }
                    hypr_dir = direction_map.get(direction.lower(), "c")
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "movewindow",
                            hypr_dir,
                            f"address:{address}",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        f"Window snapped to {direction}" if code == 0 else stderr
                    )

                elif display_server == "Sway":
                    # Use resize set with percentages
                    if direction.lower() == "left":
                        cmd = [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "resize",
                            "set",
                            "50",
                            "100",
                            "&&",
                            "move",
                            "position",
                            "0",
                            "0",
                        ]
                    elif direction.lower() == "right":
                        cmd = [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "resize",
                            "set",
                            "50",
                            "100",
                            "&&",
                            "move",
                            "position",
                            "center",
                        ]
                    else:
                        cmd = [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "floating",
                            "toggle",
                        ]
                    stdout, stderr, code = cls._run_cmd(cmd)
                    result["success"] = code == 0
                    result["message"] = (
                        f"Window snapped to {direction}" if code == 0 else stderr
                    )

                else:
                    if shutil.which("wmctrl"):
                        window_id = target_window.get("id", "")
                        # Use wmctrl with gravity
                        stdout, stderr, code = cls._run_cmd(
                            [
                                "wmctrl",
                                "-i",
                                "-r",
                                window_id,
                                "-b",
                                "add,maximized_vert",
                            ]
                        )
                        result["success"] = code == 0
                        result["message"] = f"Window snapped" if code == 0 else stderr
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                # Use Rectangle.app if available, otherwise use AppleScript
                app_name = target_window.get("app", "")
                if shutil.which("rectangle"):
                    direction_map = {
                        "left": "left",
                        "right": "right",
                        "top": "maximize",
                        "bottom": "maximize",
                        "top_left": "top-left",
                        "top_right": "top-right",
                        "bottom_left": "bottom-left",
                        "bottom_right": "bottom-right",
                        "center": "center",
                    }
                    rect_dir = direction_map.get(direction.lower(), "center")
                    cls._run_cmd(["open", "-a", "Rectangle"])
                    # Rectangle uses keyboard shortcuts
                    cls._run_shell_cmd(
                        f'osascript -e \'tell application "System Events" to keystroke "{rect_dir}" using {{control down, option down}}\''
                    )
                    result["success"] = True
                    result["message"] = f"Window snapped to {direction}"
                else:
                    result["message"] = (
                        "macOS requires Rectangle.app for window snapping"
                    )

            elif system == "Windows":
                # Use Win+Arrow keys
                handle = target_window.get("id", "")
                key_map = {
                    "left": "{LEFT}",
                    "right": "{RIGHT}",
                    "top": "{UP}",
                    "bottom": "{DOWN}",
                    "center": "",
                }
                key = key_map.get(direction.lower(), "")
                if key:
                    ps_cmd = f"""
                    $wshell = New-Object -ComObject WScript.Shell
                    $wshell.AppActivate('{target_window.get("title", "")}')
                    Start-Sleep -Milliseconds 100
                    $wshell.SendKeys('({key})')
                    """
                    stdout, stderr, code = cls._run_cmd(
                        ["powershell", "-NoProfile", "-Command", ps_cmd]
                    )
                    result["success"] = code == 0
                    result["message"] = f"Window snapped to {direction}"
                else:
                    result["message"] = "Invalid direction"

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error snapping window: {e}", "ERROR")

        return result

    # ========================================
    # WINDOW ARRANGEMENT
    # ========================================

    @classmethod
    def tile_windows(
        cls, window_queries: List[str], layout: str = "horizontal"
    ) -> Dict[str, Any]:
        """Tile multiple windows in a specific layout.

        Layouts: horizontal, vertical, grid
        """
        system = platform.system()
        result = {"success": False, "message": "", "windows": []}

        windows = cls.list_windows()
        matched_windows = []

        for query in window_queries:
            matches = [
                w
                for w in windows
                if query.lower() in w.get("title", "").lower()
                or query.lower() in w.get("app", "").lower()
            ]
            if matches:
                matched_windows.append(matches[0])

        if not matched_windows:
            result["message"] = "No windows found matching queries"
            return result

        result["windows"] = matched_windows
        count = len(matched_windows)

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    # Hyprland has built-in tiling
                    for i, w in enumerate(matched_windows):
                        address = w.get("id", "")
                        # Enable tiling mode
                        cls._run_cmd(
                            ["hyprctl", "dispatch", "togglesplit", f"address:{address}"]
                        )
                    result["success"] = True
                    result["message"] = f"Tiled {count} windows"

                elif display_server == "Sway":
                    # Sway is tiling by default
                    for i, w in enumerate(matched_windows):
                        cls._run_cmd(
                            [
                                "swaymsg",
                                f'[title="(?i).*{re.escape(w.get("title", ""))}.*"]',
                                "splith" if layout == "horizontal" else "splitv",
                            ]
                        )
                    result["success"] = True
                    result["message"] = f"Tiled {count} windows"

                else:
                    result["message"] = "Tiling requires tiling window manager"

            elif system == "Darwin":
                # Use Rectangle.app
                if shutil.which("rectangle"):
                    result["success"] = True
                    result["message"] = (
                        f"Use Rectangle.app keyboard shortcuts to tile {count} windows"
                    )
                else:
                    result["message"] = "macOS requires Rectangle.app for tiling"

            elif system == "Windows":
                # Use Windows snap
                for i, w in enumerate(matched_windows):
                    if layout == "horizontal":
                        if i % 2 == 0:
                            cls.snap_window(w.get("title", w.get("app", "")), "left")
                        else:
                            cls.snap_window(w.get("title", w.get("app", "")), "right")
                result["success"] = True
                result["message"] = f"Arranged {count} windows"

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error tiling windows: {e}", "ERROR")

        return result

    # ========================================
    # FOCUS WINDOW
    # ========================================

    @classmethod
    def focus_window(cls, window_query: str) -> Dict[str, Any]:
        """Focus a window by title or app name match."""
        system = platform.system()
        result = {"success": False, "message": "", "matched_window": None}

        windows = cls.list_windows()
        query_lower = window_query.lower()
        matches = []

        for w in windows:
            title_lower = w.get("title", "").lower()
            app_lower = w.get("app", "").lower()
            if query_lower in title_lower or query_lower in app_lower:
                matches.append(w)

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["matched_window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    stdout, stderr, code = cls._run_cmd(
                        ["hyprctl", "dispatch", "focuswindow", f"address:{address}"]
                    )
                    result["success"] = code == 0

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "focus",
                        ]
                    )
                    result["success"] = code == 0

                else:
                    if shutil.which("wmctrl"):
                        stdout, stderr, code = cls._run_cmd(
                            ["wmctrl", "-a", window_query]
                        )
                        result["success"] = code == 0
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                app_name = target_window.get("app", "")
                scpt = f'''
                tell application "{app_name}"
                    activate
                end tell
                '''
                stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0

            elif system == "Windows":
                handle = target_window.get("id", "")
                ps_code = f"""
                $w = New-Object -ComObject WScript.Shell
                $w.AppActivate('{target_window.get("title", "")}')
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_code]
                )
                result["success"] = "True" in stdout

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error focusing window: {e}", "ERROR")

        if result["success"]:
            result["message"] = "Window focused"
        elif not result["message"]:
            result["message"] = "Failed to focus window"

        return result

    # ========================================
    # CLOSE WINDOW
    # ========================================

    @classmethod
    def close_window(cls, window_query: str) -> Dict[str, Any]:
        """Close a window gracefully."""
        system = platform.system()
        result = {"success": False, "message": "", "matched_window": None}

        windows = cls.list_windows()
        query_lower = window_query.lower()
        matches = []

        for w in windows:
            title_lower = w.get("title", "").lower()
            app_lower = w.get("app", "").lower()
            if query_lower in title_lower or query_lower in app_lower:
                matches.append(w)

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["matched_window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    stdout, stderr, code = cls._run_cmd(
                        ["hyprctl", "dispatch", "closewindow", f"address:{address}"]
                    )
                    result["success"] = code == 0

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "swaymsg",
                            f'[title="(?i).*{re.escape(window_query)}.*"]',
                            "kill",
                        ]
                    )
                    result["success"] = code == 0

                else:
                    if shutil.which("wmctrl"):
                        stdout, stderr, code = cls._run_cmd(
                            ["wmctrl", "-c", window_query]
                        )
                        result["success"] = code == 0
                    else:
                        result["message"] = "wmctrl not found"

            elif system == "Darwin":
                app_name = target_window.get("app", "")
                scpt = f'''
                tell application "{app_name}"
                    activate
                    delay 0.2
                    tell application "System Events"
                        keystroke "w" using command down
                    end tell
                end tell
                '''
                stdout, stderr, code = cls._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0

            elif system == "Windows":
                handle = target_window.get("id", "")
                ps_code = f"""
                $proc = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{window_query}*"}} | Select-Object -First 1
                if ($proc) {{
                    $proc.CloseMainWindow() | Out-Null
                    "Success"
                }}
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_code]
                )
                result["success"] = "Success" in stdout

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error closing window: {e}", "ERROR")

        if result["success"]:
            result["message"] = "Window closed"
        elif not result["message"]:
            result["message"] = "Failed to close window"

        return result

    # ========================================
    # MONITOR MANAGEMENT
    # ========================================

    @classmethod
    def list_monitors(cls) -> List[Dict[str, Any]]:
        """List all connected monitors/displays."""
        system = platform.system()
        monitors = []

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    stdout, stderr, code = cls._run_cmd(["hyprctl", "monitors", "-j"])
                    if code == 0 and stdout:
                        try:
                            data = json.loads(stdout)
                            for m in data:
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
                                        "refresh_rate": m.get("refreshRate", 0),
                                    }
                                )
                        except:
                            pass

                elif display_server == "Sway":
                    stdout, stderr, code = cls._run_cmd(
                        ["swaymsg", "-t", "get_outputs"]
                    )
                    if code == 0 and stdout:
                        try:
                            data = json.loads(stdout)
                            for i, m in enumerate(data):
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
                        except:
                            pass
                else:
                    # Use xrandr
                    if shutil.which("xrandr"):
                        stdout, stderr, code = cls._run_cmd(["xrandr", "--query"])
                        if code == 0 and stdout:
                            for line in stdout.splitlines():
                                if " connected" in line:
                                    parts = line.split()
                                    name = parts[0]
                                    primary = "primary" in line

                                    # Parse resolution
                                    res_match = re.search(
                                        r"(\d+)x(\d+)\+(\d+)\+(\d+)", line
                                    )
                                    if res_match:
                                        monitors.append(
                                            {
                                                "id": str(len(monitors)),
                                                "name": name,
                                                "width": int(res_match.group(1)),
                                                "height": int(res_match.group(2)),
                                                "x": int(res_match.group(3)),
                                                "y": int(res_match.group(4)),
                                                "primary": primary,
                                                "scale": 1.0,
                                            }
                                        )

            elif system == "Darwin":
                # Use system_profiler
                stdout, stderr, code = cls._run_cmd(
                    ["system_profiler", "SPDisplaysDataType"], timeout=15
                )
                if code == 0 and stdout:
                    current_monitor = {}
                    for line in stdout.splitlines():
                        if "Display Type:" in line or "Resolution:" in line:
                            if "Resolution:" in line:
                                res_match = re.search(r"(\d+) x (\d+)", line)
                                if res_match:
                                    current_monitor["width"] = int(res_match.group(1))
                                    current_monitor["height"] = int(res_match.group(2))
                                    current_monitor["id"] = str(len(monitors))
                                    current_monitor["name"] = (
                                        f"Display {len(monitors) + 1}"
                                    )
                                    current_monitor["x"] = 0
                                    current_monitor["y"] = 0
                                    current_monitor["primary"] = len(monitors) == 0
                                    current_monitor["scale"] = 1.0
                                    monitors.append(current_monitor)
                                    current_monitor = {}

            elif system == "Windows":
                ps_cmd = """
                Add-Type -AssemblyName System.Windows.Forms
                [System.Windows.Forms.Screen]::AllScreens | ForEach-Object {
                    "$($_.DeviceName)|||$($_.Bounds.Width)|||$($_.Bounds.Height)|||$($_.Bounds.X)|||$($_.Bounds.Y)|||$($_.Primary)"
                }
                """
                stdout, stderr, code = cls._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_cmd]
                )
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

    @classmethod
    def move_window_to_monitor(
        cls, window_query: str, monitor_id: str
    ) -> Dict[str, Any]:
        """Move a window to a different monitor."""
        system = platform.system()
        result = {"success": False, "message": "", "window": None}

        windows = cls.list_windows()
        matches = [
            w
            for w in windows
            if window_query.lower() in w.get("title", "").lower()
            or window_query.lower() in w.get("app", "").lower()
        ]

        if not matches:
            result["message"] = f"No window found matching '{window_query}'"
            return result

        target_window = matches[0]
        result["window"] = target_window

        try:
            if system == "Linux":
                display_server = cls.get_display_server()

                if display_server == "Hyprland":
                    address = target_window.get("id", "")
                    stdout, stderr, code = cls._run_cmd(
                        [
                            "hyprctl",
                            "dispatch",
                            "movewindow",
                            f"mon:{monitor_id}",
                            f"address:{address}",
                        ]
                    )
                    result["success"] = code == 0
                    result["message"] = (
                        f"Window moved to monitor {monitor_id}" if code == 0 else stderr
                    )

                elif display_server == "Sway":
                    # Get monitor name from ID
                    monitors = cls.list_monitors()
                    monitor_name = None
                    for m in monitors:
                        if m["id"] == monitor_id:
                            monitor_name = m["name"]
                            break

                    if monitor_name:
                        stdout, stderr, code = cls._run_cmd(
                            [
                                "swaymsg",
                                f'[title="(?i).*{re.escape(window_query)}.*"]',
                                "move",
                                "window",
                                "to",
                                "output",
                                monitor_name,
                            ]
                        )
                        result["success"] = code == 0
                        result["message"] = (
                            f"Window moved to monitor {monitor_name}"
                            if code == 0
                            else stderr
                        )
                    else:
                        result["message"] = f"Monitor {monitor_id} not found"

                else:
                    result["message"] = "Multi-monitor requires tiling window manager"

            elif system == "Darwin":
                if shutil.which("yabai"):
                    stdout, stderr, code = cls._run_cmd(
                        ["yabai", "-m", "query", "--windows"]
                    )
                    if code == 0:
                        try:
                            yabai_windows = json.loads(stdout)
                            for yw in yabai_windows:
                                if window_query.lower() in yw.get("app", "").lower():
                                    cls._run_cmd(
                                        [
                                            "yabai",
                                            "-m",
                                            "window",
                                            str(yw.get("id")),
                                            "--display",
                                            monitor_id,
                                        ]
                                    )
                                    result["success"] = True
                                    result["message"] = (
                                        f"Window moved to display {monitor_id}"
                                    )
                                    break
                        except:
                            pass
                else:
                    result["message"] = (
                        "macOS requires yabai for multi-monitor window management"
                    )

            elif system == "Windows":
                monitors = cls.list_monitors()
                target_monitor = None
                for m in monitors:
                    if m["id"] == monitor_id:
                        target_monitor = m
                        break

                if target_monitor:
                    handle = target_window.get("id", "")
                    x = target_monitor["x"]
                    y = target_monitor["y"]
                    ps_cmd = f"""
                    Add-Type @"
                        using System;
                        using System.Runtime.InteropServices;
                        public class Win32 {{
                            [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
                        }}
"@
                    [Win32]::SetWindowPos([IntPtr]::new({handle}), [IntPtr]::Zero, {x}, {y}, 0, 0, 0x0001)
                    """
                    stdout, stderr, code = cls._run_cmd(
                        ["powershell", "-NoProfile", "-Command", ps_cmd]
                    )
                    result["success"] = code == 0
                    result["message"] = f"Window moved to monitor {monitor_id}"
                else:
                    result["message"] = f"Monitor {monitor_id} not found"

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error moving window to monitor: {e}", "ERROR")

        return result


# ============================================================
# BROWSER TAB MANAGER
# ============================================================


class BrowserTabManager:
    """Manage browser tabs across different browsers."""

    SUPPORTED_BROWSERS = ["chrome", "firefox", "edge", "safari", "brave"]

    @classmethod
    def list_tabs(cls, browser: str = "chrome") -> Dict[str, Any]:
        """List all open tabs in a browser."""
        system = platform.system()
        result = {"success": False, "tabs": [], "message": ""}

        browser = browser.lower()

        if browser == "safari" and system != "Darwin":
            result["message"] = "Safari is only available on macOS"
            return result

        try:
            if browser == "chrome":
                result = cls._list_chrome_tabs(system)
            elif browser == "firefox":
                result = cls._list_firefox_tabs(system)
            elif browser == "edge":
                result = cls._list_edge_tabs(system)
            elif browser == "safari":
                result = cls._list_safari_tabs()
            elif browser == "brave":
                result = cls._list_brave_tabs(system)
            else:
                result["message"] = f"Unsupported browser: {browser}"

        except Exception as e:
            result["message"] = f"Error: {str(e)}"
            log(f"Error listing browser tabs: {e}", "ERROR")

        return result

    @classmethod
    def _list_chrome_tabs(cls, system: str) -> Dict[str, Any]:
        """List Chrome tabs using debugging protocol."""
        result = {"success": False, "tabs": [], "message": ""}

        # Chrome remote debugging needs to be enabled
        # This is a simplified version that uses AppleScript/automation
        if system == "Darwin":
            scpt = """
            tell application "Google Chrome"
                set tabList to {}
                repeat with w in windows
                    set windowIndex to index of w
                    repeat with t in tabs of w
                        set end of tabList to (windowIndex & "|||" & index of t & "|||" & title of t & "|||" & URL of t)
                    end repeat
                end repeat
                return tabList as string
            end tell
            """
            stdout, stderr, code = WindowManager._run_cmd(
                ["osascript", "-e", scpt], timeout=15
            )

            if code == 0 and stdout:
                for line in stdout.split(", "):
                    if "|||" in line:
                        parts = line.split("|||")
                        if len(parts) >= 4:
                            result["tabs"].append(
                                {
                                    "window_id": parts[0].strip(),
                                    "tab_id": parts[1].strip(),
                                    "title": parts[2].strip(),
                                    "url": parts[3].strip(),
                                }
                            )
                result["success"] = True
                result["message"] = f"Found {len(result['tabs'])} tabs"
            else:
                result["message"] = (
                    "Failed to get Chrome tabs. Ensure Chrome is running."
                )

        elif system == "Windows":
            # Use PowerShell with Chrome COM object (limited)
            result["message"] = (
                "Chrome tab listing on Windows requires Chrome extension or debugging protocol"
            )

        elif system == "Linux":
            # Could use Chrome DevTools Protocol
            result["message"] = (
                "Chrome tab listing on Linux requires Chrome DevTools Protocol"
            )

        return result

    @classmethod
    def _list_firefox_tabs(cls, system: str) -> Dict[str, Any]:
        """List Firefox tabs."""
        result = {"success": False, "tabs": [], "message": ""}

        # Firefox doesn't expose tabs via AppleScript easily
        result["message"] = (
            "Firefox tab listing requires browser extension or remote debugging"
        )

        return result

    @classmethod
    def _list_edge_tabs(cls, system: str) -> Dict[str, Any]:
        """List Microsoft Edge tabs."""
        result = {"success": False, "tabs": [], "message": ""}

        if system == "Darwin":
            scpt = """
            tell application "Microsoft Edge"
                set tabList to {}
                repeat with w in windows
                    set windowIndex to index of w
                    repeat with t in tabs of w
                        set end of tabList to (windowIndex & "|||" & index of t & "|||" & title of t & "|||" & URL of t)
                    end repeat
                end repeat
                return tabList as string
            end tell
            """
            stdout, stderr, code = WindowManager._run_cmd(
                ["osascript", "-e", scpt], timeout=15
            )

            if code == 0 and stdout:
                for line in stdout.split(", "):
                    if "|||" in line:
                        parts = line.split("|||")
                        if len(parts) >= 4:
                            result["tabs"].append(
                                {
                                    "window_id": parts[0].strip(),
                                    "tab_id": parts[1].strip(),
                                    "title": parts[2].strip(),
                                    "url": parts[3].strip(),
                                }
                            )
                result["success"] = True
                result["message"] = f"Found {len(result['tabs'])} tabs"
            else:
                result["message"] = "Failed to get Edge tabs. Ensure Edge is running."
        else:
            result["message"] = (
                "Edge tab listing requires additional setup on this platform"
            )

        return result

    @classmethod
    def _list_safari_tabs(cls) -> Dict[str, Any]:
        """List Safari tabs (macOS only)."""
        result = {"success": False, "tabs": [], "message": ""}

        scpt = """
        tell application "Safari"
            set tabList to {}
            repeat with w in windows
                set windowIndex to index of w
                repeat with t in tabs of w
                    set end of tabList to (windowIndex & "|||" & index of t & "|||" & name of t & "|||" & URL of t)
                end repeat
            end repeat
            return tabList as string
        end tell
        """
        stdout, stderr, code = WindowManager._run_cmd(
            ["osascript", "-e", scpt], timeout=15
        )

        if code == 0 and stdout:
            for line in stdout.split(", "):
                if "|||" in line:
                    parts = line.split("|||")
                    if len(parts) >= 4:
                        result["tabs"].append(
                            {
                                "window_id": parts[0].strip(),
                                "tab_id": parts[1].strip(),
                                "title": parts[2].strip(),
                                "url": parts[3].strip(),
                            }
                        )
            result["success"] = True
            result["message"] = f"Found {len(result['tabs'])} tabs"
        else:
            result["message"] = "Failed to get Safari tabs. Ensure Safari is running."

        return result

    @classmethod
    def _list_brave_tabs(cls, system: str) -> Dict[str, Any]:
        """List Brave browser tabs."""
        result = {"success": False, "tabs": [], "message": ""}

        if system == "Darwin":
            scpt = """
            tell application "Brave Browser"
                set tabList to {}
                repeat with w in windows
                    set windowIndex to index of w
                    repeat with t in tabs of w
                        set end of tabList to (windowIndex & "|||" & index of t & "|||" & title of t & "|||" & URL of t)
                    end repeat
                end repeat
                return tabList as string
            end tell
            """
            stdout, stderr, code = WindowManager._run_cmd(
                ["osascript", "-e", scpt], timeout=15
            )

            if code == 0 and stdout:
                for line in stdout.split(", "):
                    if "|||" in line:
                        parts = line.split("|||")
                        if len(parts) >= 4:
                            result["tabs"].append(
                                {
                                    "window_id": parts[0].strip(),
                                    "tab_id": parts[1].strip(),
                                    "title": parts[2].strip(),
                                    "url": parts[3].strip(),
                                }
                            )
                result["success"] = True
                result["message"] = f"Found {len(result['tabs'])} tabs"
            else:
                result["message"] = "Failed to get Brave tabs. Ensure Brave is running."
        else:
            result["message"] = (
                "Brave tab listing requires additional setup on this platform"
            )

        return result

    @classmethod
    def switch_tab(cls, browser: str, window_id: str, tab_id: str) -> Dict[str, Any]:
        """Switch to a specific tab in a browser."""
        system = platform.system()
        result = {"success": False, "message": ""}

        browser = browser.lower()

        try:
            if system == "Darwin":
                if browser == "chrome":
                    app_name = "Google Chrome"
                elif browser == "edge":
                    app_name = "Microsoft Edge"
                elif browser == "safari":
                    app_name = "Safari"
                elif browser == "brave":
                    app_name = "Brave Browser"
                else:
                    result["message"] = f"Unsupported browser: {browser}"
                    return result

                scpt = f'''
                tell application "{app_name}"
                    activate
                    set active tab index of window {window_id} to {tab_id}
                end tell
                '''
                stdout, stderr, code = WindowManager._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = "Tab switched" if code == 0 else stderr

            else:
                result["message"] = (
                    "Tab switching on this platform requires additional setup"
                )

        except Exception as e:
            result["message"] = f"Error: {str(e)}"

        return result

    @classmethod
    def close_tab(cls, browser: str, window_id: str, tab_id: str) -> Dict[str, Any]:
        """Close a specific tab in a browser."""
        system = platform.system()
        result = {"success": False, "message": ""}

        browser = browser.lower()

        try:
            if system == "Darwin":
                if browser == "chrome":
                    app_name = "Google Chrome"
                elif browser == "edge":
                    app_name = "Microsoft Edge"
                elif browser == "safari":
                    app_name = "Safari"
                elif browser == "brave":
                    app_name = "Brave Browser"
                else:
                    result["message"] = f"Unsupported browser: {browser}"
                    return result

                scpt = f'''
                tell application "{app_name}"
                    activate
                    close tab {tab_id} of window {window_id}
                end tell
                '''
                stdout, stderr, code = WindowManager._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = "Tab closed" if code == 0 else stderr

            else:
                result["message"] = (
                    "Tab closing on this platform requires additional setup"
                )

        except Exception as e:
            result["message"] = f"Error: {str(e)}"

        return result

    @classmethod
    def open_url(cls, browser: str, url: str) -> Dict[str, Any]:
        """Open a URL in a browser."""
        system = platform.system()
        result = {"success": False, "message": ""}

        browser = browser.lower()

        # Ensure URL has protocol
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            if system == "Darwin":
                if browser == "chrome":
                    app_name = "Google Chrome"
                elif browser == "edge":
                    app_name = "Microsoft Edge"
                elif browser == "safari":
                    app_name = "Safari"
                elif browser == "brave":
                    app_name = "Brave Browser"
                elif browser == "firefox":
                    app_name = "Firefox"
                else:
                    app_name = "Google Chrome"  # Default

                scpt = f'''
                tell application "{app_name}"
                    open location "{url}"
                    activate
                end tell
                '''
                stdout, stderr, code = WindowManager._run_cmd(["osascript", "-e", scpt])
                result["success"] = code == 0
                result["message"] = f"Opened {url}" if code == 0 else stderr

            elif system == "Windows":
                ps_cmd = f'''
                Start-Process "{browser}" "{url}"
                '''
                stdout, stderr, code = WindowManager._run_cmd(
                    ["powershell", "-NoProfile", "-Command", ps_cmd]
                )
                result["success"] = code == 0
                result["message"] = f"Opened {url}" if code == 0 else stderr

            elif system == "Linux":
                # Use xdg-open or specific browser command
                browser_commands = {
                    "chrome": "google-chrome",
                    "firefox": "firefox",
                    "edge": "microsoft-edge",
                    "brave": "brave-browser",
                }

                cmd = browser_commands.get(browser, "xdg-open")
                stdout, stderr, code = WindowManager._run_cmd([cmd, url])
                result["success"] = code == 0
                result["message"] = f"Opened {url}" if code == 0 else stderr

        except Exception as e:
            result["message"] = f"Error: {str(e)}"

        return result


# ============================================================
# TODO MANAGER
# ============================================================


class TodoManager:
    """Manages todo lists for complex task tracking."""

    def __init__(self, base_dir: Path):
        self.todo_file = base_dir / TODO_FILE
        self._ensure_todo_file()

    def _ensure_todo_file(self):
        if not self.todo_file.exists():
            self._save_todos({"tasks": [], "metadata": {}})

    def _load_todos(self) -> Dict:
        try:
            if self.todo_file.exists():
                content = self.todo_file.read_text(encoding="utf-8")
                return json.loads(content)
        except Exception as e:
            log(f"Error loading todos: {e}", "ERROR")
        return {"tasks": [], "metadata": {}}

    def _save_todos(self, data: Dict):
        try:
            self.todo_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            log(f"Error saving todos: {e}", "ERROR")

    def _generate_id(self) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_suffix = hashlib.md5(
            str(datetime.now().timestamp()).encode()
        ).hexdigest()[:6]
        return f"task_{timestamp}_{random_suffix}"

    def add_task(
        self,
        title: str,
        description: str = "",
        priority: str = "medium",
        dependencies: List[str] = None,
        assigned_files: List[str] = None,
    ) -> Dict:
        data = self._load_todos()
        now = datetime.now().isoformat()

        task = {
            "id": self._generate_id(),
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
        data["metadata"]["last_updated"] = now
        self._save_todos(data)

        return task

    def update_task(
        self,
        task_id: str,
        status: str = None,
        title: str = None,
        description: str = None,
        priority: str = None,
        add_note: str = None,
        add_subtask: str = None,
    ) -> Optional[Dict]:
        data = self._load_todos()

        for task in data["tasks"]:
            if task["id"] == task_id:
                if status:
                    task["status"] = status
                if title:
                    task["title"] = title
                if description:
                    task["description"] = description
                if priority:
                    task["priority"] = priority
                if add_note:
                    task["notes"].append(
                        {"content": add_note, "timestamp": datetime.now().isoformat()}
                    )
                if add_subtask:
                    subtask_id = f"{task_id}_sub_{len(task['subtasks'])}"
                    task["subtasks"].append(
                        {"id": subtask_id, "content": add_subtask, "completed": False}
                    )

                task["updated_at"] = datetime.now().isoformat()
                data["metadata"]["last_updated"] = datetime.now().isoformat()
                self._save_todos(data)
                return task

        return None

    def delete_task(self, task_id: str) -> bool:
        data = self._load_todos()
        original_len = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]

        if len(data["tasks"]) < original_len:
            self._save_todos(data)
            return True
        return False

    def get_task(self, task_id: str) -> Optional[Dict]:
        data = self._load_todos()
        for task in data["tasks"]:
            if task["id"] == task_id:
                return task
        return None

    def list_tasks(
        self, status: str = None, priority: str = None, include_completed: bool = True
    ) -> List[Dict]:
        data = self._load_todos()
        tasks = data["tasks"]

        if status:
            tasks = [t for t in tasks if t["status"] == status]
        if priority:
            tasks = [t for t in tasks if t["priority"] == priority]
        if not include_completed:
            tasks = [t for t in tasks if t["status"] != "completed"]

        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tasks.sort(
            key=lambda t: (priority_order.get(t["priority"], 99), t["created_at"])
        )

        return tasks

    def get_summary(self) -> Dict:
        data = self._load_todos()
        tasks = data["tasks"]

        return {
            "total": len(tasks),
            "pending": len([t for t in tasks if t["status"] == "pending"]),
            "in_progress": len([t for t in tasks if t["status"] == "in_progress"]),
            "completed": len([t for t in tasks if t["status"] == "completed"]),
            "blocked": len([t for t in tasks if t["status"] == "blocked"]),
            "by_priority": {
                "critical": len([t for t in tasks if t["priority"] == "critical"]),
                "high": len([t for t in tasks if t["priority"] == "high"]),
                "medium": len([t for t in tasks if t["priority"] == "medium"]),
                "low": len([t for t in tasks if t["priority"] == "low"]),
            },
        }

    def clear_completed(self) -> int:
        data = self._load_todos()
        original_len = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t["status"] != "completed"]
        self._save_todos(data)
        return original_len - len(data["tasks"])


# Initialize todo manager
todo_manager = None


def get_todo_manager() -> TodoManager:
    global todo_manager
    if todo_manager is None:
        todo_manager = TodoManager(BASE_DIR)
    return todo_manager


# ============================================================
# FILE EDITOR
# ============================================================


class FileEditor:
    """Advanced file editing utilities."""

    @staticmethod
    def read_lines(file_path: Path) -> List[str]:
        content = file_path.read_text(encoding="utf-8")
        return content.splitlines()

    @staticmethod
    def write_lines(file_path: Path, lines: List[str]):
        content = "\n".join(lines)
        file_path.write_text(content, encoding="utf-8")

    @classmethod
    def replace_lines(
        cls, file_path: Path, start_line: int, end_line: int, new_content: str
    ) -> Dict[str, Any]:
        lines = cls.read_lines(file_path)
        total_lines = len(lines)

        if start_line < 1:
            start_line = 1
        if end_line > total_lines:
            end_line = total_lines
        if start_line > end_line:
            return {
                "success": False,
                "message": f"Invalid line range: {start_line}-{end_line}",
            }

        start_idx = start_line - 1
        end_idx = end_line

        replaced_lines = lines[start_idx:end_idx]
        new_lines = new_content.splitlines()

        final_lines = lines[:start_idx] + new_lines + lines[end_idx:]
        cls.write_lines(file_path, final_lines)

        return {
            "success": True,
            "message": f"Replaced lines {start_line}-{end_line}",
            "replaced_content": "\n".join(replaced_lines),
            "lines_before": total_lines,
            "lines_after": len(final_lines),
        }

    @classmethod
    def insert_at_line(
        cls, file_path: Path, line_number: int, content: str, mode: str = "before"
    ) -> Dict[str, Any]:
        lines = cls.read_lines(file_path)
        total_lines = len(lines)

        if line_number < 1:
            line_number = 1
        elif line_number > total_lines + 1:
            line_number = total_lines + 1

        insert_idx = line_number - 1
        new_lines = content.splitlines()

        if mode == "after":
            insert_idx = line_number

        final_lines = lines[:insert_idx] + new_lines + lines[insert_idx:]
        cls.write_lines(file_path, final_lines)

        return {
            "success": True,
            "message": f"Inserted {len(new_lines)} lines at line {line_number} ({mode})",
            "lines_before": total_lines,
            "lines_after": len(final_lines),
        }

    @classmethod
    def delete_lines(
        cls, file_path: Path, start_line: int, end_line: int
    ) -> Dict[str, Any]:
        lines = cls.read_lines(file_path)
        total_lines = len(lines)

        if start_line < 1:
            start_line = 1
        if end_line > total_lines:
            end_line = total_lines

        start_idx = start_line - 1
        end_idx = end_line

        deleted_lines = lines[start_idx:end_idx]

        final_lines = lines[:start_idx] + lines[end_idx:]
        cls.write_lines(file_path, final_lines)

        return {
            "success": True,
            "message": f"Deleted lines {start_line}-{end_line}",
            "deleted_content": "\n".join(deleted_lines),
            "lines_before": total_lines,
            "lines_after": len(final_lines),
        }

    @classmethod
    def find_and_replace(
        cls,
        file_path: Path,
        search: str,
        replace: str,
        use_regex: bool = False,
        case_sensitive: bool = True,
        replace_all: bool = True,
    ) -> Dict[str, Any]:
        content = file_path.read_text(encoding="utf-8")

        matches = []

        if use_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(search, flags)

            for match in pattern.finditer(content):
                matches.append(
                    {"start": match.start(), "end": match.end(), "text": match.group()}
                )

            if replace_all:
                new_content, count = pattern.subn(replace, content)
            else:
                new_content, count = pattern.subn(replace, content, count=1)
        else:
            search_str = search if case_sensitive else search.lower()
            content_to_search = content if case_sensitive else content.lower()

            start = 0
            while True:
                pos = content_to_search.find(search_str, start)
                if pos == -1:
                    break
                matches.append(
                    {
                        "start": pos,
                        "end": pos + len(search),
                        "text": content[pos : pos + len(search)],
                    }
                )
                start = pos + len(search)

            if replace_all:
                if case_sensitive:
                    new_content = content.replace(search, replace)
                else:
                    pattern = re.compile(re.escape(search), re.IGNORECASE)
                    new_content = pattern.sub(replace, content)
                count = len(matches)
            else:
                if matches:
                    match = matches[0]
                    new_content = (
                        content[: match["start"]] + replace + content[match["end"] :]
                    )
                    count = 1
                else:
                    new_content = content
                    count = 0

        if count > 0:
            file_path.write_text(new_content, encoding="utf-8")

        return {
            "success": True,
            "matches_found": len(matches),
            "replacements_made": count,
            "message": f"Replaced {count} occurrence(s)",
        }


# ============================================================
# MCP TOOLS
# ============================================================

# --- SYSTEM INFORMATION ---


@mcp.tool()
def get_system_info() -> str:
    """
    Returns comprehensive system information including OS, CPU, memory, GPU, disk, and display server.
    Use this tool to understand the system capabilities and constraints.
    """
    try:
        info = SystemDetector.get_full_system_info()

        output = []
        output.append("=" * 60)
        output.append("SYSTEM INFORMATION")
        output.append("=" * 60)

        output.append(f"\n[Operating System]")
        output.append(f"  Name: {info.os_name}")
        output.append(f"  Version: {info.os_version}")
        output.append(f"  Architecture: {info.architecture}")
        output.append(f"  Hostname: {info.hostname}")
        output.append(f"  Python: {info.python_version}")
        output.append(f"  Display Server: {info.display_server}")

        output.append(f"\n[CPU]")
        cpu = info.cpu_info
        output.append(f"  Model: {cpu.get('model', 'Unknown')}")
        output.append(f"  Physical Cores: {cpu.get('physical_cores', 'Unknown')}")
        output.append(f"  Logical Cores: {cpu.get('logical_cores', 'Unknown')}")
        if cpu.get("max_frequency"):
            output.append(
                f"  Max Frequency: {cpu['max_frequency']['value']} {cpu['max_frequency']['unit']}"
            )
        output.append(f"  Usage: {cpu.get('cpu_percent', 0)}%")

        output.append(f"\n[Memory]")
        mem = info.memory_info
        if "error" not in mem:
            output.append(f"  Total: {mem['total']['value']} {mem['total']['unit']}")
            output.append(
                f"  Available: {mem['available']['value']} {mem['available']['unit']}"
            )
            output.append(
                f"  Used: {mem['used']['value']} {mem['used']['unit']} ({mem['percent']}%)"
            )
        else:
            output.append(f"  Error: {mem['error']}")

        output.append(f"\n[GPU(s)]")
        for i, gpu in enumerate(info.gpu_info, 1):
            if "error" in gpu:
                output.append(f"  {i}. {gpu['error']}")
            else:
                output.append(
                    f"  {i}. {gpu.get('vendor', 'Unknown')} - {gpu.get('model', 'Unknown')}"
                )

        output.append(f"\n[Disk(s)]")
        for disk in info.disk_info:
            output.append(
                f"  {disk['device']} - {disk['total_gb']}GB ({disk['percent']}% used)"
            )

        return "\n".join(output)

    except Exception as e:
        return f"Error getting system information: {str(e)}"


# --- WINDOW MANAGEMENT TOOLS ---


@mcp.tool()
def window_list() -> str:
    """
    List all open windows with detailed information.
    Returns window ID, title, app name, workspace, position, and size.
    """
    try:
        windows = WindowManager.list_windows()

        if not windows:
            return "No visible windows found."

        output = ["=== OPEN WINDOWS ==="]
        output.append(f"Display Server: {WindowManager.get_display_server()}")
        output.append(f"Total Windows: {len(windows)}\n")

        for i, w in enumerate(windows, 1):
            output.append(f"[{i}] {w.get('title', 'N/A')}")
            output.append(f"    App: {w.get('app', 'N/A')}")
            output.append(f"    ID: {w.get('id', 'N/A')}")
            output.append(
                f"    Workspace: {w.get('workspace_name', w.get('workspace', 'N/A'))}"
            )
            if w.get("size"):
                output.append(
                    f"    Size: {w['size'].get('width', 0)}x{w['size'].get('height', 0)}"
                )
            if w.get("fullscreen"):
                output.append(f"    State: Fullscreen")
            output.append("")

        return "\n".join(output)

    except Exception as e:
        return f"Error listing windows: {str(e)}"


@mcp.tool()
def workspace_list() -> str:
    """
    List all workspaces/desktops available on the system.
    Shows workspace ID, name, number of windows, and active status.
    """
    try:
        workspaces = WindowManager.list_workspaces()

        if not workspaces:
            return "No workspaces found or workspace management not supported."

        output = ["=== WORKSPACES ==="]
        output.append(f"Display Server: {WindowManager.get_display_server()}")
        output.append(f"Total Workspaces: {len(workspaces)}\n")

        for ws in workspaces:
            active_marker = " [ACTIVE]" if ws.get("active") else ""
            output.append(
                f"  [{ws.get('id', '?')}] {ws.get('name', 'Unnamed')}{active_marker}"
            )
            output.append(f"      Windows: {ws.get('windows', 0)}")
            if ws.get("monitor"):
                output.append(f"      Monitor: {ws.get('monitor')}")

        return "\n".join(output)

    except Exception as e:
        return f"Error listing workspaces: {str(e)}"


@mcp.tool()
def workspace_switch(workspace_id: str) -> str:
    """
    Switch to a specific workspace/desktop.

    Args:
        workspace_id: The workspace ID or number to switch to.
    """
    try:
        result = WindowManager.switch_workspace(workspace_id)

        if result["success"]:
            return f"✓ Switched to workspace {workspace_id}"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error switching workspace: {str(e)}"


@mcp.tool()
def window_move_to_workspace(
    window_query: str, workspace_id: str, follow: bool = False
) -> str:
    """
    Move a window to a different workspace.

    Args:
        window_query: Part of the window title or app name.
        workspace_id: Target workspace ID or number.
        follow: If True, also switch to the target workspace.
    """
    try:
        result = WindowManager.move_window_to_workspace(
            window_query, workspace_id, follow
        )

        if result["success"]:
            msg = f"✓ Moved '{result['window'].get('title', 'window')}' to workspace {workspace_id}"
            if follow:
                msg += " and switched to it"
            return msg
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error moving window: {str(e)}"


@mcp.tool()
def window_minimize(window_query: str) -> str:
    """
    Minimize a window.

    Args:
        window_query: Part of the window title or app name to minimize.
    """
    try:
        result = WindowManager.minimize_window(window_query)

        if result["success"]:
            return f"✓ Minimized '{result['window'].get('title', 'window')}'"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error minimizing window: {str(e)}"


@mcp.tool()
def window_maximize(window_query: str, toggle: bool = True) -> str:
    """
    Maximize or unmaximize a window.

    Args:
        window_query: Part of the window title or app name.
        toggle: If True, toggle between maximized and normal state.
    """
    try:
        result = WindowManager.maximize_window(window_query, toggle)

        if result["success"]:
            return f"✓ Maximize toggled for '{result['window'].get('title', 'window')}'"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error maximizing window: {str(e)}"


@mcp.tool()
def window_fullscreen(window_query: str) -> str:
    """
    Toggle fullscreen mode for a window.

    Args:
        window_query: Part of the window title or app name.
    """
    try:
        result = WindowManager.toggle_fullscreen(window_query)

        if result["success"]:
            return (
                f"✓ Fullscreen toggled for '{result['window'].get('title', 'window')}'"
            )
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error toggling fullscreen: {str(e)}"


@mcp.tool()
def window_always_on_top(window_query: str, enable: bool = True) -> str:
    """
    Set a window to always stay on top of other windows.

    Args:
        window_query: Part of the window title or app name.
        enable: True to enable always-on-top, False to disable.
    """
    try:
        result = WindowManager.set_always_on_top(window_query, enable)

        if result["success"]:
            state = "enabled" if enable else "disabled"
            return f"✓ Always-on-top {state} for '{result['window'].get('title', 'window')}'"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error setting always-on-top: {str(e)}"


@mcp.tool()
def window_move(window_query: str, x: int, y: int) -> str:
    """
    Move a window to specific screen coordinates.

    Args:
        window_query: Part of the window title or app name.
        x: X coordinate (horizontal position).
        y: Y coordinate (vertical position).
    """
    try:
        result = WindowManager.move_window_position(window_query, x, y)

        if result["success"]:
            return f"✓ Moved '{result['window'].get('title', 'window')}' to ({x}, {y})"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error moving window: {str(e)}"


@mcp.tool()
def window_resize(window_query: str, width: int, height: int) -> str:
    """
    Resize a window to specific dimensions.

    Args:
        window_query: Part of the window title or app name.
        width: New window width in pixels.
        height: New window height in pixels.
    """
    try:
        result = WindowManager.resize_window(window_query, width, height)

        if result["success"]:
            return f"✓ Resized '{result['window'].get('title', 'window')}' to {width}x{height}"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error resizing window: {str(e)}"


@mcp.tool()
def window_snap(window_query: str, direction: str) -> str:
    """
    Snap a window to a screen edge or corner.

    Args:
        window_query: Part of the window title or app name.
        direction: One of: left, right, top, bottom, top_left, top_right,
                   bottom_left, bottom_right, center.
    """
    try:
        result = WindowManager.snap_window(window_query, direction)

        if result["success"]:
            return (
                f"✓ Snapped '{result['window'].get('title', 'window')}' to {direction}"
            )
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error snapping window: {str(e)}"


@mcp.tool()
def window_focus(window_query: str) -> str:
    """
    Bring a window to the front and focus it.

    Args:
        window_query: Part of the window title or app name to focus.
    """
    try:
        result = WindowManager.focus_window(window_query)

        if result["success"]:
            return f"✓ Focused '{result['matched_window'].get('title', 'window')}'"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error focusing window: {str(e)}"


@mcp.tool()
def window_close(window_query: str) -> str:
    """
    Close a window gracefully.

    Args:
        window_query: Part of the window title or app name to close.
    """
    try:
        result = WindowManager.close_window(window_query)

        if result["success"]:
            return f"✓ Closed '{result['matched_window'].get('title', 'window')}'"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error closing window: {str(e)}"


@mcp.tool()
def window_tile(window_queries: str, layout: str = "horizontal") -> str:
    """
    Tile multiple windows in a specific layout.

    Args:
        window_queries: Comma-separated list of window titles or app names.
        layout: Layout type - "horizontal", "vertical", or "grid".
    """
    try:
        queries = [q.strip() for q in window_queries.split(",") if q.strip()]
        result = WindowManager.tile_windows(queries, layout)

        if result["success"]:
            return f"✓ {result['message']}"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error tiling windows: {str(e)}"


# --- MONITOR TOOLS ---


@mcp.tool()
def monitor_list() -> str:
    """
    List all connected monitors/displays with their properties.
    Shows resolution, position, primary status, and scale.
    """
    try:
        monitors = WindowManager.list_monitors()

        output = ["=== MONITORS ==="]
        output.append(f"Total Monitors: {len(monitors)}\n")

        for m in monitors:
            primary_marker = " [PRIMARY]" if m.get("primary") else ""
            output.append(
                f"Monitor {m.get('id', '?')}: {m.get('name', 'Unknown')}{primary_marker}"
            )
            output.append(f"  Resolution: {m.get('width', 0)}x{m.get('height', 0)}")
            output.append(f"  Position: ({m.get('x', 0)}, {m.get('y', 0)})")
            output.append(f"  Scale: {m.get('scale', 1.0)}")
            output.append("")

        return "\n".join(output)

    except Exception as e:
        return f"Error listing monitors: {str(e)}"


@mcp.tool()
def window_move_to_monitor(window_query: str, monitor_id: str) -> str:
    """
    Move a window to a different monitor.

    Args:
        window_query: Part of the window title or app name.
        monitor_id: Target monitor ID.
    """
    try:
        result = WindowManager.move_window_to_monitor(window_query, monitor_id)

        if result["success"]:
            return f"✓ Moved '{result['window'].get('title', 'window')}' to monitor {monitor_id}"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error moving window to monitor: {str(e)}"


# --- BROWSER TAB TOOLS ---


@mcp.tool()
def browser_list_tabs(browser: str = "chrome") -> str:
    """
    List all open tabs in a browser.

    Args:
        browser: Browser name - "chrome", "firefox", "edge", "safari", or "brave".
    """
    try:
        result = BrowserTabManager.list_tabs(browser)

        if not result["success"]:
            return f"✗ {result['message']}"

        tabs = result["tabs"]

        output = [f"=== {browser.upper()} TABS ==="]
        output.append(f"Total Tabs: {len(tabs)}\n")

        current_window = None
        for tab in tabs:
            if tab["window_id"] != current_window:
                current_window = tab["window_id"]
                output.append(f"\nWindow {current_window}:")
            output.append(f"  [{tab['tab_id']}] {tab['title'][:50]}...")
            output.append(f"      {tab['url'][:60]}...")

        return "\n".join(output)

    except Exception as e:
        return f"Error listing browser tabs: {str(e)}"


@mcp.tool()
def browser_switch_tab(browser: str, window_id: str, tab_id: str) -> str:
    """
    Switch to a specific tab in a browser.

    Args:
        browser: Browser name.
        window_id: Window ID containing the tab.
        tab_id: Tab index to switch to.
    """
    try:
        result = BrowserTabManager.switch_tab(browser, window_id, tab_id)

        if result["success"]:
            return f"✓ Switched to tab {tab_id} in {browser}"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error switching tab: {str(e)}"


@mcp.tool()
def browser_close_tab(browser: str, window_id: str, tab_id: str) -> str:
    """
    Close a specific tab in a browser.

    Args:
        browser: Browser name.
        window_id: Window ID containing the tab.
        tab_id: Tab index to close.
    """
    try:
        result = BrowserTabManager.close_tab(browser, window_id, tab_id)

        if result["success"]:
            return f"✓ Closed tab {tab_id} in {browser}"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error closing tab: {str(e)}"


@mcp.tool()
def browser_open_url(browser: str, url: str) -> str:
    """
    Open a URL in a browser.

    Args:
        browser: Browser name - "chrome", "firefox", "edge", "safari", or "brave".
        url: URL to open (will add https:// if no protocol specified).
    """
    try:
        result = BrowserTabManager.open_url(browser, url)

        if result["success"]:
            return f"✓ Opened {url} in {browser}"
        return f"✗ {result['message']}"

    except Exception as e:
        return f"Error opening URL: {str(e)}"


# --- TODO LIST TOOLS ---


@mcp.tool()
def todo_add(
    title: str,
    description: str = "",
    priority: str = "medium",
    dependencies: str = "",
    assigned_files: str = "",
) -> str:
    """
    Add a new task to the todo list for complex task tracking.

    Args:
        title: The task title (required).
        description: Detailed task description.
        priority: Task priority - "low", "medium", "high", or "critical".
        dependencies: Comma-separated list of task IDs this task depends on.
        assigned_files: Comma-separated list of file paths this task involves.
    """
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

        valid_priorities = ["low", "medium", "high", "critical"]
        if priority.lower() not in valid_priorities:
            priority = "medium"

        manager = get_todo_manager()
        task = manager.add_task(
            title=title,
            description=description,
            priority=priority.lower(),
            dependencies=dep_list,
            assigned_files=file_list,
        )

        return (
            f"✓ Task Created Successfully\n"
            f"  ID: {task['id']}\n"
            f"  Title: {task['title']}\n"
            f"  Priority: {task['priority']}\n"
            f"  Status: {task['status']}"
        )

    except Exception as e:
        return f"Error creating task: {str(e)}"


@mcp.tool()
def todo_update(
    task_id: str,
    status: str = "",
    title: str = "",
    description: str = "",
    priority: str = "",
    add_note: str = "",
    add_subtask: str = "",
) -> str:
    """
    Update an existing task in the todo list.

    Args:
        task_id: The ID of the task to update (required).
        status: New status - "pending", "in_progress", "completed", "blocked", or "cancelled".
        title: New title for the task.
        description: New description.
        priority: New priority - "low", "medium", "high", or "critical".
        add_note: Add a note to the task.
        add_subtask: Add a subtask to the task.
    """
    try:
        manager = get_todo_manager()

        valid_statuses = ["pending", "in_progress", "completed", "blocked", "cancelled"]
        if status and status.lower() not in valid_statuses:
            return f"Invalid status. Must be one of: {', '.join(valid_statuses)}"

        valid_priorities = ["low", "medium", "high", "critical"]
        if priority and priority.lower() not in valid_priorities:
            return f"Invalid priority. Must be one of: {', '.join(valid_priorities)}"

        task = manager.update_task(
            task_id=task_id,
            status=status.lower() if status else None,
            title=title if title else None,
            description=description if description else None,
            priority=priority.lower() if priority else None,
            add_note=add_note if add_note else None,
            add_subtask=add_subtask if add_subtask else None,
        )

        if not task:
            return f"Task not found: {task_id}"

        return (
            f"✓ Task Updated Successfully\n"
            f"  ID: {task['id']}\n"
            f"  Title: {task['title']}\n"
            f"  Status: {task['status']}\n"
            f"  Priority: {task['priority']}"
        )

    except Exception as e:
        return f"Error updating task: {str(e)}"


@mcp.tool()
def todo_list(
    status: str = "", priority: str = "", include_completed: bool = True
) -> str:
    """
    List all tasks in the todo list with optional filtering.

    Args:
        status: Filter by status - "pending", "in_progress", "completed", "blocked", or "cancelled".
        priority: Filter by priority - "low", "medium", "high", or "critical".
        include_completed: Whether to include completed tasks (default True).
    """
    try:
        manager = get_todo_manager()
        tasks = manager.list_tasks(
            status=status if status else None,
            priority=priority if priority else None,
            include_completed=include_completed,
        )

        if not tasks:
            return "No tasks found matching the criteria."

        summary = manager.get_summary()

        output = []
        output.append("=== TODO LIST ===")
        output.append(
            f"Total: {summary['total']} | Pending: {summary['pending']} | "
            f"In Progress: {summary['in_progress']} | Completed: {summary['completed']}"
        )
        output.append("")

        status_icons = {
            "pending": "○",
            "in_progress": "◐",
            "completed": "●",
            "blocked": "⊘",
            "cancelled": "✕",
        }
        priority_icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}

        for task in tasks:
            icon = status_icons.get(task["status"], "○")
            pri = priority_icons.get(task["priority"], "⚪")
            output.append(f"{icon} {pri} [{task['id'][:12]}...] {task['title']}")

        return "\n".join(output)

    except Exception as e:
        return f"Error listing tasks: {str(e)}"


@mcp.tool()
def todo_get(task_id: str) -> str:
    """
    Get detailed information about a specific task.

    Args:
        task_id: The ID of the task to retrieve.
    """
    try:
        manager = get_todo_manager()
        task = manager.get_task(task_id)

        if not task:
            return f"Task not found: {task_id}"

        output = []
        output.append(f"=== TASK: {task['title']} ===")
        output.append(f"ID: {task['id']}")
        output.append(f"Status: {task['status']}")
        output.append(f"Priority: {task['priority']}")
        output.append(f"Created: {task['created_at']}")

        if task["description"]:
            output.append(f"\nDescription:\n{task['description']}")

        if task["assigned_files"]:
            output.append(f"\nAssigned Files:")
            for f in task["assigned_files"]:
                output.append(f"  - {f}")

        if task["notes"]:
            output.append(f"\nNotes:")
            for note in task["notes"]:
                output.append(f"  [{note['timestamp'][:10]}] {note['content']}")

        return "\n".join(output)

    except Exception as e:
        return f"Error getting task: {str(e)}"


@mcp.tool()
def todo_delete(task_id: str) -> str:
    """
    Delete a task from the todo list.

    Args:
        task_id: The ID of the task to delete.
    """
    try:
        manager = get_todo_manager()
        success = manager.delete_task(task_id)

        if success:
            return f"✓ Task deleted: {task_id}"
        return f"Task not found: {task_id}"

    except Exception as e:
        return f"Error deleting task: {str(e)}"


# --- FILE SYSTEM TOOLS ---


@mcp.tool()
def list_content(sub_path: str = ".") -> str:
    """
    Lists the contents of a directory.
    """
    try:
        target = get_secure_path(sub_path)
        if not target.exists():
            return f"Error: Directory '{sub_path}' does not exist."

        if not target.is_dir():
            return f"Error: '{sub_path}' is a file, not a directory."

        items = []
        for item in target.iterdir():
            if item.is_dir():
                items.append(f"[DIR]  {item.name}/")
            else:
                size = item.stat().st_size
                size_str = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"
                items.append(f"[FILE] {item.name} ({size_str})")

        if not items:
            return f"Directory '{sub_path}' is empty."

        return "\n".join(sorted(items))

    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def get_directory_tree(sub_path: str = ".") -> str:
    """
    Returns a visual, recursive tree structure of a directory.
    """
    try:
        target = get_secure_path(sub_path)

        if not target.exists():
            return f"Error: Directory '{sub_path}' does not exist."
        if not target.is_dir():
            return f"Error: '{sub_path}' is a file, not a directory."

        tree_lines = []
        file_count = 0
        dir_count = 0

        def build_tree(directory: Path, prefix: str = ""):
            nonlocal file_count, dir_count

            try:
                items = list(directory.iterdir())
            except PermissionError:
                tree_lines.append(f"{prefix}[ACCESS DENIED]")
                return

            filtered_items = [item for item in items if item.name not in IGNORE_DIRS]
            sorted_items = sorted(
                filtered_items, key=lambda x: (x.is_file(), x.name.lower())
            )
            count = len(sorted_items)

            for i, item in enumerate(sorted_items):
                is_last = i == count - 1
                connector = "└── " if is_last else "├── "

                if item.is_dir():
                    tree_lines.append(f"{prefix}{connector}{item.name}/")
                    dir_count += 1
                    extension = "    " if is_last else "│   "
                    build_tree(item, prefix + extension)
                else:
                    tree_lines.append(f"{prefix}{connector}{item.name}")
                    file_count += 1

        tree_lines.append(f"{target.name}/")
        build_tree(target)

        if len(tree_lines) == 1:
            return f"{target.name}/ (Empty Directory)"

        tree_lines.append("")
        tree_lines.append(f"Summary: {dir_count} directories, {file_count} files")

        return truncate_output("\n".join(tree_lines))

    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def read_file(file_path: str) -> str:
    """
    Reads the content of a file.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: File '{file_path}' not found."

        if not target.is_file():
            return f"Error: '{file_path}' is not a file."

        content = target.read_text(encoding="utf-8")
        return truncate_output(content)

    except ValueError as e:
        return str(e)
    except UnicodeDecodeError:
        return f"Error: Cannot read '{file_path}' as text. It may be a binary file."
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def write_file(file_path: str, content: str) -> str:
    """
    Writes content to a file. Creates parent directories if needed.
    """
    try:
        target = get_secure_path(file_path)

        if not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)

        sanitized_content = sanitize_input(content, max_length=10_000_000)
        target.write_text(sanitized_content, encoding="utf-8")

        return (
            f"✓ Successfully wrote to {file_path} ({len(sanitized_content)} characters)"
        )

    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def edit_file(file_path: str, start_line: int, end_line: int, new_content: str) -> str:
    """
    Edits a file by replacing a specific range of lines with new content.
    Lines are 1-indexed.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: File '{file_path}' not found."

        result = FileEditor.replace_lines(target, start_line, end_line, new_content)

        if result["success"]:
            return (
                f"✓ {result['message']}\n"
                f"  Lines: {result['lines_before']} → {result['lines_after']}\n"
                f"  File: {file_path}"
            )
        return f"Error: {result['message']}"

    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def edit_find_replace(
    file_path: str,
    search: str,
    replace: str,
    use_regex: bool = False,
    case_sensitive: bool = True,
    replace_all: bool = True,
) -> str:
    """
    Find and replace text in a file.

    Args:
        file_path: Path to the file.
        search: The text or regex pattern to search for.
        replace: The replacement text.
        use_regex: Whether to treat 'search' as a regex pattern.
        case_sensitive: Whether the search is case-sensitive.
        replace_all: Whether to replace all occurrences or just the first.
    """
    try:
        target = get_secure_path(file_path)
        if not target.exists():
            return f"Error: File '{file_path}' not found."

        result = FileEditor.find_and_replace(
            target, search, replace, use_regex, case_sensitive, replace_all
        )

        return (
            f"✓ {result['message']}\n"
            f"  Matches found: {result['matches_found']}\n"
            f"  Replacements made: {result['replacements_made']}"
        )

    except ValueError as e:
        return str(e)
    except re.error as e:
        return f"Error: Invalid regex pattern - {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def execute_command(command: str, timeout: int = 30) -> str:
    """
    Executes a terminal command.

    Args:
        command: The shell command to run.
        timeout: Timeout in seconds (default 30, max 120).
    """
    try:
        timeout = max(1, min(timeout, 120))

        if not command.strip():
            return "Error: Empty command."

        process = subprocess.run(
            command,
            shell=True,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
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

        full_output = (
            "\n\n".join(output_parts) + f"\n[Return Code: {process.returncode}]"
        )

        return truncate_output(full_output)

    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds."
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def open_in_desktop(path: str = ".") -> str:
    """
    Opens the file or directory using the OS default application.
    """
    try:
        target = get_secure_path(path)
        if not target.exists():
            return f"Error: '{path}' not found."

        system = platform.system()

        if system == "Darwin":
            subprocess.run(["open", str(target)], check=True)
        elif system == "Windows":
            try:
                os.startfile(str(target))
            except AttributeError:
                subprocess.run(["start", "", str(target)], shell=True, check=True)
        elif system == "Linux":
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return "Error: No display detected."

            if shutil.which("xdg-open"):
                subprocess.run(["xdg-open", str(target)], check=True)
            else:
                for fm in ["nautilus", "dolphin", "thunar"]:
                    if shutil.which(fm):
                        subprocess.run([fm, str(target)], check=True)
                        break

        return f"✓ Opened '{path}' in default application"

    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """
    Searches the internet for a given query.
    """
    try:
        max_results = max(1, min(max_results, 10))

        results = []
        with DDGS() as ddgs:
            search_gen = ddgs.text(query, max_results=max_results)
            for r in search_gen:
                results.append(r)

        if not results:
            return f"No results found for: '{query}'"

        formatted_output = [f"=== Search Results for '{query}' ==="]
        for i, res in enumerate(results, 1):
            title = res.get("title", "No Title")
            href = res.get("href", "No URL")
            body = res.get("body", "No Description")

            formatted_output.append(f"\n[{i}] {title}")
            formatted_output.append(f"    URL: {href}")
            formatted_output.append(f"    {body[:150]}...")

        return "\n".join(formatted_output)

    except Exception as e:
        return f"Error searching the web: {str(e)}"


# --- MAIN ---

if __name__ == "__main__":
    if not BASE_DIR.exists():
        print(f"[SERVER INIT]: Creating base directory: {BASE_DIR}", file=sys.stderr)
        BASE_DIR.mkdir(parents=True, exist_ok=True)

    log("Enhanced MCP Server starting in stdio mode...")
    log(f"Base directory: {BASE_DIR}")
    log(f"Platform: {platform.system()} ({SystemDetector.get_display_server()})")

    mcp.run(transport="stdio")
