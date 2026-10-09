from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from google.genai import types

from providers.base import ChatMessage
from providers.gemini import GeminiProvider
from tools import ConsoleConfirmationGate, ProposedAction, ToolRegistry, build_tool_registry


class RecordingGate:
    def __init__(self, approved: bool, before_confirm: Any | None = None) -> None:
        self.approved = approved
        self.before_confirm = before_confirm
        self.actions: list[ProposedAction] = []

    def confirm(self, action: ProposedAction) -> bool:
        self.actions.append(action)
        if self.before_confirm is not None:
            self.before_confirm(action)
        return self.approved


class SequenceModels:
    def __init__(self, responses: list[types.GenerateContentResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> types.GenerateContentResponse:
        self.requests.append(kwargs)
        return self.responses.pop(0)


class ToolConfirmationTests(unittest.TestCase):
    def test_registry_cannot_be_created_without_a_confirmation_gate(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirmation gate is required"):
            ToolRegistry(None)  # type: ignore[arg-type]

    def test_console_gate_requires_explicit_yes_and_displays_exact_arguments(self) -> None:
        output: list[str] = []
        answers = iter(["y", " YES "])
        gate = ConsoleConfirmationGate(
            input_fn=lambda _prompt: next(answers),
            output_fn=output.append,
        )
        content = 'Keep the quoted text: "exactly".\nSecond line.'
        action = ProposedAction(
            tool_name="write_note",
            description="Append a note.",
            arguments={"content": content},
        )

        with patch("tools.confirmation.sys.stdin") as terminal:
            terminal.isatty.return_value = True
            self.assertFalse(gate.confirm(action))
            self.assertTrue(gate.confirm(action))
        printed = "\n".join(output)
        expected_arguments = json.dumps(
            {"content": content}, ensure_ascii=True, indent=2, sort_keys=True
        )
        self.assertIn(expected_arguments, printed)
        self.assertIn("Only typing 'yes' approves", printed)

    def test_default_gate_does_not_accept_a_piped_yes(self) -> None:
        input_called = False
        output: list[str] = []

        def piped_yes(_prompt: str) -> str:
            nonlocal input_called
            input_called = True
            return "yes"

        action = ProposedAction("write_note", "Append a note.", {"content": "Test"})
        with patch("tools.confirmation.sys.stdin", io.StringIO("yes")):
            gate = ConsoleConfirmationGate(input_fn=piped_yes, output_fn=output.append)
            self.assertFalse(gate.confirm(action))

        self.assertFalse(input_called)
        self.assertTrue(any("No interactive terminal" in line for line in output))

    def test_write_note_asks_before_writing_and_audits_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes_path = root / "notes.txt"
            audit_path = root / "audit.log"
            text = "Buy tea and oranges"

            def check_before_write(action: ProposedAction) -> None:
                self.assertEqual(action.tool_name, "write_note")
                self.assertEqual(dict(action.arguments), {"content": text})
                self.assertFalse(notes_path.exists(), "note was written before confirmation")

            gate = RecordingGate(True, before_confirm=check_before_write)
            registry = build_tool_registry(
                confirmation_gate=gate,
                audit_log_path=audit_path,
                notes_path=notes_path,
            )
            outcome = registry.dispatch("write_note", {"content": text})

            self.assertEqual(outcome.confirmation, "confirmed")
            self.assertEqual(outcome.execution, "succeeded")
            self.assertEqual(len(gate.actions), 1)
            note_line = notes_path.read_text(encoding="utf-8")
            self.assertRegex(
                note_line,
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\tBuy tea and oranges\n$",
            )

            event = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(event["tool_name"], "write_note")
            self.assertEqual(event["arguments"], {"content": text})
            self.assertEqual(event["confirmation"], "confirmed")
            self.assertIs(event["confirmed"], True)
            self.assertEqual(event["execution"], "succeeded")
            self.assertGreaterEqual(event["duration_ms"], 0)

    def test_declining_does_not_write_and_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes_path = root / "notes.txt"
            audit_path = root / "audit.log"
            registry = build_tool_registry(
                confirmation_gate=RecordingGate(False),
                audit_log_path=audit_path,
                notes_path=notes_path,
            )

            outcome = registry.dispatch("write_note", {"content": "Do not save this"})

            self.assertEqual(outcome.confirmation, "declined")
            self.assertEqual(outcome.execution, "not_run")
            self.assertFalse(notes_path.exists())
            event = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(event["confirmation"], "declined")
            self.assertIs(event["confirmed"], False)
            self.assertEqual(event["execution"], "not_run")

    def test_missing_confirmation_input_fails_closed(self) -> None:
        gate = ConsoleConfirmationGate(
            input_fn=lambda _prompt: (_ for _ in ()).throw(EOFError()),
            output_fn=lambda _text: None,
        )
        action = ProposedAction("write_note", "Append a note.", {"content": "No input"})
        with patch("tools.confirmation.sys.stdin") as terminal:
            terminal.isatty.return_value = True
            self.assertFalse(gate.confirm(action))


class GeminiToolRoundTripTests(unittest.TestCase):
    def _function_call_response(self, content: str) -> types.GenerateContentResponse:
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    name="write_note",
                                    args={"content": content},
                                )
                            )
                        ],
                    )
                )
            ]
        )

    def _text_response(self, text: str) -> types.GenerateContentResponse:
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=text)],
                    )
                )
            ]
        )

    def test_stubbed_model_tool_call_is_gated_then_returned_mid_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note_text = "Call the dentist on Monday"
            gate = RecordingGate(True)
            registry = build_tool_registry(
                confirmation_gate=gate,
                audit_log_path=root / "audit.log",
                notes_path=root / "notes.txt",
            )
            models = SequenceModels(
                [
                    self._function_call_response(note_text),
                    self._text_response("The note was saved."),
                ]
            )
            provider = GeminiProvider(
                model="stub-model",
                client=type("FakeClient", (), {"models": models})(),
                tool_registry=registry,
            )

            answer = provider.reply(
                [ChatMessage(role="user", content="Save a reminder for Monday.")],
                "Use write_note when asked to save a note.",
            )

            self.assertEqual(answer, "The note was saved.")
            self.assertEqual(len(models.requests), 2)
            self.assertEqual(len(gate.actions), 1)
            self.assertEqual(dict(gate.actions[0].arguments), {"content": note_text})
            self.assertTrue((root / "notes.txt").exists())

            first_config = models.requests[0]["config"]
            declarations = first_config.tools[0].function_declarations
            self.assertEqual([declaration.name for declaration in declarations], ["write_note"])
            self.assertTrue(first_config.automatic_function_calling.disable)

            second_contents = models.requests[1]["contents"]
            self.assertEqual(len(second_contents), 3)
            self.assertEqual(second_contents[1].role, "model")
            function_response = second_contents[2].parts[0].function_response
            self.assertEqual(function_response.name, "write_note")
            self.assertEqual(function_response.response["confirmation"], "confirmed")
            self.assertEqual(function_response.response["execution"], "succeeded")

    def test_stubbed_model_decline_is_returned_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            notes_path = root / "notes.txt"
            registry = build_tool_registry(
                confirmation_gate=RecordingGate(False),
                audit_log_path=root / "audit.log",
                notes_path=notes_path,
            )
            models = SequenceModels(
                [
                    self._function_call_response("A note that should be declined"),
                    self._text_response("Okay, I did not save it."),
                ]
            )
            provider = GeminiProvider(
                model="stub-model",
                client=type("FakeClient", (), {"models": models})(),
                tool_registry=registry,
            )

            answer = provider.reply(
                [ChatMessage(role="user", content="Save this note.")],
                "Use the tool if asked.",
            )

            self.assertEqual(answer, "Okay, I did not save it.")
            self.assertFalse(notes_path.exists())
            function_response = models.requests[1]["contents"][2].parts[0].function_response
            self.assertEqual(function_response.response["confirmation"], "declined")
            event = json.loads((root / "audit.log").read_text(encoding="utf-8"))
            self.assertEqual(event["confirmation"], "declined")
            self.assertIs(event["confirmed"], False)


if __name__ == "__main__":
    unittest.main()
