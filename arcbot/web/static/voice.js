/* Voice mode: capture, playback, and the thing you actually look at.
 *
 * The visual is not a waveform or a bar meter — it is a field of particles held
 * in a ring by a spring, where loudness pushes them outward and frequency
 * content decides which band moves. Two voices share one field: yours pulls it
 * one way and tints it cool, ArcBot's pulls the other and tints it warm, so a
 * glance tells you who has the floor without reading anything.
 */
"use strict";

const VOICE = {
  active: false,
  state: "idle",
  ctx: null,             // AudioContext
  mic: null,             // MediaStream
  micAnalyser: null,
  outAnalyser: null,
  outGain: null,
  worklet: null,
  queue: [],             // scheduled playback sources
  playhead: 0,
  captions: true,
  level: { mic: 0, out: 0 },
  raf: 0,
};

/* ══════════════════════ audio capture ══════════════════════ */

/* Downsampling and framing happen off the main thread so the visual never
   stutters while the microphone is live. */
const CAPTURE_WORKLET = `
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = [];
    this._target = 2048;
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const channel = input[0];
    this._buf.push(new Float32Array(channel));
    let total = this._buf.reduce((n, b) => n + b.length, 0);
    if (total >= this._target) {
      const merged = new Float32Array(total);
      let at = 0;
      for (const b of this._buf) { merged.set(b, at); at += b.length; }
      this._buf = [];
      const pcm = new Int16Array(merged.length);
      for (let i = 0; i < merged.length; i++) {
        const s = Math.max(-1, Math.min(1, merged[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor('arcbot-capture', Capture);
`;

async function voiceStart() {
  if (VOICE.active) return;
  // Using voice mode is the strongest possible acknowledgement of "your models
  // are ready", so the notice retires itself here.
  dismissVoiceDock();
  try {
    VOICE.mic = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Without these, ArcBot hears itself through the speakers and answers
        // its own reply.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (error) {
    toast(`Microphone unavailable: ${error.message}`, "error", 9000);
    return;
  }

  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.resume();
  VOICE.ctx = ctx;

  const source = ctx.createMediaStreamSource(VOICE.mic);
  VOICE.micAnalyser = ctx.createAnalyser();
  VOICE.micAnalyser.fftSize = 512;
  VOICE.micAnalyser.smoothingTimeConstant = 0.75;
  source.connect(VOICE.micAnalyser);

  VOICE.outGain = ctx.createGain();
  VOICE.outAnalyser = ctx.createAnalyser();
  VOICE.outAnalyser.fftSize = 512;
  VOICE.outAnalyser.smoothingTimeConstant = 0.75;
  VOICE.outGain.connect(VOICE.outAnalyser);
  VOICE.outAnalyser.connect(ctx.destination);

  try {
    const url = URL.createObjectURL(new Blob([CAPTURE_WORKLET], { type: "text/javascript" }));
    await ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    const node = new AudioWorkletNode(ctx, "arcbot-capture");
    node.port.onmessage = (event) => sendAudio(event.data, ctx.sampleRate);
    source.connect(node);
    // A worklet with no destination is pruned in some engines; a muted sink keeps it alive.
    const sink = ctx.createGain();
    sink.gain.value = 0;
    node.connect(sink).connect(ctx.destination);
    VOICE.worklet = node;
  } catch (error) {
    toast(`Could not start audio capture: ${error.message}`, "error");
    await voiceStop();
    return;
  }

  VOICE.active = true;
  VOICE.playhead = ctx.currentTime;
  document.body.dataset.voice = "on";
  document.body.dataset.voiceKnown = "true";
  try { localStorage.setItem("arcbot.voiceKnown", "1"); } catch { /* private mode */ }
  $("voiceStage").hidden = false;
  send({ type: "voice.start" });
  startParticles();
}

async function voiceStop() {
  send({ type: "voice.stop" });
  VOICE.active = false;
  document.body.dataset.voice = "off";
  $("voiceStage").hidden = true;
  closeVoicePicker();
  stopPlayback();
  cancelAnimationFrame(VOICE.raf);
  VOICE.raf = 0;
  try { VOICE.worklet?.disconnect(); } catch { /* already gone */ }
  VOICE.mic?.getTracks().forEach((track) => track.stop());
  if (VOICE.ctx && VOICE.ctx.state !== "closed") await VOICE.ctx.close();
  VOICE.ctx = VOICE.mic = VOICE.worklet = null;
  VOICE.micAnalyser = VOICE.outAnalyser = VOICE.outGain = null;
  setVoiceState("idle");
}

/** One frame: a 4-byte sample rate header, then 16-bit PCM. */
function sendAudio(pcm, sampleRate) {
  if (!socket || socket.readyState !== WebSocket.OPEN || !VOICE.active) return;
  const frame = new Uint8Array(4 + pcm.byteLength);
  new DataView(frame.buffer).setUint32(0, sampleRate, true);
  frame.set(new Uint8Array(pcm.buffer), 4);
  socket.send(frame);
}

/* ══════════════════════ playback ══════════════════════ */

function playSamples(samples, sampleRate) {
  const ctx = VOICE.ctx;
  if (!ctx || !samples.length) return;
  const buffer = ctx.createBuffer(1, samples.length, sampleRate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;

  const node = ctx.createBufferSource();
  node.buffer = buffer;
  node.connect(VOICE.outGain);
  // Queue back to back so consecutive sentences sound like one utterance.
  const startAt = Math.max(ctx.currentTime + 0.02, VOICE.playhead);
  node.start(startAt);
  VOICE.playhead = startAt + buffer.duration;
  VOICE.queue.push(node);
  node.onended = () => {
    VOICE.queue = VOICE.queue.filter((n) => n !== node);
  };
}

function stopPlayback() {
  VOICE.queue.forEach((node) => { try { node.stop(); } catch { /* already stopped */ } });
  VOICE.queue = [];
  if (VOICE.ctx) VOICE.playhead = VOICE.ctx.currentTime;
}

/* ══════════════════════ the particle field ══════════════════════ */

const FIELD = { particles: [], w: 0, h: 0, t: 0, pulse: 0 };
const PARTICLE_COUNT = 220;

function initField(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, rect.width * dpr);
  canvas.height = Math.max(1, rect.height * dpr);
  FIELD.w = canvas.width;
  FIELD.h = canvas.height;
  FIELD.dpr = dpr;

  FIELD.particles = Array.from({ length: PARTICLE_COUNT }, (_, i) => {
    const angle = (i / PARTICLE_COUNT) * Math.PI * 2;
    return {
      angle,
      // Mirror the spectrum across the vertical axis: bass at the top, treble
      // at the bottom, symmetric left to right. A linear sweep looks lopsided
      // because real speech puts almost all its energy in the low bins.
      band: Math.floor(Math.abs(((i / PARTICLE_COUNT) * 2) - 1) * 47),
      radius: 0,
      target: 0,
      velocity: 0,
      drift: (Math.random() - 0.5) * 0.0007,
      size: 0.7 + Math.random() * 1.5,
      seed: Math.random() * Math.PI * 2,
    };
  });
}

function readAnalyser(analyser, bins) {
  if (!analyser) return { level: 0, spectrum: null };
  analyser.getByteFrequencyData(bins);
  let sum = 0;
  for (let i = 0; i < 48; i++) sum += bins[i];
  return { level: Math.min(1, sum / (48 * 190)), spectrum: bins };
}

function startParticles() {
  const canvas = $("voiceCanvas");
  initField(canvas);
  const ctx2d = canvas.getContext("2d");
  const micBins = new Uint8Array(256);
  const outBins = new Uint8Array(256);
  let resizeTimer = null;
  const onResize = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => initField(canvas), 120);
  };
  window.addEventListener("resize", onResize);

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function frame() {
    if (!VOICE.active) {
      window.removeEventListener("resize", onResize);
      return;
    }
    VOICE.raf = requestAnimationFrame(frame);

    const mic = readAnalyser(VOICE.micAnalyser, micBins);
    const out = readAnalyser(VOICE.outAnalyser, outBins);
    VOICE.level.mic += (mic.level - VOICE.level.mic) * 0.25;
    VOICE.level.out += (out.level - VOICE.level.out) * 0.25;

    // Whoever is louder owns the field; -1 is you, +1 is ArcBot.
    const bias = (VOICE.level.out - VOICE.level.mic);
    FIELD.pulse += (Math.max(VOICE.level.mic, VOICE.level.out) - FIELD.pulse) * 0.2;
    FIELD.t += reduced ? 0.002 : 0.01;

    const w = FIELD.w, h = FIELD.h, dpr = FIELD.dpr;
    const cx = w / 2, cy = h / 2;
    const base = Math.min(w, h) * 0.19;
    const spectrum = VOICE.level.out > VOICE.level.mic ? out.spectrum : mic.spectrum;

    ctx2d.clearRect(0, 0, w, h);

    const style = getComputedStyle(document.documentElement);
    const cool = style.getPropertyValue("--arc").trim() || "#6fd0ff";
    const warm = style.getPropertyValue("--moderate").trim() || "#e8b93f";
    const idle = style.getPropertyValue("--text-3").trim() || "#6c6779";

    // A soft core that breathes with whoever is talking.
    const coreR = base * (0.42 + FIELD.pulse * 0.5);
    const glow = ctx2d.createRadialGradient(cx, cy, 0, cx, cy, coreR * 2.6);
    const tint = bias > 0.02 ? warm : bias < -0.02 ? cool : idle;
    glow.addColorStop(0, hexAlpha(tint, 0.28 + FIELD.pulse * 0.42));
    glow.addColorStop(1, hexAlpha(tint, 0));
    ctx2d.fillStyle = glow;
    ctx2d.beginPath();
    ctx2d.arc(cx, cy, coreR * 2.6, 0, Math.PI * 2);
    ctx2d.fill();

    for (const p of FIELD.particles) {
      const energy = spectrum ? spectrum[p.band] / 255 : 0;
      // Spring toward a radius set by this band's energy, so the ring ripples
      // rather than pumping as one blob.
      p.target = base * (1 + energy * 1.35 + FIELD.pulse * 0.28)
        + Math.sin(FIELD.t * 1.4 + p.seed) * base * 0.05;
      p.velocity += (p.target - p.radius) * 0.06;
      p.velocity *= 0.82;
      p.radius += p.velocity;
      p.angle += p.drift + bias * 0.0016;

      const x = cx + Math.cos(p.angle) * p.radius;
      const y = cy + Math.sin(p.angle) * p.radius;
      const alpha = 0.2 + energy * 0.75;
      ctx2d.fillStyle = hexAlpha(energy > 0.28 ? tint : idle, alpha);
      ctx2d.beginPath();
      ctx2d.arc(x, y, p.size * dpr * (1 + energy * 1.6), 0, Math.PI * 2);
      ctx2d.fill();
    }

    // A thin ring ties the particles together into one object.
    ctx2d.strokeStyle = hexAlpha(tint, 0.14 + FIELD.pulse * 0.2);
    ctx2d.lineWidth = 1 * dpr;
    ctx2d.beginPath();
    ctx2d.arc(cx, cy, base * (1 + FIELD.pulse * 0.3), 0, Math.PI * 2);
    ctx2d.stroke();
  }

  frame();
}

function hexAlpha(hex, alpha) {
  const value = hex.replace("#", "").trim();
  if (value.length < 6) return `rgba(140,140,160,${alpha})`;
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

/* ══════════════════════ state and captions ══════════════════════ */

const VOICE_LABELS = {
  idle: "Voice mode off",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};

function setVoiceState(state, detail) {
  VOICE.state = state;
  const stage = $("voiceStage");
  if (stage) stage.dataset.state = state;
  const label = $("voiceStateLabel");
  if (label) label.textContent = detail || VOICE_LABELS[state] || state;
  const button = $("voiceBtn");
  if (button) button.setAttribute("aria-pressed", String(state !== "idle"));
}

function addCaption(role, text) {
  if (!VOICE.captions || !text) return;
  const list = $("voiceCaptions");
  if (!list) return;
  const line = el("div", "caption");
  line.dataset.role = role;
  line.append(el("span", "caption__who", role === "user" ? "You" : "ArcBot"));
  line.append(el("span", "caption__text", text));
  list.append(line);
  while (list.children.length > 8) list.firstChild.remove();
  list.scrollTop = list.scrollHeight;
}

/* ══════════════════════ events from the server ══════════════════════ */

function handleVoiceEvent(event) {
  switch (event.type) {
    case "voice.ready":
      VOICE.captions = event.captions !== false;
      $("voiceCaptions").hidden = !VOICE.captions;
      setVoiceState("listening");
      break;
    case "voice.state":
      setVoiceState(event.state, event.detail);
      break;
    case "voice.transcript":
      addCaption("user", event.text);
      trace.add("user", el("div", "user-turn", event.text));
      break;
    case "voice.speak":
      if (event.text) addCaption("assistant", event.text);
      playSamples(event.samples, event.sampleRate);
      break;
    case "voice.stop":
      stopPlayback();
      break;
    case "voice.barge":
      stopPlayback();
      addCaption("system", "…interrupted");
      break;
    case "voice.download":
      onVoiceDownload(event);
      break;
    default:
      return false;
  }
  return true;
}

function onVoiceDownload(event) {
  // The dock is the real progress display; this slim bar only exists so the
  // voice screen itself shows something while it waits for its own models.
  renderVoiceDock(event.download);
  if (PICKER.open && event.download?.done) openVoicePicker();

  const bar = $("voiceDownload");
  if (!bar) return;
  bar.hidden = false;
  $("voiceDownloadFill").style.width = `${Math.round((event.progress || 0) * 100)}%`;
  $("voiceDownloadLabel").textContent = event.label || "Downloading…";
  if ((event.progress || 0) >= 1) setTimeout(() => { bar.hidden = true; }, 1600);
}

/* ══════════════════════ wiring ══════════════════════ */

function initVoiceControls() {
  const button = $("voiceBtn");
  if (!button) return;
  try {
    if (localStorage.getItem("arcbot.voiceKnown")) document.body.dataset.voiceKnown = "true";
  } catch { /* private mode */ }
  button.addEventListener("click", async () => {
    if (VOICE.active) await voiceStop(); else await voiceStart();
  });
  $("voiceClose")?.addEventListener("click", () => voiceStop());
  $("tryVoiceBtn")?.addEventListener("click", () => voiceStart());
  $("voicePickBtn")?.addEventListener("click", () => openVoicePicker());
  $("voicePickClose")?.addEventListener("click", () => closeVoicePicker());
  $("voiceInterrupt")?.addEventListener("click", () => {
    stopPlayback();
    send({ type: "voice.interrupt" });
  });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "v") {
      event.preventDefault();
      button.click();
    }
  });
}

/* ══════════════════════ voice picker ══════════════════════
 *
 * Lives over the field so choosing a voice never means leaving the
 * conversation. Every speaker has a play button, because the only way to
 * judge a voice is to hear it.
 */

const PICKER = { catalog: null, installed: null, current: null, playing: null, open: false };
const SAMPLE_LINE = "Hello — this is how I sound when we talk.";

async function openVoicePicker() {
  const panel = $("voicePicker");
  const body = $("voicePickerBody");
  panel.hidden = false;
  PICKER.open = true;
  body.innerHTML = "";
  body.append(el("p", "picker__note", "Loading…"));

  let data;
  try {
    data = await api("/api/voice");
  } catch (error) {
    body.innerHTML = "";
    body.append(el("p", "picker__note", String(error.message)));
    return;
  }
  PICKER.catalog = data.catalog;
  PICKER.installed = data.installed;
  PICKER.current = { model: data.settings.ttsModel, voice: data.settings.voice };
  renderPicker();
}

function closeVoicePicker() {
  $("voicePicker").hidden = true;
  PICKER.open = false;
}

async function renderPicker() {
  const body = $("voicePickerBody");
  body.innerHTML = "";

  const models = el("div", "picker__group");
  models.append(el("h3", "panel__title", "Voice model"));
  PICKER.catalog.tts.forEach((model) => {
    const installed = PICKER.installed.tts.includes(model.id);
    const card = el("button", "voice-model");
    card.type = "button";
    card.setAttribute("aria-pressed", String(model.id === PICKER.current.model));

    const left = el("div");
    left.append(el("div", "voice-model__name", model.name));
    left.append(el("div", "voice-model__note",
      `${model.note} · ${model.voices > 1 ? `${model.voices} voices` : "1 voice"}`));
    card.append(left);

    const badge = el("span", "cap-badge", installed ? "ready" : `${model.sizeMb} MB`);
    badge.dataset.cap = installed ? "read" : "write";
    card.append(badge);

    card.addEventListener("click", () => selectModel(model, installed));
    models.append(card);
  });
  body.append(models);

  // Speakers for whichever model is selected.
  const chosen = PICKER.catalog.tts.find((m) => m.id === PICKER.current.model);
  if (!chosen) return;
  if (!PICKER.installed.tts.includes(chosen.id)) {
    body.append(el("p", "picker__note",
      `${chosen.name} is not downloaded yet — pick it above to fetch ${chosen.sizeMb} MB.`));
    return;
  }

  const group = el("div", "picker__group");
  group.append(el("h3", "panel__title", "Speaker"));
  const chips = el("div", "voice-chips");
  group.append(chips);
  group.append(el("p", "picker__note",
    "Tap to hear it. Tapping again while it plays switches ArcBot to that voice."));
  body.append(group);

  let info;
  try {
    info = await api(`/api/voice/voices?model=${encodeURIComponent(chosen.id)}`);
  } catch {
    info = { count: chosen.voices, names: [] };
  }

  for (let index = 0; index < Math.max(1, info.count); index++) {
    const label = info.names[index] || (info.count > 1 ? `Voice ${index + 1}` : "Default");
    const chip = el("button", "voice-chip");
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(index === PICKER.current.voice));
    chip.append(el("span", null, label));
    chip.append(el("span", "voice-chip__play", "▶"));
    chip.addEventListener("click", () => auditionVoice(chosen.id, index, chip));
    chips.append(chip);
  }
}

async function selectModel(model, installed) {
  if (!installed) {
    // Downloads run in the background, so the conversation carries on in the
    // current voice and the picker refreshes itself when the new one lands.
    try {
      const result = await post("/api/voice/install", { kind: "tts", model: model.id });
      renderVoiceDock(result.download);
      toast(result.message, result.ok ? "info" : "error", result.ok ? 6000 : 12000);
      PICKER.installed = result.installed;
      renderPicker();
    } catch (error) {
      toast(String(error.message), "error", 12000);
    }
    return;
  }
  PICKER.current = { model: model.id, voice: 0 };
  await applyVoice();
  renderPicker();
}

/** Play a speaker; a second tap while it is playing makes it the live voice. */
async function auditionVoice(modelId, index, chip) {
  if (PICKER.playing === `${modelId}:${index}`) {
    PICKER.current = { model: modelId, voice: index };
    await applyVoice();
    renderPicker();
    return;
  }

  document.querySelectorAll(".voice-chip").forEach((c) => { c.dataset.playing = "false"; });
  chip.dataset.playing = "true";
  PICKER.playing = `${modelId}:${index}`;

  try {
    const clip = await post("/api/voice/preview",
      { model: modelId, voice: index, text: SAMPLE_LINE });
    if (clip.needsDownload) {
      toast(`${clip.name} needs a ${clip.sizeMb} MB download first.`, "warn");
      return;
    }
    // Route through the session's own output so barge-in suppression applies
    // and ArcBot does not try to transcribe its own sample.
    playSamples(clip.samples, clip.sampleRate);
  } catch (error) {
    toast(String(error.message), "error");
  } finally {
    setTimeout(() => {
      if (PICKER.playing === `${modelId}:${index}`) {
        chip.dataset.playing = "false";
        PICKER.playing = null;
      }
    }, 4000);
  }
}

async function applyVoice() {
  try {
    await post("/api/voice/use", PICKER.current);
    const model = PICKER.catalog.tts.find((m) => m.id === PICKER.current.model);
    toast(`Now speaking as ${model ? model.name : PICKER.current.model}.`, "success");
  } catch (error) {
    toast(String(error.message), "error");
  }
}
