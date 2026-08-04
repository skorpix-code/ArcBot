/* ArcBot UI.
 *
 * The whole interface is a projection of one event stream, so there is no
 * client-side model of "what the agent is doing" to drift out of sync — the
 * server says what happened, and the trace renders it.
 */
"use strict";

const TOKEN = window.ARCBOT_TOKEN || "";
const $ = (id) => document.getElementById(id);
/** The product mark: an arc rising from a single point. */
const ARC_MARK =
  '<svg viewBox="0 0 24 24" fill="none"><path d="M3 18a9 9 0 0 1 18 0" stroke="currentColor" ' +
  'stroke-width="2.4" stroke-linecap="round"/><circle cx="12" cy="18" r="1.9" fill="currentColor"/></svg>';

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

/* ══════════════════════════════ api ══════════════════════════════ */

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "content-type": "application/json",
      "x-arcbot-token": TOKEN,
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch { /* body was not JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

/* ══════════════════════════════ state ══════════════════════════════ */

const state = {
  connected: false,
  working: false,
  settings: null,
  providers: [],
  toolsets: [],
  enabled: [],
  modes: [],
  sessions: [],
  currentSession: "",
  providerMode: "model",
  pendingAsks: new Set(),
  closing: false,
  streaming: null,       // { id, node, prose, thinkingBody, text, thinking }
  tools: new Map(),      // callId -> { node, out, spec }
};

/* ══════════════════════════════ toasts ══════════════════════════════ */

function toast(text, level = "info", ttl = 5200) {
  const node = el("div", "toast");
  node.dataset.level = level;
  node.append(el("span", null, text));
  $("toasts").append(node);
  setTimeout(() => {
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 250);
  }, ttl);
}

/* ══════════════════════════════ markdown ══════════════════════════════ */

if (window.marked) {
  marked.setOptions({ gfm: true, breaks: true, headerIds: false, mangle: false });
}

function renderMarkdown(target, text) {
  target.innerHTML = window.marked ? marked.parse(text || "") : "";
  target.querySelectorAll("pre code").forEach((block) => {
    if (window.hljs) {
      try { hljs.highlightElement(block); } catch { /* unknown language */ }
    }
  });
  target.querySelectorAll("a").forEach((link) => {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  });
}

/* ══════════════════════════════ the trace ══════════════════════════════ */

const trace = {
  get inner() { return $("traceInner"); },

  atBottom() {
    const box = $("trace");
    return box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  },

  scroll(force = false) {
    if (!force && !this.atBottom()) return;
    requestAnimationFrame(() => {
      const box = $("trace");
      box.scrollTop = box.scrollHeight;
    });
  },

  clearEmpty() {
    const empty = $("emptyState");
    if (empty) empty.remove();
  },

  add(kind, node) {
    this.clearEmpty();
    const stick = this.atBottom();
    const entry = el("div", "entry");
    entry.dataset.kind = kind;
    entry.append(node);
    this.inner.append(entry);
    this.scroll(stick);
    return entry;
  },

  reset() {
    this.inner.innerHTML = "";
    state.tools.clear();
    state.streaming = null;
    renderEmptyState();
  },
};

function renderEmptyState() {
  const wrap = el("div", "empty");
  wrap.id = "emptyState";
  const mark = el("div", "empty__mark");
  mark.setAttribute("aria-hidden", "true");
  mark.innerHTML = ARC_MARK;
  wrap.append(mark);
  wrap.append(el("h1", "empty__title", "What should I do?"));

  const sub = el("p", "empty__sub");
  sub.append(document.createTextNode("I work on your machine, in "));
  sub.append(el("code", null, state.settings?.workspaceResolved || "your workspace"));
  sub.append(document.createTextNode(". Every command and file change shows up here as it happens."));
  wrap.append(sub);

  const list = el("ul", "starters");
  starterPrompts().forEach((prompt) => {
    const item = el("li");
    const button = el("button", "starter", prompt);
    button.type = "button";
    button.addEventListener("click", () => {
      $("input").value = prompt;
      $("input").focus();
      autosize();
    });
    item.append(button);
    list.append(item);
  });
  wrap.append(list);
  trace.inner.append(wrap);
}

function starterPrompts() {
  const enabled = new Set(state.enabled);
  const options = [];
  if (enabled.has("files")) options.push("Give me a tour of this project — what is it and how is it laid out?");
  if (enabled.has("shell")) options.push("Run the test suite and fix anything that fails.");
  if (enabled.has("desktop")) options.push("Tile my open windows side by side.");
  if (enabled.has("system")) options.push("What's using the most memory right now?");
  if (enabled.has("web")) options.push("Find the current docs for this project's main dependency.");
  options.push("What can you do on this machine?");
  return options.slice(0, 4);
}

/* ── assistant message ─────────────────────────────────────────────── */

function beginMessage(id) {
  const wrap = el("div");

  const thinking = el("div", "thinking");
  thinking.dataset.open = "false";
  thinking.hidden = true;
  const toggle = el("button", "thinking__toggle", "Thinking");
  toggle.type = "button";
  const thinkingBody = el("div", "thinking__body");
  toggle.addEventListener("click", () => {
    thinking.dataset.open = thinking.dataset.open === "true" ? "false" : "true";
  });
  thinking.append(toggle, thinkingBody);

  const prose = el("div", "prose");
  wrap.append(thinking, prose);
  trace.add("assistant", wrap);

  state.streaming = { id, prose, thinking, thinkingBody, toggle, text: "", thinkingText: "" };
}

function appendText(id, text) {
  if (!state.streaming || state.streaming.id !== id) beginMessage(id);
  const stream = state.streaming;
  stream.text += text;
  const stick = trace.atBottom();
  renderMarkdown(stream.prose, stream.text);
  trace.scroll(stick);
}

function appendThinking(id, text) {
  if (!state.streaming || state.streaming.id !== id) beginMessage(id);
  const stream = state.streaming;
  stream.thinkingText += text;
  stream.thinking.hidden = false;
  stream.thinkingBody.textContent = stream.thinkingText;
  stream.toggle.textContent = `Thinking · ${stream.thinkingText.length.toLocaleString()} chars`;
  if (state.settings?.ui?.show_thinking && stream.thinking.dataset.open !== "false-by-user") {
    stream.thinkingBody.scrollTop = stream.thinkingBody.scrollHeight;
  }
}

function endMessage(payload) {
  const stream = state.streaming;
  if (!stream || stream.id !== payload.id) return;
  if (payload.text) {
    stream.text = payload.text;
    renderMarkdown(stream.prose, stream.text);
  }
  if (!stream.text.trim() && !stream.thinkingText.trim()) {
    stream.prose.closest(".entry")?.remove();
  } else if (!stream.text.trim()) {
    // The model folded its answer into the reasoning channel — show it.
    stream.thinking.dataset.open = "true";
  }
  state.streaming = null;
}

/* ── tool cards ────────────────────────────────────────────────────── */

const CAPABILITY_KIND = { read: "read", network: "read", write: "write", exec: "exec", system: "exec", destructive: "exec" };

function toolStart(payload) {
  const card = el("div", "tool");
  card.dataset.state = "running";
  card.dataset.open = "false";

  const head = el("button", "tool__head");
  head.type = "button";
  const glyph = el("span", "tool__glyph", "◜");
  const title = el("span", "tool__title", payload.title || payload.name);
  const tag = el("span", "tool__tag", payload.toolset || "tool");
  const time = el("span", "tool__time", "");
  head.append(glyph, title, tag, time);

  const body = el("div", "tool__body");
  const args = el("div", "tool__args");
  const argText = formatArgs(payload.args);
  if (argText) args.textContent = argText; else args.hidden = true;
  const out = el("pre", "tool__out", "…");
  body.append(args, out);

  head.addEventListener("click", () => {
    card.dataset.open = card.dataset.open === "true" ? "false" : "true";
  });

  card.append(head, body);
  trace.add(CAPABILITY_KIND[payload.capability] || "read", card);
  state.tools.set(payload.callId, { card, out, glyph, time, chunks: [] });
}

function toolProgress(payload) {
  const entry = state.tools.get(payload.callId);
  if (!entry) return;
  entry.chunks.push(payload.chunk);
  const text = stripAnsi(entry.chunks.join(""));
  const stick = trace.atBottom();
  entry.out.textContent = text.slice(-8000);
  if (entry.card.dataset.open === "false" && text.trim()) entry.card.dataset.open = "true";
  entry.out.scrollTop = entry.out.scrollHeight;
  trace.scroll(stick);
}

function toolEnd(payload) {
  const entry = state.tools.get(payload.callId);
  if (!entry) return;
  entry.card.dataset.state = payload.ok ? "ok" : "error";
  entry.glyph.textContent = payload.denied ? "⃠" : payload.ok ? "✓" : "✕";
  if (payload.elapsedMs != null) entry.time.textContent = formatDuration(payload.elapsedMs);

  const text = payload.preview || (payload.ok ? "(no output)" : "failed");
  entry.out.textContent = "";
  entry.out.append(...colourise(text));
  if (payload.truncated) {
    const note = el("div", "tool__more", "output truncated");
    entry.card.append(note);
  }
  // Failures and denials matter enough to open themselves.
  if (!payload.ok) entry.card.dataset.open = "true";
  state.tools.delete(payload.callId);
}

function formatArgs(args) {
  if (!args || typeof args !== "object") return "";
  const keys = Object.keys(args);
  if (!keys.length) return "";
  return keys
    .map((key) => {
      const value = args[key];
      const text = typeof value === "string" ? value : JSON.stringify(value);
      return `${key}: ${text}`;
    })
    .join("\n");
}

const ANSI = /\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(\x07|\x1b\\)|\r(?!\n)/g;
const stripAnsi = (text) => (text || "").replace(ANSI, "");

/** Colour diff output so a file change reads at a glance. */
function colourise(text) {
  const lines = stripAnsi(text).split("\n");
  const looksLikeDiff = lines.some((line) => line.startsWith("@@") || line.startsWith("+++"));
  if (!looksLikeDiff) return [document.createTextNode(text)];
  return lines.map((line) => {
    let cls = null;
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
    else if (line.startsWith("@@")) cls = "hunk";
    const node = cls ? el("span", cls, line + "\n") : document.createTextNode(line + "\n");
    return node;
  });
}

function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

/* ── approvals & questions ─────────────────────────────────────────── */

const RISK_LABEL = ["Safe", "Moderate", "High", "Blocked"];

function renderAsk(payload) {
  state.pendingAsks.add(payload.askId);
  updatePendingAlert();

  const card = el("div", "ask");
  const risk = payload.risk?.level ?? 1;
  card.dataset.risk = String(risk);
  card.id = `ask-${payload.askId}`;

  const head = el("div", "ask__head");
  head.append(el("span", "ask__label", askHeadline(payload)));
  const gauge = el("div", "ask__gauge");
  for (let i = 0; i < 4; i++) {
    const bar = el("i");
    if (i <= risk) bar.className = "on";
    gauge.append(bar);
  }
  if (payload.risk) head.append(gauge);
  card.append(head);

  const body = el("div", "ask__body");
  const actions = el("div", "ask__actions");

  const answer = (decision, value) => {
    send({ type: "answer", askId: payload.askId, decision, value });
    state.pendingAsks.delete(payload.askId);
    updatePendingAlert();
    card.dataset.resolved = "true";
    actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
  };

  if (payload.kind === "command") {
    body.append(el("div", "ask__subject", payload.command));
    if (payload.risk?.reasons?.length) {
      const why = el("ul", "ask__why");
      payload.risk.reasons.forEach((reason) => why.append(el("li", null, reason)));
      body.append(why);
    }
    if (payload.context) body.append(el("p", "ask__note", payload.context));
    actions.append(button("Run once", "btn--primary", () => answer("allow")));
    if (payload.offerRule && payload.suggestedRule) {
      actions.append(
        button(`Always allow \`${payload.suggestedRule}\``, "", () => answer("always", payload.suggestedRule)),
      );
    }
    actions.append(button("Deny", "btn--danger", () => answer("deny")));
    if (!payload.offerRule) {
      body.append(el("p", "ask__note",
        "Too risky to save as a standing rule — approve it each time."));
    }
  } else if (payload.kind === "tool") {
    body.append(el("div", "ask__subject", `${payload.title}\n${formatArgs(payload.args)}`.trim()));
    if (payload.detail) body.append(el("p", "ask__note", payload.detail));
    actions.append(
      button("Allow", "btn--primary", () => answer("allow")),
      button("Always allow this tool", "", () => answer("always")),
      button("Deny", "btn--danger", () => answer("deny")),
    );
  } else if (payload.kind === "toolset") {
    body.append(el("div", "ask__subject", payload.reason || payload.summary));
    const why = el("ul", "ask__why");
    why.append(el("li", null, payload.summary));
    if (payload.caution) why.append(el("li", null, payload.caution));
    body.append(why);
    actions.append(
      button(`Enable ${payload.name}`, "btn--primary", () => answer("allow")),
      button("Not now", "btn--danger", () => answer("deny")),
    );
  } else if (payload.kind === "path") {
    body.append(el("div", "ask__subject", payload.path));
    if (payload.reason) body.append(el("p", "ask__note", payload.reason));
    actions.append(
      button("Grant access", "btn--primary", () => answer("allow")),
      button("Deny", "btn--danger", () => answer("deny")),
    );
  } else if (payload.kind === "choice") {
    body.append(el("p", "ask__subject", payload.question));
    (payload.options || []).forEach((option) => {
      actions.append(button(option, "", () => answer("answer", option)));
    });
    actions.append(button("Skip", "btn--ghost", () => answer("deny")));
  } else {
    body.append(el("p", "ask__subject", payload.question || "ArcBot needs an answer."));
    const input = el("input", "ask__input");
    input.type = "text";
    input.placeholder = "Type your answer…";
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); answer("answer", input.value); }
    });
    body.append(input);
    actions.append(
      button("Send", "btn--primary", () => answer("answer", input.value)),
      button("Skip", "btn--ghost", () => answer("deny")),
    );
    setTimeout(() => input.focus(), 60);
  }

  body.append(actions);
  card.append(body);
  const entry = trace.add("ask", card);
  entry.scrollIntoView({ block: "center", behavior: "smooth" });
}

function askHeadline(payload) {
  return {
    command: `Approve command · ${RISK_LABEL[payload.risk?.level ?? 1]} risk`,
    tool: "Approve action",
    toolset: `Enable ${payload.name || "capability"}`,
    path: "Grant folder access",
    choice: "ArcBot has a question",
    input: "ArcBot has a question",
  }[payload.kind] || "Approval needed";
}

function button(label, cls, onClick) {
  const node = el("button", `btn btn--sm ${cls}`.trim(), label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function updatePendingAlert() {
  const count = state.pendingAsks.size;
  $("pendingAlert").hidden = count === 0;
  $("pendingAlertText").textContent = count === 1 ? "1 approval waiting" : `${count} approvals waiting`;
  if (count) setStatus("waiting", "Waiting for you");
}

$("pendingJump").addEventListener("click", () => {
  const first = state.pendingAsks.values().next().value;
  const node = first && $(`ask-${first}`);
  if (node) node.scrollIntoView({ block: "center", behavior: "smooth" });
});

/* ══════════════════════════════ terminal ══════════════════════════════ */

let term = null;
let fit = null;

function ensureTerminal() {
  if (term || !window.Terminal) return;
  const style = getComputedStyle(document.documentElement);
  term = new Terminal({
    fontFamily: style.getPropertyValue("--mono").trim() || "monospace",
    fontSize: 12,
    lineHeight: 1.35,
    cursorBlink: true,
    convertEol: true,
    scrollback: 6000,
    theme: {
      background: style.getPropertyValue("--code-bg").trim() || "#0f0e14",
      foreground: style.getPropertyValue("--text").trim() || "#ece9f3",
      cursor: style.getPropertyValue("--arc").trim() || "#6fd0ff",
      selectionBackground: "rgba(111,208,255,.22)",
    },
  });
  if (window.FitAddon) {
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
  }
  term.open($("term"));
  term.onData((data) => send({ type: "terminal.input", data }));
  resizeTerminal();
  window.addEventListener("resize", resizeTerminal);
}

function resizeTerminal() {
  if (!fit || $("console").dataset.open !== "true") return;
  try {
    fit.fit();
    send({ type: "terminal.resize", rows: term.rows, cols: term.cols });
  } catch { /* not visible yet */ }
}

$("consoleToggle").addEventListener("click", () => {
  const console_ = $("console");
  const open = console_.dataset.open !== "true";
  console_.dataset.open = String(open);
  $("consoleToggle").setAttribute("aria-expanded", String(open));
  if (open) { ensureTerminal(); setTimeout(resizeTerminal, 40); }
});

function writeTerminal(data) {
  ensureTerminal();
  if (term) term.write(data);
  if ($("console").dataset.open !== "true") {
    $("consoleMeta").textContent = "new output";
  }
}

/* ══════════════════════════════ web socket ══════════════════════════════ */

let socket = null;
let retry = 0;

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);

  socket.addEventListener("open", () => {
    state.connected = true;
    retry = 0;
    setStatus("ready", "Ready");
  });

  socket.addEventListener("message", (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    handleEvent(payload);
  });

  socket.addEventListener("close", (event) => {
    state.connected = false;
    if (state.closing) return;   // a deliberate shutdown, not a dropped link
    setStatus("error", event.code === 4401 ? "Unauthorised" : "Disconnected");
    if (event.code === 4401) {
      toast("This tab's token is no longer valid. Reload the page.", "error", 20000);
      return;
    }
    retry = Math.min(retry + 1, 6);
    setTimeout(connect, 400 * 2 ** retry);
  });
}

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

/* ══════════════════════════════ event handling ══════════════════════════════ */

function handleEvent(event) {
  switch (event.type) {
    case "ready": onReady(event); break;
    case "status": onStatus(event); break;
    case "turn.start": setWorking(true); break;
    case "turn.end": onTurnEnd(event); break;
    case "step": $("composerHint").textContent = `step ${event.step}/${event.maxSteps}`; break;

    case "message.start": beginMessage(event.id); break;
    case "text.delta": appendText(event.id, event.text); break;
    case "thinking.delta": appendThinking(event.id, event.text); break;
    case "message.end": endMessage(event); break;

    case "tool.start": toolStart(event); break;
    case "tool.progress": toolProgress(event); break;
    case "tool.end": toolEnd(event); break;

    case "terminal.data": writeTerminal(event.data); break;

    case "ask": renderAsk(event); break;
    case "ask.resolved": state.pendingAsks.delete(event.askId); updatePendingAlert(); break;

    case "todos": renderPlan(event.items); break;
    case "toolsets": onToolsets(event); break;
    case "usage": renderUsage(event); break;
    case "config": state.settings = event.settings; applySettings(); break;

    case "notice": toast(event.text, event.level || "info"); break;
    case "voice.ready":
    case "voice.state":
    case "voice.transcript":
    case "voice.speak":
    case "voice.stop":
    case "voice.barge":
    case "voice.download":
      handleVoiceEvent(event); break;
    case "error": onError(event); break;
    case "shutdown": onShutdown(event); break;
    case "open.settings": openSettings(event.panel || "tools"); break;
  }
}

function onReady(event) {
  state.currentSession = event.sessionId || "";
  state.providerMode = event.mode || "model";
  if (event.workspace) $("workspaceLabel").textContent = shortenPath(event.workspace);
  $("modelLabel").textContent = event.model || event.provider || "not set";
  if (Array.isArray(event.messages)) {
    trace.reset();
    event.messages.forEach((message) => {
      if (message.role === "user") {
        trace.add("user", el("div", "user-turn", message.text));
      } else {
        const prose = el("div", "prose");
        renderMarkdown(prose, message.text);
        trace.add("assistant", prose);
      }
    });
    if (event.messages.length) trace.clearEmpty();
    trace.scroll(true);
  }
  refreshSessions();
}

function onStatus(event) {
  const map = {
    starting: ["working", "Starting…"],
    thinking: ["working", "Thinking…"],
    compacting: ["working", "Summarising context…"],
    ready: ["ready", "Ready"],
    unconfigured: ["error", "No model"],
  };
  const [tone, label] = map[event.state] || ["idle", event.state];
  setStatus(tone, event.detail || label);
  setWorking(tone === "working");
}

function onTurnEnd(event) {
  setWorking(false);
  $("composerHint").textContent = "Enter to send · Shift+Enter for a new line";
  if (event.reason && !["completed", "stopped"].includes(event.reason)) {
    toast(`Turn ended: ${event.reason}`, "warn");
  }
  refreshSessions();
}

function onError(event) {
  const card = el("div", "ask");
  card.dataset.risk = "3";
  const head = el("div", "ask__head");
  head.append(el("span", "ask__label", "Problem"));
  card.append(head);
  const body = el("div", "ask__body");
  body.append(el("div", "ask__subject", event.message));
  if (event.detail) body.append(el("p", "ask__note", event.detail));
  if (event.recoverable) {
    const actions = el("div", "ask__actions");
    actions.append(button("Open settings", "btn--primary", openSettings));
    body.append(actions);
  }
  card.append(body);
  trace.add("error", card);
  setStatus("error", "Error");
  setWorking(false);
}

/* ArcBot is closing. Say so plainly, and stop trying to reconnect to a process
   that is deliberately gone. */
function onShutdown(event) {
  state.closing = true;
  setStatus("error", "Closed");
  setWorking(false);
  const card = el("div", "ask");
  card.dataset.risk = "0";
  const head = el("div", "ask__head");
  head.append(el("span", "ask__label", "ArcBot closed"));
  card.append(head);
  const body = el("div", "ask__body");
  body.append(el("div", "ask__subject", event.reason || "Closed at your request."));
  body.append(el("p", "ask__note",
    "This tab is no longer connected to anything. Run arcbot again to start a new session."));
  card.append(body);
  trace.add("error", card);
  $("input").disabled = true;
  $("sendBtn").disabled = true;
}

function setStatus(tone, text) {
  $("statusPill").dataset.state = tone;
  $("statusText").textContent = text;
}

function setWorking(working) {
  state.working = working;
  $("app").dataset.working = String(working);
  $("sendBtn").hidden = working;
  $("stopBtn").hidden = !working;
}

/* ── side panel ────────────────────────────────────────────────────── */

function renderPlan(items) {
  const panel = $("planPanel");
  const list = $("planList");
  list.innerHTML = "";
  if (!items || !items.length) { panel.hidden = true; return; }
  panel.hidden = false;
  items.forEach((item) => {
    const node = el("li");
    node.dataset.status = item.status;
    node.append(el("span", null, item.title));
    if (item.note) node.title = item.note;
    list.append(node);
  });
}

function onToolsets(event) {
  state.toolsets = event.available || [];
  state.enabled = event.enabled || [];
  const list = $("toolsetList");
  list.innerHTML = "";

  state.toolsets.forEach((entry) => {
    const item = el("li");
    const button_ = el("button", "toolset");
    button_.type = "button";
    button_.setAttribute("aria-pressed", String(state.enabled.includes(entry.id)));
    button_.disabled = entry.alwaysOn || !entry.available;
    button_.title = entry.available
      ? `${entry.summary}${entry.caution ? "\n\n" + entry.caution : ""}`
      : `Needs: ${entry.missing.join(", ")}`;

    button_.append(el("span", "toolset__switch"));
    button_.append(el("span", "toolset__name", entry.name));
    if (entry.alwaysOn) button_.append(el("span", "toolset__lock", "always"));
    else if (!entry.available) button_.append(el("span", "toolset__lock", "missing"));

    button_.addEventListener("click", () => {
      const next = button_.getAttribute("aria-pressed") !== "true";
      button_.setAttribute("aria-pressed", String(next));
      send({ type: "toolset.toggle", id: entry.id, enabled: next });
    });
    item.append(button_);
    list.append(item);
  });

  $("toolCount").textContent = `${(event.tools || []).length} tools`;
  // A link, not a switch — it opens a panel rather than toggling anything.
  const extras = el("li");
  const build = el("button", "toolset toolset--link");
  build.type = "button";
  build.title = "Build your own tools, or connect an MCP server";
  build.append(el("span", "toolset__plus", "+"));
  build.append(el("span", "toolset__name", "Build or connect a tool"));
  build.addEventListener("click", () => openSettings("tools"));
  extras.append(build);
  list.append(extras);
  if ($("emptyState")) { trace.inner.innerHTML = ""; renderEmptyState(); }
}

function renderUsage(event) {
  const pct = event.contextPct ?? 0;
  $("contextFill").style.width = `${Math.min(100, pct)}%`;
  $("contextFill").dataset.level = pct > 72 ? "warn" : "ok";
  $("statContext").textContent = `${pct}%`;
  $("statIn").textContent = compact(event.inputTokens || 0);
  $("statOut").textContent = compact(event.outputTokens || 0);
  $("statCost").textContent = event.costUsd ? `$${event.costUsd.toFixed(3)}` : "—";
}

const compact = (n) => (n < 1000 ? String(n) : n < 1e6 ? `${(n / 1000).toFixed(1)}k` : `${(n / 1e6).toFixed(1)}M`);
const shortenPath = (p) => (p || "").replace(/^\/home\/[^/]+/, "~").replace(/^\/Users\/[^/]+/, "~");

async function refreshSessions() {
  try {
    const data = await api("/api/sessions");
    state.sessions = data.sessions || [];
  } catch { return; }
  const list = $("sessionList");
  list.innerHTML = "";
  state.sessions.forEach((session) => {
    const item = el("li");
    const node = el("button", "session", session.title);
    node.type = "button";
    if (session.id === state.currentSession) node.setAttribute("aria-current", "true");
    node.addEventListener("click", async () => {
      await post(`/api/sessions/${encodeURIComponent(session.id)}/open`);
      trace.reset();
    });
    item.append(node);
    list.append(item);
  });
}

/* ══════════════════════════════ composer ══════════════════════════════ */

const input = $("input");

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * 0.4)}px`;
}

function submit() {
  const text = input.value.trim();
  if (!text || state.working) return;
  trace.add("user", el("div", "user-turn", text));
  send({ type: "chat", text });
  input.value = "";
  autosize();
  trace.scroll(true);
}

input.addEventListener("input", autosize);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit();
  }
});
$("sendBtn").addEventListener("click", submit);
$("stopBtn").addEventListener("click", () => send({ type: "stop" }));
$("newChatBtn").addEventListener("click", () => { send({ type: "clear" }); trace.reset(); });

document.addEventListener("keydown", (event) => {
  const meta = event.metaKey || event.ctrlKey;
  if (meta && event.key === "k") { event.preventDefault(); input.focus(); }
  if (meta && event.key === "j") { event.preventDefault(); $("consoleToggle").click(); }
  if (event.key === "Escape" && state.working) send({ type: "stop" });
});

/* ══════════════════════════════ theme ══════════════════════════════ */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  if (term) {
    const style = getComputedStyle(document.documentElement);
    term.options.theme = {
      background: style.getPropertyValue("--code-bg").trim(),
      foreground: style.getPropertyValue("--text").trim(),
      cursor: style.getPropertyValue("--arc").trim(),
    };
  }
}

$("themeBtn").addEventListener("click", async () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  if (state.settings) {
    state.settings.ui.theme = next;
    try { await post("/api/settings", { ui: { theme: next } }); } catch { /* offline */ }
  }
});

/* ══════════════════════════════ settings ══════════════════════════════ */

function applySettings() {
  const settings = state.settings;
  if (!settings) return;
  applyTheme(settings.ui?.theme || "dark");
  $("workspaceLabel").textContent = shortenPath(settings.workspaceResolved || settings.workspace);
  $("modelLabel").textContent = settings.model?.model || settings.model?.provider || "not set";
  const mode = settings.permissions?.mode || "guarded";
  $("trustLabel").textContent = mode;
  $("trustChip").dataset.mode = mode;
}

function openSettings(tab) {
  if (tab) activeTab = tab;
  buildSettings();
  $("settings").hidden = false;
}

$("settingsBtn").addEventListener("click", openSettings);
$("settingsClose").addEventListener("click", () => ($("settings").hidden = true));
$("settings").addEventListener("click", (event) => {
  if (event.target === $("settings")) $("settings").hidden = true;
});
$("trustChip").addEventListener("click", openSettings);
$("workspaceChip").addEventListener("click", openSettings);
$("modelChip").addEventListener("click", openSettings);

/* The settings sheet is tabbed: each panel is one job, so nothing is a wall
   of controls. `settings` is edited in place and written on Save. */

const SETTINGS_TABS = [
  { id: "model", label: "Model", render: renderModelTab },
  { id: "trust", label: "Trust", render: renderTrustTab },
  { id: "tools", label: "Tools", render: renderToolsTab },
  { id: "voice", label: "Voice", render: renderVoiceTab },
  { id: "mcp", label: "Connections", render: renderConnectionsTab },
];
let activeTab = "model";

function buildSettings() {
  const tabs = $("settingsTabs");
  tabs.innerHTML = "";
  SETTINGS_TABS.forEach((tab) => {
    const button = el("button", "tab", tab.label);
    button.type = "button";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(tab.id === activeTab));
    button.addEventListener("click", () => { activeTab = tab.id; buildSettings(); });
    tabs.append(button);
  });

  const body = $("settingsBody");
  body.innerHTML = "";
  const showSave = activeTab === "model" || activeTab === "trust";
  state.voice = state.voice || null;
  $("settingsSave").hidden = !showSave;
  $("settingsNote").textContent = "";
  (SETTINGS_TABS.find((t) => t.id === activeTab) || SETTINGS_TABS[0]).render(body);
}

/* ── model ─────────────────────────────────────────────────────────── */

function renderModelTab(body) {
  const settings = state.settings;
  const model = group("Model");
  model.append(field("Provider", selectInput(
    state.providers.map((p) => [p.id, p.name]),
    settings.model.provider,
    (value) => { settings.model.provider = value; settings.model.model = ""; buildSettings(); },
  )));
  model.append(field("Model", textInput(settings.model.model, (v) => (settings.model.model = v))));

  const spec = state.providers.find((p) => p.id === settings.model.provider);
  if (spec?.keyEnv) model.append(field(spec.keyEnv, secretInput(spec.keyEnv)));
  if (spec && !spec.agentic) {
    model.append(field("Base URL", textInput(settings.model.base_url || spec.defaultBaseUrl,
      (v) => (settings.model.base_url = v))));
  }

  const actions = el("div", "ask__actions");
  actions.append(button("Test connection", "", async () => {
    $("settingsNote").textContent = "Testing…";
    try {
      const result = await post("/api/test-connection");
      $("settingsNote").textContent = result.ok ? "Connected." : `Failed: ${result.detail}`;
    } catch (error) { $("settingsNote").textContent = String(error.message); }
  }));
  model.append(actions);
  body.append(model);

  const workspace = group("Workspace");
  workspace.append(field("Folder", textInput(settings.workspace, (v) => (settings.workspace = v))));
  workspace.append(el("p", "field__note",
    "The agent's world. File tools cannot read or write outside it."));
  body.append(workspace);
}

/* ── trust ─────────────────────────────────────────────────────────── */

function renderTrustTab(body) {
  const settings = state.settings;
  const trust = group("Trust level");
  const modes = el("div", "modes");
  state.modes.forEach((mode) => {
    modes.append(modeCard(mode, settings.permissions.mode, (id) => {
      settings.permissions.mode = id;
      buildSettings();
    }));
  });
  trust.append(modes);
  body.append(trust);

  const rules = group("Saved approvals");
  rules.append(ruleList("Always allowed commands", settings.permissions.allow_commands));
  rules.append(ruleList("Always denied commands", settings.permissions.deny_commands));
  rules.append(ruleList("Always allowed tools", settings.permissions.allow_tools));
  rules.append(ruleList("Extra folders", settings.permissions.extra_roots));
  body.append(rules);

  const limits = group("Limits");
  limits.append(field("Max steps per turn", numberInput(settings.limits.max_steps,
    (v) => (settings.limits.max_steps = v))));
  limits.append(field("Turn time budget (seconds)", numberInput(settings.limits.max_turn_seconds,
    (v) => (settings.limits.max_turn_seconds = v))));
  limits.append(field("Command timeout (seconds)", numberInput(settings.limits.command_timeout,
    (v) => (settings.limits.command_timeout = v))));
  body.append(limits);
}

/* ── connections (MCP) ─────────────────────────────────────────────── */

async function renderConnectionsTab(body) {
  const wrap = group("MCP servers");
  wrap.append(el("p", "field__note",
    "Connect a Model Context Protocol server to give ArcBot its tools. " +
    "They run as separate programs that ArcBot cannot sandbox, so their tools always ask before running."));
  body.append(wrap);

  const listBox = el("div", "group");
  body.append(listBox);
  listBox.append(el("p", "empty-note", "Loading…"));

  let data;
  try {
    data = await api("/api/mcp");
  } catch (error) {
    listBox.innerHTML = "";
    listBox.append(el("p", "empty-note", String(error.message)));
    return;
  }
  state.mcp = data;
  listBox.innerHTML = "";

  if (!data.available) {
    listBox.append(el("p", "empty-note",
      "The `mcp` package is not installed. Reinstall ArcBot to enable this."));
    return;
  }

  const names = Object.keys(data.servers || {});
  if (!names.length) {
    listBox.append(el("p", "empty-note", "No servers connected yet."));
  }
  const statusByName = Object.fromEntries((data.status || []).map((s) => [s.name, s]));

  names.forEach((name) => {
    const config = data.servers[name];
    const status = statusByName[name];
    const row = el("div", "server");
    row.dataset.connected = config.enabled === false ? "off"
      : status ? String(status.connected) : "false";
    row.append(el("span", "server__led"));

    const middle = el("div");
    middle.append(el("div", "server__name", name));
    let detail = config.url || `${config.command} ${(config.args || []).join(" ")}`.trim();
    if (config.enabled === false) detail = "disabled — " + detail;
    else if (status?.connected) detail = `${status.tools.length} ${status.tools.length === 1 ? "tool" : "tools"} · ${detail}`;
    const meta = el("div", "server__meta", detail);
    middle.append(meta);
    if (status && !status.connected && config.enabled !== false) {
      middle.append(el("div", "server__meta server__meta--error", status.error));
    }
    row.append(middle);

    const remove = button("Remove", "btn--sm btn--danger", async () => {
      try {
        await api(`/api/mcp/${encodeURIComponent(name)}`, { method: "DELETE" });
        toast(`Removed ${name}.`, "success");
        buildSettings();
      } catch (error) { toast(String(error.message), "error"); }
    });
    row.append(remove);
    listBox.append(row);
  });

  // One-click presets.
  const presets = (data.presets || []).filter((p) => !names.includes(p.id));
  if (presets.length) {
    const box = group("Add a server");
    const grid = el("div", "preset-grid");
    presets.forEach((preset) => {
      const card = el("button", "preset");
      card.type = "button";
      card.disabled = !preset.available;
      card.append(el("span", "preset__name", preset.name));
      card.append(el("span", "preset__sum", preset.summary));
      if (!preset.available) {
        card.append(el("span", "preset__need", `needs ${preset.requires} on PATH`));
      }
      card.addEventListener("click", async () => {
        card.disabled = true;
        card.querySelector(".preset__sum").textContent = "Connecting…";
        try {
          await post("/api/mcp", { name: preset.id, ...preset.config });
          toast(`${preset.name} added.`, "success");
        } catch (error) { toast(String(error.message), "error"); }
        buildSettings();
      });
      grid.append(card);
    });
    box.append(grid);
    body.append(box);
  }

  // Manual entry.
  const custom = group("Or add one manually");
  const nameInput = textInput("", () => {});
  nameInput.placeholder = "a short name, e.g. github";
  const commandInput = textInput("", () => {});
  commandInput.placeholder = "npx -y @modelcontextprotocol/server-github   ·   or an https:// URL";
  custom.append(field("Name", nameInput));
  custom.append(field("Command or URL", commandInput));
  const addRow = el("div", "ask__actions");
  addRow.append(button("Add server", "btn--primary", async () => {
    const raw = commandInput.value.trim();
    const payload = { name: nameInput.value.trim() };
    if (raw.startsWith("http://") || raw.startsWith("https://")) {
      payload.url = raw;
    } else {
      const parts = raw.split(/\s+/).filter(Boolean);
      payload.command = parts[0] || "";
      payload.args = parts.slice(1);
    }
    try {
      await post("/api/mcp", payload);
      toast(`${payload.name} added.`, "success");
      buildSettings();
    } catch (error) { toast(String(error.message), "error"); }
  }));
  custom.append(addRow);
  body.append(custom);
}

/* ── voice ─────────────────────────────────────────────────────────── */

async function renderVoiceTab(body) {
  const intro = group("Voice mode");
  intro.append(el("p", "field__note",
    "Talk to ArcBot instead of typing. It can do everything it does in text — same tools, "
    + "same permissions, same trace. Models run on your machine unless you choose a cloud service, "
    + "and nothing is downloaded until you turn it on."));
  body.append(intro);

  const box = el("div", "group");
  body.append(box);
  box.append(el("p", "empty-note", "Loading…"));

  let data;
  try {
    data = await api("/api/voice");
  } catch (error) {
    box.innerHTML = "";
    box.append(el("p", "empty-note", String(error.message)));
    return;
  }
  state.voice = data;
  box.innerHTML = "";

  if (!data.available) {
    const warn = el("div", "builder__banner");
    warn.append(el("span", null,
      'The local speech engine is not installed. Run: pip install "arcbot[voice]" — '
      + "or choose a cloud service below."));
    box.append(warn);
  }

  const save = async (patch) => {
    try {
      state.voice = await post("/api/voice/settings", patch);
      buildSettings();
    } catch (error) { toast(String(error.message), "error"); }
  };

  const settings = data.settings;
  const installed = data.installed;

  // Where the speech runs.
  const where = group("Where it runs");
  const engineRow = el("div", "ask__actions");
  [["local", "On this machine"], ["cloud", "A cloud service"]].forEach(([value, label]) => {
    const button = el("button", `btn btn--sm${settings.sttEngine === value ? " btn--primary" : ""}`, label);
    button.type = "button";
    button.addEventListener("click", () => save({ sttEngine: value, ttsEngine: value }));
    engineRow.append(button);
  });
  where.append(engineRow);
  where.append(el("p", "field__note", settings.sttEngine === "local"
    ? `Everything stays here. ${data.downloadMb} MB of models, about ${data.diskMb} MB currently on disk.`
    : "Your audio and ArcBot's replies are sent to the service you pick."));
  if (settings.sttEngine === "local" && data.modelsPath) {
    const note = el("p", "field__note", "Stored in ");
    note.append(el("code", null, data.modelsPath));
    note.append(document.createTextNode(" — delete the folder to reclaim the space."));
    where.append(note);
  }
  body.append(where);

  if (settings.sttEngine === "local") {
    body.append(modelPicker("Listening", data.catalog.stt, settings.sttModel,
      installed.stt, (id) => save({ sttModel: id }), "stt"));
    body.append(modelPicker("Speaking", data.catalog.tts, settings.ttsModel,
      installed.tts, (id) => save({ ttsModel: id }), "tts"));

    const chosen = data.catalog.tts.find((m) => m.id === settings.ttsModel);
    if (chosen && chosen.voices > 1) {
      const voices = group("Voice");
      voices.append(field(`Speaker (0 – ${chosen.voices - 1})`,
        numberInput(settings.voice, (value) => save({ voice: value }))));
      body.append(voices);
    }
  } else {
    const cloud = group("Cloud service");
    const spec = data.catalog.cloudTts.openai;
    cloud.append(field("Voice", selectInput(
      spec.voices.map((v) => [v, v]), settings.cloudTtsVoice,
      (value) => save({ cloudTtsVoice: value }))));
    cloud.append(field("OPENAI_API_KEY", secretInput("OPENAI_API_KEY")));
    cloud.append(el("p", "field__note", spec.note));
    body.append(cloud);
  }

  // Behaviour.
  const tuning = group("How it listens");
  tuning.append(field("Pause before it answers (ms)",
    numberInput(settings.silenceMs, (value) => save({ silenceMs: value }))));
  tuning.append(el("p", "field__note",
    "How long a silence means you have finished. Lower feels snappier; higher lets you think mid-sentence."));
  tuning.append(field("Speaking speed",
    numberInput(settings.speed, (value) => save({ speed: value }))));

  const toggles = el("div", "ask__actions");
  [["bargeIn", "Interrupt by talking", settings.bargeIn],
   ["captions", "Show captions", settings.captions],
   ["speakPrompts", "Read prompts aloud", settings.speakPrompts]].forEach(([key, label, on]) => {
    const button = el("button", `btn btn--sm${on ? " btn--primary" : ""}`, label);
    button.type = "button";
    button.addEventListener("click", () => save({ [key]: !on }));
    toggles.append(button);
  });
  tuning.append(toggles);
  body.append(tuning);

  // Try it.
  const actions = group("Try it");
  const row = el("div", "ask__actions");
  row.append(button("Hear this voice", "btn--primary", async () => {
    try {
      const clip = await post("/api/voice/preview", {});
      previewAudio(clip);
    } catch (error) { toast(String(error.message), "error"); }
  }));
  if (settings.sttEngine === "local") {
    row.append(button(`Download models (${data.downloadMb} MB)`, "", async () => {
      try {
        const result = await post("/api/voice/download", {});
        renderVoiceDock(result.download);
        toast(result.message, result.ok ? "info" : "error", result.ok ? 4000 : 12000);
      } catch (error) { toast(String(error.message), "error", 12000); }
    }));
  }
  row.append(button("Start voice mode", "", () => {
    $("settings").hidden = true;
    voiceStart();
  }));
  actions.append(row);
  body.append(actions);
}

function modelPicker(title, models, selected, installed, onPick) {
  const box = group(title);
  box.append(modelChoiceGrid(models, { selected, installed, onPick }));
  return box;
}

/**
 * A grid of model cards where exactly one is chosen.
 *
 * Shared by setup and settings so the chosen model looks chosen in both — the
 * card is a radio button in everything but name, and it says up front what
 * picking it will cost.
 */
function modelChoiceGrid(models, { selected, installed, onPick }) {
  const grid = el("div", "preset-grid");
  models.forEach((model) => {
    const ready = installed.includes(model.id);
    const card = el("button", "preset");
    card.type = "button";
    card.setAttribute("aria-pressed", String(model.id === selected));

    const top = el("div", "preset__top");
    top.append(el("span", "preset__name", model.name));
    const badge = el("span", "cap-badge", ready ? "ready" : `${model.sizeMb} MB`);
    badge.dataset.cap = ready ? "read" : "write";
    top.append(badge);
    card.append(top);

    card.append(el("span", "preset__sum", model.note));

    const foot = el("div", "preset__foot");
    foot.append(el("span", "preset__need", model.language));
    if (model.default) foot.append(el("span", "preset__tag", "recommended"));
    if (model.voices > 1) foot.append(el("span", "preset__tag", `${model.voices} voices`));
    card.append(foot);

    card.addEventListener("click", () => onPick(model.id));
    grid.append(card);
  });
  return grid;
}

/* ── background model downloads ────────────────────────────────────── */

/**
 * A download the user can walk away from.
 *
 * The job itself lives on the server, so this only ever renders what it is
 * told — reloading the page picks the same download back up mid-flight.
 */
function renderVoiceDock(job) {
  const dock = $("voiceDock");
  if (!dock) return;
  if (!job || job.seen || (job.done && !job.items.length)) {
    dock.hidden = true;
    return;
  }

  const failed = job.done && !job.ok;
  dock.hidden = false;
  dock.dataset.state = job.done ? (failed ? "failed" : "ready") : "running";
  $("voiceDockTitle").textContent = job.done
    ? (failed ? "Some voice models did not arrive" : "Voice mode is ready")
    : "Downloading voice models";
  $("voiceDockSub").textContent = job.done
    ? job.message
    : `${Math.round(job.progress * 100)}% of ${job.totalMb} MB`;

  const items = $("voiceDockItems");
  items.innerHTML = "";
  job.items.forEach((item) => {
    const row = el("div", "dock__item");
    row.dataset.state = item.state;
    const head = el("div", "dock__item-head");
    head.append(el("span", "dock__item-name", item.name));
    head.append(el("span", "dock__item-meta",
      item.state === "failed" ? "failed"
        : item.state === "ready" ? "done"
          : `${Math.round(item.progress * 100)}%`));
    row.append(head);
    const bar = el("div", "dock__bar");
    const fill = el("i");
    fill.style.width = `${Math.round(item.progress * 100)}%`;
    bar.append(fill);
    row.append(bar);
    if (item.state === "failed" && item.detail) {
      row.append(el("span", "dock__item-error", item.detail));
    }
    items.append(row);
  });

  $("voiceDockFoot").hidden = !(job.done && job.ok);
}

/** Put the notice away — by dismissing it, or by going and using voice mode. */
async function dismissVoiceDock() {
  const dock = $("voiceDock");
  if (!dock || dock.hidden) return;
  dock.hidden = true;
  try { await post("/api/voice/download/seen", {}); } catch { /* nothing to lose */ }
}

$("voiceDockClose").addEventListener("click", dismissVoiceDock);
$("voiceDockStart").addEventListener("click", () => {
  dismissVoiceDock();
  voiceStart();
});

/** Play a preview clip without needing the full voice-mode audio graph. */
function previewAudio(clip) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const buffer = ctx.createBuffer(1, clip.samples.length, clip.sampleRate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < clip.samples.length; i++) channel[i] = clip.samples[i] / 32768;
  const node = ctx.createBufferSource();
  node.buffer = buffer;
  node.connect(ctx.destination);
  node.onended = () => ctx.close();
  node.start();
}

/* ── tools & the builder ───────────────────────────────────────────── */

async function renderToolsTab(body) {
  const intro = group("Your own tools");
  intro.append(el("p", "field__note",
    "Describe a tool and your model writes it. Nothing is saved or run until you have read the code."));
  body.append(intro);

  const listBox = el("div", "group");
  body.append(listBox);
  listBox.append(el("p", "empty-note", "Loading…"));

  const builderBox = el("div", "group");
  body.append(builderBox);

  let data;
  try {
    data = await api("/api/tools/custom");
  } catch (error) {
    listBox.innerHTML = "";
    listBox.append(el("p", "empty-note", String(error.message)));
    return;
  }

  listBox.innerHTML = "";
  if (!data.tools.length) {
    listBox.append(el("p", "empty-note", "You have not built any tools yet."));
  }
  data.tools.forEach((tool) => {
    const row = el("div", "tool-row");
    const left = el("div");
    left.append(el("div", "tool-row__name", tool.name));
    left.append(el("div", "tool-row__desc",
      tool.valid ? (tool.description || "") : (tool.problems[0] || "does not load")));
    row.append(left);
    const badge = el("span", "cap-badge", tool.valid ? tool.capability : "broken");
    badge.dataset.cap = tool.valid ? tool.capability : "destructive";
    row.append(badge);

    const actions = el("div", "ask__actions");
    actions.append(button("Edit", "btn--sm", async () => {
      try {
        const detail = await api(`/api/tools/custom/${encodeURIComponent(tool.name)}`);
        openBuilder(builderBox, data, detail.code);
      } catch (error) { toast(String(error.message), "error"); }
    }));
    actions.append(button("Delete", "btn--sm btn--danger", async () => {
      try {
        await api(`/api/tools/custom/${encodeURIComponent(tool.name)}`, { method: "DELETE" });
        toast(`Deleted ${tool.name}.`, "success");
        buildSettings();
      } catch (error) { toast(String(error.message), "error"); }
    }));
    row.append(actions);
    listBox.append(row);
  });

  openBuilder(builderBox, data, "");
}

function openBuilder(box, data, initialCode) {
  box.innerHTML = "";
  const builder = el("div", "builder");

  const prompt = el("textarea", "builder__prompt");
  prompt.placeholder = "Describe the tool. For example: check whether a website is up and how fast it responds.";
  builder.append(el("h4", "panel__title", initialCode ? "Edit this tool" : "Build a tool"));
  builder.append(prompt);

  const examples = el("div", "builder__examples");
  (data.examples || []).forEach((text) => {
    const chip = el("button", "chip-btn", text);
    chip.type = "button";
    chip.addEventListener("click", () => { prompt.value = text; prompt.focus(); });
    examples.append(chip);
  });
  builder.append(examples);

  const actions = el("div", "ask__actions");
  const generate = button("Generate", "btn--primary", async () => {
    const description = prompt.value.trim();
    if (description.length < 8) { toast("Describe the tool in a sentence or two.", "warn"); return; }
    generate.disabled = true;
    const label = generate.textContent;
    generate.innerHTML = "";
    generate.append(el("span", "spinner"), document.createTextNode(" Writing…"));
    try {
      const draft = await post("/api/tools/custom/generate", {
        description, existing: review.dataset.code || initialCode || "",
      });
      showDraft(draft);
    } catch (error) {
      toast(String(error.message), "error");
    } finally {
      generate.disabled = false;
      generate.textContent = label;
    }
  });
  actions.append(generate);
  builder.append(actions);

  const review = el("div", "builder__review");
  review.hidden = true;
  builder.append(review);
  box.append(builder);

  if (initialCode) {
    showDraft({ code: initialCode, problems: [], notes: [], valid: false, parameters: {} });
    revalidate();
  }

  let codeArea = null;
  let timer = null;

  function showDraft(draft) {
    review.hidden = false;
    review.innerHTML = "";
    review.dataset.code = draft.code || "";

    const banner = el("div", "builder__banner");
    banner.append(el("span", null,
      "This code runs on your machine with the capability it declares. Read it before you save."));
    review.append(banner);

    codeArea = el("textarea", "builder__code");
    codeArea.value = draft.code || "";
    codeArea.spellcheck = false;
    codeArea.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(revalidate, 400);
    });
    review.append(codeArea);

    const summary = el("div", "builder__summary");
    summary.id = "builderSummary";
    review.append(summary);
    renderSummary(summary, draft);

    // The draft lands below the fold; bring it to the user rather than
    // making them hunt for the thing they just asked for.
    requestAnimationFrame(() => review.scrollIntoView({ block: "start", behavior: "smooth" }));

    const rowActions = el("div", "ask__actions");
    const save = button("Save & enable", "btn--primary", async () => {
      save.disabled = true;
      try {
        const result = await post("/api/tools/custom/save", { code: codeArea.value });
        toast(`${result.name} is ready to use.`, "success");
        buildSettings();
      } catch (error) {
        toast(String(error.message), "error");
        save.disabled = false;
      }
    });
    save.disabled = !draft.valid;
    save.id = "builderSave";
    rowActions.append(save);
    rowActions.append(button("Discard", "btn--ghost btn--sm", () => {
      review.hidden = true;
      review.dataset.code = "";
    }));
    review.append(rowActions);
  }

  async function revalidate() {
    if (!codeArea) return;
    try {
      const draft = await post("/api/tools/custom/validate", { code: codeArea.value });
      review.dataset.code = draft.code || codeArea.value;
      renderSummary($("builderSummary"), draft);
      const save = $("builderSave");
      if (save) save.disabled = !draft.valid;
    } catch (error) { /* keep the last good summary */ }
  }
}

function renderSummary(box, draft) {
  if (!box) return;
  box.innerHTML = "";
  const row = (key, node) => {
    const line = el("div", "builder__row");
    line.append(el("span", "builder__key", key));
    line.append(node);
    box.append(line);
  };

  if (draft.problems?.length) {
    const list = el("ul", "problem-list");
    draft.problems.forEach((p) => list.append(el("li", null, p)));
    row("problems", list);
    return;
  }
  row("name", el("span", "builder__val", draft.name || "—"));
  const badge = el("span", "cap-badge", draft.capability || "read");
  badge.dataset.cap = draft.capability || "read";
  row("runs as", badge);

  const args = Object.keys(draft.parameters?.properties || {});
  row("arguments", el("span", "builder__val",
    args.length ? args.map((a) => {
      const required = (draft.parameters.required || []).includes(a);
      return required ? a : `${a} (optional)`;
    }).join(", ") : "none"));

  if (draft.notes?.length) {
    const list = el("ul", "note-list");
    draft.notes.forEach((n) => list.append(el("li", null, n)));
    row("this tool", list);
  }
}

function group(title) {
  const node = el("section", "group");
  node.append(el("h3", "group__title panel__title", title));
  return node;
}

function field(label, control) {
  const node = el("label", "field");
  node.append(el("span", "field__label", label), control);
  return node;
}

function textInput(value, onChange) {
  const node = el("input");
  node.type = "text";
  node.value = value || "";
  node.spellcheck = false;
  node.addEventListener("input", () => onChange(node.value));
  return node;
}

function numberInput(value, onChange) {
  const node = el("input");
  node.type = "number";
  node.value = value ?? 0;
  node.addEventListener("input", () => onChange(Number(node.value) || 0));
  return node;
}

function secretInput(keyEnv) {
  const node = el("input");
  node.type = "password";
  node.placeholder = "•••••  (leave blank to keep the current key)";
  node.autocomplete = "off";
  node.addEventListener("change", async () => {
    if (!node.value.trim()) return;
    try {
      await post("/api/secret", { key: keyEnv, value: node.value.trim() });
      node.value = "";
      node.placeholder = "Saved.";
      toast("Key saved.", "success");
    } catch (error) { toast(String(error.message), "error"); }
  });
  return node;
}

function selectInput(options, value, onChange) {
  const node = el("select");
  options.forEach(([id, label]) => {
    const option = el("option", null, label);
    option.value = id;
    if (id === value) option.selected = true;
    node.append(option);
  });
  node.addEventListener("change", () => onChange(node.value));
  return node;
}

function modeCard(mode, current, onPick) {
  const node = el("button", "mode");
  node.type = "button";
  node.dataset.mode = mode.id;
  node.setAttribute("aria-pressed", String(mode.id === current));
  const gauge = el("div", "mode__gauge");
  const level = ["plan", "guarded", "trusted", "full"].indexOf(mode.id);
  for (let i = 0; i < 4; i++) {
    const bar = el("i");
    if (i <= level) bar.className = "on";
    gauge.append(bar);
  }
  const text = el("div");
  text.append(el("div", "mode__name", mode.name));
  text.append(el("div", "mode__summary", mode.summary));
  text.append(el("div", "mode__detail", mode.detail));
  node.append(gauge, text);
  node.addEventListener("click", () => onPick(mode.id));
  return node;
}

function ruleList(title, items) {
  const wrap = el("div", "group");
  wrap.append(el("h4", "panel__title", title));
  if (!items.length) {
    wrap.append(el("p", "empty-note", "Nothing saved yet."));
    return wrap;
  }
  const list = el("ul", "rule-list");
  items.forEach((value, index) => {
    const item = el("li", "rule");
    item.append(el("span", null, value));
    const drop = el("button", "rule__drop", "✕");
    drop.type = "button";
    drop.title = "Remove";
    drop.addEventListener("click", () => { items.splice(index, 1); buildSettings(); });
    item.append(drop);
    list.append(item);
  });
  wrap.append(list);
  return wrap;
}

$("settingsSave").addEventListener("click", async () => {
  if (activeTab !== "model" && activeTab !== "trust") return;
  $("settingsNote").textContent = "Saving…";
  try {
    const result = await post("/api/settings", {
      workspace: state.settings.workspace,
      model: state.settings.model,
      permissions: state.settings.permissions,
      limits: state.settings.limits,
      ui: state.settings.ui,
    });
    state.settings = result.settings;
    applySettings();
    $("settings").hidden = true;
    toast("Settings saved.", "success");
  } catch (error) {
    $("settingsNote").textContent = String(error.message);
  }
});

$("rerunSetup").addEventListener("click", () => {
  $("settings").hidden = true;
  startOnboarding();
});

/* ══════════════════════════════ onboarding ══════════════════════════════ */

const setup = {
  step: 0,
  draft: null,
  models: [],
  auth: null,
};

const STEP_LABELS = ["Model", "Workspace", "Trust", "Capabilities", "Voice"];

function startOnboarding() {
  setup.step = 0;
  setup.draft = JSON.parse(JSON.stringify(state.settings));
  if (!setup.draft.toolsets?.length) {
    setup.draft.toolsets = state.toolsets.filter((t) => t.default && t.available).map((t) => t.id);
  }
  $("app").hidden = true;
  $("setup").hidden = false;
  setup.voice = { enabled: false, stt: "", tts: "" };
  renderProviders();
  renderModes();
  renderToolsetPicker();
  renderVoiceStep();
  $("workspaceInput").value = setup.draft.workspace || "";
  showStep(0);
}

function showStep(index) {
  setup.step = index;
  document.querySelectorAll(".step").forEach((node) => {
    node.hidden = Number(node.dataset.step) !== index;
  });
  const nav = $("stepsNav");
  nav.innerHTML = "";
  STEP_LABELS.forEach((label, i) => {
    const item = el("li", null, `${i + 1} · ${label}`);
    item.dataset.state = i === index ? "current" : i < index ? "done" : "todo";
    nav.append(item);
  });
  $("backBtn").hidden = index === 0;
  $("nextBtn").textContent = index === STEP_LABELS.length - 1 ? "Start using ArcBot" : "Continue";
  $("setupError").textContent = "";
  document.querySelector(".setup").scrollTop = 0;
}

function renderProviders() {
  const grid = $("providerGrid");
  grid.innerHTML = "";
  state.providers.forEach((provider) => {
    const card = el("button", "provider");
    card.type = "button";
    card.setAttribute("aria-pressed", String(setup.draft.model.provider === provider.id));
    if (provider.badge) card.append(el("span", "provider__badge", provider.badge));
    card.append(el("span", "provider__name", provider.name));
    card.append(el("span", "provider__tag", provider.tagline));
    card.addEventListener("click", () => {
      setup.draft.model.provider = provider.id;
      setup.draft.model.model = provider.defaultModel || "";
      setup.draft.model.base_url = provider.defaultBaseUrl || "";
      renderProviders();
      renderProviderDetail();
    });
    grid.append(card);
  });
  if (setup.draft.model.provider) renderProviderDetail();
}

async function renderProviderDetail() {
  const provider = state.providers.find((p) => p.id === setup.draft.model.provider);
  const panel = $("providerDetail");
  if (!provider) { panel.hidden = true; return; }
  panel.hidden = false;

  const steps = $("setupSteps");
  steps.innerHTML = "";
  provider.setupSteps.forEach((text) => {
    const item = el("li");
    item.innerHTML = escapeHtml(text).replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/(npm install [^<]+|ollama pull [^<]+|ant auth login)/g, "<code>$1</code>");
    steps.append(item);
  });

  $("keyField").hidden = !provider.keyEnv || provider.local;
  $("urlField").hidden = provider.agentic || !provider.defaultBaseUrl;
  $("urlInput").value = setup.draft.model.base_url || provider.defaultBaseUrl || "";
  $("modelInput").value = setup.draft.model.model || "";

  const auth = $("authState");
  auth.textContent = "Checking…";
  auth.removeAttribute("data-ok");
  try {
    const status = await api(`/api/providers/${provider.id}/auth`);
    setup.auth = status;
    auth.dataset.ok = String(status.available);
    auth.textContent = `${status.available ? "✓" : "!"}  ${status.detail}${status.hint && !status.available ? "  " + status.hint : ""}`;
  } catch (error) {
    auth.textContent = String(error.message);
  }
  loadModels(provider);
}

async function loadModels(provider) {
  const list = $("modelOptions");
  const note = $("modelNote");
  list.innerHTML = "";
  note.textContent = "Looking for available models…";
  try {
    const url = $("urlInput").value.trim();
    const data = await api(`/api/providers/${provider.id}/models?base_url=${encodeURIComponent(url)}`);
    setup.models = data.models || [];
  } catch { setup.models = provider.suggestedModels || []; }

  setup.models.filter(Boolean).forEach((name) => {
    const option = el("option");
    option.value = name;
    list.append(option);
  });
  if (!setup.models.length) {
    note.textContent = provider.agentic
      ? "Leave blank to use whatever Claude Code is set to."
      : "No models found — start the server, then press refresh.";
  } else {
    note.textContent = `${setup.models.length} available. ${provider.agentic ? "Leave blank for the default." : ""}`;
    if (!$("modelInput").value && !provider.agentic) $("modelInput").value = setup.models[0];
  }
}

$("refreshModels").addEventListener("click", () => {
  const provider = state.providers.find((p) => p.id === setup.draft.model.provider);
  if (provider) loadModels(provider);
});

function renderModes() {
  const list = $("modeList");
  list.innerHTML = "";
  state.modes.forEach((mode) => {
    list.append(modeCard(mode, setup.draft.permissions.mode, (id) => {
      setup.draft.permissions.mode = id;
      renderModes();
    }));
  });
}

/* Voice is opt-in and opt-in only: until "Yes" is chosen the models list is not
   even shown, and nothing is fetched. */
async function renderVoiceStep() {
  const choice = $("voiceChoice");
  choice.innerHTML = "";
  [
    { id: "yes", name: "Yes, set up voice mode",
      summary: "Talk instead of typing, hands-free.",
      detail: "Downloads speech models once. Everything stays on this machine." },
    { id: "no", name: "Not now", summary: "Type as usual. Nothing is downloaded.",
      detail: "You can turn voice mode on later in Settings." },
  ].forEach((option) => {
    const chosen = setup.voice.enabled === (option.id === "yes");
    const card = el("button", "mode mode--choice");
    card.type = "button";
    card.setAttribute("aria-pressed", String(chosen));
    const text = el("div");
    text.append(el("div", "mode__name", option.name));
    text.append(el("div", "mode__summary", option.summary));
    text.append(el("div", "mode__detail", option.detail));
    card.append(el("span", "mode__check"), text);
    card.addEventListener("click", () => {
      setup.voice.enabled = option.id === "yes";
      renderVoiceStep();
    });
    choice.append(card);
  });

  const box = $("voiceSetupModels");
  box.hidden = !setup.voice.enabled;
  if (!setup.voice.enabled) return;

  box.innerHTML = "";
  let data;
  try {
    data = await api("/api/voice");
  } catch (error) {
    box.append(el("p", "field__note", String(error.message)));
    return;
  }
  if (!data.available) {
    const warn = el("div", "builder__banner");
    warn.append(el("span", null,
      'The speech engine is not installed. Run: pip install "arcbot[voice]" and restart, '
      + "then turn voice mode on in Settings."));
    box.append(warn);
    return;
  }

  setup.voice.stt = setup.voice.stt || data.settings.sttModel;
  setup.voice.tts = setup.voice.tts || data.settings.ttsModel;

  const listening = el("div", "voice-setup__group");
  listening.append(el("h3", "voice-setup__title", "How it hears you"));
  listening.append(el("p", "voice-setup__lead",
    "The recommended one is picked already — change it if you want another language or more accuracy."));
  listening.append(modelChoiceGrid(data.catalog.stt, {
    selected: setup.voice.stt,
    installed: data.installed.stt,
    onPick: (id) => { setup.voice.stt = id; renderVoiceStep(); },
  }));
  box.append(listening);

  const speaking = el("div", "voice-setup__group");
  speaking.append(el("h3", "voice-setup__title", "How it sounds"));
  speaking.append(el("p", "voice-setup__lead",
    "You can try every voice — and switch between them — from inside voice mode later."));
  speaking.append(modelChoiceGrid(data.catalog.tts, {
    selected: setup.voice.tts,
    installed: data.installed.tts,
    onPick: (id) => { setup.voice.tts = id; renderVoiceStep(); },
  }));
  box.append(speaking);

  const stt = data.catalog.stt.find((m) => m.id === setup.voice.stt);
  const tts = data.catalog.tts.find((m) => m.id === setup.voice.tts);
  const pending = [stt, tts].filter(
    (m, i) => m && !(i === 0 ? data.installed.stt : data.installed.tts).includes(m.id));
  const total = Math.round(
    (pending.reduce((sum, m) => sum + m.sizeMb, 0)
      + (data.installed.vad.length ? 0 : 0.6)) * 10) / 10;

  const summary = el("div", "callout");
  summary.append(el("strong", null, total > 0
    ? `${total} MB downloads in the background`
    : "Nothing left to download"));
  summary.append(document.createTextNode(total > 0
    ? "Only the two you picked, plus a 0.6 MB turn detector. You can start "
      + "using ArcBot straight away — voice mode switches on when they arrive."
    : "Everything these choices need is already on this machine."));
  box.append(summary);
}

function renderToolsetPicker() {
  const grid = $("toolsetPicker");
  grid.innerHTML = "";
  state.toolsets.forEach((entry) => {
    const selected = setup.draft.toolsets.includes(entry.id) || entry.alwaysOn;
    const card = el("button", "tcard");
    card.type = "button";
    card.setAttribute("aria-pressed", String(selected));
    card.disabled = entry.alwaysOn || !entry.available;

    const top = el("div", "tcard__top");
    top.append(el("span", "tcard__name", entry.name));
    top.append(el("span", "tcard__state", entry.alwaysOn ? "always on" : selected ? "on" : "off"));
    card.append(top);
    card.append(el("span", "tcard__sum", entry.summary));
    if (entry.caution) card.append(el("span", "tcard__caution", entry.caution));
    if (!entry.available) card.append(el("span", "tcard__missing", `needs ${entry.missing.join(", ")}`));

    card.addEventListener("click", () => {
      const list = setup.draft.toolsets;
      const index = list.indexOf(entry.id);
      if (index >= 0) list.splice(index, 1); else list.push(entry.id);
      renderToolsetPicker();
    });
    grid.append(card);
  });
}

$("backBtn").addEventListener("click", () => showStep(Math.max(0, setup.step - 1)));

$("nextBtn").addEventListener("click", async () => {
  const error = $("setupError");
  error.textContent = "";

  if (setup.step === 0) {
    const provider = state.providers.find((p) => p.id === setup.draft.model.provider);
    if (!provider) { error.textContent = "Pick a provider to continue."; return; }
    setup.draft.model.model = $("modelInput").value.trim();
    setup.draft.model.base_url = $("urlInput").value.trim();
    if (!provider.agentic && !setup.draft.model.model) {
      error.textContent = "Choose a model.";
      return;
    }
    const key = $("keyInput").value.trim();
    if (key && provider.keyEnv) {
      try { await post("/api/secret", { key: provider.keyEnv, value: key }); }
      catch (exc) { error.textContent = String(exc.message); return; }
    }
  }

  if (setup.step === 1) {
    const folder = $("workspaceInput").value.trim();
    if (!folder) { error.textContent = "Choose a folder."; return; }
    setup.draft.workspace = folder;
  }

  if (setup.step < STEP_LABELS.length - 1) {
    showStep(setup.step + 1);
    return;
  }

  $("nextBtn").disabled = true;
  $("nextBtn").textContent = "Saving…";
  try {
    const result = await post("/api/settings", {
      workspace: setup.draft.workspace,
      model: setup.draft.model,
      toolsets: setup.draft.toolsets,
      permissions: { mode: setup.draft.permissions.mode },
      onboarded: true,
    });
    state.settings = result.settings;
    applySettings();

    /* Setup is finished the moment it is saved. The wizard closes here and
       nothing below can reopen it — a slow or failed download must never cost
       the user their configuration. */
    $("setup").hidden = true;
    $("app").hidden = false;
    trace.reset();
    toast("You're set up. Ask ArcBot to do something.", "success");

    if (setup.voice?.enabled) startVoiceDownload();
  } catch (exc) {
    error.textContent = String(exc.message);
  } finally {
    $("nextBtn").disabled = false;
    $("nextBtn").textContent = "Start using ArcBot";
  }
});

/** Kick off the model download and let the user get on with things. */
async function startVoiceDownload() {
  try {
    await post("/api/voice/settings", {
      enabled: true, sttModel: setup.voice.stt, ttsModel: setup.voice.tts,
    });
    const result = await post("/api/voice/download", {});
    renderVoiceDock(result.download);
  } catch (exc) {
    toast(`Voice models could not be downloaded: ${exc.message}`, "error", 10000);
  }
}

/**
 * Pick a download back up after a reload.
 *
 * Only asked for when voice mode is on, so a user who never wanted it never
 * causes a request about it.
 */
async function restoreVoiceDownload() {
  if (!state.settings.voice?.enabled) return;
  try {
    const data = await api("/api/voice");
    renderVoiceDock(data.download);
  } catch { /* the dock is a nicety, not a requirement */ }
}

const escapeHtml = (text) => text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ══════════════════════════════ boot ══════════════════════════════ */

async function boot() {
  let data;
  try {
    data = await api("/api/state");
  } catch (error) {
    document.body.innerHTML =
      `<div style="padding:14vh 24px;text-align:center;font-family:var(--sans)">
         <h1 style="font-size:20px">ArcBot could not start</h1>
         <p style="color:#8b879c">${escapeHtml(String(error.message))}</p>
         <p style="color:#8b879c">Reload the page, or restart ArcBot from the terminal.</p>
       </div>`;
    return;
  }

  state.settings = data.settings;
  state.providers = data.providers;
  state.toolsets = data.toolsets;
  state.enabled = data.settings.toolsets || [];
  state.modes = data.permissionModes;
  state.sessions = data.sessions || [];
  applySettings();

  if (!data.onboarded || !data.settings.model.provider) {
    startOnboarding();
  } else {
    $("app").hidden = false;
    renderEmptyState();
    restoreVoiceDownload();
  }

  connect();
  initVoiceControls();
  autosize();
  input.focus();
}

boot();
