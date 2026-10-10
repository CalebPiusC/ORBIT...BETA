from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import DEFAULT_GEMINI_MODEL, Settings, load_settings
from providers.base import ChatMessage
from providers.gemini import GeminiProvider

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

        make_client.assert_called_once_with(api_key="local-test-key")
        self.assertEqual(client.models.request["model"], "configured-model")  # type: ignore[index]

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
