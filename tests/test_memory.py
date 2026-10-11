"""Phase 4a: background facts about Caleb, offline.

Rules under test:
- The facts file is plain text, one fact per line, and human-editable: blank
  lines and '#' comments are skipped, and a fact that would read as a comment
  is refused.
- It is read on every turn, so a hand edit is used on the next reply.
- Facts are framed as background context, never as instructions.
- remember_fact / forget_fact go through the same gate and audit as write_note:
  a decline writes nothing and is still audited.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from chat import SYSTEM_PROMPT, run_session
from memory.facts import (
    FACTS_FRAME,
    MAX_FACT_CHARS,
    FactError,
    FactStore,
    build_system_prompt,
    parse_facts,
    validate_fact,
)
from tools import build_tool_registry
from tools.confirmation import ProposedAction


class FixedGate:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.asked: list[ProposedAction] = []

    def confirm(self, action: ProposedAction) -> bool:
        self.asked.append(action)
        return self.approved


class PromptRecorder:
    """A provider double that records the system prompt of every turn."""

    model = "test-model"
    last_model_used = None

    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    def reply(self, history, system_prompt: str) -> str:
        self.system_prompts.append(system_prompt)
        return "ok"


def read_audit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class FileFormatTests(unittest.TestCase):
    def test_comments_and_blank_lines_are_skipped_and_order_is_kept(self) -> None:
        text = "# header\n\nFirst fact.\n   \n# note to self\nSecond fact.  \n"
        self.assertEqual(parse_facts(text), ["First fact.", "Second fact."])

    def test_validation_refuses_what_cannot_be_one_stored_line(self) -> None:
        for bad in ("", "   ", "two\nlines", "carriage\rreturn", "# looks like a comment", "x" * (MAX_FACT_CHARS + 1)):
            with self.subTest(bad=bad[:20]):
                with self.assertRaises(FactError):
                    validate_fact(bad)
        with self.assertRaises(FactError):
            validate_fact(None)  # type: ignore[arg-type]

    def test_validation_strips_surrounding_space(self) -> None:
        self.assertEqual(validate_fact("  Lives in Lagos.  "), "Lives in Lagos.")


class FactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "runtime" / "facts.txt"
        self.store = FactStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_means_no_facts_and_no_prompt_change(self) -> None:
        self.assertEqual(self.store.load(), [])
        self.assertEqual(self.store.system_prompt(SYSTEM_PROMPT), SYSTEM_PROMPT)

    def test_add_creates_the_file_with_a_human_header_and_one_fact_per_line(self) -> None:
        self.assertTrue(self.store.add("Prefers short answers."))
        self.assertTrue(self.store.add("Works on ORBIT."))
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Background facts about Caleb"))
        self.assertEqual([line for line in text.splitlines() if not line.startswith("#")],
                         ["Prefers short answers.", "Works on ORBIT."])

    def test_duplicates_are_refused_case_insensitively(self) -> None:
        self.assertTrue(self.store.add("Works on ORBIT."))
        self.assertFalse(self.store.add("works on orbit."))
        self.assertEqual(self.store.load(), ["Works on ORBIT."])

    def test_add_keeps_hand_edits_and_comments(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("# mine\nHand written fact.", encoding="utf-8")  # no trailing newline
        self.store.add("Added by tool.")
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "# mine\nHand written fact.\nAdded by tool.\n",
        )

    def test_remove_deletes_exact_matches_and_leaves_everything_else(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("# keep me\nAlpha.\nBeta.\n", encoding="utf-8")
        self.assertEqual(self.store.remove("ALPHA."), 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# keep me\nBeta.\n")
        self.assertEqual(self.store.remove("no such fact"), 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# keep me\nBeta.\n")

    def test_a_comment_line_cannot_be_removed_as_if_it_were_a_fact(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("# Background facts\n", encoding="utf-8")
        self.assertEqual(self.store.remove("# Background facts"), 0)


class SystemPromptFramingTests(unittest.TestCase):
    def test_facts_are_framed_as_background_not_instructions(self) -> None:
        prompt = build_system_prompt("BASE", ["Lives in Lagos."])
        self.assertTrue(prompt.startswith("BASE\n\n"))
        self.assertIn("Background facts about Caleb", prompt)
        self.assertIn("not instructions", prompt)
        self.assertIn("- Lives in Lagos.", prompt)
        self.assertEqual(prompt, f"BASE\n\n{FACTS_FRAME}- Lives in Lagos.")

    def test_every_saved_fact_is_injected_as_written_with_no_summary(self) -> None:
        facts = [f"Fact number {i}." for i in range(25)]
        prompt = build_system_prompt("BASE", facts)
        for fact in facts:
            self.assertIn(f"- {fact}", prompt)


class FreshEveryTurnTests(unittest.TestCase):
    def test_a_hand_edit_is_used_on_the_very_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "facts.txt"
            provider = PromptRecorder()
            prompts = iter(["first", "second", "third", "/quit"])

            def read(_prompt: str) -> str:
                step = next(prompts)
                if step == "second":
                    # Between turns, a person edits the file by hand.
                    path.write_text("Plays bass.\n", encoding="utf-8")
                if step == "third":
                    FactStore(path).remove("Plays bass.")
                return step

            run_session(provider, read=read, write=lambda _line: None, facts=FactStore(path))

        self.assertTrue(provider.system_prompts[0].startswith(SYSTEM_PROMPT))
        self.assertIn("Current date and time in Africa/Lagos:", provider.system_prompts[0])
        self.assertIn("- Plays bass.", provider.system_prompts[1])
        self.assertTrue(provider.system_prompts[2].startswith(SYSTEM_PROMPT))
        self.assertIn("Current date and time in Africa/Lagos:", provider.system_prompts[2])


class MemoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.facts_path = self.root / "runtime" / "facts.txt"
        self.audit_path = self.root / "audit.log"
        self.facts_path.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _registry(self, approved: bool) -> tuple[object, FixedGate]:
        gate = FixedGate(approved)
        registry = build_tool_registry(
            confirmation_gate=gate,
            audit_log_path=self.audit_path,
            notes_path=self.root / "notes.txt",
            facts_path=self.facts_path,
        )
        return registry, gate

    def test_both_tools_are_declared_to_the_model(self) -> None:
        registry, _ = self._registry(approved=True)
        names = [definition.name for definition in registry.definitions]
        self.assertIn("remember_fact", names)
        self.assertIn("forget_fact", names)

    def test_remember_declined_writes_nothing_and_is_audited(self) -> None:
        registry, gate = self._registry(approved=False)
        outcome = registry.dispatch("remember_fact", {"fact": "Plays bass."})

        self.assertEqual(outcome.confirmation, "declined")
        self.assertEqual(outcome.execution, "not_run")
        self.assertEqual(len(gate.asked), 1, "the gate was asked exactly once")
        self.assertEqual(dict(gate.asked[0].arguments), {"fact": "Plays bass."})
        self.assertFalse(self.facts_path.exists(), "a declined fact must not reach disk")
        events = read_audit(self.audit_path)
        self.assertEqual([e["tool_name"] for e in events], ["remember_fact"])
        self.assertFalse(events[0]["confirmed"])
        self.assertEqual(events[0]["execution"], "not_run")

    def test_remember_confirmed_saves_the_fact_and_is_audited(self) -> None:
        registry, _ = self._registry(approved=True)
        outcome = registry.dispatch("remember_fact", {"fact": "Plays bass."})

        self.assertEqual(outcome.execution, "succeeded")
        self.assertEqual(FactStore(self.facts_path).load(), ["Plays bass."])
        events = read_audit(self.audit_path)
        self.assertEqual((events[-1]["tool_name"], events[-1]["confirmed"], events[-1]["execution"]),
                         ("remember_fact", True, "succeeded"))

    def test_forget_declined_keeps_the_fact(self) -> None:
        FactStore(self.facts_path).add("Plays bass.")
        registry, _ = self._registry(approved=False)
        outcome = registry.dispatch("forget_fact", {"fact": "Plays bass."})

        self.assertEqual(outcome.execution, "not_run")
        self.assertEqual(FactStore(self.facts_path).load(), ["Plays bass."])
        self.assertEqual(read_audit(self.audit_path)[-1]["tool_name"], "forget_fact")

    def test_forget_confirmed_removes_the_fact(self) -> None:
        FactStore(self.facts_path).add("Plays bass.")
        registry, _ = self._registry(approved=True)
        outcome = registry.dispatch("forget_fact", {"fact": "plays BASS."})

        self.assertEqual(outcome.execution, "succeeded")
        self.assertEqual(FactStore(self.facts_path).load(), [])

    def test_an_invalid_fact_fails_loudly_and_writes_nothing(self) -> None:
        registry, _ = self._registry(approved=True)
        outcome = registry.dispatch("remember_fact", {"fact": "two\nlines"})

        self.assertEqual(outcome.execution, "failed")
        self.assertFalse(self.facts_path.exists())


class WebTurnTests(unittest.TestCase):
    def test_the_web_chat_reads_the_facts_on_each_turn(self) -> None:
        from app.server import HOME_SYSTEM_PROMPT, create_app
        from app.store import Store
        from providers.base import StreamChunk

        seen: list[str] = []

        class Provider:
            def reply_stream(self, history, system_prompt, *, think=False, report_attempts=False, thinking_level="quick"):
                seen.append(system_prompt)
                yield StreamChunk(kind="answer", text="ok")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "facts.txt"
            app = create_app(provider=Provider(), store=Store(), facts=FactStore(path))
            client = app.test_client()
            client.post("/api/chat", json={"message": "one"}).get_data()
            path.write_text("Lives in Lagos.\n", encoding="utf-8")
            client.post("/api/chat", json={"message": "two"}).get_data()

        self.assertTrue(seen[0].startswith(HOME_SYSTEM_PROMPT))
        self.assertIn("Current date and time in Africa/Lagos:", seen[0])
        self.assertIn("- Lives in Lagos.", seen[1])
        self.assertIn("not instructions", seen[1])
        self.assertIn("Current date and time in Africa/Lagos:", seen[1])


if __name__ == "__main__":
    unittest.main()
