"""Cross-platform system information detection (moved verbatim from the
original monolith; unchanged behaviour, now importable and testable)."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from typing import Any, Dict, List, Tuple

import psutil

from . import log


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
