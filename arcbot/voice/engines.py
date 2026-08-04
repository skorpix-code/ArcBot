"""Speech in, speech out — local or cloud, behind one interface.

The local path is sherpa-onnx: a small wheel with no PyTorch that covers
recognition, synthesis and voice-activity detection, so the whole feature adds
one dependency rather than three frameworks.

Models are loaded lazily and kept warm, because loading is the slow part
(around a second) and synthesis is not.
"""

from __future__ import annotations

import asyncio
import io
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..logging_setup import get_logger
from .catalog import STT_MODELS, TTS_MODELS, VAD, VoiceModel
from .models import install_path, is_installed

log = get_logger("voice.engines")

#: Everything upstream of the engines speaks 16 kHz mono float32.
SAMPLE_RATE = 16_000


class VoiceUnavailable(RuntimeError):
    """Raised when an engine cannot start, with a message for the user."""


_SHERPA_STATE: tuple[bool, str] | None = None


def sherpa_status() -> tuple[bool, str]:
    """Whether the speech engine can actually be used, and why not if it cannot.

    Checking for the module is not enough: sherpa-onnx ships native libraries
    beside it, and a half-finished install imports the Python package fine and
    then fails on a missing ``.so``. Importing once for real is the only honest
    answer, so the result is cached.
    """
    global _SHERPA_STATE
    if _SHERPA_STATE is None:
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError as exc:
            detail = str(exc)
            if ".so" in detail or "dll" in detail.lower():
                message = (
                    "The speech engine is installed but its native libraries are missing, "
                    "which usually means the download was interrupted. Reinstall it with: "
                    'pip install --force-reinstall sherpa-onnx'
                )
            else:
                message = (
                    'Voice mode needs the speech engine. Install it with: '
                    'pip install "arcbot[voice]"'
                )
            _SHERPA_STATE = (False, message)
        except Exception as exc:  # a broken build should not crash the app
            _SHERPA_STATE = (False, f"The speech engine failed to load: {exc}")
        else:
            _SHERPA_STATE = (True, "")
    return _SHERPA_STATE


def sherpa_available() -> bool:
    return sherpa_status()[0]


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #


@dataclass
class Transcript:
    text: str
    #: Seconds of audio that produced it, for the latency readout.
    audio_seconds: float = 0.0
    elapsed_ms: int = 0


class SpeechToText:
    async def transcribe(self, audio: np.ndarray) -> Transcript:
        raise NotImplementedError

    async def warm_up(self) -> None:
        """Load now, so a broken model is reported before the user speaks."""

    async def close(self) -> None:
        pass


class TextToSpeech:
    #: Output rate, which the browser needs in order to play the samples.
    sample_rate: int = 22_050

    async def synthesize(self, text: str) -> np.ndarray:
        raise NotImplementedError

    async def warm_up(self) -> None:
        """Load now, so a broken model is reported before the user speaks."""

    def voices(self) -> int:
        return 1

    async def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Local: sherpa-onnx
# --------------------------------------------------------------------------- #


def _require(model: VoiceModel) -> Path:
    if not is_installed(model):
        raise VoiceUnavailable(
            f"The {model.name} model is not downloaded yet. Turn voice mode on in "
            f"Settings and ArcBot will fetch it."
        )
    return install_path(model)


def _file(root: Path, name: str) -> str:
    """Resolve a file inside a model folder, refusing to pass on a missing one.

    sherpa-onnx takes a path that does not exist without complaint and then
    decodes to an empty string forever — which looks exactly like a microphone
    that is not working, and is impossible to diagnose from the UI.  Checking
    here turns silence into a sentence that names the file.
    """
    path = root / name
    if not path.exists():
        raise VoiceUnavailable(
            f"{name} is missing from {root.name}. That model's download looks "
            f"incomplete — remove it in Settings → Voice and fetch it again."
        )
    return str(path)


class LocalSTT(SpeechToText):
    """Offline recognition through sherpa-onnx."""

    def __init__(self, model_id: str, threads: int = 2):
        ok, why = sherpa_status()
        if not ok:
            raise VoiceUnavailable(why)
        model = STT_MODELS.get(model_id) or STT_MODELS["moonshine-tiny-en"]
        self.model = model
        self.threads = max(1, threads)
        self._recognizer = None
        self._lock = asyncio.Lock()

    def _load(self):
        import sherpa_onnx

        root = _require(self.model)
        detail = self.model.detail
        engine = self.model.engine
        tokens = _file(root, detail.get("tokens", "tokens.txt"))

        if engine == "moonshine_v2":
            return sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
                encoder=_file(root, detail["encoder"]),
                decoder=_file(root, detail["decoder"]),
                tokens=tokens,
                num_threads=self.threads,
            )
        if engine == "sense_voice":
            return sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=_file(root, detail["model"]),
                tokens=tokens,
                num_threads=self.threads,
                use_itn=True,
            )
        if engine == "whisper":
            return sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=_file(root, detail["encoder"]),
                decoder=_file(root, detail["decoder"]),
                tokens=tokens,
                num_threads=self.threads,
            )
        raise VoiceUnavailable(f"Unsupported recognition engine {engine!r}.")

    async def warm_up(self) -> None:
        async with self._lock:
            if self._recognizer is None:
                self._recognizer = await asyncio.to_thread(self._load)

    async def transcribe(self, audio: np.ndarray) -> Transcript:
        import time

        if audio.size == 0:
            return Transcript("")
        async with self._lock:
            if self._recognizer is None:
                self._recognizer = await asyncio.to_thread(self._load)
            recognizer = self._recognizer

        started = time.monotonic()

        def run() -> str:
            stream = recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio)
            recognizer.decode_stream(stream)
            return (stream.result.text or "").strip()

        text = await asyncio.to_thread(run)
        return Transcript(
            text=text,
            audio_seconds=len(audio) / SAMPLE_RATE,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


class LocalTTS(TextToSpeech):
    """Offline synthesis through sherpa-onnx."""

    def __init__(self, model_id: str, voice: int = 0, speed: float = 1.0, threads: int = 2):
        ok, why = sherpa_status()
        if not ok:
            raise VoiceUnavailable(why)
        model = TTS_MODELS.get(model_id) or TTS_MODELS["piper-amy"]
        self.model = model
        self.voice = max(0, voice)
        self.speed = min(max(speed, 0.5), 2.0)
        self.threads = max(1, threads)
        self._tts = None
        self._lock = asyncio.Lock()

    def voices(self) -> int:
        """How many speakers this model actually has.

        The catalog carries a number for the picker to show before anything is
        downloaded, but once the model is loaded the engine is authoritative.
        """
        if self._tts is not None:
            return max(1, int(self._tts.num_speakers))
        return int(self.model.detail.get("voices", 1))

    def voice_names(self) -> list[str]:
        """Speaker labels, if the model ships them.

        Most archives do not, so callers fall back to numbering. Reading a names
        file when it exists beats hard-coding an order that upstream can change.
        """
        try:
            root = install_path(self.model)
        except Exception:
            return []
        for name in ("voices.txt", "speakers.txt", "voice_names.txt"):
            candidate = root / name
            if candidate.is_file():
                try:
                    labels = [
                        line.strip() for line in
                        candidate.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                except OSError:
                    return []
                if labels:
                    return labels
        return []

    def _load(self):
        import sherpa_onnx

        root = _require(self.model)
        detail = self.model.detail
        engine = self.model.engine
        model_config = sherpa_onnx.OfflineTtsModelConfig(num_threads=self.threads)
        tokens = _file(root, detail.get("tokens", "tokens.txt"))

        if engine == "vits":
            model_config.vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=_file(root, detail["model"]),
                tokens=tokens,
                data_dir=_file(root, detail["data_dir"]),
            )
        elif engine == "kitten":
            model_config.kitten = sherpa_onnx.OfflineTtsKittenModelConfig(
                model=_file(root, detail["model"]),
                voices=_file(root, detail["voices_file"]),
                tokens=tokens,
                data_dir=_file(root, detail["data_dir"]),
            )
        elif engine == "kokoro":
            lexicons = ",".join(
                _file(root, name) for name in detail.get("lexicon", "").split(",") if name
            )
            model_config.kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=_file(root, detail["model"]),
                voices=_file(root, detail["voices_file"]),
                tokens=tokens,
                data_dir=_file(root, detail["data_dir"]),
                dict_dir=_file(root, detail["dict_dir"]),
                lexicon=lexicons,
            )
        else:
            raise VoiceUnavailable(f"Unsupported speech engine {engine!r}.")

        tts = sherpa_onnx.OfflineTts(
            sherpa_onnx.OfflineTtsConfig(model=model_config, max_num_sentences=1)
        )
        self.sample_rate = tts.sample_rate
        return tts

    async def warm_up(self) -> None:
        async with self._lock:
            if self._tts is None:
                self._tts = await asyncio.to_thread(self._load)

    async def synthesize(self, text: str) -> np.ndarray:
        cleaned = (text or "").strip()
        if not cleaned:
            return np.zeros(0, dtype=np.float32)
        async with self._lock:
            if self._tts is None:
                self._tts = await asyncio.to_thread(self._load)
            tts = self._tts

        def run() -> np.ndarray:
            audio = tts.generate(cleaned, sid=self.voice, speed=self.speed)
            return np.asarray(audio.samples, dtype=np.float32)

        return await asyncio.to_thread(run)


class LocalVAD:
    """Silero voice-activity detection, used for endpointing and barge-in.

    Wrapped rather than used directly so the conversation loop deals in
    "did a segment just finish" instead of sherpa-onnx's queue mechanics.
    """

    WINDOW = 512  # samples Silero expects per step at 16 kHz

    def __init__(self, silence_seconds: float = 0.6, threshold: float = 0.5):
        ok, why = sherpa_status()
        if not ok:
            raise VoiceUnavailable(why)
        import sherpa_onnx

        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(_require(VAD))
        config.silero_vad.threshold = threshold
        config.silero_vad.min_silence_duration = max(0.15, silence_seconds)
        config.silero_vad.min_speech_duration = 0.2
        config.sample_rate = SAMPLE_RATE
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)
        self._tail = np.zeros(0, dtype=np.float32)

    def feed(self, chunk: np.ndarray) -> list[np.ndarray]:
        """Push audio in; get back any utterances that just ended."""
        buffer = np.concatenate([self._tail, chunk]) if self._tail.size else chunk
        usable = (buffer.size // self.WINDOW) * self.WINDOW
        self._tail = buffer[usable:].copy()
        for start in range(0, usable, self.WINDOW):
            self._vad.accept_waveform(buffer[start:start + self.WINDOW])
        return self._drain()

    def flush(self) -> list[np.ndarray]:
        self._vad.flush()
        self._tail = np.zeros(0, dtype=np.float32)
        return self._drain()

    def _drain(self) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        while not self._vad.empty():
            out.append(np.asarray(self._vad.front.samples, dtype=np.float32))
            self._vad.pop()
        return out

    @property
    def speech_detected(self) -> bool:
        """True while the user is mid-utterance — this is what triggers barge-in."""
        return bool(self._vad.is_speech_detected())

    def reset(self) -> None:
        self._vad.reset()
        self._tail = np.zeros(0, dtype=np.float32)


# --------------------------------------------------------------------------- #
# Cloud
# --------------------------------------------------------------------------- #


class CloudSTT(SpeechToText):
    """Any OpenAI-compatible transcription endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str = "whisper-1"):
        if not api_key:
            raise VoiceUnavailable("This transcription service needs an API key.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def transcribe(self, audio: np.ndarray) -> Transcript:
        import time

        if audio.size == 0:
            return Transcript("")
        started = time.monotonic()
        payload = encode_wav(audio, SAMPLE_RATE)

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise VoiceUnavailable("The `openai` package is required for cloud speech.") from exc

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=60)
        try:
            result = await client.audio.transcriptions.create(
                model=self.model, file=("speech.wav", payload, "audio/wav")
            )
            text = (getattr(result, "text", "") or "").strip()
        except Exception as exc:  # network or auth failure
            raise VoiceUnavailable(f"Transcription failed: {exc}") from exc
        finally:
            await client.close()

        return Transcript(text, len(audio) / SAMPLE_RATE, int((time.monotonic() - started) * 1000))


class CloudTTS(TextToSpeech):
    """Any OpenAI-compatible speech endpoint."""

    sample_rate = 24_000

    def __init__(self, base_url: str, api_key: str, model: str = "gpt-4o-mini-tts",
                 voice: str = "alloy", speed: float = 1.0):
        if not api_key:
            raise VoiceUnavailable("This speech service needs an API key.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.speed = min(max(speed, 0.5), 2.0)

    async def synthesize(self, text: str) -> np.ndarray:
        cleaned = (text or "").strip()
        if not cleaned:
            return np.zeros(0, dtype=np.float32)
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise VoiceUnavailable("The `openai` package is required for cloud speech.") from exc

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=60)
        try:
            response = await client.audio.speech.create(
                model=self.model, voice=self.voice, input=cleaned,
                response_format="wav", speed=self.speed,
            )
            raw = response.content if hasattr(response, "content") else await response.aread()
        except Exception as exc:
            raise VoiceUnavailable(f"Speech synthesis failed: {exc}") from exc
        finally:
            await client.close()

        samples, rate = decode_wav(raw)
        self.sample_rate = rate
        return samples


# --------------------------------------------------------------------------- #
# Audio helpers
# --------------------------------------------------------------------------- #


def encode_wav(samples: np.ndarray, rate: int) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def decode_wav(payload: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(payload), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
        width = handle.getsampwidth()
        channels = handle.getnchannels()
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(width, np.int16)
    data = np.frombuffer(frames, dtype=dtype).astype(np.float32)
    data /= float(np.iinfo(dtype).max)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Linear resampling.

    Good enough at these rates and, unlike scipy or librosa, costs nothing to
    install — which matters when the whole point is a small footprint.
    """
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    length = round(samples.size * target_rate / source_rate)
    if length <= 0:
        return np.zeros(0, dtype=np.float32)
    source_index = np.linspace(0, samples.size - 1, length, dtype=np.float64)
    return np.interp(source_index, np.arange(samples.size), samples).astype(np.float32)

