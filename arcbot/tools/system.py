"""Read-only machine inspection, plus the one action that needs a prompt.

Everything here answers "what is this machine doing?" — hardware, processes,
disks, network, environment.  ``kill_process`` is the only state-changing tool,
and it is classified as a system action so it goes through approval.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
from typing import Literal

from ..native.sysdetect import SystemDetector
from .registry import ToolResult, ctx, tool

TOOLSET = "system"

#: Environment variables whose values are never shown.
_SECRET_HINTS = ("key", "token", "secret", "password", "passwd", "credential", "auth")


@tool(toolset=TOOLSET, capability="read", title="System info", preview_chars=1600)
def system_info() -> ToolResult:
    """Report OS, CPU, memory, GPU, disk and desktop environment.

    Call this before anything platform-specific — the right command differs
    between Hyprland, GNOME, macOS and Windows.
    """
    os_name, os_version, arch, hostname = SystemDetector.get_os_info()
    cpu = SystemDetector.get_cpu_info()
    mem = SystemDetector.get_memory_info()
    disks = SystemDetector.get_disk_info()
    gpus = SystemDetector.get_gpu_info()

    def size(entry) -> str:
        if isinstance(entry, dict) and "value" in entry:
            return f"{entry['value']} {entry.get('unit', '')}".strip()
        return str(entry)

    lines = [
        f"OS: {os_name} {os_version} ({arch}) on {hostname}",
        f"Python: {platform.python_version()}",
        f"Desktop/display server: {SystemDetector.get_display_server()}",
        f"CPU: {cpu.get('model', 'Unknown')} — {cpu.get('physical_cores', '?')} cores / "
        f"{cpu.get('logical_cores', '?')} threads, {cpu.get('cpu_percent', '?')}% in use",
        f"Memory: {size(mem.get('used'))} of {size(mem.get('total'))} used "
        f"({mem.get('percent', '?')}%)",
    ]
    for gpu in gpus or []:
        label = gpu.get("model") or gpu.get("info") or "unknown"
        vram = f" ({gpu['memory']})" if gpu.get("memory") else ""
        lines.append(f"GPU: {label}{vram}")
    for entry in (disks or [])[:4]:
        lines.append(
            f"Disk {entry.get('mountpoint')}: {entry.get('used_gb')} / {entry.get('total_gb')} GB "
            f"used ({entry.get('percent')}%)"
        )
    lines.append(f"Shell: {os.environ.get('SHELL') or os.environ.get('COMSPEC') or 'unknown'}")
    return ToolResult(True, "\n".join(lines))


@tool(toolset=TOOLSET, capability="read", title="Processes", preview_chars=1500)
def list_processes(sort_by: Literal["cpu", "memory", "name"] = "cpu", limit: int = 15,
                   name_filter: str = "") -> ToolResult:
    """List running processes with their CPU and memory usage.

    Args:
        sort_by: Order the list by cpu, memory or name.
        limit: How many processes to return.
        name_filter: Only include processes whose name contains this text.
    """
    import psutil

    rows = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "username"]):
        try:
            info = proc.info
            if name_filter and name_filter.lower() not in (info.get("name") or "").lower():
                continue
            rows.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    key = {"cpu": "cpu_percent", "memory": "memory_percent", "name": "name"}[sort_by]
    rows.sort(key=lambda r: (r.get(key) or 0) if sort_by != "name" else str(r.get("name", "")),
              reverse=sort_by != "name")
    rows = rows[: max(1, min(limit, 100))]

    if not rows:
        return ToolResult(True, "No matching processes.")
    header = f"{'PID':>7}  {'CPU%':>6}  {'MEM%':>6}  NAME"
    body = "\n".join(
        f"{r.get('pid', 0):>7}  {(r.get('cpu_percent') or 0):>6.1f}  "
        f"{(r.get('memory_percent') or 0):>6.1f}  {r.get('name', '?')}"
        for r in rows
    )
    return ToolResult(True, f"{header}\n{body}", {"count": len(rows)})


@tool(toolset=TOOLSET, capability="system", title="Kill process {identifier}")
def kill_process(identifier: str, force: bool = False) -> ToolResult:
    """Stop a process by PID or exact name.

    Prefer a graceful stop; only set force when a process has already refused to
    exit. Never kill a process you did not start without telling the user why.

    Args:
        identifier: Process id, or the exact process name.
        force: Send SIGKILL instead of SIGTERM.
    """
    import psutil

    targets = []
    if identifier.isdigit():
        try:
            targets.append(psutil.Process(int(identifier)))
        except psutil.NoSuchProcess:
            return ToolResult.error(f"No process with PID {identifier}.")
    else:
        targets = [p for p in psutil.process_iter(["name"])
                   if (p.info.get("name") or "").lower() == identifier.lower()]
        if not targets:
            return ToolResult.error(f"No process named {identifier!r}.")
    if len(targets) > 5:
        return ToolResult.error(
            f"{len(targets)} processes match {identifier!r} — that is too broad. Use a PID."
        )

    killed, failed = [], []
    for proc in targets:
        try:
            proc.kill() if force else proc.terminate()
            killed.append(f"{proc.pid} ({proc.name()})")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            failed.append(f"{proc.pid}: {type(exc).__name__}")
    parts = []
    if killed:
        parts.append("Stopped: " + ", ".join(killed))
    if failed:
        parts.append("Failed: " + ", ".join(failed))
    return ToolResult(bool(killed), "\n".join(parts))


@tool(toolset=TOOLSET, capability="read", title="Disk usage")
def disk_usage(path: str = "") -> ToolResult:
    """Show free and used space for a path, or for every mounted disk.

    Args:
        path: Optional path to check; empty checks all mounts.
    """
    import psutil

    if path:
        target = ctx().path(path)
        usage = shutil.disk_usage(target)
        gb = 1024 ** 3
        return ToolResult(
            True,
            f"{target}\n  total {usage.total / gb:.1f} GB · used {usage.used / gb:.1f} GB · "
            f"free {usage.free / gb:.1f} GB ({usage.used / usage.total * 100:.0f}% used)",
        )

    lines = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        gb = 1024 ** 3
        lines.append(
            f"{part.mountpoint:<24} {usage.used / gb:>7.1f} / {usage.total / gb:>7.1f} GB "
            f"({usage.percent:>3.0f}%)  [{part.fstype}]"
        )
    return ToolResult(True, "\n".join(lines) or "No readable mounts.")


@tool(toolset=TOOLSET, capability="read", title="Network info")
def network_info(check_port: int = 0) -> ToolResult:
    """Show network interfaces and active listening ports.

    Args:
        check_port: If set, report which process is listening on this port.
    """
    import psutil

    if check_port:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == check_port and conn.status == "LISTEN":
                owner = "unknown"
                if conn.pid:
                    try:
                        owner = f"{psutil.Process(conn.pid).name()} (pid {conn.pid})"
                    except psutil.Error:
                        owner = f"pid {conn.pid}"
                return ToolResult(True, f"Port {check_port} is in use by {owner}.")
        return ToolResult(True, f"Port {check_port} is free.")

    lines = [f"Hostname: {socket.gethostname()}"]
    for name, addresses in psutil.net_if_addrs().items():
        for address in addresses:
            if address.family == socket.AF_INET:
                lines.append(f"  {name}: {address.address}")
    listening = sorted({
        c.laddr.port for c in psutil.net_connections(kind="inet")
        if c.status == "LISTEN" and c.laddr
    })
    if listening:
        lines.append("Listening ports: " + ", ".join(str(p) for p in listening[:40]))
    return ToolResult(True, "\n".join(lines))


@tool(toolset=TOOLSET, capability="read", title="Environment")
def environment_info(name: str = "") -> ToolResult:
    """Read environment variables, with secret values masked.

    Args:
        name: A specific variable to read; empty lists all names.
    """
    def masked(key: str, value: str) -> str:
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            return "«hidden»" if value else "(empty)"
        return value

    if name:
        value = os.environ.get(name)
        if value is None:
            return ToolResult(True, f"{name} is not set.")
        return ToolResult(True, f"{name}={masked(name, value)}")

    keys = sorted(os.environ)
    lines = [f"{k}={masked(k, os.environ[k])}" for k in keys if len(os.environ[k]) < 400]
    return ToolResult(True, ctx().clip("\n".join(lines)), {"count": len(keys)})


@tool(toolset=TOOLSET, capability="read", title="Which {program}")
def which_program(program: str) -> ToolResult:
    """Check whether a program is installed and where it lives.

    Use this before suggesting a command that may not exist on this machine.

    Args:
        program: Executable name, e.g. 'git' or 'docker'.
    """
    found = shutil.which(program)
    if not found:
        return ToolResult(True, f"{program!r} is not installed or not on PATH.")
    return ToolResult(True, f"{program} → {found}", {"path": found})


@tool(toolset=TOOLSET, capability="read", title="Service {name}")
def service_status(name: str) -> ToolResult:
    """Check whether a background service is installed and running.

    Works with systemd, launchd and Windows services, so you do not have to
    guess which one this machine uses.

    Args:
        name: Service name, e.g. 'nginx', 'docker' or 'sshd'.
    """
    import subprocess

    def run(argv: list[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=15, check=False
            )
            return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)

    system = platform.system()
    if system == "Windows":
        code, output = run(["powershell", "-NoProfile", "-Command",
                            f"Get-Service -Name '{name}' | Select-Object -ExpandProperty Status"])
        if code != 0 or not output:
            return ToolResult(True, f"No Windows service named {name!r}.")
        return ToolResult(True, f"{name}: {output}")

    if system == "Darwin":
        code, output = run(["launchctl", "list"])
        matches = [line for line in output.splitlines() if name.lower() in line.lower()]
        if not matches:
            return ToolResult(True, f"No launchd job matching {name!r}.")
        return ToolResult(True, "\n".join(matches[:10]))

    if shutil.which("systemctl"):
        code, active = run(["systemctl", "is-active", name])
        _, enabled = run(["systemctl", "is-enabled", name])
        if "could not be found" in active.lower() or (not active and code != 0):
            return ToolResult(True, f"No systemd unit named {name!r}.")
        return ToolResult(True, f"{name}: {active or 'unknown'} (at boot: {enabled or 'unknown'})")

    if shutil.which("service"):
        code, output = run(["service", name, "status"])
        return ToolResult(True, output or f"{name}: no status reported.")
    return ToolResult.error("No service manager found (systemctl, service, launchctl).")


@tool(toolset=TOOLSET, capability="network", title="Ping {host}")
def ping_host(host: str, count: int = 3) -> ToolResult:
    """Check whether a host is reachable and how long it takes to respond.

    Args:
        host: Hostname or IP address.
        count: How many echo requests to send (1-10).
    """
    import subprocess

    if not re.fullmatch(r"[A-Za-z0-9._:\-\[\]]{1,255}", host or ""):
        return ToolResult.error("That does not look like a hostname or IP address.")
    n = max(1, min(int(count), 10))
    flag = "-n" if platform.system() == "Windows" else "-c"
    try:
        proc = subprocess.run(
            ["ping", flag, str(n), host],
            capture_output=True, text=True, timeout=5 + n * 2, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult.error(f"Could not run ping: {exc}")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return ToolResult(proc.returncode == 0, ctx().clip(output) or "No output from ping.")


@tool(toolset=TOOLSET, capability="network", title="Resolve {host}")
def resolve_host(host: str) -> ToolResult:
    """Look up the IP addresses a hostname resolves to.

    Args:
        host: The hostname to resolve.
    """
    if not re.fullmatch(r"[A-Za-z0-9._\-]{1,253}", host or ""):
        return ToolResult.error("That does not look like a hostname.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return ToolResult(False, f"{host} does not resolve: {exc.strerror or exc}")
    addresses = sorted({info[4][0] for info in infos})
    return ToolResult(True, f"{host} resolves to:\n" + "\n".join(f"  {a}" for a in addresses),
                      {"addresses": addresses})
