"""The voice models ArcBot can download, and what they cost you.

Everything here runs on a CPU. The default pairing is about 52 MB on disk and
transcribes at roughly 100x real time on a laptop core, which is what makes
hands-free use feel immediate rather than like waiting on a server.

Nothing is downloaded until the user turns voice mode on and picks a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: sherpa-onnx publishes its model archives as GitHub release assets.
_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class VoiceModel:
    id: str
    name: str
    url: str
    #: Compressed download size in megabytes, for the "this will cost you" line.
    size_mb: float
    #: Directory the archive extracts to, or "" for a bare file.
    folder: str = ""
    language: str = "English"
    note: str = ""
    #: Shown in the picker as the recommended choice.
    default: bool = False
    #: Engine family, which decides how it is loaded.
    engine: str = ""
    #: Extra per-engine details (file names inside the folder, voice count…).
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_archive(self) -> bool:
        return self.url.endswith((".tar.bz2", ".tar.gz", ".tgz"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "sizeMb": self.size_mb,
            "language": self.language,
            "note": self.note,
            "default": self.default,
            "engine": self.engine,
            "voices": self.detail.get("voices", 1),
        }


# --------------------------------------------------------------------------- #
# Voice activity detection — always needed, and tiny
# --------------------------------------------------------------------------- #

VAD = VoiceModel(
    id="silero-vad",
    name="Silero VAD",
    url=f"{_BASE}/asr-models/silero_vad.onnx",
    size_mb=0.6,
    note="Detects when you start and stop speaking.",
    engine="silero",
)


# --------------------------------------------------------------------------- #
# Speech recognition
# --------------------------------------------------------------------------- #

STT_MODELS: dict[str, VoiceModel] = {
    m.id: m for m in [
        VoiceModel(
            id="moonshine-tiny-en",
            name="Moonshine Tiny",
            url=f"{_BASE}/asr-models/sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27.tar.bz2",
            size_mb=29.9,
            folder="sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27",
            language="English",
            note="Fastest. Transcribes about 100x faster than real time on one core.",
            default=True,
            engine="moonshine_v2",
            detail={"encoder": "encoder_model.ort", "decoder": "decoder_model_merged.ort"},
        ),
        VoiceModel(
            id="moonshine-base-en",
            name="Moonshine Base",
            url=f"{_BASE}/asr-models/sherpa-onnx-moonshine-base-en-quantized-2026-02-27.tar.bz2",
            size_mb=111.3,
            folder="sherpa-onnx-moonshine-base-en-quantized-2026-02-27",
            language="English",
            note="More accurate on accents and noise. Still comfortably real time.",
            engine="moonshine_v2",
            detail={"encoder": "encoder_model.ort", "decoder": "decoder_model_merged.ort"},
        ),
        VoiceModel(
            id="sense-voice",
            name="SenseVoice",
            url=f"{_BASE}/asr-models/"
                f"sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
            size_mb=163.0,
            folder="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
            language="English, Chinese, Japanese, Korean, Cantonese",
            note="Pick this if you speak anything other than English.",
            engine="sense_voice",
            detail={"model": "model.int8.onnx"},
        ),
        VoiceModel(
            id="whisper-tiny",
            name="Whisper Tiny",
            url=f"{_BASE}/asr-models/sherpa-onnx-whisper-tiny.tar.bz2",
            size_mb=116.2,
            folder="sherpa-onnx-whisper-tiny",
            language="Multilingual (99 languages)",
            note="Widest language coverage, lower accuracy than the others.",
            engine="whisper",
            # Whisper archives prefix every file with the size, tokens included.
            detail={"encoder": "tiny-encoder.int8.onnx", "decoder": "tiny-decoder.int8.onnx",
                    "tokens": "tiny-tokens.txt"},
        ),
    ]
}


# --------------------------------------------------------------------------- #
# Speech synthesis
# --------------------------------------------------------------------------- #

TTS_MODELS: dict[str, VoiceModel] = {
    m.id: m for m in [
        VoiceModel(
            id="piper-amy",
            name="Piper · Amy",
            url=f"{_BASE}/tts-models/vits-piper-en_US-amy-low-int8.tar.bz2",
            size_mb=21.1,
            folder="vits-piper-en_US-amy-low-int8",
            language="English (US)",
            note="Fastest and smallest. Speaks about 9x faster than real time.",
            default=True,
            engine="vits",
            detail={"model": "en_US-amy-low.onnx", "data_dir": "espeak-ng-data", "voices": 1},
        ),
        VoiceModel(
            id="piper-alan",
            name="Piper · Alan",
            url=f"{_BASE}/tts-models/vits-piper-en_GB-alan-low-int8.tar.bz2",
            size_mb=21.3,
            folder="vits-piper-en_GB-alan-low-int8",
            language="English (UK)",
            note="Same engine as Amy, a male British voice.",
            engine="vits",
            detail={"model": "en_GB-alan-low.onnx", "data_dir": "espeak-ng-data", "voices": 1},
        ),
        VoiceModel(
            id="kitten-nano",
            name="KittenTTS Nano",
            url=f"{_BASE}/tts-models/kitten-nano-en-v0_2-fp16.tar.bz2",
            size_mb=26.6,
            folder="kitten-nano-en-v0_2-fp16",
            language="English",
            note="Very small, 8 voices. Slower than Piper for a similar download.",
            engine="kitten",
            detail={"model": "model.fp16.onnx", "voices_file": "voices.bin",
                    "data_dir": "espeak-ng-data", "voices": 8},
        ),
        VoiceModel(
            id="kokoro",
            name="Kokoro",
            url=f"{_BASE}/tts-models/kokoro-int8-multi-lang-v1_0.tar.bz2",
            size_mb=131.8,
            folder="kokoro-int8-multi-lang-v1_0",
            language="English, Chinese and more",
            note="The most natural of the local voices, with 53 to choose from. "
                 "A bigger download and slower, but still real time on a modern CPU.",
            engine="kokoro",
            detail={"model": "model.int8.onnx", "voices_file": "voices.bin",
                    "data_dir": "espeak-ng-data", "dict_dir": "dict",
                    "lexicon": "lexicon-us-en.txt,lexicon-zh.txt", "voices": 53},
        ),
    ]
}


# --------------------------------------------------------------------------- #
# Cloud engines — for anyone who would rather not download anything
# --------------------------------------------------------------------------- #

CLOUD_STT = {
    "openai": {
        "name": "OpenAI Whisper API",
        "keyEnv": "OPENAI_API_KEY",
        "default_model": "whisper-1",
        "base_url": "https://api.openai.com/v1",
        "note": "Audio is sent to OpenAI. Costs about $0.006 a minute.",
    },
    "groq": {
        "name": "Groq Whisper",
        "keyEnv": "GROQ_API_KEY",
        "default_model": "whisper-large-v3-turbo",
        "base_url": "https://api.groq.com/openai/v1",
        "note": "Very fast, and cheaper than OpenAI. Audio leaves your machine.",
    },
}

CLOUD_TTS = {
    "openai": {
        "name": "OpenAI TTS",
        "keyEnv": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini-tts",
        "base_url": "https://api.openai.com/v1",
        "voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer", "coral", "sage"],
        "note": "The most natural option overall. Text is sent to OpenAI.",
    },
}


def default_stt() -> VoiceModel:
    return next(m for m in STT_MODELS.values() if m.default)


def default_tts() -> VoiceModel:
    return next(m for m in TTS_MODELS.values() if m.default)


def required_download_mb(stt_id: str, tts_id: str) -> float:
    """What turning voice mode on will actually cost in disk and bandwidth."""
    total = VAD.size_mb
    stt = STT_MODELS.get(stt_id)
    tts = TTS_MODELS.get(tts_id)
    if stt:
        total += stt.size_mb
    if tts:
        total += tts.size_mb
    return round(total, 1)


def catalog_payload() -> dict[str, Any]:
    return {
        "stt": [m.to_dict() for m in STT_MODELS.values()],
        "tts": [m.to_dict() for m in TTS_MODELS.values()],
        "vad": VAD.to_dict(),
        "cloudStt": CLOUD_STT,
        "cloudTts": CLOUD_TTS,
    }
