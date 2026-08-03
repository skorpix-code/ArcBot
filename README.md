<div align="center">

# ArcBot

**A local-first AI agent for your whole computer.**

Bring your own model. Choose exactly which powers it gets.
Watch every command, file change and tool call as it happens.

[Install](#install) · [Quick start](#quick-start) · [Models](#models) · [Features](#features) · [Security](#security)

</div>

---

ArcBot runs on your machine and does real work on it: edits code, runs commands,
searches the web, manages windows, remembers what you told it last week. You
decide which of those it can do, and you see everything it does in a live trace.

It works with any model — a local one, an API key, or your existing Claude
subscription with no API key at all.

Linux (including Hyprland, Sway and X11), macOS and Windows. Python 3.10+.

---

## Install

```bash
uv tool install "arcbot[all]"     # or: pip install "arcbot[all]"
arcbot
```

That's it. ArcBot starts, opens your browser, and walks you through setup.

<details>
<summary>From source</summary>

```bash
git clone https://github.com/skorpix-code/ArcBot && cd ArcBot
uv sync --extra all               # or: pip install -e ".[all]"
uv run arcbot
```
</details>

---

## Quick start

Setup is four choices, and you can change any of them later.

**1. Pick a model.** Paste an API key, point ArcBot at a local server, or use a
Claude subscription you already have. The wizard shows what each one needs and
tells you when it can see your credentials.

**2. Pick a workspace.** The folder ArcBot works in. File tools cannot reach outside it.

**3. Pick a trust level.** *Guarded* is the default: it reads freely, and asks
before writing files or running commands.

**4. Pick your capabilities.** Only what you enable gets loaded.

Then ask it for something:

> *Run the tests and fix whatever fails.*

Something not working? `arcbot doctor` reports exactly what is installed,
authenticated and missing.

---

## Models

| Provider | What you need |
|---|---|
| **Claude (your subscription)** | A signed-in [Claude Code](https://code.claude.com) — no API key |
| **Claude (API key)** | `ANTHROPIC_API_KEY` |
| **OpenAI** | `OPENAI_API_KEY` |
| **Google Gemini** | `GEMINI_API_KEY` |
| **LM Studio** · **Ollama** | The app running locally. Nothing leaves your machine |
| **OpenRouter** | `OPENROUTER_API_KEY` — one key, hundreds of models |
| **Custom** | Any OpenAI-compatible base URL (vLLM, llama.cpp, LiteLLM, a proxy) |

---

## Features

**81 tools across 10 capabilities** — files and code, terminal, git, web, system,
desktop control, long-term memory. Switch each on or off; a disabled one is never
even imported.

**A live trace, not a chat log.** Every command shows its exact command line,
streaming output, exit code and duration. Every edit shows a diff. Failures open
themselves. A real terminal shares the same shell, so you can take over at any point.

**Trust levels that mean something.** Every command is graded before it runs, and
your level decides where approval kicks in.

| Level | Reads | Writes | Commands | Risky actions |
|---|---|---|---|---|
| Plan only | ✓ | — | — | — |
| **Guarded** *(default)* | ✓ | asks | asks | asks |
| Trusted | ✓ | ✓ | ✓ | asks |
| Full access | ✓ | ✓ | ✓ | ✓ |

**It reaches for the right tool.** Type `cat README.md` and ArcBot redirects the
agent to `read_file` — safer, structured, less approval. Real command work runs
untouched.

**It asks instead of failing.** If a task needs a capability you switched off,
you get a one-click prompt and it carries straight on.

**It doesn't get stuck.** Repeat detection, step and time budgets, per-tool
failure handling, automatic context compaction. When it does hit a wall it is
told what it tried and what failed, so it changes approach instead of looping.

**Connect any MCP server.** Presets for Fetch, Git, SQLite and more in one click,
or paste a command or URL.

**Build your own tools.** Describe one in a sentence and your model writes it.
You see the code, the arguments and what it can reach before anything is saved.

---

## Security

ArcBot runs commands on your machine, so its local server is treated as privileged.

- **Sandboxed files.** Every path is resolved and refused if it leaves your
  workspace. Credential stores (`.ssh`, `.aws`, `.netrc`) are blocked even inside it.
- **Loopback only.** Binds to `127.0.0.1`, requires a token regenerated on every
  run, and rejects web-socket handshakes from a foreign origin.
- **Catastrophic commands never run** — `rm -rf /`, `mkfs`, raw disk writes, fork
  bombs — at any trust level, including Full.
- **Secrets stay secret.** Keys are stored `0600`, never returned by the API, and
  redacted from logs.

Found a security problem? Please open an issue rather than a PR.

---

## Command line

```bash
arcbot                       # start (default)
arcbot --port 9000           # different port
arcbot --workspace ~/proj    # workspace for this run
arcbot doctor                # check this machine's setup
arcbot config                # print current configuration
arcbot ask "run the tests"   # one-shot, no UI
```

---

## Where things live

```
~/.config/arcbot/            settings, credentials (0600), your own tools
<workspace>/.arcbot/         memory, plan and chat transcripts
```

Drop an `AGENTS.md` or `CLAUDE.md` in your workspace and ArcBot reads it as
project conventions.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "No model configured" | Open Settings and pick a provider, or run `arcbot doctor` |
| Local model not responding | Check LM Studio or Ollama is running and the base URL matches |
| Claude Code says it was blocked | Raise the trust level — headless Claude Code cannot show its own prompt |
| A capability says "missing" | It needs an optional package: `pip install "arcbot[all]"` |
| An MCP server will not connect | The reason is shown beside it; usually `uvx` or `npx` is not on your PATH |
| Port already in use | ArcBot picks the next free port; pass `--port` to choose |

---

## Development

```bash
uv sync --extra all --extra dev
uv run pytest                # 232 tests
uv run ruff check arcbot
```

## License

MIT
