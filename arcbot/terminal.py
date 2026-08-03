"""Cross-platform command execution with live output.

Unix gets a real PTY so interactive programs behave normally (colours, prompts,
progress bars).  Windows falls back to piped subprocesses.  Either way the
caller receives output as it arrives, and a command that stalls waiting for
input is reported as *paused* rather than silently hanging until a timeout.

PTY reads go through the event loop's reader callback rather than a worker
thread. A blocking ``os.read`` parked in the default executor would survive
cancellation and wedge interpreter shutdown, which is exactly the kind of hang
that makes an agent feel unreliable.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .interactive import InteractivePrompt, detect_prompt
from .logging_setup import get_logger

log = get_logger("terminal")

try:  # pragma: no cover - platform dependent
    import fcntl
    import pty
    import struct
    import termios

    HAS_PTY = True
except ImportError:  # pragma: no cover - Windows
    HAS_PTY = False

IS_WINDOWS = platform.system() == "Windows"
#: Seconds of silence before a still-running process is reported as paused.
IDLE_PAUSE_SECONDS = 3.0
#: Bytes read per PTY poll.
CHUNK = 8192

OnData = Callable[[str], Awaitable[None]]


@dataclass
class CommandOutcome:
    output: str
    status: str                      # "finished" | "paused" | "timeout" | "error"
    exit_code: int | None = None
    prompt: InteractivePrompt | None = None

    @property
    def paused(self) -> bool:
        return self.status == "paused"


def default_shell() -> list[str]:
    """The shell to run commands through, honouring the user's choice on Unix."""
    if IS_WINDOWS:
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        return [pwsh, "-NoLogo", "-NoProfile", "-Command"] if pwsh else ["cmd.exe", "/c"]
    shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return [shell, "-lc"]


class TerminalManager:
    """Owns the user's interactive shell plus the agent's command process."""

    def __init__(self) -> None:
        self._cwd: Path = Path.home()
        self._on_data: OnData | None = None
        self._shutdown = False

        # user-facing shell (Unix PTY only)
        self._shell_fd: int | None = None
        self._shell_proc: asyncio.subprocess.Process | None = None

        # agent command process
        self._agent_fd: int | None = None
        self._agent_proc: asyncio.subprocess.Process | None = None
        self._agent_buffer: list[str] = []
        self._agent_queue: asyncio.Queue[bytes] | None = None
        #: Strong references to in-flight emit tasks; without them the garbage
        #: collector can drop a task mid-await and swallow terminal output.
        self._pending: set[asyncio.Task] = set()

    # ------------------------------------------------------------ lifecycle
    def configure(self, cwd: Path, on_data: OnData | None = None) -> None:
        self._cwd = Path(cwd)
        if on_data is not None:
            self._on_data = on_data

    @property
    def has_live_command(self) -> bool:
        return self._agent_proc is not None and self._agent_proc.returncode is None

    async def _emit(self, text: str) -> None:
        if self._on_data:
            try:
                await self._on_data(text)
            except Exception:  # a UI hiccup must not break execution
                pass

    async def start_shell(self) -> None:
        """Attach an interactive login shell the user can type into."""
        if IS_WINDOWS or not HAS_PTY or self._shell_proc is not None or self._shutdown:
            return
        shell = os.environ.get("SHELL") or "/bin/bash"
        try:
            master, slave = pty.openpty()
        except OSError as exc:
            log.warning("Could not allocate a PTY for the shell: %s", exc)
            return
        try:
            self._shell_proc = await asyncio.create_subprocess_exec(
                shell,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                cwd=str(self._cwd),
                env={**os.environ, "TERM": "xterm-256color"},
            )
        except OSError as exc:
            log.warning("Could not start the interactive shell: %s", exc)
            _close(master)
            _close(slave)
            return
        finally:
            _close(slave)

        self._shell_fd = master
        _watch(master, lambda data: self._on_shell_data(data))

    def _on_shell_data(self, data: bytes) -> None:
        if not data:
            self._detach_shell()
            return
        task = asyncio.ensure_future(self._emit(data.decode(errors="replace")))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _detach_shell(self) -> None:
        if self._shell_fd is not None:
            _unwatch(self._shell_fd)
            _close(self._shell_fd)
            self._shell_fd = None

    async def stop(self) -> None:
        self._shutdown = True
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        self._detach_agent()
        self._detach_shell()
        for proc in (self._agent_proc, self._shell_proc):
            await _terminate(proc)
        self._agent_proc = self._shell_proc = None

    # ------------------------------------------------------------- user I/O
    def write(self, data: str) -> None:
        """Forward keystrokes from the UI terminal to whatever is in front."""
        target = self._agent_fd if self.has_live_command else self._shell_fd
        if target is None or self._shutdown:
            return
        try:
            os.write(target, data.encode())
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        if not HAS_PTY or self._shutdown:
            return
        for fd in (self._agent_fd, self._shell_fd):
            if fd is None:
                continue
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

    async def interrupt(self) -> bool:
        """Send SIGINT to a running agent command (the Stop button)."""
        proc = self._agent_proc
        if proc is None or proc.returncode is not None:
            return False
        try:
            if IS_WINDOWS:
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            return True
        except (OSError, ProcessLookupError):
            return False

    # ------------------------------------------------------------- commands
    async def run(self, command: str, timeout: float | None = None) -> CommandOutcome:
        """Run *command*, streaming output, and return when it finishes or pauses.

        If a previous command paused waiting for input, *command* is delivered
        to that process as its answer instead of starting a new one.
        """
        if self._shutdown:
            return CommandOutcome("ArcBot is shutting down.", "error")
        if not IS_WINDOWS and HAS_PTY:
            return await self._run_pty(command, timeout)
        return await self._run_piped(command, timeout)

    # ------------------------------------------------------------- PTY path
    async def _run_pty(self, command: str, timeout: float | None) -> CommandOutcome:
        if self.has_live_command and self._agent_fd is not None:
            try:
                os.write(self._agent_fd, (command + "\n").encode())
            except OSError as exc:
                return CommandOutcome(f"Could not send input: {exc}", "error")
            self._agent_buffer = []
        else:
            self._detach_agent()
            try:
                master, slave = pty.openpty()
            except OSError as exc:
                return CommandOutcome(f"Could not allocate a terminal: {exc}", "error")
            try:
                self._agent_proc = await asyncio.create_subprocess_exec(
                    *default_shell(),
                    command,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    start_new_session=True,
                    cwd=str(self._cwd),
                    env={
                        **os.environ,
                        "TERM": "xterm-256color",
                        "PAGER": "cat",
                        "GIT_PAGER": "cat",
                        "GIT_TERMINAL_PROMPT": "0",
                    },
                )
            except (OSError, FileNotFoundError) as exc:
                _close(master)
                return CommandOutcome(f"Could not start the shell: {exc}", "error")
            finally:
                _close(slave)

            self._agent_fd = master
            self._agent_buffer = []
            self._agent_queue = asyncio.Queue()
            queue = self._agent_queue
            _watch(master, queue.put_nowait)

        queue = self._agent_queue
        if queue is None:
            return CommandOutcome("The command process is not attached.", "error")

        loop = asyncio.get_running_loop()
        deadline = (loop.time() + timeout) if timeout else None

        while True:
            if self._shutdown:
                break
            wait = IDLE_PAUSE_SECONDS
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self.interrupt()
                    await asyncio.sleep(0.25)
                    await _terminate(self._agent_proc)
                    output = "".join(self._agent_buffer)
                    self._finish_agent()
                    return CommandOutcome(output, "timeout")
                wait = min(wait, remaining)

            try:
                data = await asyncio.wait_for(queue.get(), wait)
            except asyncio.TimeoutError:
                proc = self._agent_proc
                if proc is not None and proc.returncode is None and deadline is None:
                    text = "".join(self._agent_buffer)
                    return CommandOutcome(text, "paused", None, detect_prompt(text))
                if proc is not None and proc.returncode is None:
                    # Idle, but a deadline is running: keep waiting unless it is a
                    # genuine prompt, so long builds are not misread as stuck.
                    text = "".join(self._agent_buffer)
                    prompt = detect_prompt(text)
                    if prompt is not None:
                        return CommandOutcome(text, "paused", None, prompt)
                    continue
                break

            if not data:      # EOF — the child closed the PTY
                break
            text = data.decode(errors="replace")
            self._agent_buffer.append(text)
            await self._emit(text)

        proc = self._agent_proc
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                await _terminate(proc)
        code = proc.returncode if proc else None
        output = "".join(self._agent_buffer)
        self._finish_agent()
        return CommandOutcome(output, "finished", code)

    def _detach_agent(self) -> None:
        if self._agent_fd is not None:
            _unwatch(self._agent_fd)
            _close(self._agent_fd)
            self._agent_fd = None
        self._agent_queue = None

    def _finish_agent(self) -> None:
        self._detach_agent()
        self._agent_proc = None

    # ----------------------------------------------------------- piped path
    async def _run_piped(self, command: str, timeout: float | None) -> CommandOutcome:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
        try:
            proc = await asyncio.create_subprocess_exec(
                *default_shell(),
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(self._cwd),
                creationflags=creationflags,
            )
        except (OSError, FileNotFoundError) as exc:
            return CommandOutcome(f"Could not start the shell: {exc}", "error")

        self._agent_proc = proc
        parts: list[str] = []

        async def pump() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace")
                parts.append(text)
                await self._emit(text)

        try:
            if timeout:
                await asyncio.wait_for(pump(), timeout=timeout)
            else:
                await pump()
            await proc.wait()
            status, code = "finished", proc.returncode
        except asyncio.TimeoutError:
            await _terminate(proc)
            status, code = "timeout", None
        finally:
            self._agent_proc = None

        return CommandOutcome("".join(parts), status, code)


# --------------------------------------------------------------------------- #
# fd plumbing
# --------------------------------------------------------------------------- #


def _watch(fd: int, on_data: Callable[[bytes], None]) -> None:
    """Deliver readable bytes from *fd* via the event loop (never a thread)."""
    loop = asyncio.get_running_loop()

    def reader() -> None:
        try:
            data = os.read(fd, CHUNK)
        except BlockingIOError:
            return
        except OSError:
            # On Linux a PTY master raises EIO once the child has gone.
            data = b""
        if not data:
            try:
                loop.remove_reader(fd)
            except (OSError, ValueError):
                pass
        on_data(data)

    try:
        loop.add_reader(fd, reader)
    except (NotImplementedError, OSError) as exc:  # pragma: no cover - exotic loops
        log.warning("Cannot watch the terminal descriptor: %s", exc)


def _unwatch(fd: int) -> None:
    try:
        asyncio.get_running_loop().remove_reader(fd)
    except (RuntimeError, OSError, ValueError):
        pass


def _close(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


async def _terminate(proc) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        if not IS_WINDOWS:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (asyncio.TimeoutError, OSError, ProcessLookupError):
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (asyncio.TimeoutError, OSError, ProcessLookupError):
            pass
