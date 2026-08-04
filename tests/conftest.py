"""Shared fixtures.

Every test runs against throwaway config and workspace directories, set before
``arcbot`` is imported so the real user config is never touched.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_CONFIG = Path(tempfile.mkdtemp(prefix="arcbot-test-config-"))
os.environ.setdefault("ARCBOT_CONFIG_DIR", str(_CONFIG))
os.environ.setdefault("ARCBOT_DATA_DIR", str(_CONFIG))
# Models normally live beside the app, which for a source checkout is this
# repository — a test run has no business creating folders there.
os.environ.setdefault("ARCBOT_MODELS_DIR", str(_CONFIG / "voice-models"))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def store(tmp_path: Path, workspace: Path):
    from arcbot.config import ConfigStore, Settings

    store = ConfigStore(tmp_path / "config.json", tmp_path / "credentials.json")
    settings = Settings()
    settings.workspace = str(workspace)
    settings.onboarded = True
    settings.toolsets = ["core", "files", "shell"]
    settings.permissions.mode = "trusted"
    settings.model.provider = "openai"
    settings.model.model = "test-model"
    store.save(settings)
    return store


@pytest.fixture
def bus():
    from arcbot.events import EventBus

    return EventBus()


@pytest.fixture
def recorder(bus):
    """Collects every event the agent emits, for assertions."""
    events: list[tuple[str, dict]] = []

    async def sink(event) -> None:
        events.append((event.type, event.data))

    bus.subscribe(sink)
    return events
