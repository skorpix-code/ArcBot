"""The provider catalog and factory.

The catalog is what the onboarding wizard renders, so each entry carries the
copy a first-time user needs: what it is, whether it costs money, whether it
runs locally, and exactly how to authenticate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ..config import ConfigStore, Settings
from ..logging_setup import get_logger
from . import auth
from .base import Chunk, Provider, ProviderError, ToolCall, Usage

log = get_logger("providers")

__all__ = [
    "CATALOG", "Chunk", "Provider", "ProviderError", "ProviderSpec", "ToolCall",
    "Usage", "build_provider", "catalog_payload", "describe_auth", "discover_models",
]


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    tagline: str
    #: "subscription" | "api-key" | "local" | "oauth"
    auth_kind: str
    #: Environment variable holding the key, when one is used.
    key_env: str = ""
    default_base_url: str = ""
    #: Models offered in the picker before live discovery runs.
    suggested_models: tuple = ()
    default_model: str = ""
    local: bool = False
    #: Runs its own agent loop rather than exposing a raw model.
    agentic: bool = False
    setup_steps: tuple = ()
    docs_url: str = ""
    badge: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "authKind": self.auth_kind,
            "keyEnv": self.key_env,
            "defaultBaseUrl": self.default_base_url,
            "suggestedModels": list(self.suggested_models),
            "defaultModel": self.default_model,
            "local": self.local,
            "agentic": self.agentic,
            "setupSteps": list(self.setup_steps),
            "docsUrl": self.docs_url,
            "badge": self.badge,
        }


CATALOG: dict[str, ProviderSpec] = {
    "claude-code": ProviderSpec(
        id="claude-code",
        name="Claude (your subscription)",
        tagline="Sign in with your Anthropic account — no API key, no per-token billing.",
        auth_kind="subscription",
        suggested_models=("", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
        default_model="",
        agentic=True,
        badge="Recommended",
        setup_steps=(
            "Install Claude Code:  npm install -g @anthropic-ai/claude-code",
            "Run `claude` once in a terminal and sign in with your Anthropic account.",
            "Come back here — ArcBot picks the login up automatically.",
        ),
        docs_url="https://code.claude.com/docs",
    ),
    "anthropic": ProviderSpec(
        id="anthropic",
        name="Claude (API key)",
        tagline="Pay-as-you-go access to the Claude API.",
        auth_kind="api-key",
        key_env="ANTHROPIC_API_KEY",
        suggested_models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
        default_model="claude-sonnet-5",
        setup_steps=("Create a key at console.anthropic.com and paste it below.",),
        docs_url="https://platform.claude.com/docs",
    ),
    "openai": ProviderSpec(
        id="openai",
        name="OpenAI",
        tagline="GPT models via the OpenAI API.",
        auth_kind="api-key",
        key_env="OPENAI_API_KEY",
        default_base_url="https://api.openai.com/v1",
        suggested_models=("gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o", "o4-mini"),
        default_model="gpt-4.1",
        setup_steps=("Create a key at platform.openai.com and paste it below.",),
    ),
    "gemini": ProviderSpec(
        id="gemini",
        name="Google Gemini",
        tagline="Gemini models, with a generous free tier.",
        auth_kind="api-key",
        key_env="GEMINI_API_KEY",
        suggested_models=("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"),
        default_model="gemini-2.5-flash",
        setup_steps=("Create a key at aistudio.google.com and paste it below.",),
    ),
    "lmstudio": ProviderSpec(
        id="lmstudio",
        name="LM Studio",
        tagline="Run a model entirely on your own machine. No key, no cloud.",
        auth_kind="local",
        default_base_url="http://localhost:1234/v1",
        default_model="",
        local=True,
        badge="Private",
        setup_steps=(
            "Open LM Studio, load a model, and start its local server.",
            "ArcBot detects it on http://localhost:1234 automatically.",
        ),
    ),
    "ollama": ProviderSpec(
        id="ollama",
        name="Ollama",
        tagline="Local models from the terminal. Pull one, point ArcBot at it, go.",
        auth_kind="local",
        default_base_url="http://localhost:11434/v1",
        default_model="",
        local=True,
        badge="Private",
        setup_steps=(
            "Install Ollama, then pull a model:  ollama pull qwen3",
            "Ollama serves on http://localhost:11434 by default.",
        ),
    ),
    "openrouter": ProviderSpec(
        id="openrouter",
        name="OpenRouter",
        tagline="One key, hundreds of models from every major provider.",
        auth_kind="api-key",
        key_env="OPENROUTER_API_KEY",
        default_base_url="https://openrouter.ai/api/v1",
        suggested_models=("anthropic/claude-sonnet-4.5", "openai/gpt-4.1", "google/gemini-2.5-pro"),
        default_model="anthropic/claude-sonnet-4.5",
        setup_steps=("Create a key at openrouter.ai/keys and paste it below.",),
    ),
    "custom": ProviderSpec(
        id="custom",
        name="Custom endpoint",
        tagline="Any OpenAI-compatible server — vLLM, llama.cpp, LiteLLM, a proxy.",
        auth_kind="local",
        key_env="ARCBOT_API_KEY",
        default_base_url="http://localhost:8000/v1",
        setup_steps=("Enter the base URL of your OpenAI-compatible server.",),
    ),
}

#: Providers that speak the OpenAI wire format.
_OPENAI_COMPATIBLE = {"openai", "lmstudio", "ollama", "openrouter", "custom"}


def get_spec(provider_id: str) -> ProviderSpec | None:
    return CATALOG.get(provider_id)


def catalog_payload() -> list[dict[str, Any]]:
    return [spec.to_dict() for spec in CATALOG.values()]


# --------------------------------------------------------------------------- #
# Auth reporting
# --------------------------------------------------------------------------- #


def describe_auth(provider_id: str, store: ConfigStore) -> dict[str, Any]:
    """What credentials this provider has right now — never the secret itself."""
    spec = get_spec(provider_id)
    if spec is None:
        return auth.AuthStatus(False, "none", f"Unknown provider {provider_id!r}.").to_dict()

    if spec.id == "claude-code":
        return auth.claude_code_status().to_dict()

    if spec.id == "anthropic":
        status = auth.api_key_status(spec.key_env, store)
        if status.available:
            return status.to_dict()
        ant = auth.ant_status()
        if ant.available:
            return ant.to_dict()
        status.hint = (
            "Paste an API key, run `ant auth login`, or switch to "
            "“Claude (your subscription)” to use Claude Code instead."
        )
        return status.to_dict()

    if spec.local and not spec.key_env:
        base_url = _resolve_base_url(spec, None)
        reachable, models = auth.probe_openai_endpoint(base_url)
        if reachable:
            detail = f"Server reachable — {len(models)} model(s) available."
            return auth.AuthStatus(True, "local", detail).to_dict()
        return auth.AuthStatus(
            False, "none", f"Nothing is listening on {base_url}.",
            hint=spec.setup_steps[0] if spec.setup_steps else "",
        ).to_dict()

    if spec.key_env:
        status = auth.api_key_status(spec.key_env, store)
        if not status.available and spec.local:
            return auth.AuthStatus(True, "local", "No key needed for a local server.").to_dict()
        return status.to_dict()

    return auth.AuthStatus(True, "none", "No credentials required.").to_dict()


def discover_models(provider_id: str, store: ConfigStore, base_url: str = "") -> list[str]:
    """Ask the endpoint what it can actually serve, falling back to suggestions."""
    spec = get_spec(provider_id)
    if spec is None:
        return []
    if spec.id in _OPENAI_COMPATIBLE:
        url = base_url or _resolve_base_url(spec, None)
        reachable, models = auth.probe_openai_endpoint(url, timeout=4.0)
        if reachable and models:
            return models
    return list(spec.suggested_models)


def _resolve_base_url(spec: ProviderSpec, settings: Settings | None) -> str:
    if settings and settings.model.base_url:
        return settings.model.base_url
    override = os.environ.get(f"ARCBOT_{spec.id.upper()}_URL", "").strip()
    return override or spec.default_base_url


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def build_provider(settings: Settings, store: ConfigStore) -> Provider:
    """Construct the configured provider, or raise a :class:`ProviderError`
    whose message tells the user exactly what to fix."""
    spec = get_spec(settings.model.provider)
    if spec is None:
        raise ProviderError(
            f"No provider called {settings.model.provider!r} is configured.",
            hint="Pick a provider in Settings.",
        )
    model = settings.model.model or spec.default_model
    options = settings.model.options or {}

    if spec.id == "claude-code":
        from .claude_code_provider import ClaudeCodeProvider

        return ClaudeCodeProvider(
            model=model,
            workspace=str(settings.workspace_path),
            permission_mode=settings.permissions.mode,
            enabled_toolsets=settings.toolsets,
            allow_commands=settings.permissions.allow_commands,
            control_url=str(options.get("control_url") or ""),
            control_token=str(options.get("control_token") or ""),
            extra_args=list(options.get("extra_args") or []),
        )

    if spec.id == "anthropic":
        from .anthropic_provider import AnthropicProvider

        api_key = store.get_secret(spec.key_env)
        token = "" if api_key else (auth.ant_oauth_token() or "")
        if not api_key and not token:
            raise ProviderError(
                "No Anthropic credentials found.",
                hint="Add an API key in Settings, run `ant auth login`, or switch to "
                     "“Claude (your subscription)”.",
            )
        return AnthropicProvider(
            model=model or "claude-sonnet-5",
            api_key=api_key,
            auth_token=token,
            base_url=settings.model.base_url,
            effort=str(options.get("effort") or "high"),
            thinking=bool(options.get("thinking", True)),
        )

    if spec.id == "gemini":
        from .gemini_provider import GeminiProvider

        return GeminiProvider(model=model, api_key=store.get_secret(spec.key_env))

    if spec.id in _OPENAI_COMPATIBLE:
        from .openai_provider import OpenAIProvider

        base_url = settings.model.base_url or _resolve_base_url(spec, settings)
        api_key = store.get_secret(spec.key_env) if spec.key_env else ""
        if spec.key_env and not api_key and not spec.local:
            raise ProviderError(
                f"{spec.name} needs an API key.",
                hint=f"Add one in Settings, or set {spec.key_env} in your environment.",
            )
        if not model:
            raise ProviderError(
                "No model selected.",
                hint=f"Pick a model that {spec.name} has loaded.",
            )
        return OpenAIProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            context_window=int(options.get("context_window") or 128_000),
            temperature=options.get("temperature", 0.0),
        )

    raise ProviderError(f"Provider {spec.id!r} is not implemented.")
