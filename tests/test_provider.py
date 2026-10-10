from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from google.genai import types

from config import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    TIMEOUT_ENV_VAR,
    Settings,
    load_settings,
    read_timeout_seconds,
)
from providers.base import ChatMessage
from providers.gemini import GeminiProvider, timeout_millis

_ROOT = Path(__file__).resolve().parents[1]


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


class FakeModels:
    def __init__(self) -> None:
        self.request: dict[str, object] | None = None

    def generate_content(self, **kwargs: object) -> SimpleNamespace:
        self.request = kwargs
        return SimpleNamespace(text="A mocked Gemini reply.")


class FakeClient:
    def __init__(self) -> None:
        self.models = FakeModels()


class SettingsTests(unittest.TestCase):
    def test_loads_key_and_model_from_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "GEMINI_API_KEY=local-test-key\nGEMINI_MODEL=gemini-test-model\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True), patch("config.ENV_FILE", env_file):
                settings = load_settings()

        self.assertEqual(settings.gemini_api_key, "local-test-key")
        self.assertEqual(settings.gemini_model, "gemini-test-model")
        self.assertNotIn("local-test-key", repr(settings))

    def test_requires_a_non_placeholder_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("config.load_dotenv"):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                load_settings()

        with patch.dict(os.environ, {"GEMINI_API_KEY": "your-key-here"}, clear=True), patch(
            "config.load_dotenv"
        ):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                load_settings()

    def test_example_template_default_model_and_placeholder_stay_consistent(self) -> None:
        """Guard the drift that broke the last build.

        The template and the code default used to name different models, and the
        code default named one Google had announced for shutdown. Both are
        configuration, so both must agree, and the template's key must be
        rejected rather than sent to the API.
        """
        values = _env_example_values()
        self.assertEqual(values["GEMINI_MODEL"], DEFAULT_GEMINI_MODEL)
        with patch.dict(os.environ, {"GEMINI_API_KEY": values["GEMINI_API_KEY"]}, clear=True), patch(
            "config.load_dotenv"
        ):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                load_settings()

    def test_model_id_is_configured_in_exactly_one_code_place(self) -> None:
        """Model ids live in config.py and .env only — never in a provider or route."""
        offenders: list[str] = []
        for path in (_ROOT / "providers").glob("*.py"):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if line.lstrip().startswith("#"):
                    continue
                if "gemini-" in line.lower():
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], f"hard-coded model id in provider code: {offenders}")


class GeminiProviderTests(unittest.TestCase):
    def test_reply_sends_history_and_system_prompt_and_returns_text(self) -> None:
        client = FakeClient()
        provider = GeminiProvider(model="gemini-test-model", client=client)
        history = [
            ChatMessage(role="user", content="Hello."),
            ChatMessage(role="assistant", content="Hi there."),
            ChatMessage(role="user", content="Can you help?"),
        ]

        result = provider.reply(history, system_prompt="Be concise.")

        self.assertEqual(result, "A mocked Gemini reply.")
        self.assertIsNotNone(client.models.request)
        request = client.models.request
        assert request is not None
        self.assertEqual(request["model"], "gemini-test-model")
        self.assertEqual(
            [message.role for message in request["contents"]],  # type: ignore[union-attr]
            ["user", "model", "user"],
        )
        self.assertEqual(
            [message.parts[0].text for message in request["contents"]],  # type: ignore[union-attr]
            ["Hello.", "Hi there.", "Can you help?"],
        )
        self.assertEqual(request["config"].system_instruction, "Be concise.")  # type: ignore[union-attr]

    def test_builds_sdk_client_from_loaded_settings(self) -> None:
        settings = Settings(gemini_api_key="local-test-key", gemini_model="configured-model")
        client = FakeClient()
        with patch("providers.gemini.load_settings", return_value=settings), patch(
            "providers.gemini.genai.Client", return_value=client
        ) as make_client:
            provider = GeminiProvider()
            provider.reply([ChatMessage(role="user", content="ping")], "Test prompt.")

        make_client.assert_called_once()
        self.assertEqual(make_client.call_args.kwargs["api_key"], "local-test-key")
        # A client with no deadline is the bug this replaced: the SDK would pass
        # timeout=None to httpx, which disables the wait limit entirely.
        self.assertEqual(make_client.call_args.kwargs["http_options"].timeout, 60_000)
        self.assertEqual(client.models.request["model"], "configured-model")  # type: ignore[index]

    def test_every_request_has_a_deadline(self) -> None:
        """No code path may build a client without ``http_options.timeout``."""
        settings = Settings(gemini_api_key="local-test-key", request_timeout_seconds=12.5)
        with patch("providers.gemini.load_settings", return_value=settings), patch(
            "providers.gemini.genai.Client", return_value=FakeClient()
        ) as make_client:
            GeminiProvider()

        options = make_client.call_args.kwargs["http_options"]
        self.assertIsInstance(options, types.HttpOptions)
        self.assertEqual(options.timeout, 12_500)

    def test_deadline_is_converted_to_milliseconds(self) -> None:
        """HttpOptions.timeout is MILLISECONDS. The *1000 is the point of it.

        Sending seconds here would ask for a 60-millisecond deadline and every
        request would fail instantly; the failure would look like a network
        problem and read nothing like a unit error.
        """
        self.assertEqual(timeout_millis(60), 60_000)
        self.assertEqual(timeout_millis(1.5), 1_500)
        self.assertEqual(timeout_millis(DEFAULT_REQUEST_TIMEOUT_SECONDS), 60_000)


class RequestTimeoutTests(unittest.TestCase):
    def test_default_deadline_is_sixty_seconds(self) -> None:
        self.assertEqual(DEFAULT_REQUEST_TIMEOUT_SECONDS, 60)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "local-test-key"}, clear=True), patch(
            "config.load_dotenv"
        ):
            self.assertEqual(load_settings().request_timeout_seconds, 60)

    def test_deadline_is_overridable_from_the_environment(self) -> None:
        env = {"GEMINI_API_KEY": "local-test-key", TIMEOUT_ENV_VAR: "12.5"}
        with patch.dict(os.environ, env, clear=True), patch("config.load_dotenv"):
            self.assertEqual(load_settings().request_timeout_seconds, 12.5)

    def test_a_bad_deadline_is_rejected_rather_than_ignored(self) -> None:
        """A typo must not quietly become an unbounded wait.

        Silently falling back to the default would be the same silent-failure
        shape as having no deadline at all, just harder to notice.
        """
        for bad in ("0", "-5", "abc", "nan", "inf"):
            env = {"GEMINI_API_KEY": "local-test-key", TIMEOUT_ENV_VAR: bad}
            with patch.dict(os.environ, env, clear=True), patch("config.load_dotenv"):
                with self.assertRaisesRegex(RuntimeError, "ORBIT_HTTP_TIMEOUT"):
                    load_settings()

    def test_an_unset_deadline_falls_back_to_the_default(self) -> None:
        self.assertEqual(read_timeout_seconds(None), DEFAULT_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(read_timeout_seconds(""), DEFAULT_REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(read_timeout_seconds("   "), DEFAULT_REQUEST_TIMEOUT_SECONDS)

    def test_example_template_names_the_same_deadline_as_the_code(self) -> None:
        """Model ids taught us this lesson: config lives in two files, so pin it.

        .env.example and config.py drifted apart once already (handoff §0.2).
        """
        values = _env_example_values()
        self.assertEqual(values[TIMEOUT_ENV_VAR], "60")
        self.assertEqual(
            read_timeout_seconds(values[TIMEOUT_ENV_VAR]), DEFAULT_REQUEST_TIMEOUT_SECONDS
        )

    def test_rejects_empty_or_invalid_chat_input(self) -> None:
        provider = GeminiProvider(client=FakeClient())
        with self.assertRaises(ValueError):
            provider.reply([], "Be helpful.")
        with self.assertRaises(ValueError):
            provider.reply([ChatMessage(role="user", content="Hi")], "  ")
        with self.assertRaises(ValueError):
            ChatMessage(role="tool", content="not part of chat mode")  # type: ignore[arg-type]

    def test_rejects_an_empty_model_response(self) -> None:
        client = FakeClient()
        client.models.generate_content = lambda **_: SimpleNamespace(text=" ")  # type: ignore[method-assign]
        provider = GeminiProvider(client=client)
        with self.assertRaisesRegex(RuntimeError, "no text"):
            provider.reply([ChatMessage(role="user", content="Hi")], "Be helpful.")


if __name__ == "__main__":
    unittest.main()
