"""Turns a proposed action into allow / ask / deny.

The rules are deliberately boring and inspectable:

1. Statically blocked commands are refused in every mode.
2. An explicit user deny-rule wins over everything else.
3. An explicit user allow-rule short-circuits the prompt.
4. Otherwise the mode's risk ceiling decides: at or below it, run; above it, ask.

``plan`` mode is special — it refuses anything that changes state, so a user can
let a model explore an unfamiliar machine with zero blast radius.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .asks import AskBroker, AskResult
from .config import Settings
from .events import Ask
from .logging_setup import get_logger
from .security import CommandRisk, Risk, classify_command, matches_prefix

log = get_logger("permissions")

#: Highest risk each mode runs without asking.
_CEILING: dict[str, Risk] = {
    "plan": Risk.SAFE,
    "guarded": Risk.SAFE,
    "trusted": Risk.MODERATE,
    "full": Risk.HIGH,
}

#: Tool capability -> the risk it carries when the mode has to judge it.
CAPABILITY_RISK: dict[str, Risk] = {
    "read": Risk.SAFE,
    "network": Risk.MODERATE,
    "write": Risk.MODERATE,
    "exec": Risk.HIGH,
    "system": Risk.HIGH,
    "destructive": Risk.HIGH,
}


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    #: True when the user asked to remember this decision.
    remembered: bool = False

    def __bool__(self) -> bool:  # lets callers write `if verdict:`
        return self.allowed


class PermissionEngine:
    def __init__(
        self,
        settings: Settings,
        broker: AskBroker,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        #: Invoked after a rule is persisted so the caller can save settings.
        self.on_change = on_change
        #: Allowances valid only for the current turn ("allow once" on a retry).
        self._session_allow_commands: list[str] = []
        self._session_allow_tools: list[str] = []

    # ------------------------------------------------------------------ util
    @property
    def mode(self) -> str:
        mode = (self.settings.permissions.mode or "guarded").lower()
        return mode if mode in _CEILING else "guarded"

    @property
    def ceiling(self) -> Risk:
        return _CEILING[self.mode]

    @property
    def roots(self) -> list[Path]:
        roots = [self.settings.workspace_path]
        for extra in self.settings.permissions.extra_roots:
            try:
                roots.append(Path(extra).expanduser().resolve())
            except Exception:
                continue
        return roots

    def reset_session_rules(self) -> None:
        self._session_allow_commands.clear()
        self._session_allow_tools.clear()

    def _persist(self, target: list[str], value: str) -> None:
        if value and value not in target:
            target.append(value)
            if self.on_change:
                self.on_change()

    # -------------------------------------------------------------- commands
    async def check_command(self, command: str, *, context: str = "") -> Verdict:
        risk = classify_command(command, self.roots)
        perms = self.settings.permissions

        if risk.level >= Risk.BLOCKED:
            return Verdict(False, f"Blocked: {'; '.join(risk.reasons) or 'catastrophic command'}")

        if matches_prefix(command, perms.deny_commands):
            return Verdict(False, "You previously chose to always deny this command.")

        if self.mode == "plan" and risk.level > Risk.SAFE:
            return Verdict(
                False,
                "Plan mode is read-only. Describe what you would run and why, and let the "
                "user switch to Guarded mode if they want it executed.",
            )

        if matches_prefix(command, list(perms.allow_commands) + self._session_allow_commands):
            return Verdict(True, "Allowed by a saved rule.")

        if risk.level <= self.ceiling:
            return Verdict(True, f"{risk.level.label} in {self.mode} mode.")

        return await self._ask_command(command, risk, context)

    async def _ask_command(self, command: str, risk: CommandRisk, context: str) -> Verdict:
        result: AskResult = await self.broker.ask(
            Ask.COMMAND,
            {
                "command": command,
                "risk": risk.to_dict(),
                "context": context,
                "mode": self.mode,
                "suggestedRule": _rule_for(command),
                # A saved rule is a standing grant, so it is only offered where
                # the rule itself stays narrow.  "Always allow rm" is not a
                # trade anyone should be nudged into making.
                "offerRule": risk.level <= Risk.MODERATE and not risk.outside_paths,
            },
            default="deny",
        )
        if result.decision == "never":
            self._persist(self.settings.permissions.deny_commands, _rule_for(command))
            return Verdict(False, "You chose to always deny this command.", remembered=True)
        if result.decision == "always":
            rule = result.value or _rule_for(command)
            self._persist(self.settings.permissions.allow_commands, rule)
            return Verdict(True, f"Always allowing `{rule}`.", remembered=True)
        if result.approved:
            self._session_allow_commands.append(command)
            return Verdict(True, "Approved by the user.")
        if result.timed_out:
            return Verdict(False, "No answer from the user (timed out); command not run.")
        return Verdict(False, "The user declined to run this command.")

    # ------------------------------------------------------------------ tools
    async def check_tool(
        self,
        name: str,
        capability: str,
        args: dict[str, Any],
        *,
        title: str = "",
        detail: str = "",
    ) -> Verdict:
        perms = self.settings.permissions
        risk = CAPABILITY_RISK.get(capability, Risk.MODERATE)

        if self.mode == "plan" and risk > Risk.SAFE:
            return Verdict(
                False,
                "Plan mode is read-only — this tool changes state. Explain what you would do "
                "instead, and suggest the user switch to Guarded mode.",
            )
        if name in perms.allow_tools or name in self._session_allow_tools:
            return Verdict(True, "Allowed by a saved rule.")
        if risk <= self.ceiling:
            return Verdict(True, f"{risk.label} tool in {self.mode} mode.")

        result = await self.broker.ask(
            Ask.TOOL,
            {
                "tool": name,
                "title": title or name,
                "detail": detail,
                "args": args,
                "capability": capability,
                "risk": {"level": int(risk), "label": risk.label},
                "mode": self.mode,
            },
            default="deny",
        )
        if result.decision == "never":
            return Verdict(False, "The user blocked this tool.", remembered=True)
        if result.decision == "always":
            self._persist(perms.allow_tools, name)
            return Verdict(True, f"Always allowing `{name}`.", remembered=True)
        if result.approved:
            self._session_allow_tools.append(name)
            return Verdict(True, "Approved by the user.")
        if result.timed_out:
            return Verdict(False, "No answer from the user (timed out); tool not run.")
        return Verdict(False, "The user declined this tool call.")

    # ------------------------------------------------------------------ paths
    async def request_root(self, path: str, reason: str = "") -> Verdict:
        """Ask the user to widen the sandbox to include *path*."""
        try:
            resolved = str(Path(path).expanduser().resolve())
        except Exception as exc:
            return Verdict(False, f"Invalid path: {exc}")

        result = await self.broker.ask(
            Ask.PATH,
            {"path": resolved, "reason": reason, "mode": self.mode},
            default="deny",
        )
        if result.approved:
            self._persist(self.settings.permissions.extra_roots, resolved)
            return Verdict(True, f"Granted access to {resolved}.", remembered=True)
        return Verdict(False, "The user declined to widen the workspace.")


def _rule_for(command: str) -> str:
    """A conservative 'always allow' rule derived from a command.

    ``git status --short`` → ``git status``;  ``pytest -q`` → ``pytest``.
    Never longer than two tokens, so a saved rule cannot smuggle in flags the
    user did not read.
    """
    tokens = (command or "").split()
    if not tokens:
        return ""
    head = tokens[0]
    if len(tokens) > 1 and not tokens[1].startswith("-"):
        return f"{head} {tokens[1]}"
    return head


def describe_modes() -> list[dict[str, str]]:
    """Human-facing copy for the onboarding + settings UI."""
    return [
        {
            "id": "plan",
            "name": "Plan only",
            "summary": "Read and analyse. Never writes files or runs commands.",
            "detail": "Safest. Great for exploring an unfamiliar machine or repo.",
        },
        {
            "id": "guarded",
            "name": "Guarded",
            "summary": "Reads freely; asks before writing files or running commands.",
            "detail": "Recommended. You approve each action, and can save rules as you go.",
        },
        {
            "id": "trusted",
            "name": "Trusted",
            "summary": "Writes and ordinary commands run automatically; risky ones still ask.",
            "detail": "Good once you trust the model on a project you can revert.",
        },
        {
            "id": "full",
            "name": "Full access",
            "summary": "Everything runs automatically except statically blocked commands.",
            "detail": "Fastest and least safe. Use only in a VM or throwaway workspace.",
        },
    ]
