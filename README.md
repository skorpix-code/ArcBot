<div align="center">

# ArcBot

**A local-first AI agent for your whole computer.**

Bring your own model. Choose exactly which powers it gets.
Watch every command, file change and tool call as it happens.

[Install](#install) · [Quick start](#quick-start) · [Models](#models) · [Features](#features) · [Voice](#voice-mode) · [Security](#security)

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

Setup is a handful of choices, and you can change any of them later.

**1. Pick a model.** Paste an API key, point ArcBot at a local server, or use a
Claude subscription you already have. The wizard shows what each one needs and
tells you when it can see your credentials.

**2. Pick a workspace.** The folder ArcBot works in. File tools cannot reach outside it.

**3. Pick a trust level.** *Guarded* is the default: it reads freely, and asks
before writing files or running commands.

**4. Pick your capabilities.** Only what you enable gets loaded.

**5. Voice mode, or not.** Optional. Say no and nothing is downloaded; say yes
and the models arrive in the background while you work.

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

**Hands-free.** [Voice mode](#voice-mode) runs speech recognition and synthesis
locally in about 52 MB, with barge-in and live captions.

---

## Voice mode

Talk to ArcBot instead of typing. It does everything it does in text — same
tools, same permissions, same trace — you just do not have to touch anything.

Press the microphone beside Send, or `Ctrl/Cmd + Shift + V`. The screen becomes a particle
field that reacts to whoever is talking: cool and pulling one way while you
speak, warm the other way while ArcBot answers. Captions run underneath if you
want to read along.

**It runs on your machine.** The default pair is about **52 MB** and needs no
GPU — on one laptop core it transcribes at roughly 100x real time and starts
speaking in under 200 ms.

| | Model | Size |
|---|---|---|
| Listening | Moonshine Tiny | 30 MB |
| Speaking | Piper | 21 MB |
| Turn detection | Silero VAD | 0.6 MB |

Other choices are one click away: Moonshine Base or Whisper for accuracy,
SenseVoice for Chinese, Japanese, Korean or Cantonese, Kokoro (132 MB) for the
most natural voice, KittenTTS for the smallest. Or switch to OpenAI or Groq if
you would rather not download anything — your audio then leaves your machine.

**Nothing is downloaded unless you ask.** Voice is an optional step in setup —
skip it and no model is fetched. Pick a model and only that one is downloaded;
the rest stay listed with their size until you want them.

**Downloads never make you wait.** They run in the background with live
progress, so setup finishes immediately and you can start working while the
models arrive. Close the tab and come back — the download is still going. Models
land in `models/voice/` beside the app when you run from source, so they are
easy to find and easy to delete; Settings → Voice always shows the exact path.

**Try voices without leaving the conversation.** *Change voice* in voice mode
lists every model with its size, and every speaker as a chip. Tap one to hear
it, tap again to switch. Kokoro's 53 voices and KittenTTS's 8 are all one tap
away, and a preview never triggers a download.

It interrupts properly: start talking while ArcBot is speaking and it stops mid
sentence and listens. Replies are spoken sentence by sentence as they are
written, so you are not waiting for the whole answer before hearing any of it.

```bash
pip install "arcbot[voice]"     # included in arcbot[all]
```

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
~/.local/share/arcbot/       logs
./models/voice/              voice models, beside the app
<workspace>/.arcbot/         memory, plan and chat transcripts
```

Installed from PyPI rather than a checkout? Voice models go to
`~/.local/share/arcbot/voice-models` instead. Set `ARCBOT_MODELS_DIR` to put
them anywhere you like.

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
| Voice mode will not start | `pip install "arcbot[voice]"`. If it says native libraries are missing, the install was interrupted: `pip install --force-reinstall sherpa-onnx` |

---

## Development

```bash
uv sync --extra all --extra dev
uv run pytest                # 309 tests
uv run ruff check arcbot
```

## License

MIT
