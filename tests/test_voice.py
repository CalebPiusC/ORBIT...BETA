from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from typing import Any

import inspect

import av.error
from huggingface_hub.errors import LocalEntryNotFoundError

from app.server import MAX_SPEECH_TEXT_CHARS, configure_logging, create_app
from app.voice import (
    EDGE_TTS_PITCH,
    EDGE_TTS_VOICE,
    VoiceDependencyReport,
    VoiceService,
    VoiceServiceUnavailable,
)
from tools.registry import AuditLog


def faster_whisper_can_decode_with_installed_pyav() -> bool:
    """Whether faster-whisper's decoder is usable with the PyAV that is installed.

    PyAV 19 removed ``av.open(metadata_errors=...)``; faster-whisper 1.2.1 still
    passes it, so decoding raises ``TypeError`` before any frame is read. Asking
    the installed source, rather than only its version, means this repairs
    itself the day faster-whisper stops passing the argument.
    """
    major = int(av.__version__.split(".")[0])
    if major < 19:
        return True
    import inspect

    from faster_whisper import audio

    return "metadata_errors" not in inspect.getsource(audio.decode_audio)


class FakeWhisperModel:
    def __init__(self) -> None:
        self.audio_path: str | None = None
        self.options: dict[str, Any] = {}

    def transcribe(self, audio_path: str, **options: Any):
        self.audio_path = audio_path
        self.options = options
        self.asserted_audio = Path(audio_path).read_bytes()
        return iter([SimpleNamespace(text="  Turn "), SimpleNamespace(text="left. ")]), object()


class VoiceServiceTests(unittest.TestCase):
    def test_transcription_uses_a_temporary_clip_and_cleans_it_up(self) -> None:
        model = FakeWhisperModel()
        service = VoiceService(whisper_model=model)

        transcript = service.transcribe(b"webm bytes", extension=".webm")

        self.assertEqual(transcript, "Turn left.")
        self.assertEqual(model.asserted_audio, b"webm bytes")
        self.assertEqual(model.options, {"beam_size": 5, "vad_filter": True})
        self.assertIsNotNone(model.audio_path)
        self.assertFalse(Path(model.audio_path).exists())

    def test_whisper_model_is_built_lazily_with_configured_settings(self) -> None:
        model = FakeWhisperModel()
        factory_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def factory(*args: Any, **kwargs: Any) -> FakeWhisperModel:
            factory_calls.append((args, kwargs))
            return model

        with patch.dict(
            os.environ,
            {
                "ORBIT_WHISPER_MODEL": "small",
                "ORBIT_WHISPER_DEVICE": "cpu",
                "ORBIT_WHISPER_COMPUTE_TYPE": "int8",
            },
        ):
            service = VoiceService(whisper_factory=factory)
            self.assertEqual(factory_calls, [])
            service.transcribe(b"clip", extension=".webm")

        self.assertEqual(factory_calls, [(("small",), {"device": "cpu", "compute_type": "int8"})])

    def test_edge_tts_streams_audio_with_the_requested_voice_and_pitch(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        class FakeCommunicate:
            def __init__(self, text: str, **kwargs: Any) -> None:
                calls.append((text, kwargs))

            async def stream(self):
                yield {"type": "WordBoundary", "data": {}}
                yield {"type": "audio", "data": b"mp3-first"}
                yield {"type": "audio", "data": b"mp3-second"}

        service = VoiceService(communicate_factory=FakeCommunicate)
        chunks = list(service.stream_speech("Hello from Orbit."))

        self.assertEqual(chunks, [b"mp3-first", b"mp3-second"])
        self.assertEqual(
            calls,
            [("Hello from Orbit.", {"voice": EDGE_TTS_VOICE, "pitch": EDGE_TTS_PITCH})],
        )
        self.assertEqual(EDGE_TTS_VOICE, "en-US-GuyNeural")
        self.assertEqual(EDGE_TTS_PITCH, "-15Hz")


class RecordingAuditLog:
    """Captures audit calls without touching the repository's audit.log."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record_event(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


class VoiceDependencyTests(unittest.TestCase):
    """(2) Is ffmpeg required, and is a decoder actually present? Answered from facts."""

    def test_pyav_bundled_ffmpeg_means_the_ffmpeg_program_is_not_required(self) -> None:
        report = VoiceService().dependencies()

        self.assertIsNotNone(report.pyav)  # PyAV is a hard dependency of faster-whisper
        self.assertIn("libavcodec", report.ffmpeg_libraries)
        self.assertFalse(report.ffmpeg_program_required)
        payload = report.as_dict()
        self.assertEqual(payload["ffmpeg_program_required"], False)
        self.assertIn("pyav_ffmpeg_libraries", payload)
        self.assertIn("model_cached", payload)
        json.dumps(payload)  # audit.log fields must survive JSON encoding

    def test_ffmpeg_program_is_required_when_pyav_is_absent(self) -> None:
        report = VoiceDependencyReport(model="base", device="cpu", compute_type="int8", pyav=None)

        self.assertTrue(report.ffmpeg_program_required)
        self.assertIn("MISSING", report.as_line())
        self.assertIn("ffmpeg_program_required=True", report.as_line())

    def test_model_cache_state_is_read_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as model_dir:
            with patch.dict(os.environ, {"ORBIT_WHISPER_MODEL": model_dir}):
                report = VoiceService().dependencies()

        self.assertTrue(report.model_cached)
        self.assertIn("local directory", report.model_source)

    def test_model_missing_from_the_cache_is_reported_as_not_cached(self) -> None:
        with patch.dict(os.environ, {"ORBIT_WHISPER_MODEL": "orbit-not-a-real-model-name"}):
            report = VoiceService().dependencies()

        self.assertFalse(report.model_cached)


class WhisperFailureReportingTests(unittest.TestCase):
    """(1) A model that cannot download or load must say so, and keep the real error."""

    def test_offline_model_load_keeps_the_cause_and_names_the_cache(self) -> None:
        hub_error = LocalEntryNotFoundError(
            "Got: ConnectError: TLS/SSL connection has been closed (EOF)"
        )

        def factory(*_args: Any, **_kwargs: Any) -> Any:
            raise hub_error

        service = VoiceService(whisper_factory=factory)
        with self.assertLogs("orbit.voice", level="ERROR") as captured:
            with self.assertRaises(VoiceServiceUnavailable) as raised:
                service.transcribe(b"webm clip", extension=".webm")

        error = raised.exception
        self.assertIs(error.__cause__, hub_error)  # the real exception is chained, not replaced
        self.assertIn("LocalEntryNotFoundError", str(error))
        self.assertIn("TLS/SSL connection has been closed", str(error))
        self.assertIn("huggingface.co", str(error))
        self.assertIn("ORBIT_WHISPER_MODEL", str(error))
        self.assertIn("has not been downloaded yet", error.reason)

        logged = captured.records[0]
        self.assertEqual(logged.levelno, logging.ERROR)
        self.assertIsNotNone(logged.exc_info)  # the console keeps the traceback
        self.assertIn("faster-whisper", logged.getMessage())
        self.assertIn("pyav=", logged.getMessage())  # and what this machine has

    def test_non_offline_model_load_failure_reports_settings_and_exception_type(self) -> None:
        def factory(*_args: Any, **_kwargs: Any) -> Any:
            raise ValueError("unsupported compute type 'int8' for device cpu")

        service = VoiceService(whisper_factory=factory)
        with self.assertLogs("orbit.voice", level="ERROR"):
            with self.assertRaises(VoiceServiceUnavailable) as raised:
                service.transcribe(b"clip", extension=".webm")

        message = str(raised.exception)
        self.assertIn("ValueError", message)
        self.assertIn("unsupported compute type", message)
        self.assertIn("device='cpu'", message)
        self.assertIn("compute_type='int8'", message)

    def test_missing_faster_whisper_names_the_package_to_install(self) -> None:
        service = VoiceService()

        with patch.dict(sys.modules, {"faster_whisper": None}):
            with self.assertLogs("orbit.voice", level="ERROR") as captured:
                with self.assertRaises(VoiceServiceUnavailable) as raised:
                    service.transcribe(b"clip", extension=".webm")

        self.assertIn("faster-whisper", str(raised.exception))
        self.assertIn("requirements.txt", str(raised.exception))
        self.assertIn("not installed", raised.exception.reason)
        self.assertIsNotNone(captured.records[0].exc_info)

    def test_a_successful_model_load_is_logged_and_audited(self) -> None:
        audit = RecordingAuditLog()
        service = VoiceService(
            whisper_factory=lambda *_args, **_kwargs: FakeWhisperModel(),
            audit_log=audit,
        )

        with self.assertLogs("orbit.voice", level="INFO") as captured:
            transcript = service.transcribe(b"webm bytes", extension=".webm")

        self.assertEqual(transcript, "Turn left.")
        self.assertIn("voice transcription ready", captured.records[0].getMessage())
        name, fields = audit.events[0]
        self.assertEqual(name, "voice_model_loaded")
        self.assertEqual(fields["model"], "base")
        self.assertFalse(fields["ffmpeg_program_required"])
        self.assertIsNotNone(fields["pyav"])
        # "base" is not in this machine's cache either, so there is no directory to
        # name -- but the field is always present, and names a real path when there is one.
        self.assertIn("model_dir", fields)

    def test_a_failing_audit_log_never_breaks_transcription(self) -> None:
        class BrokenAudit:
            def record_event(self, *_args: Any, **_kwargs: Any) -> None:
                raise OSError("audit.log is not writable")

        service = VoiceService(
            whisper_factory=lambda *_args, **_kwargs: FakeWhisperModel(),
            audit_log=BrokenAudit(),
        )

        with self.assertLogs("orbit.voice", level="ERROR") as captured:
            transcript = service.transcribe(b"webm bytes", extension=".webm")

        self.assertEqual(transcript, "Turn left.")
        self.assertIn("audit.log", captured.records[0].getMessage())


class DecoderCompatibilityTests(unittest.TestCase):
    def test_pyav_18_is_reported_as_compatible(self) -> None:
        with patch.object(av, "__version__", "18.1.0"):
            self.assertTrue(faster_whisper_can_decode_with_installed_pyav())

    def test_pyav_19_is_incompatible_while_faster_whisper_passes_metadata_errors(self) -> None:
        from faster_whisper import audio

        passes_argument = "metadata_errors" in inspect.getsource(audio.decode_audio)
        with patch.object(av, "__version__", "19.0.1"):
            self.assertEqual(
                faster_whisper_can_decode_with_installed_pyav(), not passes_argument
            )


class DecoderReportingTests(unittest.TestCase):
    """The browser records WebM/Opus; a decoder problem must not read as a generic failure."""

    def test_missing_pyav_is_reported_as_a_missing_decoder_with_the_ffmpeg_fallback(self) -> None:
        service = VoiceService(whisper_model=FakeWhisperModel())

        with patch.dict(sys.modules, {"av": None}):
            with self.assertLogs("orbit.voice", level="ERROR") as captured:
                with self.assertRaises(VoiceServiceUnavailable) as raised:
                    service.transcribe(b"webm clip", extension=".webm")

        message = str(raised.exception)
        self.assertIn("PyAV", message)
        self.assertIn("ffmpeg", message)
        self.assertIn("decoder", raised.exception.reason)
        # The missing decoder is logged with its traceback where it is discovered,
        # not left for the route to report as a generic failure.
        self.assertIsNotNone(captured.records[0].exc_info)

    def test_decoder_failure_that_is_not_a_dependency_problem_is_a_client_error(self) -> None:
        class UndecodableModel(FakeWhisperModel):
            def transcribe(self, audio_path: str, **_options: Any) -> Any:
                raise av.error.InvalidDataError(
                    1094995529, f"Invalid data found when processing input: '{audio_path}'"
                )

        service = VoiceService(whisper_model=UndecodableModel())

        with self.assertRaises(ValueError) as raised:
            service.transcribe(b"not really audio", extension=".webm")

        message = str(raised.exception)
        self.assertIn("could not decode that recording", message)
        self.assertIn("Invalid data found when processing input", message)
        self.assertIn("<recording>", message)  # the temp path stays out of the message
        self.assertNotIn(tempfile.gettempdir(), message)

    def test_a_real_decode_of_a_non_audio_clip_produces_the_client_error(self) -> None:
        """No ffmpeg program, no model weights: PyAV alone decides this clip is unreadable."""
        if not faster_whisper_can_decode_with_installed_pyav():
            self.skipTest(
                "PyAV 19 removed av.open(metadata_errors=...) and faster-whisper still "
                "passes it, so no clip decodes on this pair (a TypeError, not a bad "
                "recording). requirements.txt pins av<19; reinstall requirements to run "
                "this test."
            )
        from faster_whisper.audio import decode_audio

        class RealDecoderModel(FakeWhisperModel):
            """Does what WhisperModel does with the clip before transcribing: decode it."""

            def transcribe(self, audio_path: str, **options: Any) -> Any:
                decode_audio(audio_path, sampling_rate=16000)
                return super().transcribe(audio_path, **options)

        service = VoiceService(whisper_model=RealDecoderModel())

        with self.assertRaises(ValueError) as raised:
            service.transcribe(b"this is definitely not audio", extension=".webm")

        message = str(raised.exception)
        self.assertIn("could not decode that recording", message)
        self.assertIn("Invalid data found when processing input", message)
        self.assertNotIn(tempfile.gettempdir(), message)

    def test_a_missing_codec_for_the_format_points_at_ffmpeg(self) -> None:
        class NoCodecModel(FakeWhisperModel):
            def transcribe(self, _audio_path: str, **_options: Any) -> Any:
                raise av.error.DecoderNotFoundError("opus", "decoder not found")

        service = VoiceService(whisper_model=NoCodecModel())

        with self.assertLogs("orbit.voice", level="ERROR") as captured:
            with self.assertRaises(VoiceServiceUnavailable) as raised:
                service.transcribe(b"webm clip", extension=".webm")

        message = str(raised.exception)
        self.assertIn("ffmpeg", message)
        self.assertIn("no decoder", raised.exception.reason)
        self.assertIsNotNone(captured.records[0].exc_info)

    def test_an_undecodable_upload_still_cleans_up_its_temporary_file(self) -> None:
        written: list[str] = []
        real_named_temp = tempfile.NamedTemporaryFile

        class UndecodableModel(FakeWhisperModel):
            def transcribe(self, _audio_path: str, **_options: Any) -> Any:
                raise av.error.InvalidDataError(1094995529, "Invalid data found")

        def tracked_temp(*args: Any, **kwargs: Any) -> Any:
            handle = real_named_temp(*args, **kwargs)
            written.append(handle.name)
            return handle

        with patch("app.voice.tempfile.NamedTemporaryFile", tracked_temp):
            service = VoiceService(whisper_model=UndecodableModel())
            with self.assertRaises(ValueError):
                service.transcribe(b"not really audio", extension=".webm")

        self.assertEqual(len(written), 1)
        self.assertFalse(Path(written[0]).exists())


class VoiceAuditRouteTests(unittest.TestCase):
    """Everything the UI reports as a generic sentence must land in audit.log in full."""

    def _post_clip(self, client: Any) -> Any:
        return client.post(
            "/api/voice/transcribe",
            data={"audio": (io.BytesIO(b"clip"), "clip.webm", "audio/webm")},
        )

    def test_unavailable_transcription_returns_a_reason_and_audits_the_real_exception(self) -> None:
        hub_error = LocalEntryNotFoundError(
            "Got: ConnectError: TLS/SSL connection has been closed (EOF)"
        )

        class OfflineVoice(FakeVoiceService):
            def transcribe(self, audio: bytes, *, extension: str) -> str:
                raise VoiceServiceUnavailable(
                    f"cannot load the faster-whisper model 'base': {hub_error}",
                    reason="the faster-whisper model 'base' has not been downloaded yet",
                ) from hub_error

            def dependencies(self) -> VoiceDependencyReport:
                return VoiceDependencyReport(
                    model="base",
                    device="cpu",
                    compute_type="int8",
                    pyav="18.1.0",
                    ffmpeg_libraries={"libavcodec": "62.28.102"},
                    ffmpeg_program=None,
                    model_cached=False,
                    cache_dir="/tmp/hf-cache",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.log"
            client = create_app(
                voice_service=OfflineVoice(), model_audit=AuditLog(audit_path)
            ).test_client()

            with self.assertLogs("orbit.server", level="ERROR") as captured:
                response = self._post_clip(client)

            self.assertEqual(response.status_code, 503)
            payload = response.get_json()
            self.assertEqual(payload["error"], "Voice transcription is unavailable right now.")
            self.assertIn("has not been downloaded yet", payload["reason"])
            # One line with request context; the traceback itself is logged by the
            # service that found the cause and stored in audit.log below.
            self.assertIn("voice transcription unavailable", captured.records[0].getMessage())
            self.assertIn(payload["reason"], captured.records[0].getMessage())

            [event] = [json.loads(line) for line in audit_path.read_text().splitlines()]

        self.assertEqual(event["event"], "voice_transcription_unavailable")
        self.assertEqual(event["error_type"], "app.voice.VoiceServiceUnavailable")
        self.assertIn("cannot load the faster-whisper model", event["error"])
        self.assertTrue(
            any("LocalEntryNotFoundError" in entry for entry in event["error_chain"]),
            event["error_chain"],
        )
        self.assertIn("Traceback (most recent call last)", event["traceback"])
        self.assertIn("ConnectError", event["traceback"])
        # What the machine had at the moment of failure travels with the record.
        self.assertEqual(event["diagnostics"]["model_cached"], False)
        self.assertEqual(event["diagnostics"]["ffmpeg_program_required"], False)
        self.assertEqual(event["diagnostics"]["pyav_ffmpeg_libraries"], {"libavcodec": "62.28.102"})
        self.assertEqual(event["extension"], ".webm")
        self.assertEqual(event["audio_bytes"], 4)

    def test_an_unexpected_transcription_failure_is_audited_with_its_traceback(self) -> None:
        class ExplodingVoice(FakeVoiceService):
            def transcribe(self, audio: bytes, *, extension: str) -> str:
                raise RuntimeError("ctranslate2 could not decode with 8 threads")

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.log"
            client = create_app(
                voice_service=ExplodingVoice(), model_audit=AuditLog(audit_path)
            ).test_client()

            with self.assertLogs("orbit.server", level="ERROR") as captured:
                response = self._post_clip(client)

            self.assertEqual(response.status_code, 500)
            self.assertEqual(
                response.get_json()["reason"], "RuntimeError: ctranslate2 could not decode with 8 threads"
            )
            self.assertIsNotNone(captured.records[0].exc_info)

            [event] = [json.loads(line) for line in audit_path.read_text().splitlines()]

        self.assertEqual(event["event"], "voice_transcription_failed")
        self.assertEqual(event["error_type"], "builtins.RuntimeError")
        self.assertIn("ctranslate2 could not decode", event["traceback"])
        self.assertEqual(event["diagnostics"], {})  # a stub service that cannot report

    def test_an_undecodable_upload_reaches_the_client_with_the_decoder_message(self) -> None:
        class UndecodableVoice(FakeVoiceService):
            def transcribe(self, audio: bytes, *, extension: str) -> str:
                raise ValueError(
                    "could not decode that recording (InvalidDataError: Invalid data found)"
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.log"
            client = create_app(
                voice_service=UndecodableVoice(), model_audit=AuditLog(audit_path)
            ).test_client()
            response = self._post_clip(client)
            audited = audit_path.exists()

        self.assertEqual(response.status_code, 400)
        self.assertIn("could not decode that recording", response.get_json()["error"])
        # A bad clip is the client's problem: reported specifically, not audited as a fault.
        self.assertFalse(audited)

    def test_a_failed_spoken_reply_is_audited_with_its_cause(self) -> None:
        class BrokenSpeech(FakeVoiceService):
            def stream_speech(self, text: str) -> Any:
                raise RuntimeError("edge-tts: connection refused by speech.platform.bing.com")
                yield b""

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.log"
            client = create_app(
                voice_service=BrokenSpeech(), model_audit=AuditLog(audit_path)
            ).test_client()

            with self.assertLogs("orbit.server", level="ERROR"):
                response = client.post("/api/voice/speech", json={"text": "Hello"})

            self.assertEqual(response.status_code, 502)
            self.assertIn("connection refused", response.get_json()["reason"])
            [event] = [json.loads(line) for line in audit_path.read_text().splitlines()]

        self.assertEqual(event["event"], "voice_synthesis_failed")
        self.assertEqual(event["endpoint"], "/api/voice/speech")
        self.assertIn("RuntimeError", event["error_chain"][0])

    def test_an_unwritable_audit_log_does_not_hide_the_original_failure(self) -> None:
        class ExplodingVoice(FakeVoiceService):
            def transcribe(self, audio: bytes, *, extension: str) -> str:
                raise RuntimeError("the real problem")

        class BrokenAudit(AuditLog):
            def record_error(self, *_args: Any, **_kwargs: Any) -> None:
                raise OSError("audit.log is on a read-only mount")

        client = create_app(voice_service=ExplodingVoice(), model_audit=BrokenAudit()).test_client()

        with self.assertLogs("orbit.server", level="ERROR") as captured:
            response = self._post_clip(client)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["reason"], "RuntimeError: the real problem")
        self.assertTrue(
            any("could not write voice diagnostics" in record.getMessage() for record in captured.records)
        )


class ConsoleLoggingTests(unittest.TestCase):
    def test_console_logging_is_configured_once_and_honours_the_level(self) -> None:
        logger = logging.getLogger("orbit")
        with _preserved_logger(logger):
            logger.handlers.clear()
            with patch.dict(os.environ, {"ORBIT_LOG_LEVEL": ""}):
                configure_logging()
                configure_logging()

                self.assertEqual(len(logger.handlers), 1)
                self.assertEqual(logger.level, logging.INFO)
                self.assertFalse(logger.propagate)
                pattern = logger.handlers[0].formatter._fmt
                self.assertIn("%(levelname)s", pattern)
                self.assertIn("%(name)s", pattern)

            with patch.dict(os.environ, {"ORBIT_LOG_LEVEL": "debug"}):
                configure_logging()
                self.assertEqual(logger.level, logging.DEBUG)

            with patch.dict(os.environ, {"ORBIT_LOG_LEVEL": "not-a-level"}):
                configure_logging()
                self.assertEqual(logger.level, logging.INFO)


@contextlib.contextmanager
def _preserved_logger(logger: logging.Logger) -> Any:
    """Put a logger back exactly as it was, so a test cannot reconfigure the suite."""
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    try:
        yield
    finally:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


class FakeVoiceService:
    def __init__(self, *, transcript: str = "Where is order 1042?") -> None:
        self.transcript = transcript
        self.audio: bytes | None = None
        self.extension: str | None = None
        self.spoken_text: str | None = None

    def transcribe(self, audio: bytes, *, extension: str) -> str:
        self.audio = audio
        self.extension = extension
        return self.transcript

    def stream_speech(self, text: str):
        self.spoken_text = text
        yield b"mp3-part-one"
        yield b"mp3-part-two"


class VoiceRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.voice = FakeVoiceService()
        self.client = create_app(voice_service=self.voice).test_client()

    def test_upload_is_transcribed_and_returns_text_for_the_normal_chat_flow(self) -> None:
        response = self.client.post(
            "/api/voice/transcribe",
            data={"audio": (io.BytesIO(b"webm clip"), "recording.webm", "audio/webm;codecs=opus")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"transcript": "Where is order 1042?"})
        self.assertEqual(self.voice.audio, b"webm clip")
        self.assertEqual(self.voice.extension, ".webm")

    def test_transcribe_rejects_missing_empty_and_unsupported_uploads(self) -> None:
        missing = self.client.post("/api/voice/transcribe", data={})
        empty = self.client.post(
            "/api/voice/transcribe",
            data={"audio": (io.BytesIO(b""), "empty.webm", "audio/webm")},
        )
        unsupported = self.client.post(
            "/api/voice/transcribe",
            data={"audio": (io.BytesIO(b"audio"), "clip.bin", "application/octet-stream")},
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(unsupported.status_code, 415)

    def test_speech_endpoint_streams_mp3_bytes(self) -> None:
        response = self.client.post(
            "/api/voice/speech", json={"text": "Hello Orbit."}, buffered=False
        )
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "audio/mpeg")
            chunks = iter(response.response)
            self.assertEqual(next(chunks), b"mp3-part-one")
            self.assertEqual(next(chunks), b"mp3-part-two")
            with self.assertRaises(StopIteration):
                next(chunks)
            self.assertEqual(self.voice.spoken_text, "Hello Orbit.")
        finally:
            response.close()

    def test_missing_speech_dependencies_return_clear_service_errors(self) -> None:
        class UnavailableVoice(FakeVoiceService):
            def transcribe(self, audio: bytes, *, extension: str) -> str:
                raise VoiceServiceUnavailable("dependency detail")

            def stream_speech(self, text: str):
                raise VoiceServiceUnavailable("upstream detail")
                yield b""

        client = create_app(voice_service=UnavailableVoice()).test_client()
        with self.assertLogs("orbit.server", level="ERROR"):
            transcript = client.post(
                "/api/voice/transcribe",
                data={"audio": (io.BytesIO(b"clip"), "clip.webm", "audio/webm")},
            )
        with self.assertLogs("orbit.server", level="ERROR"):
            speech = client.post("/api/voice/speech", json={"text": "Hello"})
        self.assertEqual(transcript.status_code, 503)
        # The stable first line stays; `reason` carries the specific cause so the
        # failure is not only a generic sentence.
        self.assertEqual(
            transcript.get_json(),
            {"error": "Voice transcription is unavailable right now.", "reason": "dependency detail"},
        )
        self.assertEqual(speech.status_code, 503)
        self.assertEqual(
            speech.get_json(),
            {"error": "Spoken replies are unavailable right now.", "reason": "upstream detail"},
        )

    def test_speech_endpoint_validates_text_and_empty_upstream_audio(self) -> None:
        missing = self.client.post("/api/voice/speech", json={"text": "  "})
        too_long = self.client.post("/api/voice/speech", json={"text": "x" * (MAX_SPEECH_TEXT_CHARS + 1)})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(too_long.status_code, 413)

        class SilentVoice(FakeVoiceService):
            def stream_speech(self, text: str):
                if False:
                    yield b""

        silent_client = create_app(voice_service=SilentVoice()).test_client()
        empty_audio = silent_client.post("/api/voice/speech", json={"text": "Hello"})
        self.assertEqual(empty_audio.status_code, 502)


if __name__ == "__main__":
    unittest.main()
