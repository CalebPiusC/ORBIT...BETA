"""Gemini chat and gated function-calling implementation for ORBIT."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import types

from config import DEFAULT_GEMINI_MODEL, load_settings
from providers.base import ChatMessage, ChatProvider
from tools.registry import ToolRegistry


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
            self._client = genai.Client(api_key=settings.gemini_api_key)
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
