"""The hands-free conversation loop.

Microphone audio arrives continuously. Silero decides where an utterance ends,
the recogniser turns it into text, and that text goes into the *same* agent turn
a typed message would — so voice mode does everything normal mode does, with the
same tools, permissions and trace.

Three details do most of the work in making it feel like a conversation rather
than a walkie-talkie:

* **Speak by sentence.** The agent's reply is synthesised a sentence at a time
  as it streams, so the first words come back in well under a second instead of
  after the whole answer is written.
* **Barge-in.** If the user starts talking while ArcBot is speaking, playback is
  cut immediately and the new utterance wins.
* **Deaf while speaking.** Audio captured during playback is discarded unless it
  is loud enough to be a real interruption, so ArcBot never transcribes itself.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

import numpy as np

from ..events import E, EventBus
from ..logging_setup import get_logger
from .engines import (
    SAMPLE_RATE,
    LocalVAD,
    SpeechToText,
    TextToSpeech,
    VoiceUnavailable,
    resample,
)

log = get_logger("voice.session")

#: Sentence boundaries good enough to chunk speech on, without a parser.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n{2,}")
#: Speak a partial chunk once it is at least this long, so long clauses without
#: punctuation do not stall the voice.
_SOFT_LIMIT = 180
#: Utterances shorter than this are noise, not speech.
_MIN_UTTERANCE_SECONDS = 0.25
#: Substantial utterances that transcribe to nothing before ArcBot speaks up.
_SILENT_STREAK_LIMIT = 3
#: How loud incoming audio must be, relative to its own recent floor, to count
#: as a barge-in rather than the speaker leaking into the microphone.
_BARGE_RMS = 0.02


class VoiceState:
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass
class VoiceStats:
    utterances: int = 0
    last_stt_ms: int = 0
    last_tts_ms: int = 0
    spoken_chars: int = 0


@dataclass
class VoiceSession:
    """One live voice conversation, bound to an agent."""

    bus: EventBus
    stt: SpeechToText
    tts: TextToSpeech
    vad: LocalVAD
    #: Called with the finished transcript; normally ``agent.send``.
    on_utterance: object = None
    #: True while the agent is mid-turn, so we know to hold new audio.
    barge_in: bool = True
    captions: bool = True

    state: str = VoiceState.IDLE
    stats: VoiceStats = field(default_factory=VoiceStats)
    _speaking_task: asyncio.Task | None = None
    _pending: list[str] = field(default_factory=list)
    _spoken_upto: int = 0
    _cancel_speech: bool = False
    _closed: bool = False
    _turn_active: bool = False
    #: Utterances in a row that transcribed to nothing.  A recogniser that is
    #: quietly returning empty text is indistinguishable from a dead microphone
    #: unless somebody counts.
    _silent_streak: int = 0

    # ------------------------------------------------------------------ state
    async def set_state(self, state: str, detail: str = "") -> None:
        if state == self.state:
            return
        self.state = state
        await self.bus.emit(E.VOICE_STATE, {"state": state, "detail": detail})

    async def start(self) -> None:
        self.vad.reset()
        self._closed = False
        await self.set_state(VoiceState.LISTENING)
        await self.bus.emit(E.VOICE_READY, {
            "sampleRate": self.tts.sample_rate,
            "captions": self.captions,
        })

    async def close(self) -> None:
        self._closed = True
        await self.stop_speaking()
        await self.set_state(VoiceState.IDLE)

    # ------------------------------------------------------------------ input
    async def feed(self, chunk: np.ndarray) -> None:
        """Take a slice of microphone audio and act on whatever it completes."""
        if self._closed or chunk.size == 0:
            return

        # While ArcBot is talking, only a decisively loud voice counts — anything
        # quieter is its own output bleeding back through the microphone.
        if self.state == VoiceState.SPEAKING:
            if not self.barge_in:
                return
            if float(np.sqrt(np.mean(chunk * chunk))) < _BARGE_RMS:
                return
            self.vad.feed(chunk)
            if self.vad.speech_detected:
                log.debug("Barge-in detected; stopping playback.")
                await self.stop_speaking()
                await self.bus.emit(E.VOICE_BARGE, {})
                await self.set_state(VoiceState.LISTENING)
            return

        for utterance in self.vad.feed(chunk):
            await self._handle_utterance(utterance)

    async def _handle_utterance(self, audio: np.ndarray) -> None:
        seconds = audio.size / SAMPLE_RATE
        if seconds < _MIN_UTTERANCE_SECONDS:
            return
        if self._turn_active:
            # The agent is still working; queueing here would put the user's
            # words in the wrong order, so tell them rather than swallow it.
            await self.bus.emit(E.NOTICE, {
                "level": "info", "text": "Still working on the last thing you asked.",
            })
            return

        await self.set_state(VoiceState.THINKING, "Transcribing…")
        try:
            transcript = await self.stt.transcribe(audio)
        except VoiceUnavailable as exc:
            await self._fail(str(exc))
            return
        except Exception as exc:  # a recogniser failure must not end the session
            log.warning("Transcription failed: %s", exc)
            await self._fail(f"Could not transcribe that: {exc}")
            return

        text = _clean_transcript(transcript.text)
        self.stats.last_stt_ms = transcript.elapsed_ms
        if not text:
            # One empty result is ordinary — a cough, the radio, a false start.
            # Several in a row from a decent length of audio means the recogniser
            # is not working, and saying so beats letting the user talk to a
            # window that never answers.
            if transcript.audio_seconds >= 1.0:
                self._silent_streak += 1
                if self._silent_streak == _SILENT_STREAK_LIMIT:
                    await self.bus.emit(E.NOTICE, {
                        "level": "error",
                        "text": "I can hear you, but nothing is being transcribed. "
                                "Check the microphone, or pick a different listening "
                                "model in Settings → Voice.",
                    })
            await self.set_state(VoiceState.LISTENING)
            return

        self._silent_streak = 0
        self.stats.utterances += 1
        await self.bus.emit(E.VOICE_TRANSCRIPT, {
            "text": text,
            "elapsedMs": transcript.elapsed_ms,
            "audioSeconds": round(transcript.audio_seconds, 2),
        })

        if self.on_utterance is not None:
            self._turn_active = True
            self.reset_reply()
            await self.on_utterance(text)

    async def _fail(self, message: str) -> None:
        await self.bus.emit(E.NOTICE, {"level": "error", "text": message})
        await self.set_state(VoiceState.LISTENING)

    # ----------------------------------------------------------------- output
    def reset_reply(self) -> None:
        """Begin a new spoken answer."""
        self._pending = []
        self._spoken_upto = 0
        self._cancel_speech = False

    async def on_text(self, full_text: str) -> None:
        """Called as the agent's answer grows; speaks each finished sentence."""
        if self._closed or not full_text:
            return
        remainder = full_text[self._spoken_upto:]
        if not remainder.strip():
            return

        pieces = _SENTENCE_END.split(remainder)
        if len(pieces) > 1:
            complete = pieces[:-1]
            consumed = len(remainder) - len(pieces[-1])
        elif len(remainder) >= _SOFT_LIMIT:
            cut = remainder.rfind(", ", 0, _SOFT_LIMIT)
            if cut < 40:
                cut = remainder.rfind(" ", 0, _SOFT_LIMIT)
            if cut < 40:
                return
            complete, consumed = [remainder[:cut]], cut
        else:
            return

        self._spoken_upto += consumed
        for sentence in complete:
            await self.enqueue(sentence)

    async def on_turn_end(self, full_text: str) -> None:
        """Speak whatever is left over, then go back to listening."""
        self._turn_active = False
        if self._closed:
            return
        tail = full_text[self._spoken_upto:].strip()
        if tail:
            self._spoken_upto = len(full_text)
            await self.enqueue(tail)
        if self._speaking_task is None or self._speaking_task.done():
            await self.set_state(VoiceState.LISTENING)

    async def enqueue(self, text: str) -> None:
        """Queue one chunk of speech and make sure the speaker is running."""
        cleaned = speakable(text)
        if not cleaned:
            return
        self._pending.append(cleaned)
        if self._speaking_task is None or self._speaking_task.done():
            self._cancel_speech = False
            self._speaking_task = asyncio.create_task(self._speak_queue())

    async def _speak_queue(self) -> None:
        await self.set_state(VoiceState.SPEAKING)
        try:
            while self._pending and not self._cancel_speech and not self._closed:
                sentence = self._pending.pop(0)
                started = time.monotonic()
                try:
                    samples = await self.tts.synthesize(sentence)
                except VoiceUnavailable as exc:
                    await self._fail(str(exc))
                    return
                except Exception as exc:
                    log.warning("Synthesis failed: %s", exc)
                    continue
                if self._cancel_speech or self._closed:
                    return
                self.stats.last_tts_ms = int((time.monotonic() - started) * 1000)
                self.stats.spoken_chars += len(sentence)

                await self.bus.emit(E.VOICE_SPEAK, {
                    "text": sentence if self.captions else "",
                    "sampleRate": self.tts.sample_rate,
                    "samples": _to_pcm16_list(samples),
                    "elapsedMs": self.stats.last_tts_ms,
                })
        except asyncio.CancelledError:
            raise
        finally:
            if not self._closed and not self._turn_active:
                await self.set_state(VoiceState.LISTENING)

    async def stop_speaking(self) -> None:
        """Cut playback immediately — used for barge-in and for Stop."""
        self._cancel_speech = True
        self._pending.clear()
        task = self._speaking_task
        self._speaking_task = None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        await self.bus.emit(E.VOICE_STOP_AUDIO, {})


# --------------------------------------------------------------------------- #
# Text preparation
# --------------------------------------------------------------------------- #

_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MARKDOWN = re.compile(r"[*_#>|]+")
_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
_PATH = re.compile(r"(?<![\w/])(/[\w./-]{6,})")
_URL = re.compile(r"https?://\S+")
_WHITESPACE = re.compile(r"\s+")


def speakable(text: str) -> str:
    """Turn a chunk of markdown into something worth listening to.

    Reading punctuation, code fences and long paths aloud is unbearable, so they
    are summarised rather than spoken character by character.
    """
    cleaned = _CODE_BLOCK.sub(" (code block) ", text or "")
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = _URL.sub(" a link ", cleaned)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = _BULLET.sub("", cleaned)
    cleaned = _MARKDOWN.sub("", cleaned)
    cleaned = _PATH.sub(lambda m: m.group(1).rsplit("/", 1)[-1], cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    # A chunk that was only formatting, or only a placeholder we substituted in,
    # is not worth saying out loud.
    if cleaned in ("(code block)", "a link") or len(cleaned) < 2:
        return ""
    return cleaned


_FILLER = re.compile(r"^\s*(um+|uh+|erm+|hmm+|mm+)[\s,.]*", re.I)

#: What Whisper-family recognisers emit when they hear room tone and no words.
#: These are dropped outright: a hallucinated phrase starting a whole agent turn
#: is far worse than making the user repeat a genuine "thanks". Short real
#: replies the agent needs — yes, no, stop, go on — are deliberately absent.
_HALLUCINATIONS = frozenset({
    "", "you", "thank you", "thanks", "thank you very much", "bye", "goodbye",
    "hmm", "uh", "um", "mm", "the", "so", "okay okay", "thanks for watching",
    "thank you for watching", "please subscribe", "subscribe",
})


def _clean_transcript(text: str) -> str:
    cleaned = _FILLER.sub("", (text or "").strip())
    if cleaned.lower().strip(" .,!?") in _HALLUCINATIONS:
        return ""
    return cleaned


def _to_pcm16_list(samples: np.ndarray) -> list[int]:
    """16-bit PCM as plain ints, which survives JSON without a binary channel."""
    if samples.size == 0:
        return []
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tolist()


def pcm16_to_float(payload: bytes) -> np.ndarray:
    data = np.frombuffer(payload, dtype="<i2").astype(np.float32)
    return data / 32768.0


def prepare_input(payload: bytes, source_rate: int) -> np.ndarray:
    """Browser audio to the 16 kHz mono float the engines expect."""
    samples = pcm16_to_float(payload)
    return resample(samples, source_rate, SAMPLE_RATE)
