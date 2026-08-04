"""Hands-free voice mode.

Everything runs locally by default: recognition, synthesis and voice-activity
detection all come from one small `sherpa-onnx` wheel with no PyTorch, and the
default model pair is about 52 MB. Cloud engines are available for anyone who
would rather not download anything.

Nothing in this package is imported until the user turns voice mode on.
"""

from .catalog import STT_MODELS, TTS_MODELS, VAD, catalog_payload, required_download_mb
from .engines import VoiceUnavailable, sherpa_available
from .session import VoiceSession, VoiceState

__all__ = [
    "STT_MODELS",
    "TTS_MODELS",
    "VAD",
    "VoiceSession",
    "VoiceState",
    "VoiceUnavailable",
    "catalog_payload",
    "required_download_mb",
    "sherpa_available",
]
