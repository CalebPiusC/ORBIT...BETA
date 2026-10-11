"""Server-side voice services for browser push-to-talk.

Whisper and Edge TTS are loaded lazily so typed chat can run without warming
speech models. Audio uploads are written to short-lived temporary files; no
recordings are retained by the app.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

from config import ENV_FILE
from dotenv import load_dotenv

EDGE_TTS_PITCH = "-15Hz"
EDGE_TTS_VOICE = "en-US-GuyNeural"
_SUPPORTED_EXTENSIONS = {".webm", ".ogg", ".mp4", ".wav", ".mp3"}


class VoiceServiceUnavailable(RuntimeError):
    """A speech dependency or its upstream service is unavailable."""


class VoiceService:
    """Lazy faster-whisper transcription and streaming Edge TTS synthesis."""

    def __init__(
        self,
        *,
        whisper_model: Any | None = None,
        whisper_factory: Callable[..., Any] | None = None,
        communicate_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._whisper_model = whisper_model
        self._whisper_factory = whisper_factory
        self._communicate_factory = communicate_factory
        self._model_lock = threading.Lock()
        load_dotenv(dotenv_path=ENV_FILE, override=False)
        self._model_name = os.getenv("ORBIT_WHISPER_MODEL", "base").strip() or "base"
        self._device = os.getenv("ORBIT_WHISPER_DEVICE", "cpu").strip() or "cpu"
        self._compute_type = os.getenv("ORBIT_WHISPER_COMPUTE_TYPE", "int8").strip() or "int8"

    def _get_whisper_model(self) -> Any:
        if self._whisper_model is not None:
            return self._whisper_model
        with self._model_lock:
            if self._whisper_model is None:
                factory = self._whisper_factory
                if factory is None:
                    try:
                        from faster_whisper import WhisperModel
                    except ImportError as exc:
                        raise VoiceServiceUnavailable(
                            "Voice transcription needs faster-whisper. Install the project requirements."
                        ) from exc
                    factory = WhisperModel
                self._whisper_model = factory(
                    self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                )
        return self._whisper_model

    def transcribe(self, audio: bytes, *, extension: str = ".webm") -> str:
        """Transcribe an uploaded browser audio clip and return plain text."""
        if extension not in _SUPPORTED_EXTENSIONS:
            raise ValueError("unsupported audio format")
        if not audio:
            raise ValueError("the recording is empty")

        audio_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as clip:
                clip.write(audio)
                audio_path = clip.name
            segments, _info = self._get_whisper_model().transcribe(
                audio_path,
                beam_size=5,
                vad_filter=True,
            )
            return "".join(segment.text for segment in segments).strip()
        finally:
            if audio_path is not None:
                Path(audio_path).unlink(missing_ok=True)

    def _get_communicate_factory(self) -> Callable[..., Any]:
        if self._communicate_factory is not None:
            return self._communicate_factory
        try:
            import edge_tts
        except ImportError as exc:
            raise VoiceServiceUnavailable(
                "Voice replies need edge-tts. Install the project requirements."
            ) from exc
        return edge_tts.Communicate

    def stream_speech(self, text: str) -> Iterator[bytes]:
        """Yield MP3 audio chunks from Edge TTS using Orbit's configured voice."""
        communicate = self._get_communicate_factory()(
            text,
            voice=EDGE_TTS_VOICE,
            pitch=EDGE_TTS_PITCH,
        )

        async def audio_chunks() -> Any:
            async for event in communicate.stream():
                if event.get("type") == "audio" and event.get("data"):
                    yield bytes(event["data"])

        async_iter = audio_chunks()

        def generate() -> Iterator[bytes]:
            loop = asyncio.new_event_loop()
            try:
                previous_loop: asyncio.AbstractEventLoop | None
                try:
                    previous_loop = asyncio.get_running_loop()
                except RuntimeError:
                    previous_loop = None
                asyncio.set_event_loop(loop)
                while True:
                    try:
                        chunk = loop.run_until_complete(async_iter.__anext__())
                    except StopAsyncIteration:
                        break
                    if chunk:
                        yield chunk
            finally:
                try:
                    loop.run_until_complete(async_iter.aclose())
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
                    asyncio.set_event_loop(previous_loop)

        return generate()
