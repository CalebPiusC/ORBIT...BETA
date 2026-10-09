"""Shared, provider-neutral chat types and interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

ChatRole = Literal["user", "assistant"]


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
