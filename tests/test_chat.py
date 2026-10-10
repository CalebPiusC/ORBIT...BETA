from __future__ import annotations

import unittest

from chat import MAX_MESSAGES, SYSTEM_PROMPT, run_session
from providers.base import ChatMessage


class ScriptedProvider:
    """Counts turns and echoes the last user message; no network."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[list[ChatMessage]] = []
        self._fail_times = fail_times

    def reply(self, history, system_prompt: str) -> str:
        if system_prompt != SYSTEM_PROMPT:
            raise AssertionError("the chat CLI must send ORBIT's system prompt")
        self.calls.append(list(history))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("HTTP 401 — bad key")
        return f"echo:{history[-1].content}"


class ChatSessionTests(unittest.TestCase):
    def _run(self, prompts: list[str], provider: ScriptedProvider) -> tuple[int, list[str]]:
        out: list[str] = []
        remaining = list(prompts)

        def read(_prompt: str) -> str:
            if not remaining:
                raise EOFError
            return remaining.pop(0)

        code = run_session(provider, read=read, write=out.append)
        return code, out

    def test_session_survives_multiple_turns_and_carries_history(self) -> None:
        provider = ScriptedProvider()
        code, out = self._run(["first", "second", "/quit"], provider)

        self.assertEqual(code, 0)
        self.assertEqual(len(provider.calls), 2, "one model call per prompt")
        # Turn two is sent with turn one still in history, in role order.
        self.assertEqual(
            [(m.role, m.content) for m in provider.calls[1]],
            [("user", "first"), ("assistant", "echo:first"), ("user", "second")],
        )
        self.assertIn("\nORBIT: echo:first", out)
        self.assertIn("\nORBIT: echo:second", out)

    def test_provider_failure_keeps_the_session_open_and_requeues_the_turn(self) -> None:
        provider = ScriptedProvider(fail_times=1)
        code, out = self._run(["hello", "/quit"], provider)

        self.assertEqual(code, 0)
        self.assertTrue(any("[error]" in line and "bad key" in line for line in out))
        # The failed user turn was not left in history twice; the caller retries it.
        self.assertEqual([m.content for m in provider.calls[0]], ["hello"])

    def test_empty_input_and_eof_do_not_call_the_model(self) -> None:
        provider = ScriptedProvider()
        code, _ = self._run(["", "   "], provider)

        self.assertEqual(code, 0)
        self.assertEqual(provider.calls, [])

    def test_reset_clears_history(self) -> None:
        provider = ScriptedProvider()
        self._run(["keep me", "/reset", "fresh", "/quit"], provider)

        self.assertEqual([m.content for m in provider.calls[-1]], ["fresh"])

    def test_history_is_capped(self) -> None:
        provider = ScriptedProvider()
        prompts = [f"m{i}" for i in range(MAX_MESSAGES + 6)] + ["/quit"]
        self._run(prompts, provider)

        self.assertLessEqual(len(provider.calls[-1]), MAX_MESSAGES)


if __name__ == "__main__":
    unittest.main()
