"""Detect interactive prompts in streamed terminal output.

When a command pauses waiting for input (a package manager asking "Proceed?
[Y/n]", a scaffolding wizard showing a numbered menu, a "Press enter" gate, a
password prompt…), this classifies the prompt so the UI can show quick-answer
buttons and the agent can respond sensibly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Strip ANSI escape sequences before analysing text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


@dataclass
class InteractivePrompt:
    kind: str  # yes_no | menu | press_enter | password | free_text
    question: str
    options: list[dict] = field(default_factory=list)  # [{label, value, default}]
    default: str | None = None
    raw_tail: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "question": self.question,
            "options": self.options,
            "default": self.default,
        }


_YES_NO_RES = [
    re.compile(r"\[y/n\]", re.I),
    re.compile(r"\[yes/no\]", re.I),
    re.compile(r"\(y/n\)", re.I),
    re.compile(r"\(yes/no\)", re.I),
    re.compile(r"\by/n\b", re.I),
    re.compile(r"\[Y/n\]"),   # default yes
    re.compile(r"\[y/N\]"),   # default no
    re.compile(r"\?\s*\(y\)es\s*/\s*\(n\)o", re.I),
]

_MENU_LINE_RE = re.compile(r"^\s*[>*\-❯]?\s*(\d{1,2}|[a-z])[\).:]\s+(.+?)\s*$", re.I)
_PASSWORD_RE = re.compile(r"(password[^:]*:|passphrase[^:]*:|\[sudo\]\s+password)", re.I)
_PRESS_ENTER_RE = re.compile(r"press\s+(enter|return|any key)|hit\s+enter", re.I)


def _default_from_ynbrackets(text: str) -> str | None:
    if re.search(r"\[Y/n\]", text):
        return "y"
    if re.search(r"\[y/N\]", text):
        return "n"
    return None


def detect_prompt(buffer: str) -> InteractivePrompt | None:
    """Inspect recent terminal output; return a prompt if one is awaiting input."""
    if not buffer:
        return None
    clean = strip_ansi(buffer)
    lines = [ln.rstrip() for ln in clean.splitlines()]
    # Consider the tail — prompts live at the end of the stream.
    tail_lines = list(lines[-25:])
    tail_text = "\n".join(tail_lines)
    last_nonempty = next((ln for ln in reversed(tail_lines) if ln.strip()), "")

    # 1) Password prompt.
    if _PASSWORD_RE.search(last_nonempty) or _PASSWORD_RE.search(tail_text[-200:]):
        return InteractivePrompt("password", last_nonempty.strip() or "Password:", raw_tail=last_nonempty)

    # 2) Yes/No prompt.
    for rx in _YES_NO_RES:
        if rx.search(last_nonempty) or rx.search(tail_text[-160:]):
            default = _default_from_ynbrackets(last_nonempty) or _default_from_ynbrackets(tail_text)
            q = last_nonempty.strip() or "Confirm?"
            opts = [
                {"label": "Yes", "value": "y", "default": default == "y"},
                {"label": "No", "value": "n", "default": default == "n"},
            ]
            return InteractivePrompt("yes_no", q, opts, default, raw_tail=last_nonempty)

    # 3) Numbered / lettered menu.
    menu_items: list[dict] = []
    for ln in tail_lines:
        m = _MENU_LINE_RE.match(ln)
        if m:
            menu_items.append({"label": m.group(2).strip()[:80], "value": m.group(1), "default": False})
    if len(menu_items) >= 2:
        # The question is the nearest non-menu line above the block.
        question = ""
        for ln in reversed(tail_lines):
            if ln.strip() and not _MENU_LINE_RE.match(ln):
                question = ln.strip()
                break
        return InteractivePrompt("menu", question or "Select an option:", menu_items, raw_tail=tail_text[-300:])

    # 4) Press-enter gate.
    if _PRESS_ENTER_RE.search(last_nonempty) or _PRESS_ENTER_RE.search(tail_text[-120:]):
        return InteractivePrompt("press_enter", last_nonempty.strip() or "Press enter to continue", raw_tail=last_nonempty)

    # 5) Generic free-text prompt: a line ending in ':' or '?' with a trailing space
    #    and no newline after — a classic "enter value:" cue.
    if (
        last_nonempty
        and clean.rstrip(" ")
        and not clean.endswith("\n")
        and re.search(r"[:?]\s*$", last_nonempty)
        and len(last_nonempty) < 200
    ):
        return InteractivePrompt("free_text", last_nonempty.strip(), raw_tail=last_nonempty)

    return None


def summarize_for_model(p: InteractivePrompt) -> str:
    """A short instruction appended to the paused tool result for the LLM."""
    if p.kind == "yes_no":
        d = f" (default: {p.default})" if p.default else ""
        return (
            f'[INTERACTIVE: yes/no question — "{p.question}"{d}]. Reply by calling '
            f'execute_command again with "y" or "n".'
        )
    if p.kind == "menu":
        opts = "; ".join(f'{o["value"]}={o["label"]}' for o in p.options)
        return (
            f'[INTERACTIVE: menu — "{p.question}". Options: {opts}]. Reply by calling '
            f"execute_command again with the chosen number/letter."
        )
    if p.kind == "password":
        return '[INTERACTIVE: a password is required]. Ask the user to type it into the terminal panel; do NOT guess.'
    if p.kind == "press_enter":
        return '[INTERACTIVE: waiting for Enter]. Reply by calling execute_command again with an empty string "".'
    return f'[INTERACTIVE: input requested — "{p.question}"]. Provide the value via execute_command, or ask the user if unsure.'
