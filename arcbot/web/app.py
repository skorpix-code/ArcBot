"""FastAPI host: one page, one web socket, a small REST surface for settings.

Security posture: ArcBot can run commands on the machine, so the HTTP surface is
treated as privileged. It binds to loopback, requires a per-run token on every
request, and rejects web-socket handshakes from a foreign origin — which is what
stops a random web page you have open from driving your agent.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..agent import Agent
from ..config import STORE, ConfigStore, Settings
from ..events import Ask, E, Event, EventBus
from ..logging_setup import get_logger
from ..permissions import describe_modes
from ..providers import catalog_payload as provider_catalog
from ..providers import describe_auth, discover_models, get_spec
from ..session import delete_session, list_sessions
from ..tools.catalog import CATALOG as TOOLSET_CATALOG
from ..tools.catalog import catalog_payload as toolset_catalog
from ..tools.catalog import normalise as normalise_toolsets

log = get_logger("web")

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
TEMPLATE_DIR = HERE / "templates"

#: Regenerated on every start, so a stale bookmark cannot drive a new run.
SESSION_TOKEN = secrets.token_urlsafe(32)


class Runtime:
    """Everything the request handlers share."""

    def __init__(self, store: ConfigStore):
        self.store = store
        self.bus = EventBus()
        self.agent = Agent(store, self.bus)
        self.clients: set[WebSocket] = set()
        self.started_at = time.time()

    @property
    def settings(self) -> Settings:
        return self.store.settings


RUNTIME = Runtime(STORE)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    RUNTIME.agent.control_token = SESSION_TOKEN
    await RUNTIME.agent.start()
    try:
        yield
    finally:
        await RUNTIME.agent.shutdown()


app = FastAPI(title="ArcBot", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def require_token(token: str = Query(default=""), request: Request = None) -> None:
    """Every API call must carry the run token (header or query string)."""
    supplied = token or (request.headers.get("x-arcbot-token", "") if request else "")
    if not secrets.compare_digest(supplied or "", SESSION_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing token.")


Auth = Depends(require_token)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__ARCBOT_TOKEN__", SESSION_TOKEN))


@app.get("/health")
async def health() -> dict[str, Any]:
    """Unauthenticated liveness probe — deliberately reveals nothing."""
    return {"ok": True, "uptime": round(time.time() - RUNTIME.started_at, 1)}


# --------------------------------------------------------------------------- #
# State & configuration
# --------------------------------------------------------------------------- #


@app.get("/api/state", dependencies=[Auth])
async def get_state() -> dict[str, Any]:
    settings = RUNTIME.settings
    provider = RUNTIME.agent.provider
    return {
        "onboarded": settings.onboarded,
        "settings": _public_settings(settings),
        "providers": provider_catalog(),
        "toolsets": toolset_catalog(),
        "permissionModes": describe_modes(),
        "auth": describe_auth(settings.model.provider, RUNTIME.store) if settings.model.provider else None,
        "ready": provider is not None,
        "providerMode": provider.mode if provider else None,
        "sessions": list_sessions(settings.workspace_path, limit=30),
        "version": _version(),
    }


@app.post("/api/settings", dependencies=[Auth])
async def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = RUNTIME.settings
    changed_provider = False

    if "workspace" in payload:
        candidate = str(payload["workspace"]).strip()
        if candidate:
            resolved = Path(candidate).expanduser()
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(400, f"Cannot use that folder: {exc}") from exc
            settings.workspace = str(resolved)
            changed_provider = True

    model = payload.get("model") or {}
    if model:
        if "provider" in model:
            if model["provider"] and get_spec(model["provider"]) is None:
                raise HTTPException(400, f"Unknown provider {model['provider']!r}.")
            settings.model.provider = model["provider"]
        for field in ("model", "base_url"):
            if field in model:
                setattr(settings.model, field, str(model[field] or ""))
        if isinstance(model.get("options"), dict):
            settings.model.options.update(model["options"])
        changed_provider = True

    if "toolsets" in payload:
        settings.toolsets = normalise_toolsets(payload["toolsets"])

    permissions = payload.get("permissions") or {}
    if "mode" in permissions:
        if permissions["mode"] not in ("plan", "guarded", "trusted", "full"):
            raise HTTPException(400, "Unknown permission mode.")
        settings.permissions.mode = permissions["mode"]
        changed_provider = True
    for field in ("allow_commands", "deny_commands", "allow_tools", "extra_roots"):
        if field in permissions and isinstance(permissions[field], list):
            setattr(settings.permissions, field, [str(v) for v in permissions[field]][:200])
    if "autopilot_prompts" in permissions:
        settings.permissions.autopilot_prompts = bool(permissions["autopilot_prompts"])

    for section, keys in (
        ("ui", ("theme", "accent", "reduce_motion", "show_thinking", "sound")),
        ("limits", ("max_steps", "max_turn_seconds", "command_timeout", "max_tool_output", "compact_at")),
    ):
        block = payload.get(section) or {}
        target = getattr(settings, section)
        for key in keys:
            if key in block:
                setattr(target, key, block[key])

    if payload.get("onboarded"):
        settings.onboarded = True

    RUNTIME.store.save(settings)
    if changed_provider or "toolsets" in payload:
        await RUNTIME.agent.reload()
    await RUNTIME.bus.emit(E.CONFIG, {"settings": _public_settings(settings)})
    return {"ok": True, "settings": _public_settings(settings)}


@app.post("/api/secret", dependencies=[Auth])
async def set_secret(payload: dict[str, Any]) -> dict[str, Any]:
    """Store (or clear) an API key.  Values are write-only — never read back."""
    key = str(payload.get("key") or "").strip()
    if not key or not key.isidentifier():
        raise HTTPException(400, "Invalid key name.")
    RUNTIME.store.set_secret(key, str(payload.get("value") or "").strip())
    await RUNTIME.agent.reload()
    return {"ok": True, "auth": describe_auth(RUNTIME.settings.model.provider, RUNTIME.store)}


@app.get("/api/providers/{provider_id}/auth", dependencies=[Auth])
async def provider_auth(provider_id: str) -> dict[str, Any]:
    if get_spec(provider_id) is None:
        raise HTTPException(404, "Unknown provider.")
    return describe_auth(provider_id, RUNTIME.store)


@app.get("/api/providers/{provider_id}/models", dependencies=[Auth])
async def provider_models(provider_id: str, base_url: str = "") -> dict[str, Any]:
    if get_spec(provider_id) is None:
        raise HTTPException(404, "Unknown provider.")
    models = await asyncio.get_running_loop().run_in_executor(
        None, discover_models, provider_id, RUNTIME.store, base_url
    )
    return {"models": models}


@app.post("/api/test-connection", dependencies=[Auth])
async def test_connection() -> dict[str, Any]:
    provider = RUNTIME.agent.provider
    if provider is None:
        return {"ok": False, "detail": "No provider is configured."}
    ok, detail = await provider.health()
    return {"ok": ok, "detail": detail}


# --------------------------------------------------------------------------- #
# Agent control plane
# --------------------------------------------------------------------------- #
#
# These back the MCP bridge in `arcbot.agentbridge`, which is how a delegated
# agent (Claude Code) reaches ArcBot's own controls.  They are token-gated like
# everything else and deliberately mirror the equivalent local tools rather than
# reimplementing them.


@app.post("/api/agent/{action}", dependencies=[Auth])
async def agent_control(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    agent = RUNTIME.agent
    settings = RUNTIME.settings

    if action == "status":
        host = agent.describe_host()
        off = [e.id for e in TOOLSET_CATALOG.values()
               if e.id not in settings.toolsets and not e.always_on and e.available]
        lines = [
            "ArcBot is a local application running on this machine. You are the agent inside it.",
            "The user sees a live trace of your work, a plan panel, capability switches and a "
            "terminal panel — not a command line.",
            f"Workspace: {settings.workspace_path}",
            f"Trust level: {settings.permissions.mode}",
            f"Model: {settings.model.model or '(provider default)'} via {settings.model.provider}",
            f"Capabilities on: {', '.join(settings.toolsets) or 'none'}",
            f"Capabilities off: {', '.join(off) or 'none'}",
        ]
        if host.get("url"):
            lines.append(f"Serving at: {host['url']}")
        if host.get("mcpServers"):
            lines.append(f"MCP servers: {', '.join(host['mcpServers'])}")
        return {"result": "\n".join(lines)}

    if action == "enable":
        toolset_id = str(payload.get("toolset") or "").strip().lower()
        entry = TOOLSET_CATALOG.get(toolset_id)
        if entry is None:
            available = ", ".join(k for k in TOOLSET_CATALOG if k not in ("core", "custom", "mcp"))
            return {"result": f"There is no capability called '{toolset_id}'. Available: {available}."}
        if entry.id in settings.toolsets:
            return {"result": f"'{entry.name}' is already on."}
        if entry.missing_requirements():
            missing = ", ".join(entry.missing_requirements())
            return {"result": f"'{entry.name}' needs Python package(s) {missing}, which are not installed."}

        result = await agent.broker.ask(
            Ask.TOOLSET,
            {"toolset": entry.id, "name": entry.name, "summary": entry.summary,
             "caution": entry.caution, "reason": str(payload.get("reason") or "")},
            default="deny",
        )
        if not result.approved:
            return {"result": (
                f"The user declined to enable '{entry.name}'. Do not ask again this turn — "
                f"solve it another way or explain what you cannot do without it."
            )}
        error = await agent._enable_toolset(entry.id)
        if error:
            return {"result": f"Could not enable '{entry.name}': {error}"}
        return {"result": (
            f"'{entry.name}' is now on. Note that its tools live inside ArcBot, so you still "
            f"cannot call them directly — but the capability is available for the rest of "
            f"this session and the user can see it enabled."
        )}

    if action == "ask":
        question = str(payload.get("question") or "").strip()
        if not question:
            return {"result": "No question given."}
        result = await agent.broker.ask(Ask.INPUT, {"question": question, "options": []},
                                        default="answer")
        if result.timed_out or not (result.value or "").strip():
            return {"result": "The user did not answer. Continue with your best judgement "
                              "and say clearly which assumption you made."}
        return {"result": f"The user answered: {result.value.strip()}"}

    if action == "quit":
        reason = str(payload.get("reason") or "").strip()
        # The bridge is a different doorway to the same house: it goes through
        # the permission engine exactly as the local quit_arcbot tool does.
        verdict = await agent.permissions.check_tool(
            "quit_arcbot", "write", {"reason": reason},
            title="Quit ArcBot", detail="Closes ArcBot entirely.",
        )
        if not verdict.allowed:
            return {"result": verdict.reason}
        if not await agent._request_quit(reason or "the agent was asked to quit"):
            return {"result": "Nothing is listening for a shutdown, so ArcBot cannot close "
                              "itself. Tell the user to close the ArcBot window."}
        return {"result": "ArcBot is shutting down. Do not call any more tools."}

    if action == "settings":
        panel = str(payload.get("panel") or "tools")
        if panel not in ("model", "trust", "tools", "mcp"):
            panel = "tools"
        await agent._open_settings(panel)
        reason = str(payload.get("reason") or "").strip()
        if reason:
            await RUNTIME.bus.emit(E.NOTICE, {"level": "info", "text": reason})
        return {"result": f"Opened the {panel} settings panel for the user."}

    raise HTTPException(404, f"Unknown action {action!r}.")


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #


@app.get("/api/mcp", dependencies=[Auth])
async def mcp_state() -> dict[str, Any]:
    from ..tools.mcp_bridge import mcp_available, presets_payload

    return {
        "available": mcp_available(),
        "enabled": "mcp" in RUNTIME.settings.toolsets,
        "servers": RUNTIME.settings.mcp_servers,
        "status": [s.to_dict() for s in RUNTIME.agent.mcp.status.values()],
        "presets": presets_payload(),
    }


@app.post("/api/mcp", dependencies=[Auth])
async def add_mcp_server(payload: dict[str, Any]) -> dict[str, Any]:
    """Add or replace one server, then reconnect."""
    name = str(payload.get("name") or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,40}", name):
        raise HTTPException(400, "Give the server a short name (letters, digits, spaces, - . _).")

    command = str(payload.get("command") or "").strip()
    url = str(payload.get("url") or "").strip()
    if not command and not url:
        raise HTTPException(400, "A server needs either a command to run or a URL to connect to.")
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "The URL must start with http:// or https://.")

    args = payload.get("args") or []
    if isinstance(args, str):
        args = shlex.split(args)
    env = payload.get("env") if isinstance(payload.get("env"), dict) else {}

    settings = RUNTIME.settings
    settings.mcp_servers[name] = {
        "command": command,
        "args": [str(a) for a in args][:40],
        "url": url,
        "env": {str(k): str(v) for k, v in list(env.items())[:20]},
        "enabled": bool(payload.get("enabled", True)),
    }
    if "mcp" not in settings.toolsets:
        settings.toolsets = normalise_toolsets([*settings.toolsets, "mcp"])
    RUNTIME.store.save(settings)
    await RUNTIME.agent.reload()
    return await mcp_state()


@app.delete("/api/mcp/{name}", dependencies=[Auth])
async def remove_mcp_server(name: str) -> dict[str, Any]:
    settings = RUNTIME.settings
    if settings.mcp_servers.pop(name, None) is None:
        raise HTTPException(404, "No server by that name.")
    RUNTIME.store.save(settings)
    await RUNTIME.agent.reload()
    return await mcp_state()


# --------------------------------------------------------------------------- #
# Tool builder
# --------------------------------------------------------------------------- #


@app.get("/api/tools/custom", dependencies=[Auth])
async def custom_tools() -> dict[str, Any]:
    from ..toolbuilder import example_requests, list_custom

    return {
        "tools": list_custom(),
        "enabled": "custom" in RUNTIME.settings.toolsets,
        "examples": example_requests(),
        "capabilities": list(_CAPABILITY_HELP.items()),
    }


@app.post("/api/tools/custom/generate", dependencies=[Auth])
async def generate_custom_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Ask the model for a tool.  Writes nothing — the user reviews the draft."""
    from ..toolbuilder import generate

    description = str(payload.get("description") or "").strip()
    if len(description) < 8:
        raise HTTPException(400, "Describe what the tool should do in a sentence or two.")
    provider = RUNTIME.agent.provider
    if provider is None:
        raise HTTPException(400, "No model is configured yet.")
    draft = await generate(description, provider, existing=str(payload.get("existing") or ""))
    return draft.to_dict()


@app.post("/api/tools/custom/validate", dependencies=[Auth])
async def validate_custom_tool(payload: dict[str, Any]) -> dict[str, Any]:
    from ..toolbuilder import validate

    return validate(str(payload.get("code") or "")).to_dict()


@app.post("/api/tools/custom/save", dependencies=[Auth])
async def save_custom_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Save a reviewed tool and make it available immediately."""
    from ..toolbuilder import save, validate

    draft = validate(str(payload.get("code") or ""))
    if not draft.valid:
        raise HTTPException(400, "; ".join(draft.problems))
    save(draft)

    settings = RUNTIME.settings
    if "custom" not in settings.toolsets:
        settings.toolsets = normalise_toolsets([*settings.toolsets, "custom"])
        RUNTIME.store.save(settings)
    await RUNTIME.agent.reload()
    return {"ok": True, "name": draft.name}


@app.get("/api/tools/custom/{name}", dependencies=[Auth])
async def read_custom_tool(name: str) -> dict[str, Any]:
    from ..toolbuilder import read

    try:
        code = read(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not code:
        raise HTTPException(404, "No tool by that name.")
    return {"name": name, "code": code}


@app.delete("/api/tools/custom/{name}", dependencies=[Auth])
async def delete_custom_tool(name: str) -> dict[str, Any]:
    from ..toolbuilder import delete

    if not delete(name):
        raise HTTPException(404, "No tool by that name.")
    await RUNTIME.agent.reload()
    return {"ok": True}


#: Shown next to the capability picker so the choice is meaningful.
_CAPABILITY_HELP = {
    "read": "Only reads. Runs without asking at every trust level.",
    "network": "Makes web requests. Asks in Guarded mode.",
    "write": "Changes files or state. Asks in Guarded mode.",
    "exec": "Runs other programs. Asks unless you are on Full access.",
    "system": "Changes machine settings. Asks unless you are on Full access.",
    "destructive": "Deletes things. Asks unless you are on Full access.",
}


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


@app.get("/api/sessions", dependencies=[Auth])
async def get_sessions() -> dict[str, Any]:
    return {"sessions": list_sessions(RUNTIME.settings.workspace_path, limit=50)}


@app.post("/api/sessions/{session_id}/open", dependencies=[Auth])
async def open_session(session_id: str) -> dict[str, Any]:
    await RUNTIME.agent.stop()
    await RUNTIME.agent.start(session_id=session_id)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}", dependencies=[Auth])
async def remove_session(session_id: str) -> dict[str, Any]:
    return {"ok": delete_session(RUNTIME.settings.workspace_path, session_id)}


# --------------------------------------------------------------------------- #
# Web socket
# --------------------------------------------------------------------------- #


def _origin_allowed(websocket: WebSocket) -> bool:
    """Only same-machine pages may open the socket."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # non-browser client (CLI, tests) — the token still gates it
    host = urlparse(origin).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(default="")) -> None:
    if not secrets.compare_digest(token or "", SESSION_TOKEN) or not _origin_allowed(websocket):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    RUNTIME.clients.add(websocket)

    async def emit(event: Event) -> None:
        await websocket.send_json(event.to_wire())

    unsubscribe = RUNTIME.bus.subscribe(emit)
    try:
        await RUNTIME.agent.emit_toolsets()
        await websocket.send_json({
            "type": E.READY,
            "sessionId": RUNTIME.agent.session.id if RUNTIME.agent.session else "",
            "workspace": str(RUNTIME.settings.workspace_path),
            "provider": RUNTIME.settings.model.provider,
            "model": RUNTIME.settings.model.model,
            "mode": RUNTIME.agent.provider.mode if RUNTIME.agent.provider else "model",
            "ready": RUNTIME.agent.provider is not None,
        })
        while True:
            message = await websocket.receive_json()
            await _handle_client_message(message)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("Web socket closed: %s", exc)
    finally:
        unsubscribe()
        RUNTIME.clients.discard(websocket)


async def _handle_client_message(message: dict[str, Any]) -> None:
    kind = message.get("type")
    agent = RUNTIME.agent

    if kind == "chat":
        text = str(message.get("text") or "").strip()
        if text:
            await agent.send(text)
    elif kind == "stop":
        await agent.stop()
    elif kind == "clear":
        await agent.clear()
    elif kind == "answer":
        agent.broker.resolve(
            str(message.get("askId") or ""),
            str(message.get("decision") or "deny"),
            message.get("value"),
        )
    elif kind == "terminal.input":
        agent.terminal.write(str(message.get("data") or ""))
    elif kind == "terminal.resize":
        rows, cols = message.get("rows"), message.get("cols")
        if isinstance(rows, int) and isinstance(cols, int):
            agent.terminal.resize(rows, cols)
    elif kind == "toolset.toggle":
        toolset_id = str(message.get("id") or "")
        if message.get("enabled"):
            error = await agent._enable_toolset(toolset_id)
            if error:
                await RUNTIME.bus.emit(E.NOTICE, {"level": "error", "text": error})
        else:
            await agent.disable_toolset(toolset_id)
    elif kind == "ping":
        await RUNTIME.bus.emit("pong", {})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _public_settings(settings: Settings) -> dict[str, Any]:
    """Settings as the browser sees them — never includes a secret."""
    data = settings.to_dict()
    data["workspaceResolved"] = str(settings.workspace_path)
    return data


def _version() -> str:
    from .. import __version__

    return __version__
