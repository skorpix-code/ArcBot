"""Cross-platform window management (Hyprland/Sway/X11, macOS, Windows).

Moved from the original monolith with its behaviour intact.  It shells out to
native tools (hyprctl/swaymsg/wmctrl/xdotool, osascript/yabai, PowerShell) so it
degrades gracefully when a given tool isn't present.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import log
from .sysdetect import SystemDetector


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

    # ========================================
    # WORKSPACE MOVEMENT + EXTRAS (v0.2)
    # ========================================

    @classmethod
    def move_window_to_workspace(cls, window_query: str, workspace: str) -> Dict[str, Any]:
        """Move a window to another workspace/desktop/Space."""
        result = {"success": False, "message": ""}
        target = cls._find_window(window_query)
        if not target:
            result["message"] = f"No window found matching '{window_query}'"
            return result
        ws = str(workspace).strip()
        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, err, code = cls._run_cmd(
                        ["hyprctl", "dispatch", "movetoworkspacesilent", f"{ws},address:{target['id']}"]
                    )
                    result["success"] = code == 0
                    if code != 0:
                        result["message"] = err
                elif ds == "Sway":
                    _, err, code = cls._run_cmd(
                        ["swaymsg", f'[title="(?i).*{re.escape(window_query)}.*"]',
                         "move", "to", "workspace", "number", ws]
                    )
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    idx = str(int(ws) - 1) if ws.isdigit() else ws  # wmctrl desktops are 0-indexed
                    if str(target.get("id", "")).startswith("0x"):
                        _, err, code = cls._run_cmd(["wmctrl", "-i", "-r", target["id"], "-t", idx])
                    else:
                        _, err, code = cls._run_cmd(["wmctrl", "-r", window_query, "-t", idx])
                    result["success"] = code == 0
                else:
                    result["message"] = "No supported tool (need hyprctl/swaymsg/wmctrl)."
            elif system == "Darwin":
                if shutil.which("yabai"):
                    cls.focus_window(window_query)
                    _, err, code = cls._run_cmd(["yabai", "-m", "window", "--space", ws])
                    result["success"] = code == 0
                else:
                    result["message"] = "Moving windows between Spaces on macOS requires yabai."
            elif system == "Windows":
                result["message"] = "Moving windows across virtual desktops on Windows requires extra tooling (e.g. VirtualDesktop.exe)."
        except Exception as e:  # noqa: BLE001
            result["message"] = str(e)
        if result["success"]:
            result["message"] = f"Moved '{target.get('title', window_query)}' to workspace {ws}"
        elif not result["message"]:
            result["message"] = f"Could not move the window to workspace {ws} on this system."
        return result

    @classmethod
    def switch_workspace(cls, workspace: str) -> Dict[str, Any]:
        """Switch the active workspace/desktop/Space."""
        result = {"success": False, "message": ""}
        ws = str(workspace).strip()
        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    _, err, code = cls._run_cmd(["hyprctl", "dispatch", "workspace", ws])
                    result["success"] = code == 0
                elif ds == "Sway":
                    _, err, code = cls._run_cmd(["swaymsg", "workspace", "number", ws])
                    result["success"] = code == 0
                elif shutil.which("wmctrl"):
                    idx = str(int(ws) - 1) if ws.isdigit() else ws
                    _, err, code = cls._run_cmd(["wmctrl", "-s", idx])
                    result["success"] = code == 0
                else:
                    result["message"] = "No supported tool (need hyprctl/swaymsg/wmctrl)."
            elif system == "Darwin":
                if shutil.which("yabai"):
                    _, err, code = cls._run_cmd(["yabai", "-m", "space", "--focus", ws])
                    result["success"] = code == 0
                else:
                    result["message"] = "Switching Spaces on macOS requires yabai."
            elif system == "Windows":
                result["message"] = "Virtual desktop switching on Windows requires extra tooling."
        except Exception as e:  # noqa: BLE001
            result["message"] = str(e)
        if result["success"]:
            result["message"] = f"Switched to workspace {ws}"
        elif not result["message"]:
            result["message"] = f"Could not switch to workspace {ws}."
        return result

    @classmethod
    def fullscreen_window(cls, window_query: str) -> Dict[str, Any]:
        """Toggle fullscreen for a window."""
        return cls.maximize_window(window_query)

    @classmethod
    def center_window(cls, window_query: str) -> Dict[str, Any]:
        """Center a window on its monitor."""
        target = cls._find_window(window_query)
        if not target:
            return {"success": False, "message": f"No window found matching '{window_query}'"}
        if platform.system() == "Linux" and cls.get_display_server() == "Hyprland":
            cls._run_cmd(["hyprctl", "dispatch", "focuswindow", f"address:{target['id']}"])
            _, err, code = cls._run_cmd(["hyprctl", "dispatch", "centerwindow"])
            if code == 0:
                return {"success": True, "message": "Centered window"}
        return cls.snap_window(window_query, "center")

    @classmethod
    def active_window(cls) -> Dict[str, Any]:
        """Return info about the currently focused window."""
        system = platform.system()
        try:
            if system == "Linux":
                ds = cls.get_display_server()
                if ds == "Hyprland":
                    out, _, code = cls._run_cmd(["hyprctl", "activewindow", "-j"])
                    if code == 0 and out:
                        d = json.loads(out)
                        return {
                            "title": d.get("title", ""),
                            "app": d.get("class", ""),
                            "workspace": str(d.get("workspace", {}).get("id", "")),
                            "pid": d.get("pid"),
                            "size": {"width": d.get("size", [0, 0])[0], "height": d.get("size", [0, 0])[1]},
                        }
        except Exception:  # noqa: BLE001
            pass
        for w in cls.list_windows():
            if w.get("frontmost") or w.get("focused"):
                return w
        return {}
