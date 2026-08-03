"""The rails that stop a turn from running away.

Weak models fail in a small number of very predictable ways: they call the same
tool forever, they think without acting, they retry a broken tool indefinitely,
or they explore until the context window bursts.  Each of those has a counter
here, and each counter's response is to *tell the model what happened* — a
nudge it can act on beats a silent abort every time.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .config import LimitSettings


@dataclass
class GuardDecision:
    #: Message to inject into the conversation before the next model call.
    nudge: str = ""
    #: Stop calling tools; force a final answer.
    force_final: bool = False
    #: End the turn immediately.
    stop: bool = False
    #: Short reason for the transcript and the UI.
    reason: str = ""


@dataclass
class LoopGuard:
    limits: LimitSettings
    step: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _signatures: Counter = field(default_factory=Counter)
    _last_signature: str = ""
    _consecutive_repeats: int = 0
    _tool_failures: Counter = field(default_factory=Counter)
    _silent_turns: int = 0
    _benched: set = field(default_factory=set)
    _calls: list = field(default_factory=list)
    _errors: dict = field(default_factory=dict)
    forced_final: bool = False

    # ------------------------------------------------------------------ time
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def out_of_time(self) -> bool:
        budget = self.limits.max_turn_seconds
        return bool(budget) and self.elapsed > budget

    @property
    def steps_left(self) -> int:
        return max(0, self.limits.max_steps - self.step)

    # ------------------------------------------------------------- per-step
    def begin_step(self) -> GuardDecision:
        """Called before each model round-trip."""
        self.step += 1
        if self.out_of_time:
            self.forced_final = True
            return GuardDecision(
                nudge="budget", force_final=True,
                reason=f"time budget ({self.limits.max_turn_seconds}s) reached",
            )
        if self.step > self.limits.max_steps:
            return GuardDecision(stop=True, reason="step limit reached")
        if self.step == self.limits.max_steps:
            self.forced_final = True
            return GuardDecision(nudge="final", force_final=True, reason="step limit reached")
        # Warn one step before the ceiling so the model can wrap up gracefully.
        if self.step == max(1, self.limits.max_steps - 3):
            return GuardDecision(nudge="budget", reason="approaching the step limit")
        return GuardDecision()

    def observe_response(self, *, has_text: bool, has_tool_calls: bool) -> GuardDecision:
        """Called after each model response, before tools run."""
        if has_tool_calls or has_text:
            self._silent_turns = 0
            return GuardDecision()
        self._silent_turns += 1
        if self._silent_turns >= 3:
            return GuardDecision(stop=True, reason="the model returned nothing three times")
        return GuardDecision(nudge="no_action", reason="the model produced no output")

    # ------------------------------------------------------------ tool calls
    @staticmethod
    def signature(name: str, args: dict[str, Any]) -> str:
        try:
            payload = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            payload = str(args)
        return hashlib.sha1(f"{name}:{payload}".encode()).hexdigest()[:16]

    def observe_tool_batch(self, calls: list[dict[str, Any]]) -> GuardDecision:
        """Detect the model re-issuing an identical batch of calls."""
        combined = "|".join(self.signature(c["name"], c["args"]) for c in calls)
        if combined and combined == self._last_signature:
            self._consecutive_repeats += 1
        else:
            self._consecutive_repeats = 0
        self._last_signature = combined

        if self._consecutive_repeats >= self.limits.repeat_limit:
            self.forced_final = True
            return GuardDecision(
                nudge="repeat", force_final=True,
                reason="the same tool call was repeated",
            )
        if self._consecutive_repeats >= 1:
            return GuardDecision(nudge="repeat", reason="repeated tool call")
        return GuardDecision()

    def is_cached_repeat(self, name: str, args: dict[str, Any]) -> bool:
        """Has this exact call already run in this turn?"""
        return self._signatures[self.signature(name, args)] >= 1

    def record_call(self, name: str, args: dict[str, Any]) -> None:
        self._signatures[self.signature(name, args)] += 1
        self._calls.append(name)

    def record_failure(self, name: str, message: str = "") -> GuardDecision:
        self._tool_failures[name] += 1
        if message:
            self._errors[name] = message.strip().splitlines()[0][:180]
        if self._tool_failures[name] >= self.limits.tool_failure_limit:
            self._benched.add(name)
            return GuardDecision(
                nudge="tool_failing",
                reason=f"{name} failed {self._tool_failures[name]} times",
            )
        return GuardDecision()

    def record_success(self, name: str) -> None:
        self._tool_failures[name] = 0
        self._benched.discard(name)

    def is_benched(self, name: str) -> bool:
        return name in self._benched

    # ------------------------------------------------------------- reporting
    def diagnose(self) -> str:
        """A situation report the model can actually reason from.

        A bare "you are repeating yourself" tells the model it is stuck but not
        what to do about it.  Handing back what was tried, what failed and how
        much budget is left turns a dead end into a decision.
        """
        lines = ["[system] Step back and look at where you are."]

        if self._calls:
            counts = Counter(self._calls)
            tried = ", ".join(
                f"{name}×{n}" if n > 1 else name for name, n in counts.most_common(8)
            )
            lines.append(f"Tools you have called this turn: {tried}.")
        else:
            lines.append("You have not called any tool yet this turn.")

        failing = {name: self._errors.get(name, "no detail")
                   for name, n in self._tool_failures.items() if n}
        if failing:
            lines.append("What failed, and why:")
            lines.extend(f"  - {name}: {reason}" for name, reason in list(failing.items())[:5])
        if self._benched:
            lines.append(
                f"Benched for this turn (stop calling them): {', '.join(sorted(self._benched))}."
            )

        lines.append(f"Budget: step {self.step} of {self.limits.max_steps}.")
        lines.append(
            "Do not repeat what already failed. Either change approach — a different tool, a "
            "smaller step you can verify, a check of an assumption you have been making — or, "
            "if you are genuinely blocked, tell the user what you tried and what you need."
        )
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        return {
            "steps": self.step,
            "elapsedMs": int(self.elapsed * 1000),
            "toolCalls": sum(self._signatures.values()),
            "forcedFinal": self.forced_final,
        }
