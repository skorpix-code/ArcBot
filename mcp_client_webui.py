"""
ArcBot MCP Client WebUI
========================
Multi-provider AI agent with:
- Ollama, LM Studio, OpenAI, Claude, Gemini support
- Cross-platform terminal (PTY on Unix, subprocess on Windows)
- Live streaming UI with tool visibility
- .env configuration management
- Command approval guardrails
"""

import asyncio
import copy
import json
import os
import platform
import re
import shutil
import signal
import sys
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

# --- PTY Support ---
try:
    import fcntl
    import pty
    import struct
    import termios

    HAS_PTY = True
except ImportError:
    HAS_PTY = False

import uvicorn
from dotenv import load_dotenv, set_key
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent
from openai import AsyncOpenAI
from rich.console import Console
from rich.panel import Panel

try:
    from google import genai
    from google.genai import types

    GOOGLE_SDK_AVAILABLE = True
except ImportError:
    GOOGLE_SDK_AVAILABLE = False

try:
    import anthropic

    ANTHROPIC_SDK_AVAILABLE = True
except ImportError:
    ANTHROPIC_SDK_AVAILABLE = False

load_dotenv()
console = Console()

ENV_FILE = Path(".env")


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def save_env(key: str, value: str):
    os.environ[key] = value
    if not ENV_FILE.exists():
        ENV_FILE.touch()
    set_key(str(ENV_FILE), key, value)


def get_base_dir() -> str:
    return get_env("ARCBOT_BASE_DIR", os.path.expanduser("~/ArcBot_Workspace"))


# Provider-specific API key mapping
PROVIDER_KEY_MAP = {
    "OpenAI": "OPENAI_API_KEY",
    "Claude": "ANTHROPIC_API_KEY",
    "Google Gemini": "GEMINI_API_KEY",
}


def get_api_key_for_provider(provider: str) -> str:
    """Get the API key env var name for a provider."""
    return PROVIDER_KEY_MAP.get(provider, "ARCBOT_API_KEY")


def load_api_key(provider: str) -> str:
    """Load API key for a specific provider from env."""
    key_name = get_api_key_for_provider(provider)
    return get_env(key_name, "")


def save_api_key(provider: str, key: str):
    """Save API key under the provider-specific env var name."""
    key_name = get_api_key_for_provider(provider)
    save_env(key_name, key)


def build_system_prompt(base_dir: str) -> str:
    return f"""You are ArcBot, a powerful autonomous system agent. You EXECUTE tasks using tools and terminal commands — you do not just talk about them.

Workspace: "{base_dir}"

CORE RULES:
1. ACT, DON'T EXPLAIN: If a task requires a command, call execute_command immediately. Never say "you could run..." — just run it.
2. EXECUTE COMMANDS FREELY: The system automatically asks the user for approval before any command runs. You do NOT need to ask permission — just call execute_command. The user sees an Approve/Deny dialog.
3. USE TOOLS FIRST: If a tool exists for the task, use it. Don't chat when you can act.
4. CHAIN OPERATIONS: For multi-step tasks, execute all steps sequentially without pausing to explain.
5. READ BEFORE EDIT: Always read files before modifying them.
6. TERMINAL IS YOUR FRIEND: Use execute_command freely for installing packages, running scripts, git, file ops, compiling, testing, serving, etc. The approval system protects the user.

TASK PLANNING (CRITICAL for complex tasks):
When given a task with multiple steps (e.g. "build a website", "set up a project", "refactor this codebase"):
1. FIRST create a detailed todo list using todo_add for EACH step. Be specific and detailed in descriptions.
2. Update each todo to "in_progress" when you start working on it using todo_update.
3. Mark each todo as "completed" when done using todo_update.
4. The user can see your progress in real-time. Keep the todo descriptions detailed enough that you can recall what to do.
5. For simple single-step tasks, just execute directly without creating todos.

WINDOW MANAGEMENT (follow this exact workflow):
1. ALWAYS call screen_info FIRST to get exact monitor dimensions and work area.
2. Use window_list to identify windows by title.
3. Use window_organize with layout: 'side_by_side', 'grid', 'main_plus_stack', 'columns_2', 'columns_3', 'rows_2', 'rows_3', 'cascade', 'stack_left_right'.
   - Pass specific windows via window_queries as comma-separated substrings (e.g. "chrome,code,terminal").
   - Set gap for spacing between windows (e.g. gap=8).
4. After organizing, ALWAYS call window_list to VERIFY the new positions are correct.
5. If positions aren't right, use window_move and window_resize with exact pixel coordinates.

AVAILABLE TOOL CATEGORIES:
- Files: read_file, write_file, edit_file, search_files, copy_item, move_item, delete_item, directory_tree, find_files_pattern, file_info, file_hash
- Archive: compress_files, extract_archive
- Terminal: execute_command (runs ANY shell command — system handles user approval)
- Windows: window_list, window_organize, window_focus, window_snap, window_move, window_resize, screen_info, monitor_list
- Memory: memory_store, memory_recall, memory_list, memory_forget
- System: get_system_info, process_list, process_kill, network_info, disk_usage, service_check, env_variable_get, env_variable_set
- Clipboard: clipboard_read, clipboard_write
- Utils: screenshot_take, notification_send, open_url, launch_app, open_file_default, volume_get, volume_set, datetime_info, http_request
- Text: text_search_replace, batch_file_rename
- Todo: todo_add, todo_list, todo_update, todo_remove

FORMATTING: Use markdown. Code blocks with language tags. Be concise.
"""


def repair_json_string(json_str: str) -> str:
    if not json_str:
        return "{}"
    json_str = json_str.replace("```json", "").replace("```", "").strip()
    json_str = re.sub(r",\s*([\]}])", r"\1", json_str)
    return json_str


def _sanitize_openai_messages(messages: list) -> list:
    """Build a clean, valid OpenAI message sequence from scratch.

    Rules enforced:
    1. Every 'tool' message must follow an assistant with matching tool_calls
    2. Every assistant with tool_calls must have ALL tool results after it
    3. content must be string (not None)
    4. No orphaned tool messages
    """
    if not messages:
        return []

    result = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        if role == "system":
            result.append({"role": "system", "content": msg.get("content", "") or ""})
            i += 1

        elif role == "user":
            result.append({"role": "user", "content": msg.get("content", "") or ""})
            i += 1

        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            content = msg.get("content", "") or ""

            if tool_calls and len(tool_calls) > 0:
                tc_ids = [tc.get("id") for tc in tool_calls if tc.get("id")]

                # Look ahead for matching tool results
                tool_results = []
                j = i + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    tmsg = messages[j]
                    if tmsg.get("tool_call_id") in tc_ids:
                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": tmsg.get("tool_call_id"),
                                "content": str(tmsg.get("content", "") or ""),
                            }
                        )
                    j += 1

                matched_ids = {tr["tool_call_id"] for tr in tool_results}

                if matched_ids == set(tc_ids) and len(matched_ids) > 0:
                    # Complete sequence — include assistant + all tool results
                    valid_tcs = [tc for tc in tool_calls if tc.get("id") in matched_ids]
                    assistant_msg = {"role": "assistant", "content": content}
                    if valid_tcs:
                        assistant_msg["tool_calls"] = valid_tcs
                    result.append(assistant_msg)
                    for tr in tool_results:
                        result.append(tr)
                    i = j
                else:
                    # Incomplete — strip tool_calls, keep as plain text
                    if content.strip():
                        result.append({"role": "assistant", "content": content})
                    i = j  # skip orphaned tool messages
            else:
                clean = {"role": "assistant", "content": content}
                result.append(clean)
                i += 1

        elif role == "tool":
            # Orphaned tool message — skip
            i += 1

        else:
            i += 1

    # Final cleanup
    cleaned = []
    for m in result:
        mc = m.copy()
        if mc.get("content") is None:
            mc["content"] = ""
        if "tool_calls" in mc and not mc["tool_calls"]:
            del mc["tool_calls"]
        cleaned.append(mc)

    return cleaned


def _truncate_messages(messages: list, max_recent: int) -> list:
    """Truncate keeping system + recent messages, then sanitize."""
    if len(messages) <= max_recent + 1:
        return _sanitize_openai_messages(messages)

    system_msgs = [m for m in messages[:1] if m.get("role") == "system"]
    start_idx = max(len(system_msgs), len(messages) - max_recent)

    # Walk forward to find a user message (safe boundary)
    while start_idx < len(messages):
        if messages[start_idx].get("role") == "user":
            break
        start_idx += 1

    if start_idx >= len(messages):
        start_idx = max(len(system_msgs), len(messages) - max_recent)

    trimmed = system_msgs + messages[start_idx:]
    return _sanitize_openai_messages(trimmed)


class LLMConfig:
    def __init__(self, provider: str, base_url: str, api_key: str, model: str):
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self.model = model


class OpenAICompatibleProvider:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key)

    async def chat_stream(self, messages, tools):
        try:
            stream = await self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=tools if tools else None,
                stream=True,
                temperature=0,
            )
            async for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    yield {
                        "content": delta.content,
                        "tool_calls": delta.tool_calls,
                        "role": delta.role,
                    }
        except Exception as e:
            console.print(f"[bold red]LLM Error:[/]{e}")
            yield {"content": f"Error: {e}", "tool_calls": None, "role": "assistant"}


class ClaudeProvider:
    def __init__(self, config: LLMConfig):
        if not ANTHROPIC_SDK_AVAILABLE:
            raise ImportError("anthropic not installed. Run: pip install anthropic")
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=config.api_key)

    def _convert_tools(self, tools):
        if not tools:
            return None
        out = []
        for t in tools:
            fn = t.get("function", {})
            out.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": copy.deepcopy(fn.get("parameters", {})),
                }
            )
        return out

    def _convert_messages(self, messages):
        system_prompt = ""
        msgs = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_prompt = content
            elif role == "assistant":
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in m.get("tool_calls") or []:
                    try:
                        args = (
                            json.loads(tc["function"]["arguments"])
                            if isinstance(tc["function"]["arguments"], str)
                            else tc["function"]["arguments"]
                        )
                    except:
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                            "name": tc["function"]["name"],
                            "input": args,
                        }
                    )
                if blocks:
                    msgs.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.get("tool_call_id", ""),
                                "content": content or "",
                            }
                        ],
                    }
                )
            elif role == "user":
                msgs.append({"role": "user", "content": content or ""})
        return system_prompt, msgs

    async def chat_stream(self, messages, tools):
        try:
            system_prompt, anthropic_msgs = self._convert_messages(messages)
            kwargs = {
                "model": self.config.model,
                "max_tokens": 8192,
                "messages": anthropic_msgs,
                "system": system_prompt,
            }
            at = self._convert_tools(tools)
            if at:
                kwargs["tools"] = at
            async with self.client.messages.stream(**kwargs) as stream:
                tid, tname, targs = None, None, ""
                async for event in stream:
                    if event.type == "content_block_start":
                        b = event.content_block
                        if b.type == "tool_use":
                            tid, tname, targs = b.id, b.name, ""
                    elif event.type == "content_block_delta":
                        d = event.delta
                        if d.type == "text_delta":
                            yield {
                                "content": d.text,
                                "tool_calls": None,
                                "role": "assistant",
                            }
                        elif d.type == "input_json_delta":
                            targs += d.partial_json
                    elif event.type == "content_block_stop":
                        if tname:
                            yield {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": tid,
                                        "function": {"name": tname, "arguments": targs},
                                        "type": "function",
                                    }
                                ],
                                "role": "assistant",
                            }
                            tid, tname, targs = None, None, ""
        except Exception as e:
            console.print(f"[bold red]Claude Error:[/]{e}")
            yield {"content": f"Error: {e}", "tool_calls": None, "role": "assistant"}


class GeminiNativeProvider:
    def __init__(self, config: LLMConfig):
        if not GOOGLE_SDK_AVAILABLE:
            raise ImportError("Google GenAI SDK not found.")
        self.config = config
        self.client = genai.Client(api_key=config.api_key)
        self.chat_session = None

    def _sanitize_schema(self, schema: Any) -> Any:
        if isinstance(schema, dict):
            clean = schema.copy()
            for key in [
                "exclusiveMaximum",
                "exclusiveMinimum",
                "default",
                "title",
                "additionalProperties",
            ]:
                clean.pop(key, None)
            for k, v in clean.items():
                clean[k] = self._sanitize_schema(v)
            return clean
        elif isinstance(schema, list):
            return [self._sanitize_schema(item) for item in schema]
        return schema

    def _convert_tools(self, tools):
        if not tools:
            return None
        unique = {}
        for t in tools:
            name = t["function"]["name"]
            unique[name] = {
                "name": name,
                "description": t["function"]["description"],
                "parameters": self._sanitize_schema(
                    copy.deepcopy(t["function"].get("parameters", {}))
                ),
            }
        return {"function_declarations": list(unique.values())}

    async def chat_stream(self, messages, tools):
        try:
            base_dir = get_base_dir()
            if self.chat_session is None:
                tc = self._convert_tools(tools)
                gc = types.GenerateContentConfig(
                    tools=[tc] if tc else None,
                    system_instruction=build_system_prompt(base_dir),
                    temperature=0,
                )
                self.chat_session = self.client.aio.chats.create(
                    model=self.config.model, config=gc
                )
            last = messages[-1]
            rs = None
            if last["role"] == "user":
                rs = await self.chat_session.send_message_stream(last["content"])
            elif last["role"] == "tool":
                part = types.Part.from_function_response(
                    name=last.get("name"), response={"result": last["content"]}
                )
                rs = await self.chat_session.send_message_stream(part)
            if rs:
                async for chunk in rs:
                    tcs = []
                    if chunk.function_calls:
                        for fc in chunk.function_calls:
                            tcs.append(
                                {
                                    "id": f"call_{uuid.uuid4().hex[:8]}",
                                    "function": {
                                        "name": fc.name,
                                        "arguments": json.dumps(fc.args)
                                        if fc.args
                                        else "{}",
                                    },
                                    "type": "function",
                                }
                            )
                    yield {
                        "content": chunk.text if chunk.text else "",
                        "tool_calls": tcs,
                        "role": "assistant",
                    }
        except Exception as e:
            console.print(f"[bold red]Gemini Error:[/]{e}")
            yield {"content": f"Error: {e}", "tool_calls": None, "role": "assistant"}


class TerminalManager:
    def __init__(self):
        self.active_fd = None
        self.shell_fd = None
        self.shell_process = None
        self.agent_proc = None
        self.agent_fd = None
        self.agent_buffer = []
        self._shutdown = False
        self._is_windows = platform.system() == "Windows"

    async def stop(self):
        self._shutdown = True
        for proc in [self.agent_proc, self.shell_process]:
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except:
                    try:
                        proc.kill()
                    except:
                        pass
        if not self._is_windows:
            for fd in [self.agent_fd, self.shell_fd, self.active_fd]:
                if fd:
                    try:
                        os.close(fd)
                    except:
                        pass
        self.agent_fd = self.shell_fd = self.active_fd = None
        self.agent_proc = self.shell_process = None

    async def start_shell(self):
        if self._is_windows or not HAS_PTY:
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        master, slave = pty.openpty()
        self.shell_fd = master
        self.active_fd = master
        self.shell_process = await asyncio.create_subprocess_exec(
            shell,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=os.setsid,
            cwd=get_base_dir(),
        )
        os.close(slave)
        asyncio.create_task(self._read_loop(master, is_shell=True))

    async def run_interactive_command(self, cmd_input: str, broadcast_func) -> tuple:
        if self._shutdown:
            return "Shutdown in progress", "FINISHED"
        working_dir = get_base_dir()

        if self._is_windows or not HAS_PTY:
            try:
                kwargs = {
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                    "stdin": asyncio.subprocess.PIPE,
                    "cwd": working_dir,
                }
                if self._is_windows:
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    proc = await asyncio.create_subprocess_exec(
                        "cmd.exe", "/c", cmd_input, **kwargs
                    )
                else:
                    proc = await asyncio.create_subprocess_shell(cmd_input, **kwargs)
                output_parts = []

                async def read_stream(stream):
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        text = line.decode(errors="replace")
                        output_parts.append(text)
                        await broadcast_func("terminal_data", {"data": text})

                await asyncio.gather(read_stream(proc.stdout), read_stream(proc.stderr))
                await proc.wait()
                return "".join(output_parts), f"FINISHED (Exit Code: {proc.returncode})"
            except Exception as e:
                return str(e), "FINISHED (Error)"

        is_active = self.agent_proc is not None and self.agent_proc.returncode is None
        if not is_active:
            if self.agent_fd:
                try:
                    os.close(self.agent_fd)
                except:
                    pass
            master, slave = pty.openpty()
            self.agent_fd = master
            self.active_fd = master
            self.agent_proc = await asyncio.create_subprocess_shell(
                cmd_input,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=os.setsid,
                cwd=working_dir,
            )
            os.close(slave)
        else:
            if self.agent_fd:
                os.write(self.agent_fd, (cmd_input + "\n").encode())

        self.agent_buffer = []
        loop = asyncio.get_running_loop()
        while True:
            if self._shutdown or self.agent_proc.returncode is not None:
                break
            try:
                data = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: os.read(self.agent_fd, 1024)),
                    timeout=2.5,
                )
                if not data:
                    break
                text = data.decode(errors="replace")
                self.agent_buffer.append(text)
                await broadcast_func("terminal_data", {"data": text})
            except asyncio.TimeoutError:
                if self.agent_proc.returncode is None:
                    return "".join(self.agent_buffer), "PAUSED"
                break
            except OSError:
                break

        try:
            await asyncio.wait_for(self.agent_proc.wait(), 1.0)
        except:
            pass
        ret = self.agent_proc.returncode
        self.agent_proc = None
        if self.shell_fd:
            self.active_fd = self.shell_fd
        return "".join(self.agent_buffer), f"FINISHED (Exit Code: {ret})"

    async def _read_loop(self, fd, is_shell=False):
        loop = asyncio.get_running_loop()
        while not self._shutdown:
            try:
                if is_shell and self.active_fd != fd:
                    await asyncio.sleep(0.1)
                    continue
                data = await loop.run_in_executor(None, lambda: os.read(fd, 1024))
                if not data:
                    break
                if active_websocket:
                    await active_websocket.send_json(
                        {"type": "terminal_data", "data": data.decode(errors="replace")}
                    )
            except:
                break

    def write_input(self, data: str):
        if self.active_fd and not self._shutdown:
            try:
                os.write(self.active_fd, data.encode())
            except:
                pass

    def resize(self, rows: int, cols: int):
        if self.active_fd and HAS_PTY and not self._shutdown:
            try:
                fcntl.ioctl(
                    self.active_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
            except:
                pass


terminal = TerminalManager()


class MCPClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.exit_stack = AsyncExitStack()
        self.sessions = []
        self.tool_routing = {}
        self.pending_approvals: Dict[str, asyncio.Future] = {}
        self._shutdown = False
        self._cancel_current = False
        if config.provider == "Google Gemini":
            self.llm = GeminiNativeProvider(config)
        elif config.provider == "Claude":
            self.llm = ClaudeProvider(config)
        else:
            self.llm = OpenAICompatibleProvider(config)
        console.log(f"[bold green]Initialized: {config.provider} ({config.model})[/]")

    async def shutdown(self):
        self._shutdown = True
        for _, f in self.pending_approvals.items():
            if not f.done():
                f.cancel()
        self.pending_approvals.clear()
        try:
            await self.exit_stack.aclose()
        except:
            pass

    async def connect_to_server(self, name: str, config: Dict):
        console.log(f"Connecting to [cyan]{name}[/]...")
        env = {**os.environ, **config.get("env", {})}
        params = StdioServerParameters(
            command=config["command"], args=config.get("args", []), env=env
        )
        transport = await self.exit_stack.enter_async_context(stdio_client(params))
        read, write = transport
        session = await self.exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.sessions.append(session)

    async def initialize_all(self):
        try:
            if os.path.exists("servers_config.json"):
                with open("servers_config.json", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                for name, s_cfg in cfg.get("mcpServers", {}).items():
                    await self.broadcast("status", {"text": f"Connecting to {name}..."})
                    try:
                        await self.connect_to_server(name, s_cfg)
                    except Exception as e:
                        console.print(f"[yellow]Failed to connect to {name}: {e}[/]")
        except Exception as e:
            console.print(f"[red]Config Error: {e}[/]")

    async def list_tools(self):
        all_tools = []
        self.tool_routing.clear()
        for session in self.sessions:
            try:
                res = await session.list_tools()
                for tool in res.tools:
                    all_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.inputSchema,
                            },
                        }
                    )
                    self.tool_routing[tool.name] = session
            except Exception as e:
                console.print(f"[yellow]Tool listing error: {e}[/]")
        all_tools.append(
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "Executes a terminal command. The user will be asked to approve before execution. If paused for input, call again with input.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The command or input for a paused process.",
                            }
                        },
                        "required": ["command"],
                    },
                },
            }
        )
        return all_tools

    async def call_tool(self, name, args):
        session = self.tool_routing.get(name)
        if not session:
            raise ValueError(f"Tool {name} not found")
        return await session.call_tool(name, arguments=args)

    async def broadcast(self, type_str, payload={}):
        if active_websocket:
            try:
                await active_websocket.send_json({"type": type_str, **payload})
            except:
                pass

    async def request_confirmation(self, command: str) -> bool:
        rid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending_approvals[rid] = future
        await self.broadcast("request_approval", {"requestId": rid, "command": command})
        try:
            return await future
        finally:
            self.pending_approvals.pop(rid, None)

    async def run_chat_loop_web(self):
        try:
            async with self.exit_stack:
                await self.broadcast("status", {"text": "Initializing Agent..."})
                await self.initialize_all()
                await self.broadcast("status", {"text": "Discovering Tools..."})
                tools = await self.list_tools()
                tool_names = [t["function"]["name"] for t in tools]
                await self.broadcast("tools_discovered", {"tools": tool_names})

                base_dir = get_base_dir()
                messages = [
                    {"role": "system", "content": build_system_prompt(base_dir)}
                ]
                MAX_RECENT = 30

                console.rule("[bold green]Agent Active[/]")
                await self.broadcast("status", {"text": "Ready"})
                await terminal.start_shell()

                while not self._shutdown:
                    user_input = await input_queue.get()
                    if self._shutdown:
                        break

                    messages.append({"role": "user", "content": user_input})
                    await self.broadcast("status", {"text": "Thinking..."})
                    await self.broadcast("start")
                    await self.broadcast("thinking_status", {"text": "Thinking..."})

                    while not self._shutdown:
                        # Check for user-initiated stop
                        if self._cancel_current:
                            self._cancel_current = False
                            await self.broadcast("end")
                            await self.broadcast("status", {"text": "Ready"})
                            break

                        response_content, tc_buf = [], {}

                        # Sanitize for OpenAI, simple truncate for others
                        if isinstance(self.llm, OpenAICompatibleProvider):
                            msgs_to_send = _truncate_messages(messages, MAX_RECENT)
                        else:
                            msgs_to_send = (
                                messages
                                if len(messages) <= MAX_RECENT + 1
                                else [messages[0]] + messages[-(MAX_RECENT):]
                            )

                        async for chunk in self.llm.chat_stream(msgs_to_send, tools):
                            if self._shutdown or self._cancel_current:
                                break
                            if chunk.get("content"):
                                response_content.append(chunk["content"])
                                await self.broadcast(
                                    "chunk", {"content": chunk["content"]}
                                )
                            if chunk.get("tool_calls"):
                                for tc in chunk["tool_calls"]:
                                    if hasattr(tc, "index"):
                                        idx = tc.index
                                        if idx not in tc_buf:
                                            tc_buf[idx] = {
                                                "id": tc.id,
                                                "name": "",
                                                "args": "",
                                            }
                                        if tc.function.name:
                                            tc_buf[idx]["name"] = tc.function.name
                                        if tc.function.arguments:
                                            tc_buf[idx]["args"] += tc.function.arguments
                                    elif isinstance(tc, dict):
                                        idx = tc.get("id", uuid.uuid4().hex)
                                        tc_buf[idx] = {
                                            "id": idx,
                                            "name": tc["function"]["name"],
                                            "args": tc["function"]["arguments"],
                                        }

                        full = "".join(response_content)
                        htcs = []
                        for idx in sorted(tc_buf.keys()):
                            d = tc_buf[idx]
                            htcs.append(
                                {
                                    "id": d.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                    "type": "function",
                                    "function": {
                                        "name": d["name"],
                                        "arguments": d["args"],
                                    },
                                }
                            )

                        amsg = {"role": "assistant", "content": full or ""}
                        if htcs:
                            amsg["tool_calls"] = htcs
                        messages.append(amsg)

                        if not htcs:
                            await self.broadcast("end")
                            await self.broadcast("status", {"text": "Ready"})
                            await self.broadcast("thinking_status", {"text": ""})
                            break

                        await self.broadcast("tool_start")
                        await self.broadcast(
                            "thinking_status",
                            {"text": f"Executing {len(htcs)} tool(s)..."},
                        )
                        for tc in htcs:
                            if self._shutdown or self._cancel_current:
                                break
                            fn = tc["function"]["name"]
                            fa = tc["function"]["arguments"]
                            await self.broadcast(
                                "tool_call", {"name": fn, "args": fa[:200]}
                            )
                            await self.broadcast(
                                "status", {"text": f"Running: {fn}..."}
                            )
                            await self.broadcast(
                                "thinking_status", {"text": f"Running {fn}..."}
                            )
                            try:
                                args = json.loads(fa)
                            except:
                                args = json.loads(repair_json_string(fa))
                            result = ""
                            if fn == "execute_command":
                                cmd = args.get("command", "")
                                await self.broadcast(
                                    "status", {"text": "Awaiting Approval..."}
                                )
                                await self.broadcast(
                                    "thinking_status",
                                    {"text": f"Waiting for approval: {cmd}"},
                                )
                                ok = await self.request_confirmation(cmd)
                                if not ok:
                                    result = "User denied execution."
                                else:
                                    await self.broadcast("terminal_open")
                                    await self.broadcast(
                                        "terminal_data",
                                        {"data": f"\r\n\x1b[32m$ {cmd}\x1b[0m\r\n"},
                                    )
                                    try:
                                        (
                                            out,
                                            st,
                                        ) = await terminal.run_interactive_command(
                                            cmd, self.broadcast
                                        )
                                        if st == "PAUSED":
                                            result = f"COMMAND PAUSED.\nOutput:\n{out}\n\n[SYSTEM]: Process waiting. Call execute_command again with input."
                                            await self.broadcast(
                                                "terminal_data",
                                                {
                                                    "data": "\r\n\x1b[33m[Paused]\x1b[0m\r\n"
                                                },
                                            )
                                        else:
                                            result = out or "(No output)"
                                            await self.broadcast(
                                                "terminal_data",
                                                {
                                                    "data": f"\r\n\x1b[90m[{st}]\x1b[0m\r\n"
                                                },
                                            )
                                    except Exception as e:
                                        result = f"Execution failed: {e}"
                            else:
                                try:
                                    res = await self.call_tool(fn, args)
                                    result = "\n".join(
                                        [
                                            b.text
                                            for b in res.content
                                            if isinstance(b, TextContent)
                                        ]
                                    )
                                except Exception as e:
                                    result = f"Error: {e}"
                            await self.broadcast(
                                "tool_result",
                                {
                                    "name": fn,
                                    "result": result[:500]
                                    + ("..." if len(result) > 500 else ""),
                                },
                            )
                            # After any todo tool, fetch the full list and broadcast structured data
                            if fn.startswith("todo_"):
                                try:
                                    todo_res = await self.call_tool(
                                        "todo_list", {"include_completed": True}
                                    )
                                    todo_text = "\n".join(
                                        [
                                            b.text
                                            for b in todo_res.content
                                            if isinstance(b, TextContent)
                                        ]
                                    )
                                    await self.broadcast(
                                        "todo_list_data", {"raw": todo_text}
                                    )
                                except:
                                    pass
                            console.print(
                                Panel(
                                    result[:500],
                                    title=f"Output: {fn}",
                                    border_style="green",
                                )
                            )
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc.get("id"),
                                    "name": fn,
                                    "content": result,
                                }
                            )
                        await self.broadcast("tool_end")
                        await self.broadcast("status", {"text": "Analyzing results..."})
                        await self.broadcast(
                            "thinking_status", {"text": "Analyzing tool results..."}
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            console.print(f"[bold red]Chat loop error: {e}[/]")
            import traceback

            traceback.print_exc()
            raise


import subprocess

input_queue = asyncio.Queue()
active_websocket: Optional[WebSocket] = None
chat_task: Optional[asyncio.Task] = None
current_client: Optional[MCPClient] = None
shutdown_event: Optional[asyncio.Event] = None
uvicorn_server: Optional[uvicorn.Server] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global shutdown_event
    shutdown_event = asyncio.Event()
    yield
    await graceful_shutdown()


async def graceful_shutdown():
    global current_client, chat_task, terminal
    console.print("[yellow]Shutting down...[/]")
    if shutdown_event:
        shutdown_event.set()
    await terminal.stop()
    if chat_task and not chat_task.done():
        chat_task.cancel()
        try:
            await asyncio.wait_for(chat_task, timeout=3.0)
        except:
            pass
    if current_client:
        await current_client.shutdown()
    while not input_queue.empty():
        try:
            input_queue.get_nowait()
        except:
            break


app = FastAPI(lifespan=lifespan)
if not os.path.exists("templates"):
    os.makedirs("templates")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/config")
async def get_config():
    provider = get_env("ARCBOT_PROVIDER", "")
    has_key = bool(load_api_key(provider)) if provider else False
    return {
        "provider": provider,
        "model": get_env("ARCBOT_MODEL", ""),
        "apiKey": "",
        "baseDir": get_base_dir(),
        "hasKey": has_key,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global active_websocket, chat_task, current_client
    await websocket.accept()
    active_websocket = websocket
    try:
        while True:
            if shutdown_event and shutdown_event.is_set():
                break
            raw = await websocket.receive_text()
            data = json.loads(raw)
            if data.get("type") == "configure":
                cd = data.get("config")
                provider, api_key, model = (
                    cd.get("provider"),
                    cd.get("apiKey", ""),
                    cd.get("model"),
                )
                base_dir, base_url = (
                    cd.get("baseDir", "").strip(),
                    cd.get("baseUrl", ""),
                )
                save_env("ARCBOT_PROVIDER", provider)
                save_env("ARCBOT_MODEL", model)
                if api_key:
                    save_api_key(provider, api_key)
                else:
                    # Try loading existing key for this provider
                    api_key = load_api_key(provider)
                if base_dir:
                    save_env("ARCBOT_BASE_DIR", base_dir.replace("\\", "/"))
                    Path(base_dir).mkdir(parents=True, exist_ok=True)
                if not base_url:
                    if provider == "Ollama":
                        base_url = "http://localhost:11434/v1"
                    elif provider == "LM Studio":
                        base_url = "http://localhost:1234/v1"
                    elif provider == "OpenAI":
                        base_url = "https://api.openai.com/v1"
                if chat_task and not chat_task.done():
                    chat_task.cancel()
                    try:
                        await asyncio.wait_for(chat_task, timeout=3.0)
                    except:
                        pass
                if current_client:
                    await current_client.shutdown()
                while not input_queue.empty():
                    input_queue.get_nowait()
                current_client = MCPClient(
                    LLMConfig(provider, base_url, api_key or "not-needed", model)
                )
                chat_task = asyncio.create_task(current_client.run_chat_loop_web())
                await websocket.send_json({"type": "config_success"})
            elif data.get("type") == "message":
                await input_queue.put(data.get("content"))
            elif data.get("type") == "stop_generation":
                # Cancel the current chat loop iteration
                if current_client:
                    current_client._cancel_current = True
                    # Cancel any pending approvals
                    for _, f in current_client.pending_approvals.items():
                        if not f.done():
                            f.set_result(False)
                    await websocket.send_json({"type": "generation_stopped"})
            elif data.get("type") == "terminal_input":
                terminal.write_input(data.get("data"))
            elif data.get("type") == "terminal_resize":
                c, r = data.get("cols"), data.get("rows")
                if c and r:
                    terminal.resize(r, c)
            elif data.get("type") == "approval_response":
                rid, ok = data.get("requestId"), data.get("approved")
                if current_client and rid in current_client.pending_approvals:
                    f = current_client.pending_approvals[rid]
                    if not f.done():
                        f.set_result(ok)
    except WebSocketDisconnect:
        active_websocket = None
    except Exception as e:
        console.print(f"[red]WebSocket error: {e}[/]")
        active_websocket = None


def signal_handler(signum, frame):
    if shutdown_event:
        shutdown_event.set()
    if uvicorn_server:
        uvicorn_server.should_exit = True


async def run_server():
    global uvicorn_server
    config = uvicorn.Config(
        app, host="0.0.0.0", port=8000, log_level="critical", loop="asyncio"
    )
    uvicorn_server = uvicorn.Server(config)
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda s=sig: signal_handler(s, None))
        except NotImplementedError:
            pass
    try:
        await uvicorn_server.serve()
    except asyncio.CancelledError:
        pass
    finally:
        await graceful_shutdown()


if __name__ == "__main__":
    console.rule("[bold cyan]ArcBot Agent[/]")
    console.print(
        "Open [bold blue]http://localhost:8000[/] | Press [bold red]Ctrl+C[/] to quit."
    )
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
    except SystemExit:
        pass
