"""Server-side voice services for browser push-to-talk.

Whisper and Edge TTS are loaded lazily so typed chat can run without warming
speech models. Audio uploads are written to short-lived temporary files; no
recordings are retained by the app.

Two facts this module reports on when transcription fails, because the browser
only ever sees one short line:

* **The Whisper model has to be fetched once.** ``faster-whisper`` resolves a
  model name such as ``base`` to a Hugging Face repository
  (``Systran/faster-whisper-base``) and caches it under ``HF_HUB_CACHE``. With no
  cache and no route to huggingface.co the load fails, so the first recording
  needs network access or a pre-populated cache. ``ORBIT_WHISPER_MODEL`` may also
  be a path to an already-downloaded model directory.
* **ffmpeg is not needed as a *program* to read the browser's recordings.**
  ``faster-whisper`` 1.x decodes audio with PyAV, and PyAV's wheels bundle the
  FFmpeg libraries, so the WebM/Opus clips ``MediaRecorder`` produces are read
  directly -- no ffmpeg binary, no conversion step. The ffmpeg *program* is only
  required by the older openai/whisper subprocess path, or when PyAV is missing
  or was built without FFmpeg. Both cases are named explicitly in the error and
  in :meth:`VoiceService.dependencies`, rather than surfacing as a generic
  "unavailable" message.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config import ENV_FILE
from dotenv import load_dotenv

log = logging.getLogger("orbit.voice")

EDGE_TTS_PITCH = "-15Hz"
EDGE_TTS_VOICE = "en-US-GuyNeural"
_SUPPORTED_EXTENSIONS = {".webm", ".ogg", ".mp4", ".wav", ".mp3"}
# The status line the browser shows has room for a sentence, not a stack trace.
_MAX_REASON_CHARS = 300
# The exception classes huggingface_hub raises when a model cannot be fetched, and
# the transport errors underneath them. Named rather than imported so a missing
# cache can still be described when huggingface_hub itself is absent.
_OFFLINE_ERROR_NAMES = frozenset(
    {
        "LocalEntryNotFoundError",
        "OfflineModeIsEnabled",
        "HfHubHTTPError",
        "ConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ProxyError",
        "SSLError",
        "URLError",
    }
)


class VoiceServiceUnavailable(RuntimeError):
    """A speech dependency or its upstream service is unavailable.

    ``reason`` is the short, UI-safe cause ("the model is not downloaded yet and
    huggingface.co could not be reached"); the chained exception is the real one
    and is what the console log and ``audit.log`` carry.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or message


def _flatten(text: str, limit: int = _MAX_REASON_CHARS) -> str:
    """One line, bounded: the shape a status message and a log field both need."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _module_version(name: str) -> str | None:
    """The installed version of ``name``, or ``None`` when it is not importable."""
    try:
        module = importlib.import_module(name)
    except Exception:
        return None
    version = getattr(module, "__version__", "")
    return str(version) if version else "installed (no __version__)"


def _pyav_ffmpeg_libraries() -> dict[str, str]:
    """The FFmpeg library versions PyAV bundles; empty when PyAV is unavailable.

    These libraries are what actually decode an uploaded WebM/Opus clip, so their
    presence is the honest answer to "is a decoder installed?".
    """
    try:
        av = importlib.import_module("av")
        versions: Mapping[str, Any] = getattr(av, "library_versions", None) or {}
    except Exception:
        return {}
    return {
        str(library): ".".join(str(part) for part in version)
        for library, version in versions.items()
    }


@dataclass(frozen=True, slots=True)
class VoiceDependencyReport:
    """What this machine actually has, in the shape the log and errors need."""

    model: str
    device: str
    compute_type: str
    faster_whisper: str | None = None
    ctranslate2: str | None = None
    pyav: str | None = None
    ffmpeg_libraries: Mapping[str, str] = field(default_factory=dict)
    ffmpeg_program: str | None = None
    model_source: str = "huggingface"
    model_cached: bool | None = None
    cache_dir: str | None = None

    @property
    def ffmpeg_program_required(self) -> bool:
        """Whether transcription needs the ``ffmpeg`` program installed.

        Not for faster-whisper 1.x on a normal install: PyAV's bundled FFmpeg
        libraries decode the browser's WebM/Opus uploads. Only a PyAV that is
        missing (or built without FFmpeg) makes an external ffmpeg necessary.
        """
        return self.pyav is None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form, for audit.log and the error response."""
        return {
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
            "faster_whisper": self.faster_whisper,
            "ctranslate2": self.ctranslate2,
            "pyav": self.pyav,
            "pyav_ffmpeg_libraries": dict(self.ffmpeg_libraries),
            "ffmpeg_program": self.ffmpeg_program,
            "ffmpeg_program_required": self.ffmpeg_program_required,
            "model_source": self.model_source,
            "model_cached": self.model_cached,
            "cache_dir": self.cache_dir,
        }

    def as_line(self) -> str:
        """One log line answering "what is installed, and is the model here?"."""
        libraries = ",".join(sorted(self.ffmpeg_libraries)) or "none"
        return (
            f"model={self.model} device={self.device} compute_type={self.compute_type} "
            f"source={self.model_source} model_cached={self.model_cached} "
            f"faster-whisper={self.faster_whisper or 'MISSING'} "
            f"ctranslate2={self.ctranslate2 or 'MISSING'} "
            f"pyav={self.pyav or 'MISSING'} (ffmpeg libs: {libraries}) "
            f"ffmpeg_program={self.ffmpeg_program or 'not installed'} "
            f"ffmpeg_program_required={self.ffmpeg_program_required}"
        )


class VoiceService:
    """Lazy faster-whisper transcription and streaming Edge TTS synthesis."""

    def __init__(
        self,
        *,
        whisper_model: Any | None = None,
        whisper_factory: Callable[..., Any] | None = None,
        communicate_factory: Callable[..., Any] | None = None,
        audit_log: Any | None = None,
    ) -> None:
        self._whisper_model = whisper_model
        self._whisper_factory = whisper_factory
        self._communicate_factory = communicate_factory
        # Optional AuditLog. When present, a successful model load is recorded, so
        # "the model really did download and load" is answerable after the fact.
        self._audit_log = audit_log
        self._model_lock = threading.Lock()
        load_dotenv(dotenv_path=ENV_FILE, override=False)
        self._model_name = os.getenv("ORBIT_WHISPER_MODEL", "base").strip() or "base"
        self._device = os.getenv("ORBIT_WHISPER_DEVICE", "cpu").strip() or "cpu"
        self._compute_type = os.getenv("ORBIT_WHISPER_COMPUTE_TYPE", "int8").strip() or "int8"

    # ---------------------------------------------------------------- diagnostics

    def dependencies(self) -> VoiceDependencyReport:
        """What this machine has for transcription. Never raises.

        Called on every failure path (and at boot) so a log line can say whether
        the engine, the decoder, and the model itself are actually present.
        """
        local_model = Path(self._model_name).expanduser()
        is_local_dir = local_model.is_dir()
        cache_dir: str | None = None
        try:
            from huggingface_hub import constants

            cache_dir = str(getattr(constants, "HF_HUB_CACHE", "") or "") or None
        except Exception:
            cache_dir = None
        return VoiceDependencyReport(
            model=self._model_name,
            device=self._device,
            compute_type=self._compute_type,
            faster_whisper=_module_version("faster_whisper"),
            ctranslate2=_module_version("ctranslate2"),
            pyav=_module_version("av"),
            ffmpeg_libraries=_pyav_ffmpeg_libraries(),
            ffmpeg_program=shutil.which("ffmpeg"),
            model_source=f"local directory {local_model}" if is_local_dir else "huggingface",
            model_cached=True if is_local_dir else self._is_model_cached(),
            cache_dir=cache_dir,
        )

    def _is_model_cached(self) -> bool | None:
        """Whether the model resolves from the local cache alone, with no network."""
        try:
            from faster_whisper.utils import download_model
        except Exception:
            return None
        try:
            download_model(self._model_name, local_files_only=True)
        except Exception:
            return False
        return True

    def _resolved_model_dir(self) -> str | None:
        """Where the model really lives on disk, when it resolves without network."""
        local_model = Path(self._model_name).expanduser()
        if local_model.is_dir():
            return str(local_model)
        try:
            from faster_whisper.utils import download_model

            return str(download_model(self._model_name, local_files_only=True))
        except Exception:
            return None

    def _record(self, event: str, **fields: Any) -> None:
        """Best-effort audit line: diagnostics must never break transcription."""
        if self._audit_log is None:
            return
        try:
            self._audit_log.record_event(event, **fields)
        except Exception:
            log.exception("could not write voice diagnostics to audit.log")

    @staticmethod
    def _unavailable(message: str, *, reason: str, cause: BaseException) -> VoiceServiceUnavailable:
        """Log a dependency failure where it is known, then hand back the error to raise.

        The console gets the traceback exactly once, from the layer that knows the
        cause; callers (the HTTP routes) add a short line with request context
        rather than printing the same traceback again.
        """
        log.error("voice dependency unavailable: %s", reason, exc_info=cause)
        return VoiceServiceUnavailable(message, reason=reason)

    # -------------------------------------------------------------------- whisper

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
                        raise self._unavailable(
                            "Voice transcription needs faster-whisper, which is not installed in "
                            f"this Python environment ({type(exc).__name__}: {exc}). Install the "
                            "project requirements with `pip install -r requirements.txt`.",
                            reason="faster-whisper is not installed in this environment.",
                            cause=exc,
                        ) from exc
                    factory = WhisperModel
                try:
                    model = factory(
                        self._model_name,
                        device=self._device,
                        compute_type=self._compute_type,
                    )
                except Exception as exc:
                    raise self._model_load_unavailable(exc) from exc
                self._whisper_model = model
                self._log_model_loaded()
        return self._whisper_model

    def _log_model_loaded(self) -> None:
        """Record a *successful* load, so "did the model download and load?" has an answer.

        The resolved directory is part of the record: a path under the Hugging Face
        cache is proof the fetch worked, and a local path says it never had to.
        """
        report = self.dependencies()
        model_dir = self._resolved_model_dir()
        log.info("voice transcription ready at %s: %s", model_dir or "unknown path", report.as_line())
        self._record(
            "voice_model_loaded",
            model=self._model_name,
            model_dir=model_dir,
            device=self._device,
            compute_type=self._compute_type,
            model_cached_offline=report.model_cached,
            faster_whisper=report.faster_whisper,
            ctranslate2=report.ctranslate2,
            pyav=report.pyav,
            ffmpeg_program=report.ffmpeg_program,
            ffmpeg_program_required=report.ffmpeg_program_required,
        )

    def _model_load_unavailable(self, error: BaseException) -> VoiceServiceUnavailable:
        """Explain a failed model load, with this machine's state, and log the real error.

        The browser-facing line stays short; the full exception, the cause chain,
        and the dependency report go to the server console here (and to audit.log
        through the caller).
        """
        report = self.dependencies()
        cause = f"{type(error).__module__}.{type(error).__name__}: {error}"
        if self._looks_like_offline_load(error):
            message = (
                f"Voice transcription cannot load the faster-whisper model "
                f"{self._model_name!r}: it is not in the local cache and it could not be "
                f"downloaded ({cause}). The first recording needs network access to "
                f"huggingface.co, or ORBIT_WHISPER_MODEL set to a model directory that is "
                f"already on disk. Cache checked: {report.cache_dir or 'unknown'}."
            )
            reason = (
                f"The faster-whisper model {self._model_name!r} has not been downloaded yet and "
                f"huggingface.co could not be reached ({error})."
            )
        else:
            message = (
                f"Voice transcription cannot load the faster-whisper model "
                f"{self._model_name!r} with device={self._device!r} "
                f"compute_type={self._compute_type!r}: {cause}"
            )
            reason = f"Could not load the faster-whisper model: {error}"
        log.error(
            "voice transcription: faster-whisper model %r failed to load [%s]; %s",
            self._model_name,
            cause,
            report.as_line(),
            exc_info=error,
        )
        return VoiceServiceUnavailable(message, reason=_flatten(reason))

    @staticmethod
    def _looks_like_offline_load(error: BaseException) -> bool:
        """Whether this is "model not fetched and the Hub is unreachable" rather than e.g. bad settings."""
        names: set[str] = set()
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            names.add(type(current).__name__)
            current = current.__cause__ or current.__context__
        if names & _OFFLINE_ERROR_NAMES:
            return True
        text = _flatten(str(error), limit=2_000).lower()
        return "huggingface" in text or "offline" in text or "connect" in text

    # ------------------------------------------------------------------- decoding

    def _ensure_decoder(self) -> Any:
        """Return PyAV, or explain what is missing to read the uploaded clip.

        faster-whisper hands the uploaded file to PyAV. A PyAV wheel carries its
        own FFmpeg libraries, so a system ``ffmpeg`` is not required for WebM/Opus
        -- but when PyAV itself is gone the failure says so, and names ffmpeg as
        the alternative, instead of surfacing as a generic "unavailable".
        """
        try:
            import av
        except ImportError as exc:
            raise self._unavailable(
                "Voice transcription cannot decode the recording: faster-whisper reads audio "
                f"through PyAV, and PyAV is not installed ({type(exc).__name__}: {exc}). Install "
                "it with `pip install -r requirements.txt` (PyAV wheels bundle the FFmpeg "
                "libraries the browser's WebM/Opus clips need). If PyAV cannot be installed as a "
                "wheel, install the ffmpeg program as well and build PyAV against it.",
                reason=(
                    "The audio decoder is missing: faster-whisper needs PyAV (bundled FFmpeg); "
                    "install 'av' from requirements.txt, or the ffmpeg program."
                ),
                cause=exc,
            ) from exc
        return av

    def _transcription_failure(
        self, error: BaseException, *, decoder: Any, clip_path: str
    ) -> BaseException:
        """Classify a decode/transcribe failure, naming the dependency when it is one.

        Returns the exception to raise. Audio that simply cannot be read stays a
        ``ValueError`` (the client's clip is at fault) but still carries the
        decoder's own message; a missing codec is a dependency problem.
        """
        detail = _flatten(f"{type(error).__name__}: {error}".replace(clip_path, "<recording>"))
        error_type = getattr(getattr(decoder, "error", None), "FFmpegError", None)
        codec_missing = getattr(getattr(decoder, "error", None), "DecoderNotFoundError", None)
        if isinstance(codec_missing, type) and isinstance(error, codec_missing):
            return self._unavailable(
                f"Voice transcription cannot decode this recording: no decoder for that audio "
                f"format is available ({detail}). Install the ffmpeg program (for example "
                "`apt-get install ffmpeg`) or reinstall PyAV from a wheel so its bundled FFmpeg "
                "libraries are present.",
                reason=f"no decoder for this audio format: {detail}",
                cause=error,
            )
        if isinstance(error_type, type) and isinstance(error, error_type):
            return ValueError(f"could not decode that recording ({detail})")
        return error

    # ------------------------------------------------------------------ transcribe

    def transcribe(self, audio: bytes, *, extension: str = ".webm") -> str:
        """Transcribe an uploaded browser audio clip and return plain text."""
        if extension not in _SUPPORTED_EXTENSIONS:
            raise ValueError("unsupported audio format")
        if not audio:
            raise ValueError("the recording is empty")

        # Checked before the model loads: a missing decoder is a different fix
        # (install PyAV/ffmpeg) from a missing model (download it).
        decoder = self._ensure_decoder()

        audio_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as clip:
                clip.write(audio)
                audio_path = clip.name
            model = self._get_whisper_model()
            try:
                segments, _info = model.transcribe(
                    audio_path,
                    beam_size=5,
                    vad_filter=True,
                )
                return "".join(segment.text for segment in segments).strip()
            except Exception as exc:
                # faster-whisper decodes here, so this is where a broken decoder
                # or an unreadable clip surfaces.
                raise self._transcription_failure(exc, decoder=decoder, clip_path=audio_path) from exc
        finally:
            if audio_path is not None:
                Path(audio_path).unlink(missing_ok=True)

    # ----------------------------------------------------------------------- tts

    def _get_communicate_factory(self) -> Callable[..., Any]:
        if self._communicate_factory is not None:
            return self._communicate_factory
        try:
            import edge_tts
        except ImportError as exc:
            raise self._unavailable(
                "Voice replies need edge-tts. Install the project requirements.",
                reason="edge-tts is not installed in this environment.",
                cause=exc,
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
