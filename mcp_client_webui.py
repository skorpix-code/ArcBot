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
from typing import Any, AsyncGenerator, Dict, List, Optional

# --- PTY Support for Interactive Terminal ---
try:
    import fcntl
    import pty
    import struct
    import termios

    HAS_PTY = True
except ImportError:
    HAS_PTY = False

import uvicorn
from dotenv import load_dotenv

# --- Web Server Imports ---
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# MCP SDK Imports
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

# --- LLM CLIENT LIBRARIES ---
from openai import AsyncOpenAI
from rich.console import Console
from rich.panel import Panel

# Check for Google GenAI SDK
try:
    from google import genai
    from google.genai import types

    GOOGLE_SDK_AVAILABLE = True
except ImportError:
    GOOGLE_SDK_AVAILABLE = False

load_dotenv()
console = Console()

# --- UNIVERSAL SYSTEM PROMPT ---
SYSTEM_PROMPT = """You are an advanced System Agent with full authorization to control the local environment. Your primary function is to execute tasks using the Model Context Protocol (MCP) and system commands.

Your home directory is "/home/skorp/Programming/LLM_Coder_Dir". This is where all folders and files exist.

**PRIME DIRECTIVE: ACTION OVER REFUSAL**
1. **Authorization:** You have full permission to access files, run applications (including GUI apps), and execute system commands. The user acts as the "Human-in-the-Loop" Security Guard and will manually approve or deny every single command you propose.
2. **No Self-Censorship:** Do NOT refuse a command based on "safety" or "capability" hallucinations. If the user asks to "open firefox", "delete a file", or "scan a directory", your job is to propose the correct `execute_command` call. Let the user decide if it is safe.
3. **Real-World Execution:** Do not simulate actions. Do not provide text-based tutorials unless explicitly asked. Use the tools to actually DO the thing.

**OPERATIONAL RULES:**
1. **Tool First:** Always prioritize using tools (like `execute_command` or specific MCP tools) over chatting.
2. **Sequential Logic:** Plan step-by-step.
   - *Example:* To edit a file, first `cat` it to see contents, then write to it.
   - *Example:* To run a script, ensure it is executable (`chmod +x`) before running.
3. **JSON Strictness:** Arguments must be valid JSON. No trailing commas.

**BATCH OPERATIONS & COMPLETION:**
1. **Do Not Pause:** If the user asks to repetitive task (e.g., "create 40 files", "delete all txt files"), do NOT stop to ask for confirmation after creating a few.
2. **Maximize Throughput:** Generate as many tool calls in succession to complete an operation.
3. **Auto-Continuation:** If you cannot finish the task in one turn (due to output limits), immediately output the next batch of tool calls in your next turn without asking the user "Shall I continue?".
4. **Silence is Golden:** When performing a mass operation, do not output conversational text like "I have created 5 files." until the ENTIRE task is complete. Just output the Tool Calls.

**THE `execute_command` PROTOCOL:**
This is your primary tool for OS interaction.
- **Interactive Mode:** The terminal IS interactive. If a command (like `npm init` or `python script.py`) pauses to wait for user input, the tool will return a `[PAUSED]` status with the output so far.
- **Providing Input:** When you see `[PAUSED]`, check the last line of output. If it's a prompt (e.g., "Proceed? y/n"), simply call `execute_command` again with your answer (e.g., `command="y"`).
- **GUI Applications:** You CAN open GUI apps. Always append `&` to detach them (e.g., `firefox &`).
- **Chaining:** You may chain commands with `&&` for efficiency (e.g., `mkdir test && cd test`).

**FORMATTING & FALLBACK:**
You are configured for native function calling. If that fails, strictly use this XML fallback format:

<tool_call]>
{"name": "tool_name", "arguments": {"arg_name": "value"}}
</tool_call]>
"""


# --- UTILS ---
def repair_json_string(json_str: str) -> str:
    if not json_str:
        return "{}"
    json_str = json_str.replace("```json", "").replace("```", "").strip()
    json_str = re.sub(r",\s*([\]}])", r"\1", json_str)
    return json_str


# --- CONFIGURATION ---
class LLMConfig:
    def __init__(self, provider: str, base_url: str, api_key: str, model: str):
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self.model = model


WORKING_DIR = "/home/skorp/Programming/LLM_Coder_Dir"


# --- PROVIDERS ---
class OpenAICompatibleProvider:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key)

    async def chat_stream(self, messages, tools):
        try:
            clean_messages = []
            for m in messages:
                msg = m.copy()
                if msg.get("content") is None:
                    msg["content"] = ""
                if "tool_calls" in msg and msg["tool_calls"] is None:
                    del msg["tool_calls"]
                clean_messages.append(msg)

            stream = await self.client.chat.completions.create(
                model=self.config.model,
                messages=clean_messages,
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
            unsupported_keys = [
                "exclusiveMaximum",
                "exclusiveMinimum",
                "default",
                "title",
                "additionalProperties",
            ]
            for key in unsupported_keys:
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
        unique_tools = {}
        for t in tools:
            name = t["function"]["name"]
            raw_params = copy.deepcopy(t["function"].get("parameters", {}))
            clean_params = self._sanitize_schema(raw_params)
            unique_tools[name] = {
                "name": name,
                "description": t["function"]["description"],
                "parameters": clean_params,
            }
        return {"function_declarations": list(unique_tools.values())}

    async def chat_stream(self, messages, tools):
        try:
            if self.chat_session is None:
                tool_config = self._convert_tools(tools)
                gen_config = types.GenerateContentConfig(
                    tools=[tool_config] if tool_config else None,
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0,
                )
                self.chat_session = self.client.aio.chats.create(
                    model=self.config.model,
                    config=gen_config,
                )

            last_msg = messages[-1]
            response_stream = None

            if last_msg["role"] == "user":
                response_stream = await self.chat_session.send_message_stream(
                    last_msg["content"]
                )
            elif last_msg["role"] == "tool":
                part = types.Part.from_function_response(
                    name=last_msg.get("name"), response={"result": last_msg["content"]}
                )
                response_stream = await self.chat_session.send_message_stream(part)

            if response_stream:
                async for chunk in response_stream:
                    tool_calls_out = []
                    if chunk.function_calls:
                        for fc in chunk.function_calls:
                            args_json = json.dumps(fc.args) if fc.args else "{}"
                            tool_calls_out.append(
                                {
                                    "id": f"call_{uuid.uuid4().hex[:8]}",
                                    "function": {
                                        "name": fc.name,
                                        "arguments": args_json,
                                    },
                                    "type": "function",
                                }
                            )

                    yield {
                        "content": chunk.text if chunk.text else "",
                        "tool_calls": tool_calls_out,
                        "role": "assistant",
                    }
        except Exception as e:
            console.print(f"[bold red]Gemini Error:[/]{e}")
            yield {"content": f"Error: {e}", "tool_calls": None, "role": "assistant"}


# --- TERMINAL MANAGER (MODIFIED) ---
class TerminalManager:
    """Manages persistent and ephemeral PTY sessions with Pause Detection."""

    def __init__(self):
        self.active_fd = None
        self.shell_fd = None
        self.shell_process = None

        # --- Interactive Agent Process State ---
        self.agent_proc = None
        self.agent_fd = None
        self.agent_buffer = []
        self._shutdown = False

    async def stop(self):
        """Gracefully stop all running processes."""
        self._shutdown = True

        # Kill agent process if running
        if self.agent_proc and self.agent_proc.returncode is None:
            try:
                self.agent_proc.terminate()
                await asyncio.wait_for(self.agent_proc.wait(), timeout=2.0)
            except:
                try:
                    self.agent_proc.kill()
                except:
                    pass

        # Kill shell process if running
        if self.shell_process and self.shell_process.returncode is None:
            try:
                self.shell_process.terminate()
                await asyncio.wait_for(self.shell_process.wait(), timeout=2.0)
            except:
                try:
                    self.shell_process.kill()
                except:
                    pass

        # Close file descriptors
        for fd in [self.agent_fd, self.shell_fd, self.active_fd]:
            if fd:
                try:
                    os.close(fd)
                except:
                    pass

        self.agent_fd = None
        self.shell_fd = None
        self.active_fd = None
        self.agent_proc = None
        self.shell_process = None

    async def start_shell(self):
        """Starts a persistent background shell (bash/zsh) for user interaction."""
        if not HAS_PTY or os.name != "posix":
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
            cwd=WORKING_DIR,
        )
        os.close(slave)

        asyncio.create_task(self._read_loop(master, is_shell=True))
        console.log(f"[green]Started User Shell: {shell}[/]")

    async def run_interactive_command(
        self, cmd_input: str, broadcast_func
    ) -> (str, str):
        """
        Runs a command OR sends input to an existing active command.

        Returns: (output_str, status)
        Status can be: "FINISHED" or "PAUSED"
        """
        if self._shutdown:
            return "Shutdown in progress", "FINISHED"

        if not HAS_PTY:
            # Fallback for Windows/Non-PTY (No interaction support yet)
            proc = await asyncio.create_subprocess_shell(
                cmd_input,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=WORKING_DIR,
            )
            stdout, stderr = await proc.communicate()
            return (stdout.decode() + stderr.decode()), "FINISHED"

        # 1. Determine: New Command or Input?
        # A process is 'active' if it exists and hasn't returned an exit code
        is_active = self.agent_proc is not None and self.agent_proc.returncode is None

        if not is_active:
            # --- START NEW COMMAND ---
            # Cleanup old FD if exists
            if self.agent_fd:
                try:
                    os.close(self.agent_fd)
                except:
                    pass

            master, slave = pty.openpty()
            self.agent_fd = master
            self.active_fd = master  # Steal UI focus

            # Start process
            self.agent_proc = await asyncio.create_subprocess_shell(
                cmd_input,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=os.setsid,
                cwd=WORKING_DIR,
            )
            os.close(slave)
        else:
            # --- SEND INPUT TO EXISTING ---
            if self.agent_fd:
                # Send input + newline
                input_bytes = (cmd_input + "\n").encode()
                os.write(self.agent_fd, input_bytes)

        # 2. READ LOOP (With Silence Detection)
        self.agent_buffer = []
        loop = asyncio.get_running_loop()

        # Time to wait for output before deciding the process is paused (waiting for input)
        SILENCE_THRESHOLD = 2.5

        while True:
            if self._shutdown:
                break

            # Check if died
            if self.agent_proc.returncode is not None:
                break

            try:
                # Helper to read from FD in thread executor
                read_fut = loop.run_in_executor(
                    None, lambda: os.read(self.agent_fd, 1024)
                )

                # Wait for data OR silence timeout
                data = await asyncio.wait_for(read_fut, timeout=SILENCE_THRESHOLD)

                if not data:  # EOF
                    break

                text = data.decode(errors="replace")
                self.agent_buffer.append(text)
                await broadcast_func("terminal_data", {"data": text})

            except asyncio.TimeoutError:
                # Timeout hit!
                # Check if process is still alive using wait_for(wait(), 0) trick or checking returncode
                if self.agent_proc.returncode is None:
                    # It's alive, just silent. We assume it's PAUSED asking for input.
                    return "".join(self.agent_buffer), "PAUSED"
                else:
                    break  # It's dead
            except OSError:
                break

        # 3. Process Finished
        # Ensure we get the return code
        try:
            await asyncio.wait_for(self.agent_proc.wait(), 1.0)
        except:
            pass  # Forcefully moving on

        ret_code = self.agent_proc.returncode

        # Cleanup State
        self.agent_proc = None
        # Restore User Shell Focus
        if self.shell_fd:
            self.active_fd = self.shell_fd

        output = "".join(self.agent_buffer)
        return output, f"FINISHED (Exit Code: {ret_code})"

    async def _read_loop(self, fd, is_shell=False):
        """Reads output from the User Shell PTY."""
        loop = asyncio.get_running_loop()
        while True:
            if self._shutdown:
                break

            try:
                if is_shell and self.active_fd != fd:
                    await asyncio.sleep(0.1)
                    continue

                data = await loop.run_in_executor(None, lambda: os.read(fd, 1024))
                if not data:
                    break

                text = data.decode(errors="replace")
                if active_websocket:
                    await active_websocket.send_json(
                        {"type": "terminal_data", "data": text}
                    )
            except OSError:
                break
            except Exception:
                break

    def write_input(self, data: str):
        """Writes user input to the CURRENT active FD."""
        if self.active_fd and not self._shutdown:
            try:
                os.write(self.active_fd, data.encode())
            except OSError:
                pass

    def resize(self, rows: int, cols: int):
        """Resizes the PTY window."""
        if self.active_fd and HAS_PTY and not self._shutdown:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.active_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
                pass


# Global Terminal Instance
terminal = TerminalManager()


# --- MCP CLIENT ---
class MCPClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.exit_stack = AsyncExitStack()
        self.sessions = []
        self.tool_routing = {}
        self.pending_approvals: Dict[str, asyncio.Future] = {}
        self._shutdown = False

        if config.provider == "Google Gemini":
            console.log(f"[bold purple]Initialized Gemini: {config.model}[/]")
            self.llm = GeminiNativeProvider(config)
        else:
            console.log(f"[bold green]Initialized OpenAI/Compatible: {config.model}[/]")
            self.llm = OpenAICompatibleProvider(config)

    async def shutdown(self):
        """Graceful shutdown."""
        self._shutdown = True

        # Cancel any pending approvals
        for req_id, future in self.pending_approvals.items():
            if not future.done():
                future.cancel()
        self.pending_approvals.clear()

        # Close exit stack
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
                with open("servers_config.json", "r") as f:
                    cfg = json.load(f)
                for name, s_cfg in cfg.get("mcpServers", {}).items():
                    await self.broadcast("status", {"text": f"Connecting to {name}..."})
                    await self.connect_to_server(name, s_cfg)
            else:
                console.log(
                    "[yellow]No servers_config.json found. Running in LLM-only mode.[/]"
                )
        except Exception as e:
            console.print(f"[red]Config Error: {e}[/]")

    async def list_tools(self):
        all_tools = []
        self.tool_routing.clear()
        for session in self.sessions:
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

        # --- MODIFIED execute_command TOOL DEFINITION ---
        all_tools.append(
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "Executes a terminal command. IMPORTANT: This tool is INTERACTIVE. If a command (like 'npm init' or 'python script.py') pauses for input (e.g. 'package name:'), I will return the output so far with a [PAUSED] status. You MUST then call `execute_command` again with your input string (e.g., 'my-app' or 'y') to continue execution. To just wait/listen without typing, send an empty string.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The bash command OR the input string for a paused process.",
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
            except Exception:
                pass

    async def request_confirmation(self, command: str) -> bool:
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending_approvals[request_id] = future
        await self.broadcast(
            "request_approval", {"requestId": request_id, "command": command}
        )
        try:
            approved = await future
            return approved
        finally:
            self.pending_approvals.pop(request_id, None)

    async def run_chat_loop_web(self):
        try:
            async with self.exit_stack:
                await self.broadcast("status", {"text": "Initializing Agent..."})
                await self.initialize_all()
                await self.broadcast("status", {"text": "Discovering Tools..."})
                tools = await self.list_tools()
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                MAX_RECENT = 25
                console.rule("[bold green]Agent Active[/]")
                await self.broadcast("status", {"text": "Ready"})

                # Start User Shell if on Posix
                await terminal.start_shell()

                while True:
                    if self._shutdown:
                        break

                    user_input = await input_queue.get()

                    if self._shutdown:
                        break

                    messages.append({"role": "user", "content": user_input})
                    await self.broadcast("status", {"text": "Thinking..."})
                    await self.broadcast("start")

                    while True:
                        if self._shutdown:
                            break

                        response_content, tool_calls_buffer = [], {}
                        msgs_to_send = (
                            [messages[0]] + messages[-MAX_RECENT:]
                            if len(messages) > MAX_RECENT + 1
                            else messages
                        )

                        async for chunk in self.llm.chat_stream(msgs_to_send, tools):
                            if self._shutdown:
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
                                        if idx not in tool_calls_buffer:
                                            tool_calls_buffer[idx] = {
                                                "id": tc.id,
                                                "name": "",
                                                "args": "",
                                            }
                                        if tc.function.name:
                                            tool_calls_buffer[idx]["name"] = (
                                                tc.function.name
                                            )
                                        if tc.function.arguments:
                                            tool_calls_buffer[idx]["args"] += (
                                                tc.function.arguments
                                            )
                                    elif isinstance(tc, dict):
                                        idx = tc.get("id", uuid.uuid4().hex)
                                        tool_calls_buffer[idx] = {
                                            "id": idx,
                                            "name": tc["function"]["name"],
                                            "args": tc["function"]["arguments"],
                                        }

                        full_content = "".join(response_content)
                        history_tool_calls = []
                        for idx in sorted(tool_calls_buffer.keys()):
                            data = tool_calls_buffer[idx]
                            history_tool_calls.append(
                                {
                                    "id": data["id"] or f"call_{uuid.uuid4().hex[:8]}",
                                    "type": "function",
                                    "function": {
                                        "name": data["name"],
                                        "arguments": data["args"],
                                    },
                                }
                            )

                        messages.append(
                            {
                                "role": "assistant",
                                "content": full_content if full_content else None,
                                "tool_calls": history_tool_calls
                                if history_tool_calls
                                else None,
                            }
                        )

                        if not history_tool_calls:
                            await self.broadcast("end")
                            await self.broadcast("status", {"text": "Ready"})
                            break

                        await self.broadcast("tool_start")

                        for tool_call in history_tool_calls:
                            if self._shutdown:
                                break

                            fn_name, fn_args_str = (
                                tool_call["function"]["name"],
                                tool_call["function"]["arguments"],
                            )
                            await self.broadcast(
                                "status", {"text": f"Running: {fn_name}..."}
                            )

                            try:
                                args = json.loads(fn_args_str)
                            except:
                                args = json.loads(repair_json_string(fn_args_str))

                            result = ""

                            # --- SPECIAL HANDLING FOR execute_command ---
                            if fn_name == "execute_command":
                                cmd = args.get("command", "")
                                await self.broadcast(
                                    "status", {"text": "Waiting for Approval..."}
                                )
                                approved = await self.request_confirmation(cmd)

                                if not approved:
                                    result = "User denied execution."
                                    console.print("[red]Blocked by UI.[/]")
                                else:
                                    console.print(
                                        f"[green]Allowed by UI. Executing: {cmd}[/]"
                                    )
                                    await self.broadcast("terminal_open")
                                    await self.broadcast(
                                        "terminal_data",
                                        {"data": f"\r\n\x1b[32m$ {cmd}\x1b[0m\r\n"},
                                    )

                                    try:
                                        # Use the Interactive Runner
                                        # Returns (output, status)
                                        (
                                            output_str,
                                            status_str,
                                        ) = await terminal.run_interactive_command(
                                            cmd, self.broadcast
                                        )

                                        # Format result for Agent
                                        if status_str == "PAUSED":
                                            result = f"COMMAND STARTED BUT PAUSED (WAITING FOR INPUT).\nOutput so far:\n{output_str}\n\n[SYSTEM]: The process is still running. It is likely waiting for user input (e.g. y/n, password). Call 'execute_command' again with the input string to continue."
                                            exit_msg = "\r\n\x1b[33m[Paused for Input - Agent Control]\x1b[0m\r\n"
                                        else:
                                            result = (
                                                output_str
                                                if output_str
                                                else "(Command executed with no output)"
                                            )
                                            exit_msg = (
                                                f"\r\n\x1b[90m[{status_str}]\x1b[0m\r\n"
                                            )

                                        await self.broadcast(
                                            "terminal_data", {"data": exit_msg}
                                        )

                                    except Exception as e:
                                        result = f"Execution failed: {str(e)}"
                                        await self.broadcast(
                                            "terminal_data",
                                            {"data": f"\r\nError: {e}\r\n"},
                                        )

                            # --- REGULAR MCP TOOLS ---
                            else:
                                console.log(f"[dim cyan]Tool Call: {fn_name}[/]")
                                try:
                                    res = await self.call_tool(fn_name, args)
                                    result = "\n".join(
                                        [
                                            b.text
                                            for b in res.content
                                            if isinstance(b, TextContent)
                                        ]
                                    )
                                except Exception as e:
                                    result = f"Error: {e}"
                                    console.print(f"[red]Tool Error: {e}[/]")

                            # Truncate long outputs for Console display (LLM gets full)
                            display_result = (
                                result[:500] + "..." if len(result) > 500 else result
                            )
                            console.print(
                                Panel(
                                    display_result,
                                    title=f"Output: {fn_name}",
                                    border_style="green",
                                )
                            )

                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.get("id"),
                                    "name": fn_name,
                                    "content": result,
                                }
                            )

                        await self.broadcast("tool_end")
                        await self.broadcast("status", {"text": "Analyzing Results..."})

        except asyncio.CancelledError:
            console.log("[bold yellow]Chat loop cancelled. Cleaning up...[/]")
            raise
        except Exception as e:
            console.log(f"[bold red]Chat loop error: {e}[/]")
            raise


# --- SERVER & STARTUP ---
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
    # Cleanup on shutdown
    await graceful_shutdown()


async def graceful_shutdown():
    """Perform graceful shutdown of all resources."""
    global current_client, chat_task, terminal

    console.print("[yellow]Initiating shutdown...[/]")

    # Signal shutdown to all components
    if shutdown_event:
        shutdown_event.set()

    # Stop terminal
    await terminal.stop()

    # Cancel chat task
    if chat_task and not chat_task.done():
        chat_task.cancel()
        try:
            await asyncio.wait_for(chat_task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Shutdown client
    if current_client:
        await current_client.shutdown()

    # Clear queues
    while not input_queue.empty():
        try:
            input_queue.get_nowait()
        except:
            break

    console.print("[green]Shutdown complete.[/]")


app = FastAPI(lifespan=lifespan)

if not os.path.exists("templates"):
    os.makedirs("templates")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def get_home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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
                config_data = data.get("config")
                provider = config_data.get("provider")
                api_key = config_data.get("apiKey")
                model = config_data.get("model")
                base_url = None

                if provider == "Ollama":
                    base_url = "http://localhost:11434/v1"
                elif provider == "LM Studio":
                    base_url = "http://localhost:1234/v1"

                if chat_task and not chat_task.done():
                    chat_task.cancel()
                    try:
                        await asyncio.wait_for(chat_task, timeout=3.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                while not input_queue.empty():
                    input_queue.get_nowait()

                current_client = MCPClient(
                    LLMConfig(provider, base_url, api_key, model)
                )
                chat_task = asyncio.create_task(current_client.run_chat_loop_web())
                await websocket.send_json({"type": "config_success"})

            elif data.get("type") == "message":
                await input_queue.put(data.get("content"))

            # --- TERMINAL INPUT HANDLING ---
            elif data.get("type") == "terminal_input":
                # Route input to current active PTY (Shell or Agent Command)
                terminal.write_input(data.get("data"))

            elif data.get("type") == "terminal_resize":
                # Handle window resize
                cols = data.get("cols")
                rows = data.get("rows")
                if cols and rows:
                    terminal.resize(rows, cols)

            elif data.get("type") == "approval_response":
                req_id = data.get("requestId")
                approved = data.get("approved")
                if current_client and req_id in current_client.pending_approvals:
                    future = current_client.pending_approvals[req_id]
                    if not future.done():
                        future.set_result(approved)

            elif data.get("type") == "update_directory":
                new_path = data.get("path")
                clean_path = new_path.replace("\\", "/")
                console.log(f"[yellow]Updating BASE_DIR to: {clean_path}[/]")
                try:
                    with open("mcp_server.py", "r") as f:
                        content = f.read()
                    new_content = re.sub(
                        r"BASE_DIR = Path\((?:r?[\"\']).*?(?:[\"\'])\)",
                        f'BASE_DIR = Path(r"{clean_path}")',
                        content,
                    )
                    with open("mcp_server.py", "w") as f:
                        f.write(new_content)
                    if current_client:
                        await websocket.send_json(
                            {"type": "status", "text": "Restarting Agent..."}
                        )
                        if chat_task and not chat_task.done():
                            chat_task.cancel()
                            try:
                                await asyncio.wait_for(chat_task, timeout=3.0)
                            except (asyncio.CancelledError, asyncio.TimeoutError):
                                pass
                        while not input_queue.empty():
                            input_queue.get_nowait()
                        console.log("[green]Respawning Client...[/]")
                        current_client = MCPClient(current_client.config)
                        chat_task = asyncio.create_task(
                            current_client.run_chat_loop_web()
                        )
                        await websocket.send_json(
                            {"type": "status", "text": "Directory Updated & Restarted"}
                        )
                except Exception as e:
                    console.print(f"[red]Failed to update directory:[/]{e}")
                    await websocket.send_json({"type": "status", "text": f"Error: {e}"})

    except WebSocketDisconnect:
        active_websocket = None
    except Exception as e:
        console.print(f"[red]WebSocket error: {e}[/]")
        active_websocket = None


def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    console.print(f"\n[yellow]Received signal {signum}, shutting down...[/]")

    # Set the shutdown event if available
    if shutdown_event:
        shutdown_event.set()

    # Stop the uvicorn server if available
    if uvicorn_server:
        uvicorn_server.should_exit = True


async def run_server():
    """Run the server with proper async signal handling."""
    global uvicorn_server

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="critical",
        loop="asyncio",
    )
    uvicorn_server = uvicorn.Server(config)

    # Setup signal handlers for async context
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s, None))

    try:
        await uvicorn_server.serve()
    except asyncio.CancelledError:
        pass
    finally:
        await graceful_shutdown()


if __name__ == "__main__":
    console.rule("[bold cyan]ArcBot[/]")
    console.print("1. Open [bold blue]http://localhost:8000[/] in your browser.")
    console.print("2. Configure your LLM in the Web UI.")
    console.print("3. Press [bold red]Ctrl+C[/] here to quit.")

    try:
        # Use asyncio.run for proper async signal handling
        asyncio.run(run_server())
    except KeyboardInterrupt:
        console.print("\n[yellow]Keyboard interrupt received.[/]")
    except SystemExit:
        pass
    finally:
        console.print("[green]Goodbye![/]")
