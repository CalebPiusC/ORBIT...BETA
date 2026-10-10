"""Shared, provider-neutral chat types and interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

ChatRole = Literal["user", "assistant"]

# A streamed chunk from the model. ``kind`` is ``"thinking"`` when Orbit is
# narrating its own plain-language reasoning as it works, and ``"answer"`` for
# the final reply. See ``ChatProvider.reply_stream`` for the contract.
# ``"status"`` is a line about which model is being asked (Agent mode only; see
# ``report_attempts``). It is never part of the reply text.
StreamKind = Literal["thinking", "answer", "status"]


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One incremental piece of a streamed reply."""

    kind: StreamKind
    text: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single text message in a conversation history."""

    role: ChatRole
    content: str

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise ValueError("ChatMessage role must be 'user' or 'assistant'.")
        if not isinstance(self.content, str):
            raise TypeError("ChatMessage content must be a string.")
        if not self.content.strip():
            raise ValueError("ChatMessage content must not be empty.")


class ChatProvider(ABC):
    """The chat-only seam between ORBIT and a language-model provider."""

    @abstractmethod
    def reply(self, history: Sequence[ChatMessage], system_prompt: str) -> str:
        """Return the provider's text reply to a conversation."""
        raise NotImplementedError

    def reply_stream(
        self,
        history: Sequence[ChatMessage],
        system_prompt: str,
        *,
        think: bool = False,
        report_attempts: bool = False,
    ) -> Iterator[StreamChunk]:
        """Yield the reply as it is generated, chunk by chunk.

        This is an *addition* to the one-shot ``reply()``: chat mode's quick
        replies may still use ``reply()`` when a simple turn is preferable
        (e.g. tool-using flows). ``reply_stream`` is for the streaming UX in
        the UI — the thinking block and Orbit's own chat replies appearing
        incrementally instead of all at once.

        When ``think`` is True, the provider narrates its reasoning in plain
        language first (``kind="thinking"``) and then streams the final answer
        (``kind="answer"``). Providers that cannot stream may raise
        ``NotImplementedError``; ``GeminiProvider`` implements it for real.

        When ``report_attempts`` is True, the provider also yields
        ``kind="status"`` chunks as it asks each model (for example "Asking
        Chidi…", then a fallback notice). Chat mode leaves it False, so no
        status line appears there.
        """
        raise NotImplementedError
