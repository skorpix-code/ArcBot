"""Starts, stops and configures a voice conversation.

The web layer talks to this and nothing else: it decides which engines to build
from the user's settings, makes sure the models are on disk, and hands the
resulting session to the agent so spoken input becomes an ordinary turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..config import ConfigStore, VoiceSettings
from ..events import E, EventBus
from ..logging_setup import get_logger
from .catalog import CLOUD_STT, CLOUD_TTS, VoiceModel
from .engines import (
    CloudSTT,
    CloudTTS,
    LocalSTT,
    LocalTTS,
    LocalVAD,
    SpeechToText,
    TextToSpeech,
    VoiceUnavailable,
    sherpa_available,
    sherpa_status,
)
from .models import disk_usage_mb, installed_ids, models_root, required_models
from .session import VoiceSession, prepare_input

log = get_logger("voice.controller")


@dataclass
class _Row:
    """One model's share of a download."""

    id: str
    name: str
    kind: str
    size_mb: float
    progress: float = 0.0
    state: str = "waiting"          # waiting | downloading | ready | failed
    detail: str = ""

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "sizeMb": self.size_mb, "progress": round(self.progress, 3),
            "state": self.state, "detail": self.detail,
        }


@dataclass
class _Download:
    """A background fetch the user can walk away from.

    Lives on the server, not in the page, so reloading the browser — or closing
    it and coming back — never loses a half-finished download or its result.
    """

    rows: dict[str, _Row] = field(default_factory=dict)
    task: asyncio.Task | None = None
    done: bool = False
    ok: bool = True
    message: str = ""
    seen: bool = False              # the user has acknowledged the result

    @property
    def running(self) -> bool:
        """Still going.  ``done`` flips before the task itself finishes, and a
        cancelled task must not leave the job looking alive forever."""
        return self.task is not None and not self.done and not self.task.done()

    def progress(self) -> float:
        """Weighted by size, so a 132 MB model does not tick like a 0.6 MB one."""
        rows = list(self.rows.values())
        total = sum(r.size_mb for r in rows)
        if total <= 0:
            return 1.0 if self.done else 0.0
        return sum(r.size_mb * r.progress for r in rows) / total

    def public(self) -> dict:
        return {
            "running": self.running,
            "done": self.done,
            "ok": self.ok,
            "message": self.message,
            "seen": self.seen,
            "progress": round(self.progress(), 3),
            "totalMb": round(sum(r.size_mb for r in self.rows.values()), 1),
            "items": [row.public() for row in self.rows.values()],
        }


class VoiceController:
    """Owns at most one live voice session."""

    def __init__(self, store: ConfigStore, bus: EventBus, agent):
        self.store = store
        self.bus = bus
        self.agent = agent
        self.session: VoiceSession | None = None
        self._starting = asyncio.Lock()
        self._download: _Download | None = None

    @property
    def settings(self) -> VoiceSettings:
        return self.store.settings.voice

    @property
    def active(self) -> bool:
        return self.session is not None

    # --------------------------------------------------------------- engines
    def _build_stt(self) -> SpeechToText:
        voice = self.settings
        if voice.stt_engine == "cloud":
            spec = CLOUD_STT.get(voice.stt_cloud) or CLOUD_STT["openai"]
            return CloudSTT(
                base_url=str(spec["base_url"]),
                api_key=self.store.get_secret(str(spec["keyEnv"])),
                model=voice.cloud_stt_model or str(spec["default_model"]),
            )
        return LocalSTT(voice.stt_model, threads=voice.threads)

    def _build_tts(self) -> TextToSpeech:
        voice = self.settings
        if voice.tts_engine == "cloud":
            spec = CLOUD_TTS.get(voice.tts_cloud) or CLOUD_TTS["openai"]
            return CloudTTS(
                base_url=str(spec["base_url"]),
                api_key=self.store.get_secret(str(spec["keyEnv"])),
                model=voice.cloud_tts_model or str(spec["default_model"]),
                voice=voice.cloud_tts_voice,
                speed=voice.speed,
            )
        return LocalTTS(voice.tts_model, voice=voice.voice,
                        speed=voice.speed, threads=voice.threads)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> tuple[bool, str]:
        """Bring voice mode up, downloading models first if they are missing."""
        async with self._starting:
            if self.session is not None:
                return True, "Voice mode is already running."

            voice = self.settings
            needs_local = voice.stt_engine == "local" or voice.tts_engine == "local"
            if needs_local:
                ready, why = sherpa_status()
                if not ready:
                    return False, why
                ready, message = await self.ensure_models()
                if not ready:
                    return False, message

            try:
                stt = self._build_stt()
                tts = self._build_tts()
                # Silero is always local: it is 600 kB and runs per audio frame,
                # so sending every frame to a cloud service would be absurd.
                vad = LocalVAD(silence_seconds=voice.silence_ms / 1000.0)
                # Load both models now rather than on the first utterance. A
                # model that cannot load has to say so while the user is looking
                # at the button they just pressed — not by quietly doing nothing
                # every time they speak.
                await stt.warm_up()
                await tts.warm_up()
            except VoiceUnavailable as exc:
                return False, str(exc)
            except Exception as exc:  # a bad model directory, a missing file…
                log.exception("Voice engines failed to build")
                return False, f"Voice mode could not start: {exc}"

            session = VoiceSession(
                bus=self.bus,
                stt=stt,
                tts=tts,
                vad=vad,
                on_utterance=self._on_utterance,
                barge_in=voice.barge_in,
                captions=voice.captions,
            )
            self.session = session
            self.agent.voice = session
            # Hands-free means questions have to be audible too, or the agent
            # stops on an approval the user never knew it was waiting for.
            self.agent.broker.narrate = self.say
            await session.start()
            log.info("Voice mode started (%s / %s).", voice.stt_engine, voice.tts_engine)
            return True, "Voice mode is on."

    async def stop(self) -> None:
        session, self.session = self.session, None
        self.agent.voice = None
        self.agent.broker.narrate = None
        if session is not None:
            await session.close()
            log.info("Voice mode stopped.")

    async def _on_utterance(self, text: str) -> None:
        """A finished utterance becomes an ordinary agent turn."""
        await self.agent.send(text)

    # ------------------------------------------------------------------ audio
    async def feed(self, payload: bytes, sample_rate: int) -> None:
        session = self.session
        if session is None:
            return
        try:
            audio = prepare_input(payload, sample_rate)
        except Exception as exc:  # a malformed frame must not kill the socket
            log.debug("Bad audio frame: %s", exc)
            return
        await session.feed(audio)

    async def interrupt(self) -> None:
        if self.session is not None:
            await self.session.stop_speaking()

    async def say(self, text: str) -> None:
        """Speak something that did not come from the model — a prompt, a notice."""
        if self.session is not None and self.settings.speak_prompts:
            await self.session.enqueue(text)

    # ------------------------------------------------------------------ models
    def wanted_models(self) -> list[VoiceModel]:
        """Everything the current choices need on disk.  Cloud engines need nothing."""
        voice = self.settings
        return required_models(
            voice.stt_model if voice.stt_engine == "local" else "",
            voice.tts_model if voice.tts_engine == "local" else "",
        )

    def download_state(self) -> dict | None:
        return self._download.public() if self._download else None

    def acknowledge_download(self) -> None:
        """Stop showing a download the user has dismissed.

        Dismissing one still in flight means "stop hovering", not "cancel" — so
        it goes quiet now and speaks up once more when the models actually
        land, which is the part worth interrupting for.
        """
        job = self._download
        if job is None:
            return
        if job.running:
            job.seen = True
        else:
            self._download = None

    async def cancel_download(self) -> None:
        """Let go of a download in flight, for shutdown.

        The fetch itself lives in a worker thread and cannot be interrupted, so
        this only stops ArcBot waiting on it — the partial file is scratch and
        gets swept up on the next run.
        """
        job = self._download
        if job is None or job.task is None or job.task.done():
            return
        job.task.cancel()
        try:
            await job.task
        except BaseException:                     # nothing may block a shutdown
            pass

    async def start_download(self, models: list[VoiceModel]) -> dict:
        """Begin fetching in the background and return straight away.

        Downloading is the one part of setup that can take minutes, so it must
        never be something the user waits in front of.  A download already in
        flight is joined rather than restarted.
        """
        from .models import is_installed, kind_of

        if self._download is not None and self._download.running:
            return self._download.public()

        pending = [m for m in models if not is_installed(m)]
        job = _Download(rows={
            m.id: _Row(m.id, m.name, kind_of(m), m.size_mb) for m in pending
        })
        self._download = job
        if not pending:
            job.done, job.ok, job.message = True, True, "Everything is already downloaded."
            return job.public()

        job.task = asyncio.create_task(self._run_download(job, pending))
        await self._emit_download(job)
        return job.public()

    async def _run_download(self, job: _Download, models: list[VoiceModel]) -> None:
        """Fetch one model at a time, reporting as it goes."""
        from .models import install

        failures: list[str] = []
        for model in models:
            row = job.rows[model.id]
            row.state = "downloading"

            async def progress(_id: str, fraction: float, label: str, _row=row) -> None:
                _row.progress = max(_row.progress, min(fraction, 0.999))
                _row.detail = label
                await self._emit_download(job)

            try:
                report = await install([model], progress)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # never kill the task
                log.exception("Voice download crashed for %s", model.id)
                report = None
                row.state, row.detail = "failed", str(exc)[:200]
                failures.append(f"{model.name}: {exc}")

            if report is not None:
                if report.failed:
                    reason = next(iter(report.failed.values()), "unknown error")
                    row.state, row.detail = "failed", reason
                    failures.append(f"{model.name}: {reason}")
                else:
                    row.state, row.progress, row.detail = "ready", 1.0, "Ready"
            await self._emit_download(job)

        job.done = True
        job.seen = False          # the result is worth surfacing once, even if
        job.ok = not failures     # the download itself was waved away
        job.message = (
            "Voice models are ready." if job.ok
            else "Some voice models could not be downloaded — " + "; ".join(failures)
        )
        await self._emit_download(job)

    async def _emit_download(self, job: _Download) -> None:
        if self._download is not job:
            return
        snapshot = job.public()
        current = next(
            (r for r in job.rows.values() if r.state == "downloading"), None
        )
        await self.bus.emit(E.VOICE_DOWNLOAD, {
            # Flat keys the voice overlay's slim bar already understands…
            "modelId": current.id if current else "",
            "progress": snapshot["progress"],
            "label": (current.detail if current else job.message) or "Downloading…",
            # …and the whole picture for the dock.
            "download": snapshot,
        })

    async def ensure_models(self) -> tuple[bool, str]:
        """Download what the current choices need and wait for it.

        Only used where waiting is genuinely unavoidable — voice mode cannot
        start without its models.
        """
        await self.start_download(self.wanted_models())
        job = self._download
        if job is None:
            return True, "Models ready."
        if job.task is not None:
            try:
                await asyncio.shield(job.task)
            except asyncio.CancelledError:
                return False, "The download was cancelled."
        return job.ok, job.message or "Models ready."

    async def describe_voices(self, model_id: str = "") -> dict:
        """How many speakers a speaking model offers, and what to call them."""
        from .catalog import TTS_MODELS
        from .models import is_installed

        model = TTS_MODELS.get(model_id or self.settings.tts_model)
        if model is None:
            return {"count": 0, "names": [], "installed": False}
        if not is_installed(model):
            # Nothing is downloaded to inspect, so fall back to the catalog's
            # number purely so the picker can say what you would be getting.
            return {
                "count": int(model.detail.get("voices", 1)),
                "names": [],
                "installed": False,
                "sizeMb": model.size_mb,
            }

        engine = LocalTTS(model.id, threads=self.settings.threads)
        try:
            await engine.synthesize("hi")          # forces the model to load
        except Exception as exc:
            log.debug("Could not load %s to count voices: %s", model.id, exc)
            return {"count": int(model.detail.get("voices", 1)), "names": [],
                    "installed": True}
        return {
            "count": engine.voices(),
            "names": engine.voice_names(),
            "installed": True,
            "sizeMb": model.size_mb,
        }

    async def preview(self, text: str, model_id: str = "", voice: int = 0) -> dict:
        """Synthesise a sample without changing what the session is using.

        Deliberately does not download: a preview that quietly pulls 132 MB is
        not a preview. If the model is missing, say so and let the user decide.
        """
        from .catalog import TTS_MODELS
        from .models import is_installed

        if self.settings.tts_engine == "cloud" and not model_id:
            engine = self._build_tts()
        else:
            model = TTS_MODELS.get(model_id or self.settings.tts_model)
            if model is None:
                raise VoiceUnavailable("No such voice model.")
            if not is_installed(model):
                return {
                    "needsDownload": True,
                    "modelId": model.id,
                    "name": model.name,
                    "sizeMb": model.size_mb,
                }
            engine = LocalTTS(model.id, voice=voice, speed=self.settings.speed,
                              threads=self.settings.threads)

        samples = await engine.synthesize(text)
        import numpy as np

        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tolist()
        return {"sampleRate": engine.sample_rate, "samples": pcm,
                "voices": engine.voices()}

    async def install_one(self, kind: str, model_id: str) -> tuple[bool, str]:
        """Download exactly one model — never a set the user did not ask for."""
        from .models import install, resolve

        model = resolve(kind, model_id)
        if model is None:
            return False, "No such voice model."

        async def progress(mid: str, fraction: float, label: str) -> None:
            await self.bus.emit(E.VOICE_DOWNLOAD, {
                "modelId": mid, "progress": round(fraction, 3), "label": label,
            })

        report = await install([model], progress)
        if report.ok:
            return True, f"{model.name} is ready."
        return False, f"{model.name} could not be downloaded: "\
                      f"{next(iter(report.failed.values()), 'unknown error')}"

    async def swap_voice(self) -> None:
        """Rebuild the speaking engine so a voice change lands mid-conversation.

        Anything already queued is dropped rather than finished in the old
        voice: the user just asked for a different one, so hearing the old one
        for another two sentences would read as the change not having worked.
        """
        session = self.session
        if session is None:
            return
        await session.stop_speaking()
        session.tts = self._build_tts()

    def status(self) -> dict:
        voice = self.settings
        return {
            "available": sherpa_available(),
            "active": self.active,
            "state": self.session.state if self.session else "idle",
            "installed": installed_ids(),
            "diskMb": disk_usage_mb(),
            "modelsPath": str(models_root()),
            "download": self.download_state(),
            "settings": {
                "enabled": voice.enabled,
                "sttEngine": voice.stt_engine,
                "ttsEngine": voice.tts_engine,
                "sttModel": voice.stt_model,
                "ttsModel": voice.tts_model,
                "voice": voice.voice,
                "speed": voice.speed,
                "silenceMs": voice.silence_ms,
                "bargeIn": voice.barge_in,
                "captions": voice.captions,
                "speakPrompts": voice.speak_prompts,
                "cloudTtsVoice": voice.cloud_tts_voice,
            },
        }
