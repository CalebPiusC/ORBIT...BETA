from __future__ import annotations

import unittest
from unittest.mock import patch

from config import Settings

from chat import (
    MAX_MESSAGES,
    SYSTEM_PROMPT,
    mask_key,
    run_check,
    run_session,
)
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


class CancelOnceProvider:
    """Raises KeyboardInterrupt on the first turn, then answers normally."""

    def __init__(self) -> None:
        self.histories: list[list[ChatMessage]] = []
        self.model = "gemini-test-model"

    def reply(self, history, system_prompt: str) -> str:
        if system_prompt != SYSTEM_PROMPT:
            raise AssertionError("the chat CLI must send ORBIT's system prompt")
        self.histories.append(list(history))
        if len(self.histories) == 1:
            raise KeyboardInterrupt
        return "ok"


class WaitingTests(unittest.TestCase):
    def _run(self, prompts: list[str], provider: CancelOnceProvider) -> tuple[int, list[str]]:
        out: list[str] = []
        remaining = list(prompts)

        def read(_prompt: str) -> str:
            if not remaining:
                raise EOFError
            return remaining.pop(0)

        code = run_session(provider, read=read, write=out.append)
        return code, out

    def test_a_blocking_turn_says_so_before_it_blocks(self) -> None:
        """A silent wait is indistinguishable from a hang — the original bug."""
        provider = CancelOnceProvider()
        _, out = self._run(["hello", "hello", "/quit"], provider)

        self.assertTrue(
            any("waiting on gemini-test-model" in line for line in out),
            f"no wait line before blocking: {out}",
        )
        self.assertTrue(any("Ctrl-C cancels this turn" in line for line in out))

    def test_ctrl_c_cancels_the_turn_not_the_session(self) -> None:
        provider = CancelOnceProvider()
        code, out = self._run(["hello", "hello", "/quit"], provider)

        self.assertEqual(code, 0, "the session must survive a cancelled turn")
        self.assertEqual(len(provider.histories), 2, "the retry must reach the model again")
        self.assertTrue(any("cancelled" in line for line in out))
        # The cancelled turn was popped, so the retry is not sent twice.
        self.assertEqual([m.content for m in provider.histories[1]], ["hello"])


class SetupCheckTests(unittest.TestCase):
    # A made-up word, not a credential and not a Google key shape: the
    # pre-commit guard rejects AIza/AQ. patterns, and no real key belongs in a
    # fixture in the first place.
    KEY = "local-test-key-for-offline-tests"

    def _settings(self, **kwargs: object) -> Settings:
        base = {"gemini_api_key": self.KEY, "gemini_model": "gemini-test-model"}
        base.update(kwargs)
        return Settings(**base)  # type: ignore[arg-type]

    def _check(self, **kwargs: object) -> tuple[int, str]:
        out: list[str] = []
        code = run_check(write=out.append, **kwargs)  # type: ignore[arg-type]
        return code, "\n".join(out)

    def _all_passing(self) -> dict[str, object]:
        class OkProvider:
            def reply(self, history, system_prompt: str) -> str:
                return "pong"

        return {"settings": self._settings(), "connect": lambda *a: 0.01, "provider": OkProvider()}

    def test_a_healthy_setup_reports_all_three_layers(self) -> None:
        code, text = self._check(**self._all_passing())

        self.assertEqual(code, 0)
        self.assertIn("[config] ok", text)
        self.assertIn("[network] ok", text)
        self.assertIn("[model] ok", text)

    def test_the_key_is_masked_in_every_branch(self) -> None:
        """No full credential in any output, including the failure paths."""

        class Boom:
            def reply(self, history, system_prompt: str) -> str:
                raise RuntimeError(f"HTTP 401 while using key {SetupCheckTests.KEY}")

        def boom(*_a: object) -> float:
            raise OSError("timed out")

        cases = [
            self._all_passing(),
            {**self._all_passing(), "connect": boom},
            {**self._all_passing(), "provider": Boom()},
        ]
        for kwargs in cases:
            _, text = self._check(**kwargs)
            self.assertNotIn(self.KEY, text)
            self.assertIn(mask_key(self.KEY), text)

    def test_a_failing_layer_stops_the_walk(self) -> None:
        def boom(*_a: object) -> float:
            raise OSError("timed out")

        code, text = self._check(**{**self._all_passing(), "connect": boom})

        self.assertEqual(code, 3)
        self.assertIn("[network] FAIL", text)
        self.assertNotIn("[model]", text, "once a layer fails, later ones must not run")

    def test_a_config_failure_prints_a_line_not_a_traceback(self) -> None:
        def boom() -> Settings:
            raise RuntimeError("GEMINI_API_KEY is not configured. Copy .env.example to .env and add your key.")

        out: list[str] = []
        with patch("chat.load_settings", side_effect=boom):
            code = run_check(write=out.append)

        self.assertEqual(code, 2)
        self.assertIn("[config] FAIL", "\n".join(out))
        self.assertIn("cp .env.example .env", "\n".join(out))

    def test_a_deadline_failure_blames_the_network_not_the_key(self) -> None:
        """Hitting the deadline means nothing came back: blocked, not rejected."""

        class Boom:
            def reply(self, history, system_prompt: str) -> str:
                raise TimeoutError("timed out")

        clock = iter([0.0, 55.0])
        with patch("chat.time.monotonic", side_effect=lambda: next(clock)):
            code, text = self._check(**{**self._all_passing(), "provider": Boom()})

        self.assertEqual(code, 4)
        self.assertIn("[model] FAIL after 55.0s", text)
        self.assertIn("blocked or firewalled", text)
        self.assertIn("HTTPS_PROXY", text)

    def test_a_401_blames_the_key(self) -> None:
        class Boom:
            def reply(self, history, system_prompt: str) -> str:
                raise RuntimeError("HTTP 401 — invalid API key")

        clock = iter([0.0, 0.4])
        with patch("chat.time.monotonic", side_effect=lambda: next(clock)):
            code, text = self._check(**{**self._all_passing(), "provider": Boom()})

        self.assertEqual(code, 4)
        self.assertIn("rejected or is restricted", text)

    def test_a_broken_tls_connection_blames_the_network_not_the_key(self) -> None:
        """A reachable TCP probe plus a broken TLS handshake is still network."""

        class Boom:
            def reply(self, history, system_prompt: str) -> str:
                raise ConnectionError("TLS/SSL connection has been closed (EOF)")

        clock = iter([0.0, 0.1])
        with patch("chat.time.monotonic", side_effect=lambda: next(clock)):
            code, text = self._check(**{**self._all_passing(), "provider": Boom()})

        self.assertEqual(code, 4)
        self.assertIn("not a key problem", text)

    def test_a_404_blames_the_model_id(self) -> None:
        class Boom:
            def reply(self, history, system_prompt: str) -> str:
                raise RuntimeError("404 NotFoundError: models/x is not found for API version")

        clock = iter([0.0, 0.3])
        with patch("chat.time.monotonic", side_effect=lambda: next(clock)):
            code, text = self._check(**{**self._all_passing(), "provider": Boom()})

        self.assertEqual(code, 4)
        self.assertIn("not available to your key", text)
        self.assertIn("ai.google.dev", text)


if __name__ == "__main__":
    unittest.main()
