"""Downloading voice models, only when the user asks for them.

Nothing here runs at import time and nothing is fetched until voice mode is
switched on and a model is chosen.  Downloads report progress, resume-safe by
writing to a temporary file and renaming on success, and a half-finished
download never leaves a directory that looks valid.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get_logger
from ..paths import data_dir, ensure_dir
from .catalog import STT_MODELS, TTS_MODELS, VAD, VoiceModel

log = get_logger("voice.models")

CHUNK = 262_144
#: Marker written after a successful extraction, so a partial unpack is not
#: mistaken for a usable model.
READY = ".arcbot-ready"
#: Where models used to live.  Kept so an existing install is moved rather than
#: re-downloaded.
LEGACY_DIR = "voice-models"

Progress = Callable[[str, float, str], Awaitable[None]]

_migrated = False


def _app_root() -> Path | None:
    """The checkout ArcBot is running from, if models can be kept beside it.

    Running from source, models next to the app are easy to find, easy to
    delete and travel with the folder the user already knows about.  An
    installed copy lives in ``site-packages``, where hundreds of megabytes have
    no business going, so that case falls back to the platform data directory.
    """
    root = Path(__file__).resolve().parents[2]
    if {p.lower() for p in root.parts} & {"site-packages", "dist-packages"}:
        return None
    if not (root / "pyproject.toml").is_file():
        return None
    return root if os.access(root, os.W_OK) else None


def models_root() -> Path:
    """The folder holding every downloaded model, without touching the disk."""
    override = os.environ.get("ARCBOT_MODELS_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    root = _app_root()
    return root / "models" / "voice" if root else data_dir() / LEGACY_DIR


def models_dir() -> Path:
    directory = ensure_dir(models_root())
    _claim_from_git(directory.parent)
    _migrate_legacy(directory)
    _sweep_partials(directory)
    return directory


def _claim_from_git(models: Path) -> None:
    """Keep a source checkout clean — downloaded weights are not source."""
    marker = models / ".gitignore"
    if models.name != "models" or marker.exists():
        return
    try:
        marker.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


def _migrate_legacy(target: Path) -> None:
    """Move an older install into the new location instead of re-fetching it.

    Runs at most once per process and never overwrites: anything already at the
    destination wins, and a failure leaves the old copy exactly where it was.
    """
    global _migrated
    if _migrated:
        return
    _migrated = True

    legacy = data_dir() / LEGACY_DIR
    if legacy == target or not legacy.is_dir():
        return
    for entry in legacy.iterdir():
        if entry.name.startswith(".dl-") or entry.name.endswith(".unpacking"):
            # Scratch from a download that never finished — worth nothing.
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            continue
        destination = target / entry.name
        if destination.exists():
            continue
        try:
            shutil.move(str(entry), str(destination))
            log.info("Moved voice model %s to %s", entry.name, target)
        except OSError as exc:
            log.warning("Could not move %s (%s); leaving it in place.", entry.name, exc)
            return
    try:
        legacy.rmdir()
    except OSError:
        pass


def _sweep_partials(directory: Path) -> None:
    """Clear scratch files left by a download that was killed rather than failed.

    ``finally`` never runs on SIGKILL, so without this a hard stop mid-download
    leaks a temp file every time.
    """
    import time

    cutoff = time.time() - 3600
    for path in directory.glob(".dl-*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
    for path in directory.glob("*.unpacking"):
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def install_path(model: VoiceModel) -> Path:
    """Where this model lives once installed."""
    base = models_dir()
    if model.is_archive:
        return base / (model.folder or model.id)
    return base / Path(model.url).name


def is_installed(model: VoiceModel) -> bool:
    target = install_path(model)
    if model.is_archive:
        return (target / READY).exists()
    return target.is_file() and target.stat().st_size > 0


@dataclass
class InstallReport:
    installed: list[str]
    skipped: list[str]
    failed: dict[str, str]

    @property
    def ok(self) -> bool:
        return not self.failed


def kind_of(model: VoiceModel) -> str:
    if model.id in STT_MODELS:
        return "stt"
    if model.id in TTS_MODELS:
        return "tts"
    return "vad"


def required_models(stt_id: str, tts_id: str) -> list[VoiceModel]:
    """The VAD plus whichever of the chosen pair run locally."""
    wanted = [VAD]
    stt = STT_MODELS.get(stt_id)
    tts = TTS_MODELS.get(tts_id)
    if stt:
        wanted.append(stt)
    if tts:
        wanted.append(tts)
    return wanted


def resolve(kind: str, model_id: str) -> VoiceModel | None:
    if kind == "stt":
        return STT_MODELS.get(model_id)
    if kind == "tts":
        return TTS_MODELS.get(model_id)
    if kind == "vad":
        return VAD
    return None


def installed_ids() -> dict[str, list[str]]:
    return {
        "stt": [m.id for m in STT_MODELS.values() if is_installed(m)],
        "tts": [m.id for m in TTS_MODELS.values() if is_installed(m)],
        "vad": [VAD.id] if is_installed(VAD) else [],
    }


def disk_usage_mb() -> float:
    total = 0
    for path in models_dir().rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return round(total / 1_000_000, 1)


def remove(model: VoiceModel) -> bool:
    target = install_path(model)
    if model.is_archive and target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        return True
    if target.is_file():
        target.unlink()
        return True
    return False


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #


def _download_blocking(model: VoiceModel, report: Callable[[float, str], None]) -> Path:
    """Fetch and unpack one model.  Runs in a worker thread."""
    target = install_path(model)
    if is_installed(model):
        return target

    ensure_dir(models_dir())
    suffix = ".tar.bz2" if model.is_archive else Path(model.url).suffix
    fd, tmp_name = tempfile.mkstemp(dir=str(models_dir()), prefix=".dl-", suffix=suffix)
    tmp = Path(tmp_name)

    try:
        with open(fd, "wb") as handle:
            fd = -1  # ownership passes to the file object
            _fetch(model, handle, report)
        if model.is_archive:
            report(0.92, f"Unpacking {model.name}")
            _extract(tmp, target)
            (target / READY).write_text(model.id, encoding="utf-8")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.replace(target)
        report(1.0, f"{model.name} ready")
        return target
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp.unlink(missing_ok=True)


#: A model download is tens to hundreds of megabytes, so a single blip should
#: not lose the whole thing.
ATTEMPTS = 3
CONNECT_TIMEOUT = 30


def _fetch(model: VoiceModel, handle, report: Callable[[float, str], None]) -> None:
    """Stream the model into *handle*, retrying a stalled connection."""
    last_error: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        handle.seek(0)
        handle.truncate()
        try:
            request = urllib.request.Request(
                model.url, headers={"User-Agent": "ArcBot/1.0 (+voice-models)"}
            )
            with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                while chunk := response.read(CHUNK):
                    handle.write(chunk)
                    done += len(chunk)
                    if total:
                        report(done / total * 0.9, f"Downloading {model.name}")
            if total and done < total:
                raise OSError(f"connection closed after {done:,} of {total:,} bytes")
            return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_error = exc
            if attempt < ATTEMPTS:
                log.info("Retrying %s (attempt %d): %s", model.id, attempt + 1, exc)
                report(0.0, f"Retrying {model.name} ({attempt + 1} of {ATTEMPTS})")
                time.sleep(2 * attempt)
    raise OSError(
        f"could not download after {ATTEMPTS} attempts: {last_error}. "
        f"Check your connection, or download it manually into {models_dir()}"
    )


def _extract(archive: Path, target: Path) -> None:
    """Unpack into a scratch directory, then swap it in.

    An archive is third-party data, so every member is checked for a path that
    escapes the destination before anything is written.
    """
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    staging = target.with_name(target.name + ".unpacking")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    try:
        with tarfile.open(archive, "r:*") as tar:
            root = staging.resolve()
            members = []
            for member in tar.getmembers():
                destination = (staging / member.name).resolve()
                if destination != root and root not in destination.parents:
                    raise ValueError(f"archive entry escapes the target: {member.name!r}")
                if member.issym() or member.islnk():
                    continue  # a model archive has no business containing links
                members.append(member)
            tar.extractall(staging, members=members)

        # Archives wrap everything in one folder; lift it so paths are predictable.
        entries = [p for p in staging.iterdir() if not p.name.startswith(".")]
        if len(entries) == 1 and entries[0].is_dir():
            entries[0].replace(target)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        raise


async def install(
    models: list[VoiceModel],
    on_progress: Progress | None = None,
) -> InstallReport:
    """Download whatever is missing, reporting progress as it goes."""
    report = InstallReport([], [], {})
    loop = asyncio.get_running_loop()

    for index, model in enumerate(models):
        if is_installed(model):
            report.skipped.append(model.id)
            continue

        async def emit(fraction: float, label: str, _m=model, _i=index) -> None:
            if on_progress:
                overall = (_i + fraction) / max(1, len(models))
                await on_progress(_m.id, overall, label)

        def sync_report(fraction: float, label: str) -> None:
            asyncio.run_coroutine_threadsafe(emit(fraction, label), loop)

        try:
            await asyncio.to_thread(_download_blocking, model, sync_report)
            report.installed.append(model.id)
            log.info("Installed voice model %s", model.id)
        except (urllib.error.URLError, OSError, ValueError, tarfile.TarError) as exc:
            message = str(exc)[:200]
            report.failed[model.id] = message
            log.warning("Voice model %s failed: %s", model.id, message)
            if on_progress:
                await on_progress(model.id, 1.0, f"{model.name} failed: {message}")

    return report


async def ensure_ready(
    stt_id: str, tts_id: str, on_progress: Progress | None = None
) -> InstallReport:
    """Make sure the VAD plus the chosen pair are on disk."""
    return await install(required_models(stt_id, tts_id), on_progress)
