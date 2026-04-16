# ArcBot MCP Client WebUI

ArcBot is a FastAPI + WebSocket web interface for running an MCP-enabled AI agent with live streaming responses, tool execution visibility, command approvals, and interactive terminal output.

It is designed so you can connect multiple MCP servers, choose your preferred LLM provider, and run coding/system tasks from one UI.

## Features

- Multi-provider LLM support: `OpenAI`, `Claude`, `Google Gemini`, `Ollama`, `LM Studio`, and `NVIDIA NIM`
- MCP tool integration via `servers_config.json` (local and remote-compatible server processes)
- Real-time streaming responses with a collapsible reasoning/thought process section
- Built-in command approval flow before terminal execution
- Live terminal panel with streamed command output and interactive input support
- Persistent configuration through `.env` (provider, model, keys, workspace path)

## Tech Stack

- Python `3.10+`
- FastAPI + Uvicorn
- WebSocket UI
- MCP Python SDK

## Project Structure

- `mcp_client_webui.py` - Main web app (FastAPI server + WebSocket runtime)
- `mcp_server.py` - Local MCP server used by default config
- `servers_config.json` - MCP servers loaded at startup
- `templates/index.html` - Frontend UI
- `.env` - Local environment configuration

## Prerequisites

Install what you need based on your setup:

- Python `3.10` or newer
- One of:
  - `uv` (recommended), or
  - standard Python tooling (`venv` + `pip`)
- Optional, depending on your MCP server config:
  - `Node.js` (for `npx`-based MCP servers)
  - `uvx` (for running MCP servers distributed as Python tools)
- API key for any cloud provider you plan to use

## Installation

Choose **one** installation path.

### Option A: Install and run with `uv` (recommended)

1. Clone the repository:

   ```bash
   git clone https://github.com/skorpix-code/ArcBot.git
   cd MCP_CodeAI
   ```

2. Install dependencies:

   ```bash
   uv sync
   ```

3. Create your `.env` file:

   ```bash
   cp .env.example .env
   ```

   If `.env.example` does not exist yet in your local copy, create `.env` manually using the template in the next section.

4. Start the app:

   ```bash
   uv run python mcp_client_webui.py
   ```

### Option B: Install and run with standard Python (`venv` + `pip`)

1. Clone the repository:

   ```bash
   git clone https://github.com/skorpix-code/ArcBot.git
   cd MCP_CodeAI
   ```

2. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:

   ```bash
   pip install --upgrade pip
   pip install -e .
   ```

4. Create your `.env` file:

   ```bash
   cp .env.example .env
   ```

   If `.env.example` does not exist yet in your local copy, create `.env` manually using the template in the next section.

5. Start the app:

   ```bash
   python mcp_client_webui.py
   ```

## Environment Configuration (`.env`)

Create a `.env` file in the project root. You can copy and paste this template:

```env
# --- Core app settings ---
ARCBOT_PROVIDER=OpenAI
ARCBOT_MODEL=gpt-4o-mini
ARCBOT_BASE_DIR=~/ArcBot_Workspace

# --- Provider API keys (fill only what you use) ---
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
NVIDIA_NIM_API_KEY=

# --- Optional provider endpoints ---
# OpenAI-compatible local servers
LM_STUDIO_URL=http://localhost:1234/v1
OLLAMA_URL=http://localhost:11434/v1

# --- Optional MCP transport variables (if used by your local server setup) ---
MCP_TRANSPORT=stdio
MCP_SERVER_COMMAND=python
MCP_SERVER_SCRIPT=./mcp_server.py
MCP_SSE_URL=http://localhost:8000/sse
```

Notes:

- Keep secrets out of version control.
- Use only the API keys for providers you actually use.
- If you use local `Ollama` or `LM Studio`, make sure the local model server is running.

## Configure MCP Servers

MCP servers are loaded from `servers_config.json`.

Default configuration includes:

- a local Python server (`mcp_server.py`)
- optional utility servers started with `uvx` and `npx`

If you do not have `uvx` or `npx`, remove or comment those entries from `servers_config.json`, or install the required tooling.

## Run the Application

After setup, open:

- [http://localhost:8000](http://localhost:8000)

On first use in the UI:

1. Select provider
2. Set model name
3. Add API key (or keep blank for local providers that do not require one)
4. Confirm workspace path

The app will initialize MCP servers, discover tools, and become ready for chat.

## Provider Notes

- `OpenAI`: uses `OPENAI_API_KEY`
- `Claude`: uses `ANTHROPIC_API_KEY`
- `Google Gemini`: uses `GEMINI_API_KEY`
- `NVIDIA NIM`: uses `NVIDIA_NIM_API_KEY` and defaults to `https://integrate.api.nvidia.com/v1`
- `Ollama` / `LM Studio`: OpenAI-compatible local endpoints (normally no cloud API key required)

## Troubleshooting

- **`ModuleNotFoundError`**: ensure dependencies are installed in the active environment.
- **Provider auth errors**: check the correct API key variable is set in `.env`.
- **No tools discovered**: verify `servers_config.json` commands exist on your machine.
- **Port already in use**: free port `8000` or update app launch configuration.
- **Local model not responding**: verify Ollama/LM Studio server is running and URL is correct.

