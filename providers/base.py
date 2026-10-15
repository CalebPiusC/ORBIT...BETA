"""Shared, provider-neutral chat types and interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

ChatRole = Literal["user", "assistant"]
ThinkingLevel = Literal["quick", "deep"]

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
class ToolProposal:
    """A tool call the model asked for. Nothing has run; the person must approve it first.

    Chat mode yields at most one of these per reply, and then the stream ends.
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


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


TITLE_SYSTEM_PROMPT = (
    "You write short titles for chat conversations. Reply with only the title: "
    "three to six words, no quotes, no trailing punctuation, no label such as "
    "'Title:'."
)


def title_request(user_text: str, answer: str) -> str:
    """The user turn sent with TITLE_SYSTEM_PROMPT. Only the first exchange is sent."""
    return (
        "Title this conversation from its first exchange.\n\n"
        f"User: {user_text[:1000]}\n\nAssistant: {answer[:1000]}"
    )


SUMMARY_SYSTEM_PROMPT = (
    "You write short project summaries. Reply with only the summary: one or "
    "two plain sentences about what the project is, no quotes, no label such "
    "as 'Summary:'."
)


def summary_request(project_name: str, description: str, user_text: str, answer: str) -> str:
    """The user turn sent with SUMMARY_SYSTEM_PROMPT. Only the first exchange is sent."""
    return (
        "Summarize what this project is, from its first exchange.\n\n"
        f"Project: {project_name[:200]}\n"
        f"Description from the owner: {description[:500] or '(none)'}\n\n"
        f"User: {user_text[:1000]}\n\nAssistant: {answer[:1000]}"
    )


class ChatProvider(ABC):
    """The chat-only seam between ORBIT and a language-model provider."""

    @abstractmethod
    def reply(self, history: Sequence[ChatMessage], system_prompt: str) -> str:
        """Return the provider's text reply to a conversation."""
        raise NotImplementedError

    def title(self, user_text: str, answer: str) -> str:
        """Return a short title for a conversation's first exchange.

        One lightweight call, made once per conversation. The default goes
        through ``reply()``. A provider that can make a call with no tools
        attached should override this, so a title request can never propose
        an action.
        """
        history = [ChatMessage(role="user", content=title_request(user_text, answer))]
        return self.reply(history, TITLE_SYSTEM_PROMPT)

    def summarize(self, project_name: str, description: str, user_text: str, answer: str) -> str:
        """Return a one-or-two-sentence summary of what a project is.

        One lightweight call, made once per project after its first Main-Agent
        exchange — the same contract as ``title()``: the default goes through
        ``reply()``, and a provider that can detach tools should override it,
        so a summary request can never propose an action.
        """
        history = [
            ChatMessage(
                role="user",
                content=summary_request(project_name, description, user_text, answer),
            )
        ]
        return self.reply(history, SUMMARY_SYSTEM_PROMPT)

    def reply_stream(
        self,
        history: Sequence[ChatMessage],
        system_prompt: str,
        *,
        think: bool = False,
        report_attempts: bool = False,
        allow_tools: bool = False,
        thinking_level: ThinkingLevel = "quick",
    ) -> Iterator["StreamChunk | ToolProposal"]:
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
        Titan…", then a fallback notice). Chat mode leaves it False, so no
        status line appears there.

        ``thinking_level`` selects the provider's Quick or Deep response policy.
        Quick uses the provider's low-latency route and smaller output-token cap;
        Deep uses a larger cap and the provider's configured deep-model chain.

        When ``allow_tools`` is True, the model may ask for one tool call. The
        provider then yields a :class:`ToolProposal` and stops reading the
        stream. It never runs the tool: the caller queues the proposal for the
        person to approve. Providers that cannot offer tools ignore the flag.
        """
        raise NotImplementedError
