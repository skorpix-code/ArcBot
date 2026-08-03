"""The local web UI (FastAPI + a single static page)."""

from .app import SESSION_TOKEN, app

__all__ = ["SESSION_TOKEN", "app"]
