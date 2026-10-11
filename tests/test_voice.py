from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from typing import Any

from app.server import MAX_SPEECH_TEXT_CHARS, create_app
from app.voice import EDGE_TTS_PITCH, EDGE_TTS_VOICE, VoiceService, VoiceServiceUnavailable


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
        self.assertEqual(transcript.get_json(), {"error": "Voice transcription is unavailable right now."})
        self.assertEqual(speech.status_code, 503)
        self.assertEqual(speech.get_json(), {"error": "Spoken replies are unavailable right now."})

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
