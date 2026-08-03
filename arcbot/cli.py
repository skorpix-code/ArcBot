"""Command line entry points.

``arcbot`` starts the app and opens a browser.  ``arcbot doctor`` explains what
is and is not working on this machine, which is the first thing to reach for
when something misbehaves.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .config import STORE, load_dotenv
from .logging_setup import setup_logging
from .paths import config_dir, config_file, data_dir

BANNER = "\033[38;5;117m◠\033[0m"


def _colour(text: str, code: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def _free_port(host: str, preferred: int) -> int:
    """Return *preferred* if it is free, otherwise the next free port."""
    for candidate in [preferred, *range(preferred + 1, preferred + 25)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(f"No free port near {preferred}. Pass --port to choose one.")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def command_run(args: argparse.Namespace) -> int:
    import uvicorn

    settings = STORE.settings
    if args.workspace:
        settings.workspace = str(Path(args.workspace).expanduser())
        STORE.save(settings)

    host = args.host or settings.server.host
    port = _free_port(host, args.port or settings.server.port)

    from .web.app import RUNTIME, SESSION_TOKEN, app

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    print()
    print(f"  {BANNER}  {_colour('ArcBot', '1')} {_colour(f'v{__version__}', '2')}")
    print()
    print(f"     {_colour('open', '2')}       {_colour(url, '4;38;5;117')}")
    print(f"     {_colour('workspace', '2')}  {settings.workspace_path}")
    print(f"     {_colour('model', '2')}      {settings.model.model or settings.model.provider or _colour('not configured yet', '33')}")
    print(f"     {_colour('trust', '2')}      {settings.permissions.mode}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print()
        print(_colour("     ⚠  Bound to a non-loopback address. Anyone who can reach this", "33"))
        print(_colour("        port and guess the token can run commands on this machine.", "33"))
    print()
    print(f"     {_colour('Ctrl+C to stop', '2')}")
    print(flush=True)   # the banner must appear even when stdout is a pipe

    if args.open if args.open is not None else settings.server.open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"{url}?token={SESSION_TOKEN}")).start()

    # The server object is built explicitly (rather than uvicorn.run) so the
    # agent can shut the app down when the user asks it to.
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning" if not args.verbose else "info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def quit_app(reason: str = "") -> None:
        print(f"\n  {_colour('Closing:', '2')} {reason or 'asked to quit'}")
        # Let the UI render the goodbye before the socket goes away.
        await asyncio.sleep(0.6)
        server.should_exit = True

    RUNTIME.agent.on_quit = quit_app
    RUNTIME.agent.host_url = url.rstrip("/")
    server.run()
    return 0


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def command_doctor(_args: argparse.Namespace) -> int:
    from .providers import CATALOG as PROVIDERS
    from .providers import describe_auth
    from .tools.catalog import CATALOG as TOOLSETS

    ok = _colour("ok", "32")
    warn = _colour("--", "33")
    bad = _colour("no", "31")

    def row(label: str, status: str, detail: str = "") -> None:
        print(f"  {status}  {label:<22} {_colour(detail, '2')}")

    settings = STORE.settings
    print(f"\n{BANNER}  ArcBot v{__version__} — environment check\n")

    print(_colour("  Runtime", "1"))
    row("python", ok if sys.version_info >= (3, 10) else bad,
        f"{platform.python_version()} ({sys.executable})")
    row("platform", ok, f"{platform.system()} {platform.release()}")
    if platform.system() == "Linux":
        desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "unknown"
        row("desktop", ok, f"{desktop} / {os.environ.get('XDG_SESSION_TYPE', '?')}")

    print(f"\n{_colour('  Configuration', '1')}")
    row("config file", ok if config_file().exists() else warn, str(config_file()))
    row("workspace", ok if settings.workspace_path.exists() else warn, str(settings.workspace_path))
    row("trust level", ok, settings.permissions.mode)
    row("onboarded", ok if settings.onboarded else warn,
        "yes" if settings.onboarded else "run `arcbot` to finish setup")

    print(f"\n{_colour('  Dependencies', '1')}")
    for module, why in (
        ("fastapi", "web server"), ("uvicorn", "web server"), ("anthropic", "Claude API"),
        ("openai", "OpenAI-compatible models"), ("google.genai", "Gemini"),
        ("psutil", "system toolset"), ("ddgs", "web search"),
    ):
        import importlib.util

        found = importlib.util.find_spec(module.split(".")[0]) is not None
        row(module, ok if found else warn, why if found else f"missing — {why} unavailable")

    print(f"\n{_colour('  Model providers', '1')}")
    for spec in PROVIDERS.values():
        status = describe_auth(spec.id, STORE)
        row(spec.id, ok if status["available"] else warn, status["detail"])

    print(f"\n{_colour('  Toolsets', '1')}")
    for entry in TOOLSETS.values():
        enabled = entry.id in settings.toolsets
        missing = entry.missing_requirements()
        detail = "enabled" if enabled else "off"
        if missing:
            detail = f"needs {', '.join(missing)}"
        row(entry.id, ok if enabled and not missing else warn if not missing else bad, detail)

    print(f"\n{_colour('  Paths', '1')}")
    row("config dir", ok, str(config_dir()))
    row("data dir", ok, str(data_dir()))
    print()
    return 0


# --------------------------------------------------------------------------- #
# ancillary commands
# --------------------------------------------------------------------------- #


def command_config(args: argparse.Namespace) -> int:
    import json

    if args.reset:
        path = config_file()
        if path.exists():
            path.unlink()
            print(f"Removed {path}")
        else:
            print("No config file to remove.")
        return 0
    print(json.dumps(STORE.settings.to_dict(), indent=2))
    return 0


def command_ask(args: argparse.Namespace) -> int:
    """One-shot headless run — useful in scripts and for smoke-testing."""
    from .agent import Agent
    from .events import E, EventBus

    setup_logging("WARNING")

    async def run() -> int:
        bus = EventBus()
        finished = asyncio.Event()
        exit_code = 0

        async def printer(event) -> None:
            nonlocal exit_code
            if event.type == E.TEXT_DELTA:
                sys.stdout.write(event.data.get("text", ""))
                sys.stdout.flush()
            elif event.type == E.TOOL_START:
                print(_colour(f"\n  · {event.data.get('title')}", "2"), file=sys.stderr)
            elif event.type == E.ERROR:
                print(_colour(f"\nerror: {event.data.get('message')}", "31"), file=sys.stderr)
                exit_code = 1
            elif event.type == E.TURN_END:
                finished.set()

        bus.subscribe(printer)
        agent = Agent(STORE, bus)
        await agent.start()
        if agent.provider is None:
            print("No model configured. Run `arcbot` first.", file=sys.stderr)
            return 2
        await agent.send(args.prompt)
        try:
            await asyncio.wait_for(finished.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print(_colour("\ntimed out", "33"), file=sys.stderr)
            exit_code = 1
        await agent.shutdown()
        print()
        return exit_code

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcbot",
        description="A local-first AI agent for your whole computer.",
    )
    parser.add_argument("--version", action="version", version=f"arcbot {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")

    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Start ArcBot (default).")
    run.add_argument("--host", default=None, help="Bind address (default 127.0.0.1).")
    run.add_argument("--port", type=int, default=None, help="Port (default 8765).")
    run.add_argument("--workspace", default=None, help="Workspace folder for this run.")
    run.add_argument("--no-open", dest="open", action="store_false", default=None,
                     help="Do not open a browser.")
    run.set_defaults(func=command_run)

    doctor = sub.add_parser("doctor", help="Check this machine's setup.")
    doctor.set_defaults(func=command_doctor)

    config = sub.add_parser("config", help="Show or reset the configuration.")
    config.add_argument("--reset", action="store_true", help="Delete the config file.")
    config.set_defaults(func=command_config)

    ask = sub.add_parser("ask", help="Run a single prompt without the UI.")
    ask.add_argument("prompt", help="What to do.")
    ask.add_argument("--timeout", type=float, default=900.0, help="Seconds to wait.")
    ask.set_defaults(func=command_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()   # optional, for containers and CI; real env vars still win
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging("DEBUG" if getattr(args, "verbose", False) else None)

    if not getattr(args, "func", None):
        args = parser.parse_args(["run", *(argv or [])])
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
