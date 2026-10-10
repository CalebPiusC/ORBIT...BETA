"""Gemini chat and gated function-calling implementation for ORBIT."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError

from config import DEFAULT_GEMINI_FALLBACK_MODEL, DEFAULT_GEMINI_MODEL, load_settings
from providers.base import ChatMessage, ChatProvider, StreamChunk
from providers.personas import slot_name
from tools.registry import AuditLog, ToolRegistry

# The only failures the fallback model is allowed to get past: the server
# said it is slow or overloaded, or the connection broke before an answer.
# Auth, quota, and bad-request errors are NOT retried. A different model would
# fail the same way, and retrying would hide the real problem.
FALLBACK_STATUS_CODES = (503, 504)

_END = object()

# Instructs the model to narrate its reasoning as plain language wrapped in
# <thinking>…</thinking>, then give the final answer. We surface that
# narration as the UI "thinking" block. This is honest model-generated
# narration, not exposed internal reasoning tokens (see reply_stream).
_THINK_INSTRUCTION = (
    "Before your answer, write a short plain-language narration of your reasoning "
    "for the user, wrapped in <thinking> and </thinking> tags. Then provide only "
    "the final answer after the closing tag. Keep the narration to one or two "
    "sentences."
)

# Tags used to separate the thinking narration from the answer in one streamed
# generation. They are stripped before anything reaches the UI.
_THINK_OPEN = "<thinking>"
_THINK_CLOSE = "</thinking>"


def timeout_millis(seconds: float) -> int:
    """Convert a deadline in seconds to the milliseconds HttpOptions expects.

    The ``* 1000`` is not decoration and must not be "simplified" away:
    ``HttpOptions.timeout`` is documented in milliseconds, and the SDK divides
    it by 1000 before handing it to httpx. More importantly, leaving it unset
    is not neutral — the SDK then passes ``timeout=None`` to httpx, and an
    explicit ``None`` disables the timeout instead of falling back to the
    client default, so a blocked connection hangs forever with no reply and no
    error.
    """
    return int(seconds * 1000)


def _fallback_reason(exc: BaseException) -> str | None:
    """Name why a failed request may be retried on the fallback model, or None."""
    if isinstance(exc, APIError) and exc.code in FALLBACK_STATUS_CODES:
        return str(exc.code)
    if isinstance(exc, httpx.TransportError):
        return type(exc).__name__
    return None


def _describe_failure(reason: str) -> str:
    """Plain words for a fallback reason, for the Agent-mode status line."""
    if reason.isdigit():
        return f"Google returned {reason}"
    if "Timeout" in reason:
        return "the request timed out"
    return "the connection failed"


def _failure_label(exc: BaseException) -> str:
    """A short, key-free label for an audit line. Never the full message."""
    if isinstance(exc, APIError):
        return f"APIError {exc.code}"
    return type(exc).__name__


class GeminiProvider(ChatProvider):
    """Send chat history to Gemini and run declared tools only through a gate.

    Two models are configured: the primary (``model``) and an optional fallback
    (``fallback_model``). ``None`` means "use the configured fallback"; ``""``
    disables the fallback. The fallback is tried once, and
    only when the primary fails with 503/504 or a transport error. It is never
    tried after a tool has run, so a tool call is never repeated.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        fallback_model: str | None = None,
        client: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int = 5,
        audit_log: AuditLog | None = None,
    ) -> None:
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1.")

        if client is None:
            settings = load_settings()
            # The deadline is set on the client, so it applies to reply() and
            # to reply_stream() alike — every request this provider makes.
            self._client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=timeout_millis(settings.request_timeout_seconds)
                ),
            )
            self.model = model or settings.gemini_model
            configured_fallback = (
                settings.gemini_fallback_model if fallback_model is None else fallback_model
            )
            # Real use writes model events to audit.log. Injected clients (tests)
            # do not, unless a log is passed explicitly.
            self._audit = audit_log if audit_log is not None else AuditLog()
        else:
            # Client injection keeps provider tests offline; normal use always
            # builds the SDK client from config.py and the local .env.
            self._client = client
            self.model = model or DEFAULT_GEMINI_MODEL
            configured_fallback = (
                DEFAULT_GEMINI_FALLBACK_MODEL if fallback_model is None else fallback_model
            )
            self._audit = audit_log

        # "" or a fallback equal to the primary both mean "no fallback".
        self.fallback_model: str | None = configured_fallback or None
        if self.fallback_model == self.model:
            self.fallback_model = None
        # The model that produced the most recent answer. Set on success only.
        self.last_model_used: str | None = None

        self._tool_registry = tool_registry
        self._max_tool_rounds = max_tool_rounds

    # --- model calls with a single fallback ---------------------------------

    def _audit_failure(self, model: str, role: str, exc: BaseException, started: float) -> None:
        if self._audit is not None:
            self._audit.record_model_event(
                event="model_attempt_failed",
                model=model,
                role=role,
                reason=_failure_label(exc),
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )

    def _audit_answer(self, model: str, *, stream: bool) -> None:
        self.last_model_used = model
        if self._audit is not None:
            self._audit.record_model_event(
                event="model_answered",
                model=model,
                role="primary" if model == self.model else "fallback",
                fallback_used=model != self.model,
                stream=stream,
            )

    def _generate(
        self,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
        *,
        model: str,
        allow_fallback: bool,
    ) -> tuple[Any, str]:
        """One generate_content call. Returns (response, model that answered)."""
        started = time.monotonic()
        try:
            response = self._client.models.generate_content(
                model=model, contents=contents, config=config
            )
        except Exception as exc:
            reason = _fallback_reason(exc)
            self._audit_failure(model, "primary" if model == self.model else "fallback", exc, started)
            if not (allow_fallback and reason and model == self.model and self.fallback_model):
                raise
            fallback = self.fallback_model
            started = time.monotonic()
            try:
                response = self._client.models.generate_content(
                    model=fallback, contents=contents, config=config
                )
            except Exception as fallback_exc:
                self._audit_failure(fallback, "fallback", fallback_exc, started)
                raise
            self._audit_answer(fallback, stream=False)
            return response, fallback

        self._audit_answer(model, stream=False)
        return response, model

    def _stream_responses(
        self,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
        notify: Callable[[str], None] | None = None,
    ) -> Iterator[Any]:
        """Yield SDK stream responses, failing over only before the first chunk.

        Once any chunk has reached the user, a failure is surfaced rather than
        restarted on the fallback, because a restart would repeat the text
        already shown. ``notify`` receives a short status line before each
        model is asked, and when the primary has failed.
        """
        say = notify or (lambda _text: None)
        primary_name = slot_name(0)
        model = self.model
        say(f"Asking {primary_name}…")
        started = time.monotonic()
        try:
            stream, first = self._open_stream(model, contents, config)
        except Exception as exc:
            reason = _fallback_reason(exc)
            self._audit_failure(model, "primary", exc, started)
            if not (reason and self.fallback_model):
                raise
            model = self.fallback_model
            say(
                f"{primary_name} didn't answer ({_describe_failure(reason)}). "
                f"Asking {slot_name(1)}…"
            )
            started = time.monotonic()
            try:
                stream, first = self._open_stream(model, contents, config)
            except Exception as fallback_exc:
                self._audit_failure(model, "fallback", fallback_exc, started)
                raise

        self._audit_answer(model, stream=True)
        if first is not _END:
            yield first
        yield from stream

    def _open_stream(
        self,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> tuple[Iterator[Any], Any]:
        """Start a stream and read its first chunk, so connection errors surface here."""
        stream = iter(
            self._client.models.generate_content_stream(
                model=model, contents=contents, config=config
            )
        )
        first = next(stream, _END)
        return stream, first

    def reply(self, history: Sequence[ChatMessage], system_prompt: str) -> str:
        if not isinstance(system_prompt, str):
            raise TypeError("system_prompt must be a string.")
        if not system_prompt.strip():
            raise ValueError("system_prompt must not be empty.")
        if not history:
            raise ValueError("history must contain at least one chat message.")

        contents: list[types.Content] = []
        for message in history:
            if not isinstance(message, ChatMessage):
                raise TypeError("history must contain ChatMessage instances.")
            sdk_role = "user" if message.role == "user" else "model"
            contents.append(
                types.Content(
                    role=sdk_role,
                    parts=[types.Part.from_text(text=message.content)],
                )
            )

        config = self._generation_config(system_prompt)
        tool_rounds = 0
        model = self.model
        while True:
            # Fallback is allowed only before any tool has run. After a fallback
            # answers, the rest of this turn stays on that model, because the
            # function-call parts it produced belong to it.
            response, model = self._generate(
                contents,
                config,
                model=model,
                allow_fallback=tool_rounds == 0,
            )
            function_calls = getattr(response, "function_calls", None) or []
            if not function_calls:
                reply_text = response.text
                if not isinstance(reply_text, str) or not reply_text.strip():
                    raise RuntimeError("Gemini returned no text for the chat request.")
                return reply_text

            if self._tool_registry is None:
                raise RuntimeError("Gemini requested a tool, but no gated tool registry is configured.")
            if tool_rounds >= self._max_tool_rounds:
                for function_call in function_calls:
                    self._tool_registry.reject_call(
                        function_call.name or "unknown_tool",
                        function_call.args or {},
                        execution="tool_call_limit",
                        message="The configured tool-call round limit was reached; nothing ran.",
                    )
                raise RuntimeError("Gemini exceeded the configured tool-call round limit.")

            candidates = getattr(response, "candidates", None) or []
            if not candidates or candidates[0].content is None:
                for function_call in function_calls:
                    self._tool_registry.reject_call(
                        function_call.name or "unknown_tool",
                        function_call.args or {},
                        execution="invalid_model_response",
                        message="The model response lacked the content required for safe dispatch.",
                    )
                raise RuntimeError("Gemini returned a function call without its model content.")

            # Preserve Gemini's model turn (including function-call parts) before
            # adding user-role function results to the next request.
            contents.append(candidates[0].content)
            function_response_parts: list[types.Part] = []
            for function_call in function_calls:
                tool_name = function_call.name or "unknown_tool"
                outcome = self._tool_registry.dispatch(tool_name, function_call.args or {})
                function_response_parts.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response=outcome.as_function_response(),
                    )
                )
            contents.append(types.Content(role="user", parts=function_response_parts))
            tool_rounds += 1

    def reply_stream(
        self,
        history: Sequence[ChatMessage],
        system_prompt: str,
        *,
        think: bool = False,
        report_attempts: bool = False,
    ) -> Iterator[StreamChunk]:
        """Stream the reply chunk by chunk instead of returning it all at once.

        Yields :class:`StreamChunk` objects as Gemini generates them. When
        ``think`` is True, the model is asked to narrate its reasoning first
        (``kind="thinking"``) and then the final answer (``kind="answer"``); the
        two are split out of the single streamed generation by
        :func:`_split_thinking`. When ``think`` is False, every chunk is an
        ``"answer"`` chunk.

        Streaming is text-only on purpose: function-calling turns should keep
        using the one-shot :meth:`reply` so the confirmation gate runs
        synchronously. The ``system_instruction`` is still sent, so the model's
        persona and constraints apply.
        """
        if not isinstance(system_prompt, str):
            raise TypeError("system_prompt must be a string.")
        if not system_prompt.strip():
            raise ValueError("system_prompt must not be empty.")
        if not history:
            raise ValueError("history must contain at least one chat message.")

        contents: list[types.Content] = []
        for message in history:
            if not isinstance(message, ChatMessage):
                raise TypeError("history must contain ChatMessage instances.")
            sdk_role = "user" if message.role == "user" else "model"
            contents.append(
                types.Content(
                    role=sdk_role,
                    parts=[types.Part.from_text(text=message.content)],
                )
            )

        call_prompt = system_prompt
        if think:
            call_prompt = f"{system_prompt}\n\n{_THINK_INSTRUCTION}"

        config = types.GenerateContentConfig(system_instruction=call_prompt)

        # Status lines are queued by the stream as it opens and flushed in order
        # ahead of the next text piece, so they never interleave a reply chunk.
        pending: list[StreamChunk] = []
        notify: Callable[[str], None] | None = None
        if report_attempts:
            def notify(text: str) -> None:
                pending.append(StreamChunk(kind="status", text=text))
        response_iter = self._stream_responses(contents, config, notify=notify)

        def texts() -> Iterator[str]:
            for response in response_iter:
                text = _chunk_text(response)
                if text:
                    yield text

        pieces = _split_thinking(texts()) if think else (
            StreamChunk(kind="answer", text=text) for text in texts()
        )
        for piece in pieces:
            yield from pending
            pending.clear()
            yield piece
        yield from pending
        pending.clear()

    def _generation_config(self, system_prompt: str) -> types.GenerateContentConfig:
        config: dict[str, Any] = {"system_instruction": system_prompt}
        if self._tool_registry is not None and self._tool_registry.definitions:
            declarations = [
                types.FunctionDeclaration(
                    name=definition.name,
                    description=definition.description,
                    parameters_json_schema=dict(definition.parameters),
                )
                for definition in self._tool_registry.definitions
            ]
            config["tools"] = [types.Tool(function_declarations=declarations)]
            # We execute function calls ourselves. This explicitly prevents SDK
            # automatic function calling from bypassing the confirmation gate.
            config["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                disable=True
            )
        return types.GenerateContentConfig(**config)


def _chunk_text(response: Any) -> str:
    """Pull the concatenated text out of one streamed Gemini response chunk."""
    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        return text
    parts = getattr(response, "parts", None) or []
    return "".join(getattr(part, "text", "") or "" for part in parts)


def _split_thinking(chunks: Iterator[str]) -> Iterator[StreamChunk]:
    """Split a stream of text chunks into thinking/answer StreamChunks.

    Expects the model to wrap its plain-language reasoning in
    ``<thinking>…</thinking>`` and put the answer afterwards. Emits the
    thinking portion as it arrives (progressive reveal) and emits the answer
    as it arrives too, so the reply streams instead of landing at the end. If
    no opening tag appears, the whole stream is the answer. If a tag opens but
    never closes, whatever was emitted as thinking stays as thinking.
    """
    state: str = "unknown"  # "unknown" -> "thinking" -> "answer"
    tail = ""

    for chunk in chunks:
        if not chunk:
            continue
        buf = tail + chunk
        tail = ""

        if state == "answer":
            yield StreamChunk(kind="answer", text=buf)
            continue

        if state == "unknown":
            if buf.startswith(_THINK_OPEN):
                buf = buf[len(_THINK_OPEN):]
                state = "thinking"
            elif _THINK_OPEN.startswith(buf):
                # Partial opening tag; wait for more before deciding.
                tail = buf
                continue
            else:
                # No thinking tag at all -> straight to the answer.
                state = "answer"
                yield StreamChunk(kind="answer", text=buf)
                continue

        # state == "thinking"
        close_index = buf.find(_THINK_CLOSE)
        if close_index != -1:
            pre = buf[:close_index]
            if pre:
                yield StreamChunk(kind="thinking", text=pre)
            state = "answer"
            after = buf[close_index + len(_THINK_CLOSE):]
            if after:
                yield StreamChunk(kind="answer", text=after)
        else:
            # Keep the last (len(close)-1) chars in case they begin the close tag.
            keep = len(_THINK_CLOSE) - 1
            if len(buf) > keep:
                emit = buf[:-keep]
                yield StreamChunk(kind="thinking", text=emit)
                tail = buf[len(emit):]
            else:
                tail = buf

    if state == "thinking" and tail:
        yield StreamChunk(kind="thinking", text=tail)
    elif state == "unknown" and tail:
        # The stream ended while holding a possible opening tag. It was never a
        # tag, so it is plain answer text. Dropping it would lose the reply's end.
        yield StreamChunk(kind="answer", text=tail)