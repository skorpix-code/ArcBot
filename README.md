# ArcBot - Local MCP System Agent

ArcBot is a powerful, locally-hosted AI System Agent built on the Model Context Protocol (MCP). It acts as a bridge between Large Language Models (LLMs) and your local operating system, allowing AI to perform real-world tasks like file editing, command execution, and web searching, all while keeping a "Human-in-the-Loop" for security.

It features a modern, reactive web interface (FastAPI + AlpineJS) and supports multiple LLM providers including Ollama, LM Studio, OpenAI, and Google Gemini.

### ✨ Features

    Universal LLM Support: Connect to local models (Ollama, LM Studio) or cloud providers (OpenAI, Gemini).

    File System Control: Create, read, edit, delete, and move files and directories.

    Directory Visualization: View recursive tree structures of your project.

    Web Search: Integrated DuckDuckGo search for retrieving live information.

    System Command Execution: Run shell commands (e.g., git, python, npm) with Strict User Approval.

    Window Management: List, focus, and close windows (Supports Windows, macOS, and Linux/Hyprland/Sway/X11).

    Chain of Thought UI: Visualizes the model's reasoning process (supports <think> tags).

    Secure Sandbox: All file operations are restricted to a specific directory by default.

### 🛠️ Prerequisites

    Python 3.10 or higher.

    uv (Optional, but recommended for speed).

    A browser

    LM Studio or Ollama (if using local models)

## 📦 Installation

Clone the repository:
    Bash

    git clone https://github.com/skorpix-code/ArcBot.git
    cd ArcBot

Install Dependencies:

You can set up the project using either uv (recommended) or standard pip.
#### Option A: Using uv (Fast & Recommended)

If you have uv installed, setup is instant.

Create a virtual environment:
    
Bash

        uv venv

Install requirements:
    
Bash

        uv pip install -r requirements.txt

#### Option B: Using Standard pip

Create a virtual environment:
        
Bash

        python -m venv venv
        # Windows:
        .\venv\Scripts\activate
        # Mac/Linux:
        source venv/bin/activate

Install requirements:
        
Bash

        pip install -r requirements.txt

Dependencies List (requirements.txt)

Save this into a file named requirements.txt in the root folder:
Plaintext

fastapi
uvicorn
python-dotenv
mcp
openai
google-genai
rich
duckduckgo-search
jinja2
websockets

⚙️ Configuration
1. Environment Variables (.env)

While many configurations can be set in the Web UI, the application looks for a .env file for environment context.

    Create a file named .env in the root directory.

    Add the following configurations (optional, depending on your provider):

Ini, TOML

# .env file

# If using Google Gemini
GOOGLE_API_KEY=your_google_api_key_here

# If using OpenAI
OPENAI_API_KEY=your_openai_api_key_here

    Note: You can also enter your API keys directly into the Web UI when you launch the application.

2. Directory Access

By default, mcp_server.py may have a hardcoded path. You should configure the "Sandbox" directory where the agent is allowed to work.

    Open mcp_server.py.

    Look for BASE_DIR.

    You can change it manually now, or use the Web UI later to change it (the agent can rewrite its own config file).

🚀 Usage
1. Start the Server

Run the client script, which spins up the web server and manages the MCP connection.

If using uv:
Bash

uv run mcp_client_webui.py

If using standard python:
Bash

# Ensure your venv is activated first
python mcp_client_webui.py

2. Access the Interface

Open your web browser and navigate to:

http://localhost:8000

3. Connect an LLM

    Provider: Select your provider (e.g., Ollama, Google Gemini).

    API Key: Enter key (if not using local models).

    Model: Enter the model name (e.g., deepseek-r1, gemini-2.0-flash-exp).

    Click Initialize System.

4. Start Chatting

    Ask the agent to "Create a Python script that calculates fibonacci" or "Search the web for the latest news on AI".

    Security Check: If the agent tries to run a terminal command (like pip install), a red modal will appear asking for your permission.

🛡️ Security Architecture

ArcBot is designed with a "Human-in-the-Loop" security model.

    File Sandboxing: The agent operates within a restricted BASE_DIR. It cannot access system files outside this path (path traversal attacks are blocked).

    Command Approval: The execute_command tool is the most powerful tool. Every single attempt to execute a shell command requires manual approval in the Web UI.

    Local Execution: The code runs entirely on your machine.

📁 Project Structure

    mcp_client_webui.py: The FastAPI web server and the MCP Client that talks to the LLM.

    mcp_server.py: The MCP Server that provides the "Tools" (File editing, command execution, etc.).

    templates/index.html: The frontend UI.
