"""Gemini chat and gated function-calling implementation for ORBIT."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError

from config import (
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    load_openrouter_settings,
    load_settings,
)
from providers.base import (
    SUMMARY_SYSTEM_PROMPT,
    TITLE_SYSTEM_PROMPT,
    summary_request,
    ChatMessage,
    ChatProvider,
    StreamChunk,
    ThinkingLevel,
    ToolProposal,
    title_request,
)
from providers.openrouter import OpenRouterClient, OpenRouterDelta, OpenRouterError
from providers.personas import slot_name
from providers.response_diagnostics import response_diagnostics
from tools.registry import AuditLog, ToolRegistry

# The only failures the fallback model is allowed to get past: the server
# said it is slow or overloaded, or the connection broke before an answer.
# Auth, quota, and bad-request errors are NOT retried. A different model would
# fail the same way, and retrying would hide the real problem.
FALLBACK_STATUS_CODES = (503, 504)

_END = object()
log = logging.getLogger("orbit.gemini")
QUICK_MAX_OUTPUT_TOKENS = 512
DEEP_MAX_OUTPUT_TOKENS = 4096

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


def _attempt_reason(exc: BaseException) -> str:
    """The short reason one model attempt failed, e.g. ``"504"`` or ``"ReadTimeout"``."""
    return _fallback_reason(exc) or _failure_label(exc)


def _model_slots(values: Sequence[str]) -> tuple[str, str, str]:
    """Normalize the three pinned ids without shifting their persona slots."""
    slots = [raw.strip() for raw in values[:3]]
    slots.extend([""] * (3 - len(slots)))
    return slots[0], slots[1], slots[2]


class GeminiProvider(ChatProvider):
    """Stream Gemini replies, using an optional OpenRouter chain for Deep mode.

    Quick mode retains Gemini's configured primary/fallback behavior. Deep
    mode tries the configured OpenRouter models in order, then its auto-router;
    Gemini's configured primary is the final fallback. Gemini tools still pass
    through the confirmation gate, and no model failover happens after text or
    a tool proposal has started streaming.
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
        openrouter_api_key: str | None = None,
        openrouter_models: Sequence[str] | None = None,
        openrouter_fallback_model: str | None = None,
        openrouter_client: Any | None = None,
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
            configured_openrouter_key = (
                settings.openrouter_api_key if openrouter_api_key is None else openrouter_api_key
            )
            configured_openrouter_models = (
                settings.openrouter_models if openrouter_models is None else openrouter_models
            )
            configured_openrouter_fallback = (
                settings.openrouter_fallback_model
                if openrouter_fallback_model is None
                else openrouter_fallback_model
            )
            request_timeout_seconds = settings.request_timeout_seconds
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
            optional_settings = load_openrouter_settings()
            configured_openrouter_key = (
                optional_settings.api_key if openrouter_api_key is None else openrouter_api_key
            )
            configured_openrouter_models = (
                optional_settings.models if openrouter_models is None else openrouter_models
            )
            configured_openrouter_fallback = (
                optional_settings.fallback_model
                if openrouter_fallback_model is None
                else openrouter_fallback_model
            )
            request_timeout_seconds = DEFAULT_REQUEST_TIMEOUT_SECONDS
            self._audit = audit_log

        # "" or a fallback equal to the primary both mean "no fallback".
        self.fallback_model: str | None = configured_fallback or None
        if self.fallback_model == self.model:
            self.fallback_model = None

        self._openrouter_api_key = (configured_openrouter_key or "").strip()
        self.openrouter_models = _model_slots(configured_openrouter_models or ())
        self.openrouter_fallback_model = (configured_openrouter_fallback or "").strip()
        if self.openrouter_fallback_model in self.openrouter_models:
            self.openrouter_fallback_model = ""
        self._openrouter_client = openrouter_client
        if self._openrouter_client is None and self._openrouter_api_key:
            self._openrouter_client = OpenRouterClient(
                self._openrouter_api_key,
                timeout_seconds=request_timeout_seconds,
            )

        # The model that produced the most recent answer. Set on success only.
        self.last_model_used: str | None = None
        # Why each failed attempt in the most recent turn failed, in order. A
        # turn that failed on the fallback still shows the primary's reason, so
        # the caller can tell "Google said 504, then the fallback timed out"
        # apart from "the connection never came back".
        self.last_attempts: list[str] = []

        self._tool_registry = tool_registry
        self._max_tool_rounds = max_tool_rounds

    # --- model calls with a single fallback ---------------------------------

    def _audit_failure(self, model: str, role: str, exc: BaseException, started: float) -> None:
        self.last_attempts.append(_attempt_reason(exc))
        if self._audit is not None:
            self._audit.record_model_event(
                event="model_attempt_failed",
                model=model,
                role=role,
                reason=_failure_label(exc),
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )

    def _audit_answer(
        self,
        model: str,
        *,
        stream: bool,
        role: str | None = None,
        fallback_used: bool | None = None,
        provider: str | None = None,
        persona: str | None = None,
    ) -> None:
        self.last_model_used = model
        if self._audit is not None:
            fields: dict[str, Any] = {
                "event": "model_answered",
                "model": model,
                "role": role or ("primary" if model == self.model else "fallback"),
                "fallback_used": model != self.model if fallback_used is None else fallback_used,
                "stream": stream,
                "persona": persona or self._persona_for_model(model),
            }
            if provider is not None:
                fields["provider"] = provider
            self._audit.record_model_event(**fields)

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

    def _persona_for_model(self, model: str) -> str:
        """Return the assigned display name for the model that is actually called."""
        if self.fallback_model and model == self.fallback_model:
            return slot_name(1)
        return slot_name(0)

    def _model_order_for_level(self, thinking_level: ThinkingLevel) -> tuple[str, str | None]:
        """The native-model chain used when OpenRouter is not in the request path."""
        if thinking_level == "deep":
            return self.model, None
        return self.model, self.fallback_model

    def _stream_responses(
        self,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
        notify: Callable[[str], None] | None = None,
        *,
        model_order: tuple[str, str | None] | None = None,
        persona_resolver: Callable[[str], str] | None = None,
        role_resolver: Callable[[str], str] | None = None,
        fallback_used: bool | None = None,
    ) -> Iterator[Any]:
        """Yield SDK stream responses, failing over only before the first chunk.

        Once any chunk has reached the user, a failure is surfaced rather than
        restarted on the fallback, because a restart would repeat the text
        already shown. ``notify`` receives a short status line before each
        model is asked, and when the primary has failed.
        """
        say = notify or (lambda _text: None)
        model, alternate_model = model_order or (self.model, self.fallback_model)
        get_persona = persona_resolver or self._persona_for_model
        get_role = role_resolver or (
            lambda requested: "primary" if requested == self.model else "fallback"
        )
        first_persona = get_persona(model)
        say(f"Asking {first_persona}…")
        started = time.monotonic()
        try:
            stream, first = self._open_stream(model, contents, config)
        except Exception as exc:
            reason = _fallback_reason(exc)
            self._audit_failure(model, get_role(model), exc, started)
            if not (reason and alternate_model):
                raise
            model = alternate_model
            alternate_persona = get_persona(model)
            say(
                f"{first_persona} didn't answer ({_describe_failure(reason)}). "
                f"Asking {alternate_persona}…"
            )
            started = time.monotonic()
            try:
                stream, first = self._open_stream(model, contents, config)
            except Exception as fallback_exc:
                self._audit_failure(model, get_role(model), fallback_exc, started)
                raise

        self._audit_answer(
            model,
            stream=True,
            role=get_role(model),
            fallback_used=fallback_used,
            persona=get_persona(model),
        )
        if first is not _END:
            yield first
        yield from stream

    def _openrouter_attempt_chain(self) -> tuple[tuple[str, str], ...]:
        """Return configured OpenRouter attempts with position-based personas."""
        if not self._openrouter_api_key or self._openrouter_client is None:
            return ()

        attempts: list[tuple[str, str]] = []
        seen: set[str] = set()
        for slot, model in enumerate(self.openrouter_models):
            if model and model not in seen:
                attempts.append((model, slot_name(slot + 2)))
                seen.add(model)
        if self.openrouter_fallback_model and self.openrouter_fallback_model not in seen:
            # The auto-router is a safety net, not one of the named model slots.
            attempts.append((self.openrouter_fallback_model, self.openrouter_fallback_model))
        return tuple(attempts)

    @staticmethod
    def _describe_openrouter_failure(exc: BaseException) -> str:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return f"OpenRouter returned {status}"
        if isinstance(exc, httpx.TimeoutException):
            return "the request timed out"
        if isinstance(exc, httpx.TransportError):
            return "the connection failed"
        return "the request failed"

    @staticmethod
    def _openrouter_attempt_reason(exc: BaseException) -> str:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return str(status)
        return type(exc).__name__

    @staticmethod
    def _openrouter_failure_label(exc: BaseException) -> str:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return f"OpenRouterError {status}"
        return type(exc).__name__

    def _audit_openrouter_failure(
        self, model: str, role: str, exc: BaseException, started: float
    ) -> None:
        self.last_attempts.append(self._openrouter_attempt_reason(exc))
        if self._audit is not None:
            self._audit.record_model_event(
                event="model_attempt_failed",
                model=model,
                role=role,
                provider="openrouter",
                reason=self._openrouter_failure_label(exc),
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )

    def _audit_openrouter_tail(
        self,
        first: OpenRouterDelta,
        rest: Iterator[OpenRouterDelta],
        *,
        model: str,
        role: str,
        started: float,
    ) -> Iterator[OpenRouterDelta]:
        yield first
        try:
            yield from rest
        except Exception as exc:
            self._audit_openrouter_failure(model, role, exc, started)
            raise

    def _select_openrouter_stream(
        self,
        history: Sequence[ChatMessage],
        system_prompt: str,
        *,
        notify: Callable[[str], None] | None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[str, str, str, Iterator[OpenRouterDelta], int] | None:
        """Open the first usable OpenRouter stream, retrying before any output.

        Connection/setup errors and empty preambles may move to the next model.
        Once the first text or tool-call delta arrives, this turn is committed to
        that stream; restarting after partial output could duplicate a reply.
        """
        attempts = self._openrouter_attempt_chain()
        if not attempts:
            return None

        messages: list[Mapping[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend({"role": message.role, "content": message.content} for message in history)
        previous_persona: str | None = None
        previous_failure: BaseException | None = None

        for index, (model, persona) in enumerate(attempts):
            role = "primary" if index == 0 else "fallback"
            if notify is not None:
                if previous_failure is None:
                    notify(f"Asking {persona}…")
                else:
                    assert previous_persona is not None
                    reason = self._describe_openrouter_failure(previous_failure)
                    notify(f"{previous_persona} didn't answer ({reason}). Asking {persona}…")

            started = time.monotonic()
            stream: Iterator[OpenRouterDelta] | None = None
            try:
                stream = iter(
                    self._openrouter_client.stream(
                        model=model,
                        messages=messages,
                        max_tokens=DEEP_MAX_OUTPUT_TOKENS,
                        tools=tools,
                    )
                )
                first_raw = next(stream, None)
                if first_raw is None:
                    raise OpenRouterError("OpenRouter returned an empty stream.")
                first = (
                    first_raw
                    if isinstance(first_raw, OpenRouterDelta)
                    else OpenRouterDelta(
                        text=(
                            first_raw.text
                            if isinstance(getattr(first_raw, "text", None), str)
                            else ""
                        ),
                        tool_calls=tuple(
                            call
                            for call in (getattr(first_raw, "tool_calls", ()) or ())
                            if isinstance(call, Mapping)
                        ),
                    )
                )
                if not first.text.strip() and not (tools and first.tool_calls):
                    raise OpenRouterError("OpenRouter returned an empty stream.")
            except Exception as exc:
                if stream is not None:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                self._audit_openrouter_failure(model, role, exc, started)
                previous_persona = persona
                previous_failure = exc
                continue

            self._audit_answer(
                model,
                stream=True,
                role=role,
                fallback_used=index > 0,
                provider="openrouter",
                persona=persona,
            )
            audited = self._audit_openrouter_tail(
                first,
                stream,
                model=model,
                role=role,
                started=started,
            )
            return model, persona, role, audited, index

        if notify is not None and previous_failure is not None:
            assert previous_persona is not None
            reason = self._describe_openrouter_failure(previous_failure)
            notify(f"{previous_persona} didn't answer ({reason}). Asking {slot_name(1)}…")
        return None

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

    def _stream_deep_responses(
        self,
        history: Sequence[ChatMessage],
        contents: list[types.Content],
        config: types.GenerateContentConfig,
        system_prompt: str,
        *,
        notify: Callable[[str], None] | None,
    ) -> Iterator[Any]:
        """Try the configured OpenRouter chain, then stream Europa from Gemini."""
        openrouter_enabled = bool(self._openrouter_attempt_chain())
        selected = self._select_openrouter_stream(
            history,
            system_prompt,
            notify=notify,
        )
        if selected is not None:
            _model, _persona, _role, stream, _index = selected
            for delta in stream:
                if delta.text:
                    yield SimpleNamespace(text=delta.text, parts=[])
            return

        # When OpenRouter has no usable configuration, make the direct route
        # explicit in Agent mode. Exhausted OpenRouter attempts already queued
        # the final "... Asking Europa" status line above.
        if not openrouter_enabled and notify is not None:
            notify(f"Asking {slot_name(1)}…")
        yield from self._stream_responses(
            contents,
            config,
            model_order=(self.model, None),
            persona_resolver=lambda _model: slot_name(1),
            role_resolver=lambda _model: "fallback" if openrouter_enabled else "primary",
            fallback_used=openrouter_enabled,
        )

    def _openrouter_tool_definitions(self) -> list[Mapping[str, Any]] | None:
        if self._tool_registry is None or not self._tool_registry.definitions:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": dict(definition.parameters),
                },
            }
            for definition in self._tool_registry.definitions
        ]

    @staticmethod
    def _accumulate_openrouter_tool_calls(
        accumulated: dict[int, dict[str, str]],
        calls: Sequence[Mapping[str, Any]],
    ) -> None:
        for call in calls:
            try:
                index = int(call.get("index", 0))
            except (TypeError, ValueError):
                index = 0
            current = accumulated.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                current["id"] = call_id
            function = call.get("function")
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(name, str):
                current["name"] += name
            if isinstance(arguments, str):
                current["arguments"] += arguments

    def _tool_proposal_from_openrouter_calls(
        self, accumulated: Mapping[int, Mapping[str, str]]
    ) -> ToolProposal | None:
        if self._tool_registry is None or not accumulated:
            return None
        calls = [accumulated[index] for index in sorted(accumulated)]
        parsed: list[tuple[str, Mapping[str, Any]]] = []
        for call in calls:
            name = call.get("name", "").strip() or "unknown_tool"
            raw_arguments = call.get("arguments", "")
            try:
                arguments = json.loads(raw_arguments) if raw_arguments else {}
            except json.JSONDecodeError:
                arguments = {"_invalid_arguments": raw_arguments}
            if not isinstance(arguments, Mapping):
                arguments = {"_invalid_arguments": raw_arguments}
            parsed.append((name, arguments))

        for name, arguments in parsed[1:]:
            self._tool_registry.reject_call(
                name,
                arguments,
                execution="tool_call_limit",
                message="Chat mode allows one tool call per reply; this extra call did not run.",
            )
        name, arguments = parsed[0]
        return ToolProposal(name=name, arguments=dict(arguments))

    def _stream_deep_with_tools(
        self,
        history: Sequence[ChatMessage],
        contents: list[types.Content],
        system_prompt: str,
        *,
        notify: Callable[[str], None] | None,
        pending: list[StreamChunk],
    ) -> Iterator[StreamChunk | ToolProposal]:
        """Offer gated tool calls through OpenRouter, with Europa as final fallback."""
        assert self._tool_registry is not None
        openrouter_enabled = bool(self._openrouter_attempt_chain())
        selected = self._select_openrouter_stream(
            history,
            system_prompt,
            notify=notify,
            tools=self._openrouter_tool_definitions(),
        )
        if selected is None:
            if not openrouter_enabled and notify is not None:
                notify(f"Asking {slot_name(1)}…")
            gemini_chunks = self._stream_with_tools(
                contents,
                system_prompt,
                thinking_level="deep",
                model_order=(self.model, None),
                persona_resolver=lambda _model: slot_name(1),
                role_resolver=lambda _model: "fallback" if openrouter_enabled else "primary",
                fallback_used=openrouter_enabled,
            )
            for chunk in gemini_chunks:
                yield from pending
                pending.clear()
                yield chunk
            yield from pending
            pending.clear()
            return

        _model, _persona, _role, stream, _index = selected
        accumulated: dict[int, dict[str, str]] = {}
        for delta in stream:
            if delta.text:
                yield from pending
                pending.clear()
                yield StreamChunk(kind="answer", text=delta.text)
            self._accumulate_openrouter_tool_calls(accumulated, delta.tool_calls)

        proposal = self._tool_proposal_from_openrouter_calls(accumulated)
        if proposal is not None:
            yield from pending
            pending.clear()
            yield proposal
        yield from pending
        pending.clear()

    def title(self, user_text: str, answer: str) -> str:
        """One no-tools call that names the conversation.

        The config carries no tool declarations, so a title request can never
        propose an action. Fallback and audit apply as they do for any call.
        """
        self.last_attempts = []
        contents = [
            types.Content(role="user", parts=[types.Part.from_text(text=title_request(user_text, answer))])
        ]
        config = types.GenerateContentConfig(system_instruction=TITLE_SYSTEM_PROMPT)
        response, _model = self._generate(contents, config, model=self.model, allow_fallback=True)
        text = getattr(response, "text", None)
        return text if isinstance(text, str) else ""

    def summarize(self, project_name: str, description: str, user_text: str, answer: str) -> str:
        """One no-tools call that summarizes the project.

        Same shape as ``title()``: no tool declarations, so a summary request
        can never propose an action. Fallback and audit apply as for any call.
        """
        self.last_attempts = []
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=summary_request(project_name, description, user_text, answer))],
            )
        ]
        config = types.GenerateContentConfig(system_instruction=SUMMARY_SYSTEM_PROMPT)
        response, _model = self._generate(contents, config, model=self.model, allow_fallback=True)
        text = getattr(response, "text", None)
        return text if isinstance(text, str) else ""

    def reply(self, history: Sequence[ChatMessage], system_prompt: str) -> str:
        if not isinstance(system_prompt, str):
            raise TypeError("system_prompt must be a string.")
        if not system_prompt.strip():
            raise ValueError("system_prompt must not be empty.")
        if not history:
            raise ValueError("history must contain at least one chat message.")

        self.last_attempts = []
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
        allow_tools: bool = False,
        thinking_level: ThinkingLevel = "quick",
    ) -> Iterator[StreamChunk | ToolProposal]:
        """Stream the reply chunk by chunk instead of returning it all at once.

        Yields :class:`StreamChunk` objects as Gemini generates them. When
        ``think`` is True, the model is asked to narrate its reasoning first
        (``kind="thinking"``) and then the final answer (``kind="answer"``); the
        two are split out of the single streamed generation by
        :func:`_split_thinking`. When ``think`` is False, every chunk is an
        ``"answer"`` chunk.

        The ``system_instruction`` is sent to every provider, so the model's
        persona and constraints apply. Quick uses Gemini's configured primary
        with a 512-token cap and its existing fallback behavior. Deep uses a
        4096-token cap and tries configured OpenRouter models in order, then the
        configured OpenRouter auto-router; Gemini's primary is Europa's final
        fallback. When tools are enabled, proposals from either provider still
        go through the existing approval gate.
        """
        if not isinstance(system_prompt, str):
            raise TypeError("system_prompt must be a string.")
        if not system_prompt.strip():
            raise ValueError("system_prompt must not be empty.")
        if thinking_level not in ("quick", "deep"):
            raise ValueError("thinking_level must be 'quick' or 'deep'.")
        if not history:
            raise ValueError("history must contain at least one chat message.")

        self.last_attempts = []
        self.last_model_used = None
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

        if allow_tools and think:
            raise ValueError("Tool calls are offered only for plain replies (think=False).")

        # Status lines are queued by the stream as it opens and flushed in order
        # ahead of the next text piece, so they never interleave a reply chunk.
        pending: list[StreamChunk] = []
        notify: Callable[[str], None] | None = None
        if report_attempts:
            def notify(text: str) -> None:
                pending.append(StreamChunk(kind="status", text=text))

        if (
            thinking_level == "deep"
            and allow_tools
            and self._tool_registry is not None
            and self._tool_registry.definitions
        ):
            yield from self._stream_deep_with_tools(
                history,
                contents,
                system_prompt,
                notify=notify,
                pending=pending,
            )
            return

        if allow_tools and self._tool_registry is not None and self._tool_registry.definitions:
            yield from self._stream_with_tools(
                contents, system_prompt, thinking_level=thinking_level
            )
            return

        call_prompt = system_prompt
        if think:
            call_prompt = f"{system_prompt}\n\n{_THINK_INSTRUCTION}"

        config = self._generation_config(
            call_prompt, thinking_level=thinking_level, include_tools=False
        )
        if thinking_level == "deep":
            response_iter = self._stream_deep_responses(
                history,
                contents,
                config,
                call_prompt,
                notify=notify,
            )
        else:
            response_iter = self._stream_responses(
                contents,
                config,
                notify=notify,
                model_order=self._model_order_for_level(thinking_level),
            )

        response_count = 0
        response_notes: list[dict[str, Any]] = []
        visible_text_characters = 0
        stream_completed = False

        def texts() -> Iterator[str]:
            nonlocal response_count, visible_text_characters, stream_completed
            try:
                for response in response_iter:
                    response_count += 1
                    text = _chunk_text(response)
                    if text and text.strip():
                        visible_text_characters += len(text.strip())
                        yield text
                    elif len(response_notes) < 32:
                        response_notes.append(
                            response_diagnostics(response, chunk_index=response_count - 1)
                        )
            except Exception as exc:
                if response_count and visible_text_characters == 0:
                    self._record_empty_response(
                        response_count, response_notes, failure_type=type(exc).__name__
                    )
                raise
            stream_completed = True

        pieces = _split_thinking(texts()) if think else (
            StreamChunk(kind="answer", text=text) for text in texts()
        )
        for piece in pieces:
            yield from pending
            pending.clear()
            yield piece
        yield from pending
        pending.clear()
        if stream_completed and visible_text_characters == 0:
            self._record_empty_response(response_count, response_notes)

    def _record_empty_response(
        self,
        response_count: int,
        response_notes: list[dict[str, Any]],
        *,
        failure_type: str | None = None,
    ) -> None:
        """Log a textless response with safe raw-response metadata."""
        model = self.last_model_used or self.model
        role = "primary" if model == self.model else "fallback"
        diagnostics = {
            "response_count": response_count,
            "response_diagnostics": response_notes,
        }
        if failure_type is not None:
            diagnostics["failure_type"] = failure_type
        log.error(
            "Gemini stream produced no reply text; raw response diagnostics: %s",
            json.dumps(diagnostics, ensure_ascii=False, sort_keys=True, default=str),
        )
        if self._audit is not None:
            try:
                self._audit.record_model_event(
                    event="model_empty_response",
                    model=model,
                    role=role,
                    stream=True,
                    reason="no_text",
                    **diagnostics,
                )
            except Exception:
                log.exception("could not write Gemini empty-response diagnostics to audit.log")

    def _stream_with_tools(
        self,
        contents: list[types.Content],
        system_prompt: str,
        *,
        thinking_level: ThinkingLevel,
        model_order: tuple[str, str | None] | None = None,
        persona_resolver: Callable[[str], str] | None = None,
        role_resolver: Callable[[str], str] | None = None,
        fallback_used: bool | None = None,
    ) -> Iterator[StreamChunk | ToolProposal]:
        """Stream a plain reply that may end in one tool proposal.

        Text is streamed as it arrives. The first function call ends the stream
        and is returned as a :class:`ToolProposal`, which nothing executes here.
        Any further calls in the same reply are audited as rejected, so one
        reply can never ask for more than one action.
        """
        assert self._tool_registry is not None
        config = self._generation_config(system_prompt, thinking_level=thinking_level)
        for response in self._stream_responses(
            contents,
            config,
            model_order=model_order or self._model_order_for_level(thinking_level),
            persona_resolver=persona_resolver,
            role_resolver=role_resolver,
            fallback_used=fallback_used,
        ):
            parts = _response_parts(response)
            text = "".join(part.text for part in parts if isinstance(getattr(part, "text", None), str))
            if text:
                yield StreamChunk(kind="answer", text=text)
            calls = [part.function_call for part in parts if getattr(part, "function_call", None)]
            if not calls:
                continue
            for extra in calls[1:]:
                self._tool_registry.reject_call(
                    extra.name or "unknown_tool",
                    extra.args or {},
                    execution="tool_call_limit",
                    message="Chat mode allows one tool call per reply; this extra call did not run.",
                )
            first = calls[0]
            yield ToolProposal(name=first.name or "unknown_tool", arguments=dict(first.args or {}))
            return

    def _generation_config(
        self,
        system_prompt: str,
        *,
        thinking_level: ThinkingLevel | None = None,
        include_tools: bool = True,
    ) -> types.GenerateContentConfig:
        config: dict[str, Any] = {"system_instruction": system_prompt}
        if thinking_level is not None:
            if thinking_level not in ("quick", "deep"):
                raise ValueError("thinking_level must be 'quick' or 'deep'.")
            config["max_output_tokens"] = (
                QUICK_MAX_OUTPUT_TOKENS
                if thinking_level == "quick"
                else DEEP_MAX_OUTPUT_TOKENS
            )
        if include_tools and self._tool_registry is not None and self._tool_registry.definitions:
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


def _response_parts(response: Any) -> list[Any]:
    """The parts of a streamed response's first candidate, or an empty list."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates or candidates[0].content is None:
        return []
    return list(candidates[0].content.parts or [])


def _chunk_text(response: Any) -> str:
    """Pull text from a response without triggering SDK text-accessor errors.

    Gemini's convenience ``response.text`` property can raise when the first
    candidate is a function call or has no text part. In that case, inspect the
    raw parts directly so the caller can record useful empty-response metadata.
    """
    try:
        text = getattr(response, "text", None)
    except (AttributeError, ValueError):
        text = None
    if isinstance(text, str) and text:
        return text
    parts = getattr(response, "parts", None) or _response_parts(response)
    pieces: list[str] = []
    for part in parts:
        try:
            part_text = getattr(part, "text", None)
        except (AttributeError, ValueError):
            part_text = None
        if isinstance(part_text, str):
            pieces.append(part_text)
    return "".join(pieces)


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