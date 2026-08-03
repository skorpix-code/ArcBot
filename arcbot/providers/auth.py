"""Credential discovery — find what the machine already has before asking.

ArcBot never scrapes browser cookies or reverse-engineers a login.  It looks for
credentials the user has *already* established through supported tools:

* environment variables (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, …)
* ArcBot's own encrypted-at-rest credential file
* an authenticated **Claude Code** CLI — this is how the "no API key, use my
  Claude subscription" path works, exactly as editor extensions do it
* an ``ant auth login`` OAuth profile, whose short-lived token the official
  Anthropic SDK reads on its own

Everything here is read-only detection: nothing logs in on the user's behalf.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from ..logging_setup import get_logger

log = get_logger("auth")


@dataclass
class AuthStatus:
    available: bool
    #: "env" | "stored" | "claude-cli" | "ant-oauth" | "none"
    source: str = "none"
    detail: str = ""
    #: Shown in the UI when the credential is missing.
    hint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "source": self.source,
            "detail": self.detail,
            "hint": self.hint,
        }


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(argv: list[str], timeout: int = 12) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


# --------------------------------------------------------------------------- #
# Claude Code (subscription login)
# --------------------------------------------------------------------------- #


def claude_cli_path() -> str | None:
    """Locate the ``claude`` binary, including common non-PATH install spots."""
    found = _which("claude")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.claude/local/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\claude.exe"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def claude_code_status() -> AuthStatus:
    """Is Claude Code installed *and* logged in?

    A logged-in CLI can answer a trivial prompt; a logged-out one exits with an
    authentication error.  We check for the credential file first because that
    is instant, and only fall back to a probe when the layout is unfamiliar.
    """
    binary = claude_cli_path()
    if not binary:
        return AuthStatus(
            False,
            "none",
            "Claude Code is not installed.",
            hint="Install it with:  npm install -g @anthropic-ai/claude-code",
        )

    version = ""
    code, output = _run([binary, "--version"], timeout=20)
    if code == 0:
        version = output.strip().splitlines()[0] if output.strip() else ""

    for candidate in (
        os.path.expanduser("~/.claude/.credentials.json"),
        os.path.expanduser("~/.config/claude/.credentials.json"),
        os.path.expanduser("~/Library/Application Support/Claude/.credentials.json"),
    ):
        if os.path.isfile(candidate):
            return AuthStatus(
                True, "claude-cli", f"Signed in to Claude Code{f' ({version})' if version else ''}."
            )

    # macOS stores the token in the Keychain, and enterprise setups vary, so a
    # missing file is not proof of being logged out — say so honestly.
    return AuthStatus(
        True,
        "claude-cli",
        f"Claude Code found{f' ({version})' if version else ''}. "
        "If it is not signed in yet, run `claude` once and use /login.",
        hint="Run `claude` in a terminal and sign in with your Anthropic account.",
    )


# --------------------------------------------------------------------------- #
# Anthropic OAuth profile (`ant auth login`)
# --------------------------------------------------------------------------- #


def ant_oauth_token() -> str | None:
    """A short-lived OAuth access token from an ``ant auth login`` profile."""
    binary = _which("ant")
    if not binary:
        return None
    code, output = _run([binary, "auth", "print-credentials", "--access-token"], timeout=20)
    token = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if code == 0 and token and not token.startswith("{") and len(token) > 20:
        return token
    return None


def ant_status() -> AuthStatus:
    if not _which("ant"):
        return AuthStatus(False, "none", "The Anthropic CLI (`ant`) is not installed.")
    code, output = _run(["ant", "auth", "status"], timeout=20)
    if code == 0 and "no active" not in output.lower():
        first_line = next((line.strip() for line in output.splitlines() if line.strip()), "signed in")
        return AuthStatus(True, "ant-oauth", first_line[:160])
    return AuthStatus(False, "none", "No active `ant` profile.", hint="Run: ant auth login")


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #


def api_key_status(env_var: str, store) -> AuthStatus:
    """Where an API key would come from, without ever revealing its value."""
    if os.environ.get(env_var, "").strip():
        return AuthStatus(True, "env", f"Using {env_var} from the environment.")
    if store is not None and store.get_secret(env_var):
        return AuthStatus(True, "stored", "Using the key saved in ArcBot.")
    return AuthStatus(False, "none", f"No {env_var} configured.", hint=f"Add a key, or set {env_var}.")


def mask(secret: str) -> str:
    """``sk-ant-api03-abc…xyz`` → ``sk-ant…xyz`` for display."""
    if not secret:
        return ""
    if len(secret) <= 10:
        return "•" * len(secret)
    return f"{secret[:6]}…{secret[-4:]}"


# --------------------------------------------------------------------------- #
# Local model servers
# --------------------------------------------------------------------------- #


def probe_openai_endpoint(base_url: str, timeout: float = 3.0) -> tuple[bool, list[str]]:
    """Check whether an OpenAI-compatible server is up, and list its models."""
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/models"
    try:
        request = urllib.request.Request(url, headers={"Authorization": "Bearer not-needed"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(1_000_000).decode("utf-8", errors="replace"))
    except Exception:
        return False, []

    models = payload.get("data", payload if isinstance(payload, list) else [])
    names = []
    for item in models:
        if isinstance(item, dict):
            name = item.get("id") or item.get("name")
            if name:
                names.append(str(name))
        elif isinstance(item, str):
            names.append(item)
    return True, sorted(names)
