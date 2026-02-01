import asyncio
import copy
import json
import os
import re
import signal
import sys
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional

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

# --- UNIVERSAL SYSTEM PROMPT (UPDATED) ---
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
1. **Do Not Pause:** If the user asks for a repetitive task (e.g., "create 40 files", "delete all txt files"), do NOT stop to ask for confirmation after creating a few.
2. **Maximize Throughput:** Generate as many tool calls in succession to complete an operation.
3. **Auto-Continuation:** If you cannot finish the task in one turn (due to output limits), immediately output the next batch of tool calls in your next turn without asking the user "Shall I continue?".
4. **Silence is Golden:** When performing a mass operation, do not output conversational text like "I have created 5 files." until the ENTIRE task is complete. Just output the Tool Calls.

**THE `execute_command` PROTOCOL:**
This is your primary tool for OS interaction.
- **GUI Applications:** You CAN and MUST open GUI apps (Firefox, VS Code, etc.) if requested.
  - *CRITICAL:* Always append `&` to detach GUI processes so the agent doesn't hang (e.g., `firefox google.com &`).
- **Non-Interactive:** Do NOT run interactive TUI programs like `nano`, `vim`, `top` (without batch mode), or Python REPLs. Use `cat`, `grep`, `sed`, or simple scripts instead.
- **Chaining:** You may chain commands with `&&` for efficiency (e.g., `mkdir test && cd test`).

**FORMATTING & FALLBACK:**
You are configured for native function calling. If that fails, strictly use this XML fallback format:

<tool_call>
{"name": "tool_name", "arguments": {"arg_name": "value"}}
</tool_call>
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
        """
        Recursively removes schema keys that the Google GenAI SDK
        does not support or validates strictly (like exclusiveMaximum).
        """
        if isinstance(schema, dict):
            # Create a copy to avoid modifying the original dictionary in place
            clean = schema.copy()

            # Keys known to cause validation errors in Google GenAI SDK
            unsupported_keys = [
                "exclusiveMaximum",
                "exclusiveMinimum",
                "default",
                "title",
                "additionalProperties",  # Sometimes causes issues if True
            ]

            for key in unsupported_keys:
                clean.pop(key, None)

            # Recursively clean nested dictionaries (properties, items, etc.)
            for k, v in clean.items():
                clean[k] = self._sanitize_schema(v)
            return clean

        elif isinstance(schema, list):
            return [self._sanitize_schema(item) for item in schema]

        return schema

    def _convert_tools(self, tools):
        if not tools:
            return None

        # Use a dictionary to deduplicate by name.
        # Since standard Python dicts preserve insertion order (3.7+),
        # and later keys overwrite earlier ones, this ensures that if
        # your local 'execute_command' is added last, it wins.
        unique_tools = {}

        for t in tools:
            name = t["function"]["name"]

            # Deep copy parameters to ensure we don't mutate global state
            raw_params = copy.deepcopy(t["function"].get("parameters", {}))

            # Sanitize the parameters schema (remove exclusiveMaximum, etc.)
            clean_params = self._sanitize_schema(raw_params)

            unique_tools[name] = {
                "name": name,
                "description": t["function"]["description"],
                "parameters": clean_params,
            }

        # Convert the dictionary values back to a list
        return {"function_declarations": list(unique_tools.values())}

    async def chat_stream(self, messages, tools):
        try:
            if self.chat_session is None:
                tool_config = self._convert_tools(tools)

                # Create the chat config
                # We strictly ensure tool_config is passed as a list containing the dict
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
                # Google GenAI requires the function response to match the structure
                part = types.Part.from_function_response(
                    name=last_msg.get("name"), response={"result": last_msg["content"]}
                )
                response_stream = await self.chat_session.send_message_stream(part)

            if response_stream:
                async for chunk in response_stream:
                    tool_calls_out = []
                    # Handle function calls from Gemini
                    if chunk.function_calls:
                        for fc in chunk.function_calls:
                            # Gemini args are already dicts, convert to JSON string for consistency
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


# --- MCP CLIENT ---
class MCPClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.exit_stack = AsyncExitStack()
        self.sessions = []
        self.tool_routing = {}
        # Store pending approvals: request_id -> Future
        self.pending_approvals: Dict[str, asyncio.Future] = {}

        # Initialize Provider
        if config.provider == "Google Gemini":
            console.log(f"[bold purple]Initialized Gemini: {config.model}[/]")
            self.llm = GeminiNativeProvider(config)
        else:
            console.log(f"[bold green]Initialized OpenAI/Compatible: {config.model}[/]")
            self.llm = OpenAICompatibleProvider(config)

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

        # 1. Fetch tools from connected MCP servers
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

        # 2. Inject Client-Side 'execute_command' tool
        # Updated description to be more permissive
        all_tools.append(
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "PRIMARY TOOL. Executes a terminal command. Use this for EVERYTHING: opening apps, reading files, listing directories, system checks. Input is a standard bash command string.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The bash command. Example: 'firefox google.com', 'ls -la', 'cat file.txt'.",
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

    # New helper for waiting on UI confirmation
    async def request_confirmation(self, command: str) -> bool:
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending_approvals[request_id] = future

        # Send request to UI
        await self.broadcast(
            "request_approval", {"requestId": request_id, "command": command}
        )

        try:
            # Wait for the future to be set by the websocket handler
            approved = await future
            return approved
        finally:
            self.pending_approvals.pop(request_id, None)

    async def run_chat_loop_web(self):
        # We start the context manager HERE, inside the task
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

                while True:
                    user_input = await input_queue.get()
                    messages.append({"role": "user", "content": user_input})

                    await self.broadcast("status", {"text": "Thinking..."})
                    await self.broadcast("start")

                    while True:
                        response_content, tool_calls_buffer = [], {}
                        msgs_to_send = (
                            [messages[0]] + messages[-MAX_RECENT:]
                            if len(messages) > MAX_RECENT + 1
                            else messages
                        )

                        async for chunk in self.llm.chat_stream(msgs_to_send, tools):
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
                            fn_name, fn_args_str = (
                                tool_call["function"]["name"],
                                tool_call["function"]["arguments"],
                            )
                            # Broadcast specific tool activity
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

                                # Send request to UI and wait
                                approved = await self.request_confirmation(cmd)

                                if not approved:
                                    result = "User denied execution."
                                    console.print("[red]Blocked by UI.[/]")
                                else:
                                    console.print(
                                        "[green]Allowed by UI. Executing...[/]"
                                    )
                                    try:
                                        # ACTUALLY EXECUTE THE COMMAND
                                        proc = await asyncio.create_subprocess_shell(
                                            cmd,
                                            stdout=asyncio.subprocess.PIPE,
                                            stderr=asyncio.subprocess.PIPE,
                                        )
                                        stdout, stderr = await proc.communicate()

                                        output = stdout.decode().strip()
                                        error = stderr.decode().strip()

                                        result = output
                                        if error:
                                            result += f"\nSTDERR: {error}"
                                        if not result:
                                            result = "Command executed successfully (no output)."

                                    except Exception as e:
                                        result = f"Execution failed: {str(e)}"

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

                            # Truncate long outputs for display
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
            # The context manager (with self.exit_stack) exits here automatically
            raise


# --- SERVER & STARTUP ---
input_queue = asyncio.Queue()
active_websocket: Optional[WebSocket] = None
# Helper to track the background task
chat_task: Optional[asyncio.Task] = None
# Global reference to the client to access pending approvals
current_client: Optional[MCPClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global chat_task
    if chat_task and not chat_task.done():
        chat_task.cancel()
        try:
            await chat_task
        except asyncio.CancelledError:
            pass
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

                # Start new task logic
                if chat_task and not chat_task.done():
                    chat_task.cancel()
                    try:
                        await chat_task
                    except asyncio.CancelledError:
                        pass

                # Clear queue if any pending messages
                while not input_queue.empty():
                    input_queue.get_nowait()

                current_client = MCPClient(
                    LLMConfig(provider, base_url, api_key, model)
                )
                chat_task = asyncio.create_task(current_client.run_chat_loop_web())

                await websocket.send_json({"type": "config_success"})

            elif data.get("type") == "message":
                await input_queue.put(data.get("content"))

            # --- HANDLE APPROVAL RESPONSE ---
            elif data.get("type") == "approval_response":
                req_id = data.get("requestId")
                approved = data.get("approved")
                if current_client and req_id in current_client.pending_approvals:
                    future = current_client.pending_approvals[req_id]
                    if not future.done():
                        future.set_result(approved)

            # --- 4. HANDLE DIRECTORY UPDATE (NEW) ---
            elif data.get("type") == "update_directory":
                new_path = data.get("path")
                # Normalize path to forward slashes for consistency
                clean_path = new_path.replace("\\", "/")

                console.log(f"[yellow]Updating BASE_DIR to: {clean_path}[/]")

                try:
                    # A. Overwrite the file on disk
                    with open("mcp_server.py", "r") as f:
                        content = f.read()

                    # Regex to find: BASE_DIR = Path("...") or BASE_DIR = Path(r"...")
                    # matches single or double quotes, raw string or normal
                    new_content = re.sub(
                        r"BASE_DIR = Path\((?:r?[\"\']).*?(?:[\"\'])\)",
                        f'BASE_DIR = Path(r"{clean_path}")',
                        content,
                    )

                    with open("mcp_server.py", "w") as f:
                        f.write(new_content)

                    # B. AUTO-RESTART THE AGENT
                    if current_client:
                        await websocket.send_json(
                            {"type": "status", "text": "Restarting Agent..."}
                        )

                        # 1. Kill existing task
                        if chat_task and not chat_task.done():
                            chat_task.cancel()
                            try:
                                await chat_task
                            except asyncio.CancelledError:
                                pass

                        # 2. Clear queue
                        while not input_queue.empty():
                            input_queue.get_nowait()

                        # 3. Respawn with SAME config
                        # The new subprocess will read the NEW file from disk
                        console.log("[green]Respawning Client...[/]")
                        # Re-initialize using the existing config object
                        current_client = MCPClient(current_client.config)
                        chat_task = asyncio.create_task(
                            current_client.run_chat_loop_web()
                        )

                        await websocket.send_json(
                            {"type": "status", "text": "Directory Updated & Restarted"}
                        )
                    else:
                        await websocket.send_json(
                            {
                                "type": "status",
                                "text": "Directory Updated (Agent was offline)",
                            }
                        )

                except Exception as e:
                    console.print(f"[red]Failed to update directory:[/]{e}")
                    await websocket.send_json({"type": "status", "text": f"Error: {e}"})

    except WebSocketDisconnect:
        active_websocket = None
        current_client = None


# --- CLEAN SIGNAL HANDLING ---
def handle_sigint(signum, frame):
    # This just ensures we exit the process, triggering the shutdown event
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    console.rule("[bold cyan]MCP Web Agent[/]")
    console.print("1. Open [bold blue]http://localhost:8000[/] in your browser.")
    console.print("2. Configure your LLM in the Web UI.")
    console.print("3. Press [bold red]Ctrl+C[/] here to quit.")

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="critical")
