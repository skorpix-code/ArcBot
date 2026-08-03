"""Desktop control: windows, workspaces, screenshots, clipboard, media, notifications.

The heavy lifting lives in :mod:`arcbot.native.windows`, which speaks hyprctl,
swaymsg, wmctrl, yabai and PowerShell as appropriate.  These wrappers exist to
give the model a small, well-described surface instead of a hundred variants.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from typing import Literal

from ..native.windows import WindowManager
from .registry import ToolResult, ctx, tool

TOOLSET = "desktop"
IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"


def _run(command: list[str], timeout: int = 12, stdin_text: str | None = None):
    """Run a helper binary, returning ``(ok, output)``."""
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin_text,
            check=False,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, output
    except FileNotFoundError:
        return False, f"{command[0]} is not installed."
    except subprocess.TimeoutExpired:
        return False, f"{command[0]} timed out."
    except OSError as exc:
        return False, str(exc)


def _first(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _outcome(result: dict, success_message: str) -> ToolResult:
    """Normalise the ``{success, message}`` dicts the window manager returns."""
    if isinstance(result, dict):
        ok = bool(result.get("success", True))
        return ToolResult(ok, result.get("message") or (success_message if ok else "Failed."), result)
    return ToolResult(True, success_message)


# --------------------------------------------------------------------------- #
# Windows & workspaces
# --------------------------------------------------------------------------- #


@tool(toolset=TOOLSET, capability="read", title="List windows", preview_chars=1500)
def list_windows() -> ToolResult:
    """List open windows with their titles, apps, positions and workspaces.

    Call this before acting on a window so you can match the user's description
    to a real title, and again afterwards to confirm the change landed.
    """
    windows = WindowManager.list_windows()
    if not windows:
        return ToolResult(True, "No windows found (or this desktop is not supported).")
    lines = []
    for win in windows:
        geometry = f"{win.get('width', '?')}x{win.get('height', '?')}+{win.get('x', '?')}+{win.get('y', '?')}"
        lines.append(
            f"• {win.get('title', '(untitled)')[:70]}\n"
            f"    app={win.get('class') or win.get('app') or '?'}  workspace={win.get('workspace', '?')}  {geometry}"
        )
    return ToolResult(True, f"{len(windows)} window(s):\n" + "\n".join(lines), {"windows": windows})


@tool(toolset=TOOLSET, capability="read", title="Active window")
def active_window() -> ToolResult:
    """Report which window currently has focus."""
    info = WindowManager.active_window()
    if not info:
        return ToolResult(True, "Could not determine the focused window.")
    return ToolResult(
        True,
        f"{info.get('title', '(untitled)')} — app={info.get('class') or info.get('app', '?')}, "
        f"workspace={info.get('workspace', '?')}",
        info,
    )


@tool(toolset=TOOLSET, capability="system", title="{action} window '{query}'")
def control_window(
    query: str,
    action: Literal["focus", "minimize", "maximize", "fullscreen", "center", "close"],
) -> ToolResult:
    """Focus, minimise, maximise, fullscreen, centre or close a window.

    Args:
        query: Part of the window's title or application name.
        action: What to do with it.
    """
    handlers = {
        "focus": WindowManager.focus_window,
        "minimize": WindowManager.minimize_window,
        "maximize": WindowManager.maximize_window,
        "fullscreen": WindowManager.fullscreen_window,
        "center": WindowManager.center_window,
        "close": WindowManager.close_window,
    }
    return _outcome(handlers[action](query), f"{action} applied to {query!r}.")


@tool(toolset=TOOLSET, capability="system", title="Move window '{query}'")
def move_window(query: str, x: int, y: int) -> ToolResult:
    """Move a window to absolute screen coordinates.

    Args:
        query: Part of the window's title or application name.
        x: Target left edge in pixels.
        y: Target top edge in pixels.
    """
    return _outcome(WindowManager.move_window(query, x, y), f"Moved {query!r} to {x},{y}.")


@tool(toolset=TOOLSET, capability="system", title="Resize window '{query}'")
def resize_window(query: str, width: int, height: int) -> ToolResult:
    """Resize a window.

    Args:
        query: Part of the window's title or application name.
        width: New width in pixels.
        height: New height in pixels.
    """
    return _outcome(
        WindowManager.resize_window(query, width, height), f"Resized {query!r} to {width}x{height}."
    )


@tool(toolset=TOOLSET, capability="system", title="Snap window '{query}' {direction}")
def snap_window(
    query: str,
    direction: Literal["left", "right", "top", "bottom",
                       "top-left", "top-right", "bottom-left", "bottom-right"],
) -> ToolResult:
    """Snap a window to a half or quarter of the screen.

    Args:
        query: Part of the window's title or application name.
        direction: Which region to snap it to.
    """
    return _outcome(WindowManager.snap_window(query, direction), f"Snapped {query!r} {direction}.")


@tool(toolset=TOOLSET, capability="system", title="Tile windows ({layout})")
def tile_windows(layout: Literal["horizontal", "vertical", "grid"] = "grid") -> ToolResult:
    """Arrange all visible windows in a layout.

    Check screen_info first so the arrangement matches the real display size.

    Args:
        layout: How to arrange them.
    """
    return _outcome(WindowManager.tile_windows(layout), f"Tiled windows ({layout}).")


@tool(toolset=TOOLSET, capability="read", title="Workspaces")
def list_workspaces() -> ToolResult:
    """List virtual desktops / workspaces and which one is active."""
    spaces = WindowManager.list_workspaces()
    if not spaces:
        return ToolResult(True, "No workspace information available on this desktop.")
    lines = [
        f"{'→' if s.get('active') or s.get('focused') else ' '} {s.get('name') or s.get('id')}"
        f"  ({s.get('windows', '?')} windows)"
        for s in spaces
    ]
    return ToolResult(True, "\n".join(lines), {"workspaces": spaces})


@tool(toolset=TOOLSET, capability="system", title="Window '{query}' → workspace {workspace}")
def move_window_to_workspace(query: str, workspace: str) -> ToolResult:
    """Send a window to another workspace.

    Use this rather than improvising raw hyprctl/swaymsg commands — it handles
    every supported desktop.

    Args:
        query: Part of the window's title or application name.
        workspace: Target workspace name or number.
    """
    return _outcome(
        WindowManager.move_window_to_workspace(query, workspace),
        f"Moved {query!r} to workspace {workspace}.",
    )


@tool(toolset=TOOLSET, capability="system", title="Switch to workspace {workspace}")
def switch_workspace(workspace: str) -> ToolResult:
    """Switch to a different workspace / virtual desktop.

    Args:
        workspace: Workspace name or number.
    """
    return _outcome(WindowManager.switch_workspace(workspace), f"Switched to workspace {workspace}.")


@tool(toolset=TOOLSET, capability="read", title="Screen info")
def screen_info() -> ToolResult:
    """Report monitor resolutions, positions and scaling.

    Call this before positioning or tiling windows.
    """
    monitors = WindowManager.list_monitors()
    if not monitors:
        return ToolResult(True, "No monitor information available.")
    lines = [
        f"{m.get('name', '?')}: {m.get('width', '?')}x{m.get('height', '?')} "
        f"at ({m.get('x', 0)},{m.get('y', 0)})"
        + (f" scale {m['scale']}" if m.get("scale") else "")
        + ("  [primary]" if m.get("primary") or m.get("focused") else "")
        for m in monitors
    ]
    return ToolResult(True, "\n".join(lines), {"monitors": monitors})


# --------------------------------------------------------------------------- #
# Screen, clipboard, notifications
# --------------------------------------------------------------------------- #


@tool(toolset=TOOLSET, capability="system", title="Screenshot")
def take_screenshot(destination: str = "") -> ToolResult:
    """Capture the screen to an image file in the workspace.

    Args:
        destination: Where to save the PNG; empty picks a timestamped name.
    """
    context = ctx()
    target = context.path(destination or f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png")
    target.parent.mkdir(parents=True, exist_ok=True)

    if IS_MAC:
        ok, output = _run(["screencapture", "-x", str(target)])
    elif IS_WINDOWS:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
            "$g=[System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
            f"$bmp.Save('{target}')"
        )
        ok, output = _run(["powershell", "-NoProfile", "-Command", script], timeout=25)
    else:
        tool_path = _first("grim", "spectacle", "gnome-screenshot", "scrot", "import", "maim")
        if not tool_path:
            return ToolResult.error(
                "No screenshot tool found. Install one of: grim (Wayland), scrot or maim (X11)."
            )
        name = os.path.basename(tool_path)
        argv = {
            "grim": [tool_path, str(target)],
            "scrot": [tool_path, str(target)],
            "maim": [tool_path, str(target)],
            "import": [tool_path, "-window", "root", str(target)],
            "spectacle": [tool_path, "-b", "-n", "-o", str(target)],
            "gnome-screenshot": [tool_path, "-f", str(target)],
        }[name]
        ok, output = _run(argv, timeout=25)

    if not ok or not target.exists():
        return ToolResult.error(f"Screenshot failed: {output or 'no file was produced'}")
    return ToolResult(
        True,
        f"Saved a screenshot to {context.rel(target)} ({target.stat().st_size:,} bytes).",
        {"path": context.rel(target)},
    )


@tool(toolset=TOOLSET, capability="read", title="Read clipboard")
def read_clipboard() -> ToolResult:
    """Read the current text contents of the system clipboard."""
    if IS_MAC:
        ok, output = _run(["pbpaste"])
    elif IS_WINDOWS:
        ok, output = _run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"])
    else:
        binary = _first("wl-paste", "xclip", "xsel")
        if not binary:
            return ToolResult.error("Install wl-clipboard (Wayland) or xclip/xsel (X11) to read the clipboard.")
        argv = {
            "wl-paste": [binary, "--no-newline"],
            "xclip": [binary, "-selection", "clipboard", "-o"],
            "xsel": [binary, "--clipboard", "--output"],
        }[os.path.basename(binary)]
        ok, output = _run(argv)
    if not ok:
        return ToolResult.error(f"Could not read the clipboard: {output}")
    return ToolResult(True, ctx().clip(output or "(clipboard is empty)"))


@tool(toolset=TOOLSET, capability="system", title="Copy to clipboard")
def write_clipboard(text: str) -> ToolResult:
    """Put text on the system clipboard.

    Args:
        text: The text to copy.
    """
    if IS_MAC:
        ok, output = _run(["pbcopy"], stdin_text=text)
    elif IS_WINDOWS:
        ok, output = _run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
                          stdin_text=text)
    else:
        binary = _first("wl-copy", "xclip", "xsel")
        if not binary:
            return ToolResult.error("Install wl-clipboard (Wayland) or xclip/xsel (X11) to use the clipboard.")
        argv = {
            "wl-copy": [binary],
            "xclip": [binary, "-selection", "clipboard"],
            "xsel": [binary, "--clipboard", "--input"],
        }[os.path.basename(binary)]
        ok, output = _run(argv, stdin_text=text)
    if not ok:
        return ToolResult.error(f"Could not write to the clipboard: {output}")
    return ToolResult(True, f"Copied {len(text)} characters to the clipboard.")


@tool(toolset=TOOLSET, capability="system", title="Notify: {title}")
def send_notification(title: str, message: str = "") -> ToolResult:
    """Show a desktop notification.

    Args:
        title: Notification headline.
        message: Optional body text.
    """
    if IS_MAC:
        script = f'display notification "{message}" with title "{title}"'
        ok, output = _run(["osascript", "-e", script])
    elif IS_WINDOWS:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n=New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
            f"$n.ShowBalloonTip(5000,'{title}','{message}',"
            "[System.Windows.Forms.ToolTipIcon]::Info); Start-Sleep -s 5"
        )
        ok, output = _run(["powershell", "-NoProfile", "-Command", script], timeout=20)
    else:
        binary = _first("notify-send", "dunstify")
        if not binary:
            return ToolResult.error("Install libnotify (notify-send) to show notifications.")
        ok, output = _run([binary, title, message])
    if not ok:
        return ToolResult.error(f"Notification failed: {output}")
    return ToolResult(True, f"Notification sent: {title}")


@tool(toolset=TOOLSET, capability="system", title="Open {target}")
def open_in_desktop(target: str) -> ToolResult:
    """Open a file, folder or URL in the system's default application.

    Args:
        target: A workspace path, or an http(s) URL.
    """
    context = ctx()
    if target.startswith(("http://", "https://")):
        location = target
    else:
        resolved = context.path(target)
        if not resolved.exists():
            return ToolResult.error(f"No such path: {context.rel(resolved)}")
        location = str(resolved)

    if IS_MAC:
        ok, output = _run(["open", location])
    elif IS_WINDOWS:
        ok, output = _run(["cmd", "/c", "start", "", location])
    else:
        binary = _first("xdg-open", "gio")
        if not binary:
            return ToolResult.error("Install xdg-utils to open files from the desktop.")
        ok, output = _run([binary, "open", location] if binary.endswith("gio") else [binary, location])
    if not ok:
        return ToolResult.error(f"Could not open {location}: {output}")
    return ToolResult(True, f"Opened {location}")


# --------------------------------------------------------------------------- #
# Media & power
# --------------------------------------------------------------------------- #


@tool(toolset=TOOLSET, capability="system", title="Media: {action}")
def media_control(
    action: Literal["play-pause", "next", "previous", "stop",
                    "volume-up", "volume-down", "mute", "status"],
) -> ToolResult:
    """Control media playback and system volume.

    Args:
        action: What to do.
    """
    if action == "status":
        if IS_MAC or IS_WINDOWS:
            return ToolResult(True, "Playback status is not available on this platform.")
        binary = _first("playerctl")
        if not binary:
            return ToolResult.error("Install playerctl to query playback status.")
        ok, output = _run([binary, "metadata", "--format", "{{artist}} — {{title}} [{{status}}]"])
        return ToolResult(ok, output or "Nothing is playing.")

    if action in ("volume-up", "volume-down", "mute"):
        if IS_MAC:
            change = {"volume-up": "output volume of (get volume settings) + 10",
                      "volume-down": "output volume of (get volume settings) - 10",
                      "mute": "output muted of (get volume settings) is false"}[action]
            script = ("set volume output muted (not (output muted of (get volume settings)))"
                      if action == "mute" else f"set volume output volume ({change})")
            ok, output = _run(["osascript", "-e", script])
        else:
            binary = _first("wpctl", "pactl", "amixer")
            if not binary:
                return ToolResult.error("No volume control found (wpctl, pactl or amixer).")
            name = os.path.basename(binary)
            if name == "wpctl":
                arg = {"volume-up": "5%+", "volume-down": "5%-", "mute": "toggle"}[action]
                argv = [binary, "set-mute" if action == "mute" else "set-volume",
                        "@DEFAULT_AUDIO_SINK@", arg]
            elif name == "pactl":
                arg = {"volume-up": "+5%", "volume-down": "-5%", "mute": "toggle"}[action]
                argv = [binary, "set-sink-mute" if action == "mute" else "set-sink-volume",
                        "@DEFAULT_SINK@", arg]
            else:
                arg = {"volume-up": "5%+", "volume-down": "5%-", "mute": "toggle"}[action]
                argv = [binary, "-q", "sset", "Master", arg]
            ok, output = _run(argv)
        return ToolResult(ok, output or f"Volume: {action}")

    binary = _first("playerctl")
    if binary:
        ok, output = _run([binary, {"play-pause": "play-pause", "next": "next",
                                    "previous": "previous", "stop": "stop"}[action]])
        return ToolResult(ok, output or f"Media: {action}")
    if IS_MAC:
        keycode = {"play-pause": 16, "next": 17, "previous": 18, "stop": 16}[action]
        ok, output = _run(["osascript", "-e",
                           f'tell application "System Events" to key code {keycode} using {{fn down}}'])
        return ToolResult(ok, output or f"Media: {action}")
    return ToolResult.error("No media controller found. Install playerctl.")


@tool(toolset=TOOLSET, capability="read", title="Battery")
def battery_status() -> ToolResult:
    """Report battery charge, charging state and estimated time remaining."""
    try:
        import psutil
    except ImportError:
        return ToolResult.error("Battery status needs psutil. Install it with: pip install psutil")
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is None:
        return ToolResult(True, "No battery detected (this looks like a desktop machine).")
    parts = [f"Charge: {battery.percent:.0f}%",
             "Plugged in" if battery.power_plugged else "On battery"]
    if battery.secsleft and battery.secsleft > 0 and not battery.power_plugged:
        parts.append(f"About {battery.secsleft // 3600}h {(battery.secsleft % 3600) // 60}m left")
    return ToolResult(True, " · ".join(parts), {"percent": battery.percent})


@tool(toolset=TOOLSET, capability="system", title="Lock screen")
def lock_screen() -> ToolResult:
    """Lock the screen."""
    if IS_MAC:
        ok, output = _run(["pmset", "displaysleepnow"])
    elif IS_WINDOWS:
        ok, output = _run(["rundll32.exe", "user32.dll,LockWorkStation"])
    else:
        binary = _first("hyprlock", "swaylock", "loginctl", "i3lock", "xdg-screensaver")
        if not binary:
            return ToolResult.error("No screen locker found (hyprlock, swaylock, i3lock, loginctl).")
        argv = [binary, "lock-session"] if binary.endswith("loginctl") else (
            [binary, "lock"] if binary.endswith("xdg-screensaver") else [binary]
        )
        ok, output = _run(argv, timeout=5)
    return ToolResult(ok, output or "Screen locked.")


@tool(toolset=TOOLSET, capability="read", title="Screen brightness")
def get_brightness() -> ToolResult:
    """Report the current display brightness as a percentage."""
    if IS_MAC:
        binary = _first("brightness")
        if not binary:
            return ToolResult.error("Install the `brightness` CLI (brew install brightness).")
        ok, output = _run([binary, "-l"])
        for line in output.splitlines():
            if "brightness" in line.lower():
                try:
                    return ToolResult(True, f"Brightness: {float(line.split()[-1]) * 100:.0f}%")
                except ValueError:
                    break
        return ToolResult(ok, output or "Could not read the brightness.")
    if IS_WINDOWS:
        ok, output = _run(["powershell", "-NoProfile", "-Command",
                           "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness)"
                           ".CurrentBrightness"])
        return ToolResult(ok, f"Brightness: {output.strip()}%" if ok and output.strip() else output)

    binary = _first("brightnessctl", "light")
    if binary and binary.endswith("brightnessctl"):
        ok, current = _run([binary, "get"])
        ok2, maximum = _run([binary, "max"])
        if ok and ok2:
            try:
                return ToolResult(True, f"Brightness: {int(current) / int(maximum) * 100:.0f}%")
            except (ValueError, ZeroDivisionError):
                pass
    if binary and binary.endswith("light"):
        ok, output = _run([binary, "-G"])
        if ok:
            return ToolResult(True, f"Brightness: {float(output):.0f}%")
    return ToolResult.error("No brightness control found. Install brightnessctl or light.")


@tool(toolset=TOOLSET, capability="system", title="Brightness → {percent}%")
def set_brightness(percent: int) -> ToolResult:
    """Set the display brightness.

    Args:
        percent: Brightness from 1 to 100.
    """
    level = max(1, min(int(percent), 100))
    if IS_MAC:
        binary = _first("brightness")
        if not binary:
            return ToolResult.error("Install the `brightness` CLI (brew install brightness).")
        ok, output = _run([binary, str(level / 100)])
    elif IS_WINDOWS:
        ok, output = _run(["powershell", "-NoProfile", "-Command",
                           "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
                           f".WmiSetBrightness(1,{level})"])
    else:
        binary = _first("brightnessctl", "light")
        if not binary:
            return ToolResult.error("No brightness control found. Install brightnessctl or light.")
        argv = ([binary, "set", f"{level}%"] if binary.endswith("brightnessctl")
                else [binary, "-S", str(level)])
        ok, output = _run(argv)
    if not ok:
        return ToolResult.error(f"Could not set the brightness: {output}")
    return ToolResult(True, f"Brightness set to {level}%.")


@tool(toolset=TOOLSET, capability="system", title="Launch {name}")
def launch_app(name: str, args: str = "") -> ToolResult:
    """Start an application.

    Args:
        name: Application name or executable, e.g. 'firefox' or 'Visual Studio Code'.
        args: Optional arguments to pass to it.
    """
    extra = args.split() if args else []
    if IS_MAC:
        argv = ["open", "-a", name] + (["--args", *extra] if extra else [])
    elif IS_WINDOWS:
        argv = ["cmd", "/c", "start", "", name, *extra]
    else:
        binary = shutil.which(name)
        if not binary:
            binary = _first("gtk-launch")
            if not binary:
                return ToolResult.error(
                    f"{name!r} is not on PATH and gtk-launch is unavailable. "
                    f"Use the exact executable name."
                )
            argv = [binary, name, *extra]
        else:
            argv = [binary, *extra]

    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=not IS_WINDOWS,
        )
    except (OSError, FileNotFoundError) as exc:
        return ToolResult.error(f"Could not launch {name!r}: {exc}")
    return ToolResult(True, f"Launched {name}." + (f" ({args})" if args else ""))


@tool(toolset=TOOLSET, capability="system", title="Type text")
def type_text(text: str) -> ToolResult:
    """Type text into whatever window currently has focus.

    This goes to the focused window, not to ArcBot — check with active_window
    first, and never type passwords or anything secret.

    Args:
        text: The text to type.
    """
    if len(text) > 5000:
        return ToolResult.error("That is too much text to type at once (limit 5000 characters).")
    if IS_MAC:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        ok, output = _run(["osascript", "-e", f'tell application "System Events" to keystroke "{escaped}"'])
    elif IS_WINDOWS:
        escaped = text.replace("{", "{{").replace("}", "}}").replace('"', '`"')
        ok, output = _run(["powershell", "-NoProfile", "-Command",
                           f'Add-Type -AssemblyName System.Windows.Forms; '
                           f'[System.Windows.Forms.SendKeys]::SendWait("{escaped}")'])
    else:
        binary = _first("wtype", "ydotool", "xdotool")
        if not binary:
            return ToolResult.error(
                "No keyboard automation tool found. Install wtype or ydotool (Wayland), or xdotool (X11)."
            )
        name = os.path.basename(binary)
        argv = {"wtype": [binary, text],
                "ydotool": [binary, "type", text],
                "xdotool": [binary, "type", "--clearmodifiers", text]}[name]
        ok, output = _run(argv, timeout=30)
    if not ok:
        return ToolResult.error(f"Could not type: {output}")
    return ToolResult(True, f"Typed {len(text)} characters into the focused window.")


@tool(toolset=TOOLSET, capability="system", title="Press {keys}")
def press_keys(keys: str) -> ToolResult:
    """Press a key or key combination in the focused window.

    Args:
        keys: A combination such as 'ctrl+s', 'alt+Tab', 'Return' or 'Escape'.
    """
    combo = keys.strip()
    if not combo:
        return ToolResult.error("No keys given.")
    if IS_MAC:
        modifiers = {"ctrl": "control down", "control": "control down", "cmd": "command down",
                     "command": "command down", "alt": "option down", "option": "option down",
                     "shift": "shift down"}
        parts = [p.strip().lower() for p in combo.split("+")]
        mods = [modifiers[p] for p in parts[:-1] if p in modifiers]
        key = parts[-1]
        using = f" using {{{', '.join(mods)}}}" if mods else ""
        ok, output = _run(["osascript", "-e",
                           f'tell application "System Events" to keystroke "{key}"{using}'])
    elif IS_WINDOWS:
        sendkeys = combo.lower().replace("ctrl+", "^").replace("alt+", "%").replace("shift+", "+")
        ok, output = _run(["powershell", "-NoProfile", "-Command",
                           f'Add-Type -AssemblyName System.Windows.Forms; '
                           f'[System.Windows.Forms.SendKeys]::SendWait("{sendkeys}")'])
    else:
        binary = _first("wtype", "ydotool", "xdotool")
        if not binary:
            return ToolResult.error(
                "No keyboard automation tool found. Install wtype or ydotool (Wayland), or xdotool (X11)."
            )
        name = os.path.basename(binary)
        if name == "xdotool":
            argv = [binary, "key", "--clearmodifiers", combo]
        elif name == "ydotool":
            argv = [binary, "key", combo]
        else:  # wtype takes each modifier as a -M flag
            parts = [p.strip() for p in combo.split("+")]
            argv = [binary]
            for modifier in parts[:-1]:
                argv += ["-M", modifier.lower()]
            argv += ["-k", parts[-1]]
        ok, output = _run(argv)
    if not ok:
        return ToolResult.error(f"Could not press {combo!r}: {output}")
    return ToolResult(True, f"Pressed {combo}.")
