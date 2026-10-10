"""Gemini chat and gated function-calling implementation for ORBIT."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import types

from config import DEFAULT_GEMINI_MODEL, load_settings
from providers.base import ChatMessage, ChatProvider, StreamChunk
from tools.registry import ToolRegistry

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


class GeminiProvider(ChatProvider):
    """Send chat history to Gemini and run declared tools only through a gate."""

    def __init__(
        self,
        *,
        model: str | None = None,
        client: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        max_tool_rounds: int = 5,
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
        else:
            # Client injection keeps provider tests offline; normal use always
            # builds the SDK client from config.py and the local .env.
            self._client = client
            self.model = model or DEFAULT_GEMINI_MODEL

        self._tool_registry = tool_registry
        self._max_tool_rounds = max_tool_rounds

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
        while True:
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
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

        response_iter = self._client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=config,
        )

        text_iter = (_chunk_text(response) for response in response_iter)
        if think:
            yield from _split_thinking(text_iter)
        else:
            for text in text_iter:
                if text:
                    yield StreamChunk(kind="answer", text=text)

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
    thinking portion as it arrives (progressive reveal) and flips to the
    answer once the closing tag is seen. If no opening tag appears, the whole
    stream is treated as the answer. If a tag opens but never closes, whatever
    was emitted as thinking stays as thinking.
    """
    state: str = "unknown"  # "unknown" -> "thinking" -> "answer"
    tail = ""
    answer = ""

    for chunk in chunks:
        if not chunk:
            continue
        buf = tail + chunk

        if state == "answer":
            answer += buf
            tail = ""
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
                answer += buf
                tail = ""
                continue

        # state == "thinking"
        close_index = buf.find(_THINK_CLOSE)
        if close_index != -1:
            pre = buf[:close_index]
            if pre:
                yield StreamChunk(kind="thinking", text=pre)
            answer = buf[close_index + len(_THINK_CLOSE):]
            state = "answer"
            tail = ""
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
    if answer:
        yield StreamChunk(kind="answer", text=answer)
