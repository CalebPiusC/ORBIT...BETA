"""Small OpenRouter Chat Completions client used by Deep-mode failover."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterError(RuntimeError):
    """A safe-to-display OpenRouter failure without response bodies or secrets."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenRouterProtocolError(OpenRouterError):
    """OpenRouter returned a response that did not match the stream contract."""


@dataclass(frozen=True, slots=True)
class OpenRouterDelta:
    """One streamed content or tool-call update from a Chat Completions response."""

    text: str = ""
    tool_calls: tuple[Mapping[str, Any], ...] = ()


class OpenRouterClient:
    """OpenRouter's OpenAI-compatible streaming endpoint.

    Model ids come from ``.env`` through ``config.py``. This module contains no
    pinned model ids; the caller selects a configured id for each request.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        http_client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("An OpenRouter API key is required.")
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._client = (
            http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)
        )

    def stream(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> Iterator[OpenRouterDelta]:
        """POST one streaming request and yield incremental text/tool updates."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": "ORBIT",
        }
        with self._client.stream(
            "POST",
            OPENROUTER_CHAT_COMPLETIONS_URL,
            headers=headers,
            json=payload,
            timeout=self._timeout_seconds,
        ) as response:
            status = getattr(response, "status_code", 200)
            if status >= 400:
                raise OpenRouterError(
                    f"OpenRouter returned HTTP {status}.", status_code=status
                )

            for raw_line in response.iter_lines():
                if isinstance(raw_line, bytes):
                    line = raw_line.decode("utf-8", errors="replace")
                else:
                    line = str(raw_line)
                if not line or line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if not separator or field != "data":
                    continue
                data = value.lstrip()
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise OpenRouterProtocolError(
                        "OpenRouter returned malformed streaming data."
                    ) from exc
                if not isinstance(event, Mapping):
                    raise OpenRouterProtocolError(
                        "OpenRouter returned malformed streaming data."
                    )
                if "error" in event:
                    error = event.get("error")
                    status_code = _error_status_code(error)
                    message = (
                        f"OpenRouter returned HTTP {status_code}."
                        if status_code is not None
                        else "OpenRouter returned a streaming error."
                    )
                    raise OpenRouterError(message, status_code=status_code)

                choices = event.get("choices")
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, Mapping):
                        continue
                    delta = choice.get("delta")
                    if not isinstance(delta, Mapping):
                        continue
                    text = _content_text(delta.get("content"))
                    raw_calls = delta.get("tool_calls")
                    calls = tuple(
                        call for call in raw_calls if isinstance(call, Mapping)
                    ) if isinstance(raw_calls, list) else ()
                    if text or calls:
                        yield OpenRouterDelta(text=text, tool_calls=calls)


def _content_text(content: Any) -> str:
    """Extract text from either a plain string or a text-part array."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        text = part.get("text")
        if isinstance(text, str):
            pieces.append(text)
    return "".join(pieces)


def _error_status_code(error: Any) -> int | None:
    """Read a numeric status from an API error without preserving its message."""
    if not isinstance(error, Mapping):
        return None
    value = error.get("code") or error.get("status")
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None
