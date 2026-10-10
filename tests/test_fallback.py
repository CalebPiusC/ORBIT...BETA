"""Primary/fallback model behaviour, offline. The SDK is replaced by a script.

Rules under test (see providers/gemini.py and docs/HANDOFF.md):
- The fallback is tried once, only for 503/504 or a transport error.
- Auth, quota, and bad-request errors are never retried on the fallback.
- No fallback after a tool has run, and none after a stream has produced text.
- Every answer and every failed attempt is written to audit.log, without content.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from google.genai.errors import APIError

from chat import _model_hint, run_session
from config import DEFAULT_GEMINI_FALLBACK_MODEL, DEFAULT_GEMINI_MODEL, load_settings
from providers.base import ChatMessage
from providers.gemini import GeminiProvider
from tools import build_tool_registry
from tools.confirmation import ProposedAction
from tools.registry import AuditLog

PRIMARY = "primary-test-model"
FALLBACK = "fallback-test-model"
_ROOT = Path(__file__).resolve().parents[1]


def api_error(code: int, status: str) -> APIError:
    """An error shaped like the one the SDK raises for a real HTTP error."""
    return APIError(code, {"error": {"code": code, "message": f"{status} test", "status": status}}, None)


def reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, function_calls=None, candidates=None)


class ScriptedModels:
    """Each model name gets a queue of outcomes: a response, or an exception to raise."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self._script = {name: list(items) for name, items in script.items()}
        self.calls: list[tuple[str, str]] = []

    def _next(self, model: str) -> object:
        queue = self._script.get(model)
        if not queue:
            raise AssertionError(f"unexpected call to {model!r}")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def generate_content(self, *, model: str, contents: object, config: object) -> object:
        self.calls.append(("generate", model))
        return self._next(model)

    def generate_content_stream(self, *, model: str, contents: object, config: object):
        self.calls.append(("stream", model))
        entry = self._next(model)  # a list of str and exceptions, consumed lazily

        def chunks():
            for item in entry:
                if isinstance(item, BaseException):
                    raise item
                yield SimpleNamespace(text=item)

        return chunks()


class DeclineGate:
    def __init__(self) -> None:
        self.asked: list[ProposedAction] = []

    def confirm(self, action: ProposedAction) -> bool:
        self.asked.append(action)
        return False


def make_provider(script, *, fallback=FALLBACK, tool_registry=None, audit_path=None):
    models = ScriptedModels(script)
    client = SimpleNamespace(models=models)
    audit = AuditLog(audit_path) if audit_path is not None else None
    provider = GeminiProvider(
        model=PRIMARY,
        fallback_model=fallback,
        client=client,
        tool_registry=tool_registry,
        audit_log=audit,
    )
    return provider, models


def read_audit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


HISTORY = [ChatMessage(role="user", content="hello")]


class ReplyFallbackTests(unittest.TestCase):
    def test_primary_answers_and_no_fallback_is_used(self) -> None:
        provider, models = make_provider({PRIMARY: [reply("from primary")]})

        self.assertEqual(provider.reply(HISTORY, "sys"), "from primary")
        self.assertEqual(models.calls, [("generate", PRIMARY)])
        self.assertEqual(provider.last_model_used, PRIMARY)

    def test_504_retries_once_on_fallback_and_reports_it(self) -> None:
        provider, models = make_provider(
            {PRIMARY: [api_error(504, "DEADLINE_EXCEEDED")], FALLBACK: [reply("from fallback")]}
        )

        self.assertEqual(provider.reply(HISTORY, "sys"), "from fallback")
        self.assertEqual(models.calls, [("generate", PRIMARY), ("generate", FALLBACK)])
        self.assertEqual(provider.last_model_used, FALLBACK)

    def test_503_retries_on_fallback(self) -> None:
        provider, models = make_provider(
            {PRIMARY: [api_error(503, "UNAVAILABLE")], FALLBACK: [reply("ok")]}
        )

        self.assertEqual(provider.reply(HISTORY, "sys"), "ok")
        self.assertEqual(models.calls[-1], ("generate", FALLBACK))

    def test_transport_errors_retry_on_fallback(self) -> None:
        for error in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ReadError("eof")):
            with self.subTest(error=type(error).__name__):
                provider, models = make_provider(
                    {PRIMARY: [error], FALLBACK: [reply("ok")]}
                )
                self.assertEqual(provider.reply(HISTORY, "sys"), "ok")
                self.assertEqual(models.calls[-1], ("generate", FALLBACK))

    def test_auth_quota_and_bad_request_errors_are_not_retried(self) -> None:
        for code, status in ((400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"), (404, "NOT_FOUND"), (429, "RESOURCE_EXHAUSTED")):
            with self.subTest(code=code):
                provider, models = make_provider({PRIMARY: [api_error(code, status)]})
                with self.assertRaises(APIError):
                    provider.reply(HISTORY, "sys")
                self.assertEqual(models.calls, [("generate", PRIMARY)])

    def test_a_failing_fallback_surfaces_its_own_error(self) -> None:
        provider, models = make_provider(
            {PRIMARY: [api_error(504, "DEADLINE_EXCEEDED")], FALLBACK: [api_error(503, "UNAVAILABLE")]}
        )

        with self.assertRaises(APIError) as caught:
            provider.reply(HISTORY, "sys")
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(models.calls, [("generate", PRIMARY), ("generate", FALLBACK)])
        self.assertIsNone(provider.last_model_used, "a failed turn must not claim an answer")

    def test_no_fallback_when_disabled(self) -> None:
        provider, models = make_provider({PRIMARY: [api_error(504, "DEADLINE_EXCEEDED")]}, fallback="")

        with self.assertRaises(APIError):
            provider.reply(HISTORY, "sys")
        self.assertEqual(models.calls, [("generate", PRIMARY)])

    def test_empty_fallback_disables_it_and_equal_fallback_is_ignored(self) -> None:
        provider, _ = make_provider({}, fallback="")
        self.assertIsNone(provider.fallback_model)
        provider, _ = make_provider({}, fallback=PRIMARY)
        self.assertIsNone(provider.fallback_model)

    def test_no_retry_after_a_tool_has_run(self) -> None:
        """A tool must never run twice. The failure after a tool call is surfaced, not retried."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            gate = DeclineGate()
            registry = build_tool_registry(
                confirmation_gate=gate,
                audit_log_path=temp / "audit.log",
                notes_path=temp / "notes.txt",
            )
            call = SimpleNamespace(
                text=None,
                function_calls=[SimpleNamespace(name="write_note", args={"content": "x"})],
                candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=[]))],
            )
            provider, models = make_provider(
                {PRIMARY: [call, api_error(504, "DEADLINE_EXCEEDED")], FALLBACK: [reply("never")]},
                tool_registry=registry,
            )

            with self.assertRaises(APIError):
                provider.reply(HISTORY, "sys")

        self.assertEqual(models.calls, [("generate", PRIMARY), ("generate", PRIMARY)])
        self.assertEqual(len(gate.asked), 1, "the tool was proposed once, and only once")


class StreamFallbackTests(unittest.TestCase):
    def test_stream_failure_before_first_chunk_falls_back(self) -> None:
        provider, models = make_provider(
            {PRIMARY: [api_error(504, "DEADLINE_EXCEEDED")], FALLBACK: [["Hel", "lo"]]}
        )

        text = "".join(c.text for c in provider.reply_stream(HISTORY, "sys") if c.kind == "answer")
        self.assertEqual(text, "Hello")
        self.assertEqual(models.calls, [("stream", PRIMARY), ("stream", FALLBACK)])
        self.assertEqual(provider.last_model_used, FALLBACK)

    def test_stream_failure_after_text_is_surfaced_not_restarted(self) -> None:
        """Restarting would repeat text the user has already seen."""
        provider, models = make_provider(
            {PRIMARY: [["Partly ", api_error(504, "DEADLINE_EXCEEDED")]], FALLBACK: [["never"]]}
        )

        received: list[str] = []
        with self.assertRaises(APIError):
            for chunk in provider.reply_stream(HISTORY, "sys"):
                received.append(chunk.text)
        self.assertEqual(received, ["Partly "])
        self.assertEqual(models.calls, [("stream", PRIMARY)])


class AuditTests(unittest.TestCase):
    def test_fallback_turn_writes_failure_then_answer_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.log"
            provider, _ = make_provider(
                {PRIMARY: [api_error(504, "DEADLINE_EXCEEDED")], FALLBACK: [reply("secret reply text")]},
                audit_path=audit_path,
            )
            provider.reply([ChatMessage(role="user", content="secret prompt text")], "sys")
            events = read_audit(audit_path)
            raw = audit_path.read_text(encoding="utf-8")

        self.assertEqual([e["event"] for e in events], ["model_attempt_failed", "model_answered"])
        failed, answered = events
        self.assertEqual((failed["model"], failed["role"], failed["reason"]), (PRIMARY, "primary", "APIError 504"))
        self.assertEqual((answered["model"], answered["role"], answered["fallback_used"]), (FALLBACK, "fallback", True))
        self.assertIn("timestamp", answered)
        self.assertNotIn("secret prompt text", raw, "audit lines must not carry prompts")
        self.assertNotIn("secret reply text", raw, "audit lines must not carry replies")

    def test_primary_answer_is_logged_as_primary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.log"
            provider, _ = make_provider({PRIMARY: [reply("ok")]}, audit_path=audit_path)
            provider.reply(HISTORY, "sys")
            events = read_audit(audit_path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "model_answered")
        self.assertEqual(events[0]["model"], PRIMARY)
        self.assertFalse(events[0]["fallback_used"])

    def test_injected_client_writes_no_audit_file_by_default(self) -> None:
        """Offline tests must not write to the real audit.log at the repo root."""
        provider, _ = make_provider({PRIMARY: [reply("ok")]}, audit_path=None)
        provider.reply(HISTORY, "sys")
        self.assertIsNone(provider._audit)


class SettingsFallbackTests(unittest.TestCase):
    def _load(self, env: dict[str, str]):
        base = {"GEMINI_API_KEY": "local-test-key-for-offline-tests"}
        with patch.dict(os.environ, {**base, **env}, clear=True), patch("config.load_dotenv"):
            return load_settings()

    def test_defaults_are_the_checked_pair(self) -> None:
        settings = self._load({})
        self.assertEqual(settings.gemini_model, DEFAULT_GEMINI_MODEL)
        self.assertEqual(settings.gemini_fallback_model, DEFAULT_GEMINI_FALLBACK_MODEL)

    def test_blank_fallback_disables_it(self) -> None:
        self.assertEqual(self._load({"GEMINI_FALLBACK_MODEL": "  "}).gemini_fallback_model, "")

    def test_fallback_equal_to_primary_is_disabled(self) -> None:
        settings = self._load({"GEMINI_MODEL": "same-model", "GEMINI_FALLBACK_MODEL": "same-model"})
        self.assertEqual(settings.gemini_fallback_model, "")

    def test_env_example_names_the_same_pair_as_the_code(self) -> None:
        values: dict[str, str] = {}
        for line in (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip()
        self.assertEqual(values["GEMINI_MODEL"], DEFAULT_GEMINI_MODEL)
        self.assertEqual(values["GEMINI_FALLBACK_MODEL"], DEFAULT_GEMINI_FALLBACK_MODEL)


class ModelHintTests(unittest.TestCase):
    """The hint must blame Google's server for 503/504, not the network."""

    def test_504_near_the_deadline_is_google_overloaded_not_blocked(self) -> None:
        hint = _model_hint(api_error(504, "DEADLINE_EXCEEDED"), elapsed=61.9, timeout=60.0)
        self.assertIn("slow or overloaded", hint)
        self.assertNotIn("blocked", hint)
        self.assertNotIn("firewalled", hint)

    def test_503_is_google_overloaded_even_when_fast(self) -> None:
        hint = _model_hint(RuntimeError("503 UNAVAILABLE. high demand"), elapsed=2.1, timeout=60.0)
        self.assertIn("slow or overloaded", hint)
        self.assertNotIn("blocked", hint)

    def test_a_silent_timeout_with_no_status_still_blames_the_network(self) -> None:
        hint = _model_hint(TimeoutError("timed out"), elapsed=55.0, timeout=60.0)
        self.assertIn("blocked or firewalled", hint)


class ChatSurfaceTests(unittest.TestCase):
    def test_repl_says_when_the_fallback_answered(self) -> None:
        class FallbackAnswered:
            model = PRIMARY
            last_model_used = FALLBACK

            def reply(self, history, system_prompt: str) -> str:
                return "hi"

        inputs = iter(["hello", "/quit"])
        out: list[str] = []
        run_session(FallbackAnswered(), read=lambda _prompt: next(inputs), write=out.append)
        text = "\n".join(out)
        self.assertIn(f"answered by fallback {FALLBACK}", text)
        self.assertIn(f"primary {PRIMARY} failed", text)


if __name__ == "__main__":
    unittest.main()
