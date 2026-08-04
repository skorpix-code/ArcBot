"""Voice mode.

The parts that decide whether hands-free feels natural are turn-taking and text
preparation, so those get the most attention here. The engines themselves are
covered by a round-trip test that only runs when the models are on disk.
"""

from __future__ import annotations

import asyncio

import pytest

np = pytest.importorskip("numpy")

from arcbot.voice import catalog  # noqa: E402
from arcbot.voice import models as vm  # noqa: E402
from arcbot.voice.session import (  # noqa: E402
    VoiceSession,
    VoiceState,
    _clean_transcript,
    prepare_input,
    speakable,
)

# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


class TestCatalog:
    def test_every_model_is_described_for_a_human(self):
        for group in (catalog.STT_MODELS, catalog.TTS_MODELS):
            for model in group.values():
                assert model.name and model.note, model.id
                assert model.size_mb > 0
                assert model.url.startswith("https://")
                assert model.engine

    def test_exactly_one_default_each(self):
        assert sum(m.default for m in catalog.STT_MODELS.values()) == 1
        assert sum(m.default for m in catalog.TTS_MODELS.values()) == 1

    def test_the_default_pair_stays_small(self):
        """The whole promise is that this runs on an ordinary laptop."""
        total = catalog.required_download_mb(
            catalog.default_stt().id, catalog.default_tts().id
        )
        assert total < 80, f"the default download grew to {total} MB"

    def test_archives_declare_where_they_extract_to(self):
        for group in (catalog.STT_MODELS, catalog.TTS_MODELS):
            for model in group.values():
                if model.is_archive:
                    assert model.folder, f"{model.id} has no extract folder"

    def test_payload_is_json_safe(self):
        import json

        json.dumps(catalog.catalog_payload())


# --------------------------------------------------------------------------- #
# Speaking text aloud
# --------------------------------------------------------------------------- #


class TestSpeakable:
    def test_code_blocks_are_summarised_not_read_out(self):
        spoken = speakable("Here you go:\n```python\nprint('hi')\n```\nThat works.")
        assert "print" not in spoken
        assert "code block" in spoken

    def test_urls_become_a_phrase(self):
        assert "http" not in speakable("See https://example.com/a/b?c=d for details")

    def test_long_paths_shrink_to_a_filename(self):
        spoken = speakable("I edited /home/someone/projects/thing/src/main.py just now")
        assert "main.py" in spoken
        assert "/home/someone" not in spoken

    def test_markdown_punctuation_is_dropped(self):
        spoken = speakable("**Bold** and _italic_ and `code`")
        assert "*" not in spoken and "_" not in spoken and "`" not in spoken
        assert "Bold" in spoken and "italic" in spoken and "code" in spoken

    def test_link_text_survives_but_the_target_does_not(self):
        assert speakable("[the docs](https://x.com/y)") == "the docs"

    def test_bullets_lose_their_markers(self):
        assert not speakable("- one\n- two").startswith("-")

    @pytest.mark.parametrize("junk", ["", "   ", "**", "```\n```", "#"])
    def test_formatting_only_chunks_are_not_spoken(self, junk):
        assert speakable(junk) == ""


class TestTranscriptCleaning:
    @pytest.mark.parametrize("raw", ["um, open the file", "Uh open the file"])
    def test_leading_filler_is_removed(self, raw):
        assert _clean_transcript(raw).lower().startswith("open")

    @pytest.mark.parametrize(
        "noise", ["you", "Thank you.", "hmm", "", "Thanks for watching!", "Please subscribe."]
    )
    def test_recogniser_hallucinations_are_discarded(self, noise):
        assert _clean_transcript(noise) == ""

    @pytest.mark.parametrize("real", ["yes", "no", "stop", "go on", "run the tests please"])
    def test_short_real_replies_survive(self, real):
        """The agent asks yes/no questions; dropping the answer would be worse
        than occasionally passing through a hallucination."""
        assert _clean_transcript(real) == real


# --------------------------------------------------------------------------- #
# Audio plumbing
# --------------------------------------------------------------------------- #


class TestAudio:
    def test_browser_pcm_becomes_engine_float(self):
        pcm = (np.sin(np.arange(4800) / 10) * 20000).astype("<i2").tobytes()
        out = prepare_input(pcm, 48_000)
        assert out.dtype == np.float32
        assert abs(out.size - 1600) <= 2, "48 kHz should become 16 kHz"
        assert float(np.abs(out).max()) <= 1.0

    def test_resampling_is_a_no_op_at_the_target_rate(self):
        from arcbot.voice.engines import resample

        data = np.linspace(-1, 1, 800, dtype=np.float32)
        assert resample(data, 16_000, 16_000).size == 800

    def test_wav_round_trip(self):
        from arcbot.voice.engines import decode_wav, encode_wav

        original = (np.sin(np.arange(1600) / 8) * 0.5).astype(np.float32)
        decoded, rate = decode_wav(encode_wav(original, 16_000))
        assert rate == 16_000
        assert decoded.size == original.size
        assert float(np.abs(decoded - original).max()) < 0.01


# --------------------------------------------------------------------------- #
# Turn taking
# --------------------------------------------------------------------------- #


class FakeSTT:
    def __init__(self, text="hello there"):
        self.text = text

    async def warm_up(self):
        pass

    async def transcribe(self, audio):
        from arcbot.voice.engines import Transcript

        return Transcript(self.text, audio.size / 16_000, 5)


class FakeTTS:
    sample_rate = 16_000

    def __init__(self):
        self.spoken: list[str] = []

    async def warm_up(self):
        pass

    async def synthesize(self, text):
        self.spoken.append(text)
        return np.zeros(1600, dtype=np.float32)

    def voices(self):
        return 1


class FakeVAD:
    """Returns whatever segments the test queues, so turn logic is testable
    without generating real speech."""

    def __init__(self):
        self.queued: list[np.ndarray] = []
        self.speech = False

    def feed(self, chunk):
        out, self.queued = self.queued, []
        return out

    def flush(self):
        return []

    def reset(self):
        self.queued = []

    @property
    def speech_detected(self):
        return self.speech


@pytest.fixture
def session(bus):
    return VoiceSession(bus=bus, stt=FakeSTT(), tts=FakeTTS(), vad=FakeVAD())


@pytest.mark.asyncio
class TestTurnTaking:
    async def test_a_finished_utterance_becomes_a_turn(self, session):
        heard = []
        session.on_utterance = lambda text: heard.append(text) or asyncio.sleep(0)
        session.vad.queued = [np.zeros(16_000, dtype=np.float32)]
        await session.feed(np.zeros(512, dtype=np.float32))
        assert heard == ["hello there"]

    async def test_a_blip_is_ignored(self, session):
        heard = []
        session.on_utterance = lambda text: heard.append(text) or asyncio.sleep(0)
        session.vad.queued = [np.zeros(800, dtype=np.float32)]   # 50 ms
        await session.feed(np.zeros(512, dtype=np.float32))
        assert heard == []

    async def test_silence_from_the_recogniser_does_not_start_a_turn(self, session):
        session.stt = FakeSTT("you")     # what a recogniser emits for noise
        heard = []
        session.on_utterance = lambda text: heard.append(text) or asyncio.sleep(0)
        session.vad.queued = [np.zeros(16_000, dtype=np.float32)]
        await session.feed(np.zeros(512, dtype=np.float32))
        assert heard == []
        assert session.state == VoiceState.LISTENING

    async def test_the_reply_is_spoken_sentence_by_sentence(self, session):
        session.reset_reply()
        await session.on_text("First part. ")
        await session.on_text("First part. Second part. ")
        await session.on_turn_end("First part. Second part. And the last bit.")
        await asyncio.sleep(0.2)
        assert session.tts.spoken == ["First part.", "Second part.", "And the last bit."]

    async def test_a_long_clause_does_not_stall_the_voice(self, session):
        session.reset_reply()
        await session.on_text("word " * 60)     # 300 chars, no sentence end
        await asyncio.sleep(0.1)
        assert session.tts.spoken, "a long run-on should still start speaking"

    async def test_nothing_is_spoken_twice(self, session):
        session.reset_reply()
        await session.on_text("One. ")
        await session.on_text("One. Two. ")
        await session.on_turn_end("One. Two.")
        await asyncio.sleep(0.2)
        assert session.tts.spoken.count("One.") == 1


@pytest.mark.asyncio
class TestBargeIn:
    async def test_loud_speech_during_playback_interrupts(self, session, recorder):
        await session.set_state(VoiceState.SPEAKING)
        session.vad.speech = True
        loud = np.full(512, 0.4, dtype=np.float32)
        await session.feed(loud)
        assert any(k == "voice.barge" for k, _ in recorder)
        assert session.state == VoiceState.LISTENING

    async def test_quiet_audio_during_playback_is_the_speakers(self, session, recorder):
        await session.set_state(VoiceState.SPEAKING)
        session.vad.speech = True
        await session.feed(np.full(512, 0.001, dtype=np.float32))
        assert not any(k == "voice.barge" for k, _ in recorder)

    async def test_barge_in_can_be_switched_off(self, session, recorder):
        session.barge_in = False
        await session.set_state(VoiceState.SPEAKING)
        session.vad.speech = True
        await session.feed(np.full(512, 0.4, dtype=np.float32))
        assert not any(k == "voice.barge" for k, _ in recorder)


# --------------------------------------------------------------------------- #
# Model management
# --------------------------------------------------------------------------- #


class TestModels:
    def test_install_paths_are_inside_the_models_directory(self):
        root = vm.models_dir().resolve()
        for model in (*catalog.STT_MODELS.values(), *catalog.TTS_MODELS.values(), catalog.VAD):
            assert root in vm.install_path(model).resolve().parents

    def test_an_archive_needs_its_marker_to_count_as_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vm, "models_dir", lambda: tmp_path)
        model = catalog.default_tts()
        (tmp_path / model.folder).mkdir(parents=True)
        assert not vm.is_installed(model), "an unpacked-but-unmarked folder is not ready"
        (tmp_path / model.folder / vm.READY).write_text("x")
        assert vm.is_installed(model)

    def test_an_archive_that_escapes_its_folder_is_refused(self, tmp_path):
        import tarfile

        archive = tmp_path / "evil.tar"
        payload = tmp_path / "payload"
        payload.write_text("pwned")
        with tarfile.open(archive, "w") as tar:
            tar.add(payload, arcname="../escaped.txt")

        with pytest.raises(ValueError, match="escapes"):
            vm._extract(archive, tmp_path / "target")
        assert not (tmp_path.parent / "escaped.txt").exists()

    def test_resolve_knows_each_kind(self):
        assert vm.resolve("stt", "moonshine-tiny-en") is not None
        assert vm.resolve("tts", "piper-amy") is not None
        assert vm.resolve("vad", "silero-vad") is not None
        assert vm.resolve("nope", "x") is None


# --------------------------------------------------------------------------- #
# The real engines, when the models happen to be installed
# --------------------------------------------------------------------------- #


def _models_ready() -> bool:
    from arcbot.voice.engines import sherpa_available

    if not sherpa_available():
        return False
    installed = vm.installed_ids()
    return bool(installed["vad"] and installed["stt"] and installed["tts"])


@pytest.mark.asyncio
@pytest.mark.skipif(not _models_ready(), reason="voice models are not downloaded")
async def test_round_trip_through_the_real_engines():
    """Speak a sentence, then listen to it — the whole local stack in one go."""
    from arcbot.voice.engines import SAMPLE_RATE, LocalSTT, LocalTTS, resample

    phrase = "Open the settings panel please."
    tts = LocalTTS(catalog.default_tts().id)
    audio = await tts.synthesize(phrase)
    assert audio.size > 0

    stt = LocalSTT(catalog.default_stt().id)
    heard = await stt.transcribe(resample(audio, tts.sample_rate, SAMPLE_RATE))
    words = {w.strip(".,").lower() for w in heard.text.split()}
    assert {"open", "settings"} <= words, heard.text


# --------------------------------------------------------------------------- #
# Nothing is fetched behind the user's back
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestNothingDownloadsUninvited:
    """The promise is that voice models cost nothing until you ask for them.
    These pin the paths where that could quietly stop being true."""

    @staticmethod
    def _models(tmp_path, monkeypatch):
        """An empty directory of its own, so 'nothing was written' means it."""
        directory = tmp_path / "voice-models"
        directory.mkdir()
        monkeypatch.setattr(vm, "models_dir", lambda: directory)
        return directory

    async def _controller(self, store, bus):
        from arcbot.agent import Agent
        from arcbot.voice.controller import VoiceController

        return VoiceController(store, bus, Agent(store, bus))

    async def test_voice_is_off_on_a_fresh_install(self, store):
        assert store.settings.voice.enabled is False

    async def test_reading_status_downloads_nothing(self, store, bus, tmp_path, monkeypatch):
        models = self._models(tmp_path, monkeypatch)
        controller = await self._controller(store, bus)
        controller.status()
        assert list(models.iterdir()) == []

    async def test_the_catalog_can_be_browsed_without_downloading(self, tmp_path, monkeypatch):
        models = self._models(tmp_path, monkeypatch)
        catalog.catalog_payload()
        assert list(models.iterdir()) == []

    async def test_previewing_a_missing_model_reports_instead_of_fetching(
        self, store, bus, tmp_path, monkeypatch
    ):
        models = self._models(tmp_path, monkeypatch)
        controller = await self._controller(store, bus)
        result = await controller.preview("hello", "kokoro", 0)
        assert result["needsDownload"] is True
        assert result["sizeMb"] > 0
        assert list(models.iterdir()) == [], "a preview must not download anything"

    async def test_describing_voices_does_not_fetch(self, store, bus, tmp_path, monkeypatch):
        models = self._models(tmp_path, monkeypatch)
        controller = await self._controller(store, bus)
        info = await controller.describe_voices("kokoro")
        assert info["installed"] is False
        assert info["count"] > 1, "the catalog count still guides the picker"
        assert list(models.iterdir()) == []

    async def test_installing_one_model_installs_only_that_one(
        self, store, bus, tmp_path, monkeypatch
    ):
        self._models(tmp_path, monkeypatch)
        controller = await self._controller(store, bus)
        asked: list[str] = []

        async def fake_install(models, on_progress=None):
            asked.extend(m.id for m in models)
            return vm.InstallReport([m.id for m in models], [], {})

        monkeypatch.setattr(vm, "install", fake_install)
        ok, _ = await controller.install_one("tts", "kokoro")
        assert ok
        assert asked == ["kokoro"], "one request must not pull the whole set"


class TestPickerContract:
    def test_every_speaking_model_declares_its_voice_count(self):
        for model in catalog.TTS_MODELS.values():
            assert model.detail.get("voices", 0) >= 1, model.id

    def test_at_least_one_model_offers_a_real_choice_of_voices(self):
        assert any(m.detail.get("voices", 1) > 5 for m in catalog.TTS_MODELS.values())


# --------------------------------------------------------------------------- #
# Where the models live
# --------------------------------------------------------------------------- #


class TestModelsLocation:
    """Models belong somewhere the user can find without being told."""

    def test_an_explicit_folder_always_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ARCBOT_MODELS_DIR", str(tmp_path / "elsewhere"))
        assert vm.models_root() == tmp_path / "elsewhere"

    def test_a_source_checkout_keeps_them_beside_the_app(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ARCBOT_MODELS_DIR", raising=False)
        checkout = tmp_path / "ArcBot"
        checkout.mkdir()
        (checkout / "pyproject.toml").write_text("[project]\n")
        monkeypatch.setattr(vm, "_app_root", lambda: checkout)
        assert vm.models_root() == checkout / "models" / "voice"

    def test_an_installed_copy_falls_back_to_the_data_directory(self, monkeypatch):
        """Nobody wants 300 MB of weights inside site-packages."""
        monkeypatch.delenv("ARCBOT_MODELS_DIR", raising=False)
        monkeypatch.setattr(vm, "_app_root", lambda: None)
        assert vm.models_root().name == vm.LEGACY_DIR

    def test_a_checkout_is_not_polluted_for_git(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ARCBOT_MODELS_DIR", str(tmp_path / "models" / "voice"))
        vm.models_dir()
        assert (tmp_path / "models" / ".gitignore").read_text().strip() == "*"

    def test_an_older_install_is_moved_not_downloaded_again(
        self, tmp_path, monkeypatch
    ):
        legacy = tmp_path / "data" / vm.LEGACY_DIR
        (legacy / "some-model").mkdir(parents=True)
        (legacy / "some-model" / vm.READY).write_text("x")
        (legacy / ".dl-halfway.tar.bz2").write_text("junk")

        monkeypatch.setattr(vm, "data_dir", lambda: tmp_path / "data")
        monkeypatch.setenv("ARCBOT_MODELS_DIR", str(tmp_path / "new"))
        monkeypatch.setattr(vm, "_migrated", False)

        target = vm.models_dir()
        assert (target / "some-model" / vm.READY).exists(), "the model came across"
        assert not legacy.exists(), "and the old folder was tidied away"

    def test_migration_never_clobbers_what_is_already_there(self, tmp_path, monkeypatch):
        legacy = tmp_path / "data" / vm.LEGACY_DIR
        legacy.mkdir(parents=True)
        (legacy / "model.onnx").write_text("old")
        new = tmp_path / "new"
        new.mkdir()
        (new / "model.onnx").write_text("current")

        monkeypatch.setattr(vm, "data_dir", lambda: tmp_path / "data")
        monkeypatch.setenv("ARCBOT_MODELS_DIR", str(new))
        monkeypatch.setattr(vm, "_migrated", False)

        assert (vm.models_dir() / "model.onnx").read_text() == "current"


# --------------------------------------------------------------------------- #
# Downloading in the background
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestBackgroundDownload:
    """Minutes of downloading must never be minutes of waiting."""

    async def _controller(self, store, bus, tmp_path, monkeypatch):
        from arcbot.agent import Agent
        from arcbot.voice.controller import VoiceController

        directory = tmp_path / "voice-models"
        directory.mkdir()
        monkeypatch.setattr(vm, "models_dir", lambda: directory)
        return VoiceController(store, bus, Agent(store, bus)), directory

    @staticmethod
    def _slow_install(monkeypatch, delay=0.05, fail=""):
        async def fake(models, on_progress=None):
            for model in models:
                for step in (0.3, 0.7):
                    if on_progress:
                        await on_progress(model.id, step, f"Downloading {model.name}")
                    await asyncio.sleep(delay)
            if fail:
                return vm.InstallReport([], [], {models[0].id: fail})
            return vm.InstallReport([m.id for m in models], [], {})

        monkeypatch.setattr(vm, "install", fake)

    async def test_starting_a_download_returns_before_it_finishes(
        self, store, bus, tmp_path, monkeypatch
    ):
        controller, _ = await self._controller(store, bus, tmp_path, monkeypatch)
        self._slow_install(monkeypatch, delay=0.2)

        job = await controller.start_download(controller.wanted_models())
        assert job["running"] is True, "the caller must not be left waiting"
        assert job["done"] is False
        assert [i["name"] for i in job["items"]], "it says what it is fetching"

        ok, _ = await controller.ensure_models()
        assert ok
        assert controller.download_state()["done"] is True

    async def test_progress_is_reported_as_it_goes(
        self, store, bus, tmp_path, monkeypatch, recorder
    ):
        controller, _ = await self._controller(store, bus, tmp_path, monkeypatch)
        self._slow_install(monkeypatch)

        await controller.ensure_models()
        seen = [data["progress"] for kind, data in recorder if kind == "voice.download"]
        assert len(seen) > 3, "a bar with no updates is a bar that looks stuck"
        assert seen == sorted(seen), "progress must never go backwards"
        assert seen[-1] == 1.0

    async def test_a_second_request_joins_the_running_one(
        self, store, bus, tmp_path, monkeypatch
    ):
        controller, _ = await self._controller(store, bus, tmp_path, monkeypatch)
        self._slow_install(monkeypatch, delay=0.2)

        first = await controller.start_download(controller.wanted_models())
        second = await controller.start_download(controller.wanted_models())
        assert second["items"] == first["items"]
        await controller.ensure_models()

    async def test_a_failure_is_reported_rather_than_swallowed(
        self, store, bus, tmp_path, monkeypatch
    ):
        controller, _ = await self._controller(store, bus, tmp_path, monkeypatch)
        self._slow_install(monkeypatch, fail="the network went away")

        ok, message = await controller.ensure_models()
        assert not ok
        assert "the network went away" in message
        states = {i["state"] for i in controller.download_state()["items"]}
        assert states == {"failed"}

    async def test_dismissing_a_running_download_still_announces_the_result(
        self, store, bus, tmp_path, monkeypatch
    ):
        controller, _ = await self._controller(store, bus, tmp_path, monkeypatch)
        self._slow_install(monkeypatch, delay=0.2)

        await controller.start_download(controller.wanted_models())
        controller.acknowledge_download()
        assert controller.download_state()["seen"] is True, "it goes quiet…"

        await controller.ensure_models()
        assert controller.download_state()["seen"] is False, "…then says it is ready"

        controller.acknowledge_download()
        assert controller.download_state() is None, "and then stays gone"

    async def test_nothing_is_queued_when_everything_is_present(
        self, store, bus, tmp_path, monkeypatch
    ):
        controller, _ = await self._controller(store, bus, tmp_path, monkeypatch)
        monkeypatch.setattr(vm, "is_installed", lambda model: True)

        job = await controller.start_download(controller.wanted_models())
        assert job["items"] == []
        assert job["done"] is True and job["running"] is False

    async def test_a_cloud_engine_needs_no_download_at_all(self, store, bus):
        from arcbot.agent import Agent
        from arcbot.voice.controller import VoiceController

        store.settings.voice.stt_engine = "cloud"
        store.settings.voice.tts_engine = "cloud"
        controller = VoiceController(store, bus, Agent(store, bus))
        assert [m.id for m in controller.wanted_models()] == [catalog.VAD.id]


@pytest.mark.asyncio
class TestChangingVoiceMidConversation:
    """Picking a new voice has to land now, not next conversation."""

    async def _controller(self, store, bus):
        from arcbot.agent import Agent
        from arcbot.voice.controller import VoiceController

        return VoiceController(store, bus, Agent(store, bus))

    async def test_swapping_with_no_session_is_harmless(self, store, bus):
        controller = await self._controller(store, bus)
        await controller.swap_voice()          # must not raise

    async def test_swapping_replaces_the_engine_and_stops_the_old_one(
        self, store, bus, session, monkeypatch
    ):
        controller = await self._controller(store, bus)
        controller.session = session
        stopped: list[bool] = []
        monkeypatch.setattr(session, "stop_speaking",
                            lambda: (stopped.append(True), asyncio.sleep(0))[1])

        replacement = object()
        monkeypatch.setattr(controller, "_build_tts", lambda: replacement)
        await controller.swap_voice()

        assert stopped == [True], "the old voice must not keep talking"
        assert session.tts is replacement


# --------------------------------------------------------------------------- #
# Failures that used to be silent
# --------------------------------------------------------------------------- #


class TestModelFilesAreDeclaredCorrectly:
    """A wrong filename here transcribes nothing, forever, without an error —
    sherpa-onnx accepts a path that does not exist. These pin the names."""

    def test_whisper_knows_its_tokens_are_prefixed(self):
        model = catalog.STT_MODELS["whisper-tiny"]
        assert model.detail["tokens"] == "tiny-tokens.txt"
        assert model.detail["encoder"].startswith("tiny-")
        assert model.detail["decoder"].startswith("tiny-")

    def test_every_model_says_which_files_it_needs(self):
        for model in catalog.STT_MODELS.values():
            keys = ("model",) if model.engine == "sense_voice" else ("encoder", "decoder")
            for key in keys:
                assert model.detail.get(key), f"{model.id} has no {key}"
        for model in catalog.TTS_MODELS.values():
            assert model.detail.get("model"), f"{model.id} has no model file"
            assert model.detail.get("data_dir"), f"{model.id} has no data_dir"

    def test_a_missing_file_is_named_rather_than_ignored(self, tmp_path):
        from arcbot.voice.engines import VoiceUnavailable, _file

        (tmp_path / "there.txt").write_text("x")
        assert _file(tmp_path, "there.txt").endswith("there.txt")
        with pytest.raises(VoiceUnavailable, match=r"gone\.txt"):
            _file(tmp_path, "gone.txt")


@pytest.mark.asyncio
class TestSilenceIsReported:
    """A recogniser returning empty text looks exactly like a dead microphone."""

    @staticmethod
    def _long(seconds=2.0):
        return np.zeros(int(16_000 * seconds), dtype=np.float32)

    async def test_one_empty_result_says_nothing(self, session, recorder):
        session.stt.text = ""
        session.vad.queued = [self._long()]
        await session.feed(np.zeros(512, dtype=np.float32))
        assert not [d for k, d in recorder if k == "notice"]

    async def test_a_run_of_them_speaks_up(self, session, recorder):
        session.stt.text = ""
        for _ in range(3):
            session.vad.queued = [self._long()]
            await session.feed(np.zeros(512, dtype=np.float32))

        notices = [d["text"] for k, d in recorder if k == "notice"]
        assert len(notices) == 1, "say it once, not once per utterance"
        assert "nothing is being transcribed" in notices[0]

    async def test_a_good_result_clears_the_count(self, session, recorder):
        for text in ("", "", "open the file", "", ""):
            session.stt.text = text
            session.vad.queued = [self._long()]
            await session.feed(np.zeros(512, dtype=np.float32))
        assert not [d for k, d in recorder if k == "notice"]

    async def test_brief_noise_is_not_counted(self, session, recorder):
        """Half a second of nothing is a cough, not a broken recogniser."""
        session.stt.text = ""
        for _ in range(5):
            session.vad.queued = [self._long(0.5)]
            await session.feed(np.zeros(512, dtype=np.float32))
        assert not [d for k, d in recorder if k == "notice"]


@pytest.mark.asyncio
class TestEnginesLoadBeforeYouSpeak:
    async def test_warm_up_is_part_of_the_interface(self):
        from arcbot.voice.engines import SpeechToText, TextToSpeech

        await SpeechToText().warm_up()      # the cloud engines inherit these
        await TextToSpeech().warm_up()

    async def test_a_model_that_cannot_load_stops_voice_mode_starting(
        self, store, bus, monkeypatch
    ):
        from arcbot.agent import Agent
        from arcbot.voice.controller import VoiceController
        from arcbot.voice.engines import VoiceUnavailable

        controller = VoiceController(store, bus, Agent(store, bus))
        monkeypatch.setattr(controller, "ensure_models",
                            lambda: asyncio.sleep(0, result=(True, "")))

        class Broken(FakeSTT):
            async def warm_up(self):
                raise VoiceUnavailable("tiny-tokens.txt is missing from whisper.")

        monkeypatch.setattr(controller, "_build_stt", lambda: Broken())
        monkeypatch.setattr(controller, "_build_tts", FakeTTS)
        monkeypatch.setattr("arcbot.voice.controller.LocalVAD", lambda **kw: FakeVAD())

        ok, message = await controller.start()
        assert not ok
        assert "tiny-tokens.txt" in message, "the user is told which file"
        assert controller.session is None
