"""The Agent panel's plan must come from the request, and only move on evidence.

Two properties are pinned here because both were violated by the panel this
replaced:

1. The checklist is *derived*. Two different requests must produce two
   different checklists — the previous build showed the same four steps
   ("Read repository context" → "Extract order status logic" → "Run tests" →
   "Ready for your review") for every task.
2. Progress is *earned*. A step only leaves ``pending`` when Orbit's narration
   gives evidence for it, and that sentence is kept so the checklist can be
   read back against what Orbit actually said.
"""

from __future__ import annotations

import unittest

from app.taskplan import (
    PENDING,
    SOURCE_ORBIT,
    SOURCE_REQUEST,
    TaskRun,
    add_files,
    apply_narration,
    files_in,
    finish_run,
    leading_clause,
    new_run,
    plan_steps,
    summarize_request,
)


class PlanFromRequestTests(unittest.TestCase):
    def test_two_requests_give_two_checklists(self) -> None:
        bug = [s.title for s in plan_steps("Fix the crash in checkout.py when the cart is empty.")]
        review = [s.title for s in plan_steps("Review the diff in orders.py and tell me what is risky.")]
        docs = [s.title for s in plan_steps("Write the docs for the webhook handler.")]
        self.assertIn("Reproduce the failure", bug)
        self.assertIn("Fix the root cause", bug)
        self.assertIn("Report what you find", review)
        self.assertNotIn("Fix the root cause", review)
        self.assertIn("Write the docs", docs)
        self.assertNotIn("Reproduce the failure", docs)
        self.assertNotEqual(bug, review)
        self.assertNotEqual(bug, docs)
        self.assertNotEqual(review, docs)

    def test_the_old_fixed_workflow_is_gone(self) -> None:
        titles = [s.title for s in plan_steps("Add order status tracking to the bot.")]
        self.assertNotIn("Read repository context", titles)
        self.assertNotIn("Extract order status logic", titles)
        self.assertNotIn("Ready for your review", titles)

    def test_a_request_that_mentions_tests_gets_a_test_step(self) -> None:
        titles = [s.title for s in plan_steps("Add a status field to orders.py and add tests.")]
        self.assertIn("Add or update the tests", titles)

    def test_a_request_that_does_not_mention_tests_gets_a_verify_step(self) -> None:
        titles = [s.title for s in plan_steps("Add a status field to orders.py.")]
        self.assertIn("Verify the change", titles)
        self.assertNotIn("Add or update the tests", titles)

    def test_the_request_that_already_names_the_work_is_not_planned_twice(self) -> None:
        """'Add tests for the order lookup' must not plan 'Add tests' and 'Add or update the tests'."""
        titles = [s.title for s in plan_steps("Add tests for the order lookup and raise coverage.")]
        self.assertEqual(len(titles), len(set(titles)), f"duplicate steps: {titles}")
        self.assertNotIn("Add or update the tests", titles)

    def test_every_plan_starts_and_ends_the_same_two_ways(self) -> None:
        titles = [s.title for s in plan_steps("Make the bot nicer.")]
        self.assertTrue(titles[0].startswith("Find the code"))
        self.assertEqual(titles[-1], "Summarize it for your review")
        self.assertLessEqual(len(titles), 5)

    def test_an_empty_request_still_produces_a_plan(self) -> None:
        titles = [s.title for s in plan_steps("")]
        self.assertEqual(titles[0], "Find the code this touches")
        self.assertIn("Make the change", titles)

    def test_the_requests_own_words_appear_in_the_steps(self) -> None:
        titles = [s.title for s in plan_steps("Add a status field to each order in orders.py.")]
        self.assertIn("Add a status field", titles)
        self.assertIn("Plan how to add a status field", titles)
        self.assertIn("Find the code for a status field", titles)

    def test_a_leading_preposition_is_dropped_from_the_object(self) -> None:
        self.assertEqual(leading_clause("Look at orders.py and explain it.")[1], "orders")
        self.assertEqual(leading_clause("Add a status field to each order."), ("Add", "a status field"))
        self.assertEqual(leading_clause("hello"), ("", ""))


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.steps = plan_steps("Fix the crash in checkout.py when the cart is empty.")

    def test_nothing_moves_without_narration(self) -> None:
        self.assertEqual([s.status for s in self.steps], [PENDING] * len(self.steps))
        self.assertFalse(apply_narration(self.steps, "Sure!"))

    def test_a_step_moves_on_orbit_narration_and_keeps_the_line(self) -> None:
        self.assertTrue(apply_narration(self.steps, "I am reading checkout.py to locate the cart code."))
        first = self.steps[0]
        self.assertEqual(first.status, "active")
        self.assertIn("reading checkout.py", first.evidence)
        # Nothing else moved on the strength of that one sentence.
        self.assertEqual([s.status for s in self.steps[1:]], [PENDING] * (len(self.steps) - 1))

    def test_the_previous_step_is_completed_when_orbit_moves_on(self) -> None:
        apply_narration(self.steps, "I am reading checkout.py to locate the cart code.")
        apply_narration(self.steps, "I can reproduce the failure: an empty cart raises IndexError.")
        self.assertEqual(self.steps[0].status, "done")
        self.assertEqual(self.steps[1].status, "active")
        self.assertIn("reproduce", self.steps[1].evidence)

    def test_a_step_orbit_never_mentioned_stays_pending(self) -> None:
        apply_narration(self.steps, "I am reading checkout.py to locate the cart code.")
        apply_narration(self.steps, "I can reproduce the failure: an empty cart raises IndexError.")
        apply_narration(self.steps, "Fixed: guard the empty cart before indexing. All tests pass now.")
        # "Fix the root cause" moved; "Verify the change" has no line behind it
        # yet, so it is not claimed.
        self.assertEqual(self.steps[2].status, "active")
        self.assertEqual(self.steps[3].status, PENDING)

    def test_a_finished_turn_completes_the_step_it_was_narrating(self) -> None:
        apply_narration(self.steps, "I am reading checkout.py to locate the cart code.")
        finish_run(self.steps)
        self.assertEqual(self.steps[0].status, "done")

    def test_a_verbose_narration_trims_the_evidence_line(self) -> None:
        long_line = "I am reading " + "checkout.py and " * 40 + "to find it."
        apply_narration(self.steps, long_line)
        self.assertLessEqual(len(self.steps[0].evidence), 150)
        self.assertTrue(self.steps[0].evidence.endswith("…"))


class FileTests(unittest.TestCase):
    def test_filenames_are_found_in_order_and_de_duplicated(self) -> None:
        self.assertEqual(
            files_in("Edit orders.py, then bot.py, then orders.py again."), ["orders.py", "bot.py"]
        )

    def test_paths_keep_their_directory(self) -> None:
        self.assertIn("src/app/orders.py", files_in("open src/app/orders.py please"))

    def test_plain_prose_yields_no_files(self) -> None:
        self.assertEqual(files_in("Add a status field to each order."), [])
        self.assertEqual(files_in(""), [])
        # A number and a sentence end are not filenames.
        self.assertEqual(files_in("Bump it to 3.5. Then stop."), [])

    def test_new_run_seeds_files_from_the_request_only(self) -> None:
        run = new_run("Add a status field to orders.py and expose it in bot.py.")
        self.assertEqual([(f.name, f.source) for f in run.files][:2], [("orders.py", SOURCE_REQUEST), ("bot.py", SOURCE_REQUEST)])
        self.assertEqual(run.status, "running")

    def test_orbit_mentioned_files_are_added_later_and_labelled(self) -> None:
        run = new_run("Fix the crash in the checkout flow.")
        self.assertEqual(run.files, [])
        self.assertTrue(add_files(run, files_in("I will patch checkout.py now."), SOURCE_ORBIT))
        self.assertEqual([(f.name, f.source) for f in run.files], [("checkout.py", SOURCE_ORBIT)])
        # Already known: no duplicate, no change.
        self.assertFalse(add_files(run, ["checkout.py"], SOURCE_ORBIT))

    def test_the_file_list_is_bounded(self) -> None:
        run = TaskRun()
        add_files(run, [f"file{i}.py" for i in range(40)], SOURCE_ORBIT)
        self.assertLessEqual(len(run.files), 8)

    def test_run_state_serialises_for_the_browser(self) -> None:
        payload = new_run("Add a status field to orders.py.").as_dict()
        self.assertEqual(set(payload), {"status", "operation", "steps", "files", "started"})
        self.assertEqual(payload["steps"][0]["title"], "Find the code for a status field")

    def test_a_planned_run_has_not_started(self) -> None:
        """The panel can draw a plan before the stream begins. That is not 'in progress'."""
        run = new_run("Fix the crash in checkout.py.")
        self.assertFalse(run.started)
        run.started = True
        self.assertTrue(run.as_dict()["started"])


class TitleTests(unittest.TestCase):
    def test_the_title_comes_from_the_request(self) -> None:
        self.assertEqual(summarize_request("Fix the crash in checkout.py."), "Fix the crash in checkout.py.")

    def test_a_long_request_is_trimmed_at_a_sentence_then_a_length(self) -> None:
        self.assertEqual(summarize_request("Short one. And a second sentence."), "Short one.")
        trimmed = summarize_request("Word " * 60)
        self.assertLessEqual(len(trimmed), 71)
        self.assertTrue(trimmed.endswith("…"))

    def test_an_empty_request_is_untitled_not_a_crash(self) -> None:
        self.assertEqual(summarize_request(""), "Untitled task")
        self.assertEqual(summarize_request("   "), "Untitled task")


if __name__ == "__main__":
    unittest.main()
