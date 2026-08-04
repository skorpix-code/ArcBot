"""The single event vocabulary shared by the agent, the transport and the UI.

Every observable thing the agent does becomes one of these events.  The web
socket serialises them verbatim, so adding a UI affordance never requires a new
transport concept — only a new event type.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .logging_setup import get_logger

log = get_logger("events")


class E:
    """Event type constants (string values are the wire format)."""

    # lifecycle
    READY = "ready"                   # agent connected, tools discovered
    STATUS = "status"                 # {state, detail}
    TURN_START = "turn.start"         # {turnId}
    TURN_END = "turn.end"             # {turnId, reason, steps, elapsedMs}
    STEP = "step"                     # {turnId, step, maxSteps}

    # assistant output
    MESSAGE_START = "message.start"   # {id, role}
    TEXT_DELTA = "text.delta"         # {id, text}
    THINKING_DELTA = "thinking.delta" # {id, text}
    MESSAGE_END = "message.end"       # {id, text, thinking, usage}

    # tools
    TOOL_START = "tool.start"         # {callId, name, toolset, title, args, risk}
    TOOL_PROGRESS = "tool.progress"   # {callId, chunk}
    TOOL_END = "tool.end"             # {callId, ok, preview, elapsedMs, meta}

    # terminal
    TERMINAL_DATA = "terminal.data"   # {data}
    TERMINAL_EXIT = "terminal.exit"   # {code}

    # interaction
    ASK = "ask"                       # {askId, kind, ...}  -> expects a reply
    ASK_RESOLVED = "ask.resolved"     # {askId, decision}

    # side panels
    TODOS = "todos"                   # {items}
    TOOLSETS = "toolsets"             # {available, enabled}
    USAGE = "usage"                   # {inputTokens, outputTokens, costUsd, contextPct}
    NOTICE = "notice"                 # {level, text}
    SHUTDOWN = "shutdown"             # {reason}   — the app is closing
    OPEN_SETTINGS = "open.settings"   # {panel}    — show the user a panel

    # voice mode
    VOICE_READY = "voice.ready"           # {sampleRate, captions}
    VOICE_STATE = "voice.state"           # {state: idle|listening|thinking|speaking}
    VOICE_TRANSCRIPT = "voice.transcript" # {text, elapsedMs, audioSeconds}
    VOICE_SPEAK = "voice.speak"           # {text, sampleRate, samples[]}
    VOICE_STOP_AUDIO = "voice.stop"       # cut playback now (barge-in / stop)
    VOICE_BARGE = "voice.barge"           # the user interrupted
    VOICE_DOWNLOAD = "voice.download"     # {modelId, progress, label}
    ERROR = "error"                   # {message, detail, recoverable}
    CONFIG = "config"                 # {settings}


#: Ask kinds the UI knows how to render.
class Ask:
    COMMAND = "command"        # approve running a shell command
    TOOL = "tool"              # approve a tool call
    TOOLSET = "toolset"        # enable a disabled toolset
    INPUT = "input"            # free-text answer to an interactive prompt
    CHOICE = "choice"          # pick one of several options
    PATH = "path"              # grant access to a path outside the workspace


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


Emitter = Callable[[Event], Awaitable[None]]


class EventBus:
    """Fan-out to any number of async subscribers.

    A slow or dead subscriber can never stall the agent: delivery is per
    subscriber and failures unsubscribe rather than propagate.
    """

    def __init__(self) -> None:
        self._subscribers: list[Emitter] = []
        self._lock = asyncio.Lock()

    def subscribe(self, emitter: Emitter) -> Callable[[], None]:
        self._subscribers.append(emitter)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(emitter)
            except ValueError:
                pass

        return unsubscribe

    async def emit(self, type_: str, data: dict[str, Any] | None = None) -> None:
        event = Event(type_, data or {})
        if not self._subscribers:
            return
        for emitter in list(self._subscribers):
            try:
                await emitter(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Dropping subscriber after delivery error: %s", exc)
                try:
                    self._subscribers.remove(emitter)
                except ValueError:
                    pass

    @property
    def has_listeners(self) -> bool:
        return bool(self._subscribers)
