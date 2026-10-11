"""Follow-up chips are only offered when the reply gives them something to act on.

The rule this pins: a chip is suggested for an observable property of the reply
on screen (a code block), never for a guess about what the conversation was
"about". A wrong suggestion is worse than no suggestion, so the common case in
these tests is "no chips".
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Optional

from app.followups import (
    CODE_FOLLOWUPS,
    followups_for,
    has_code_block,
    last_orbit_answer,
    suggest_followups,
)

CODE_REPLY = (
    "Here is the helper:\n\n"
    "```python\n"
    "def status_for(order):\n"
    "    return order.get('status', 'pending')\n"
    "```\n\n"
    "Wire it into the lookup command."
)


@dataclass
class FakeMessage:
    body: str
    is_orbit: bool
    action: Optional[object] = None


class SuggestionTests(unittest.TestCase):
    def test_a_reply_with_code_offers_the_three_code_actions(self) -> None:
        chips = suggest_followups(CODE_REPLY)
        self.assertEqual([c.label for c in chips], ["Explain this code", "Find potential bugs", "Improve implementation"])
        self.assertEqual(chips, CODE_FOLLOWUPS)
        # Each chip is a real prompt, not a label: it names the thing to act on.
        for chip in chips:
            self.assertTrue(chip.prompt.strip())
            self.assertGreater(len(chip.prompt), 30)

    def test_a_reply_without_code_offers_nothing(self) -> None:
        self.assertEqual(suggest_followups("The order lookup lives in orders.py."), ())
        self.assertEqual(suggest_followups("Sure — what would you like to do next?"), ())
        self.assertEqual(suggest_followups(""), ())
        self.assertEqual(suggest_followups(None), ())

    def test_an_unterminated_fence_is_not_a_code_block(self) -> None:
        self.assertFalse(has_code_block("```python\ndef status_for(order):"))
        self.assertEqual(suggest_followups("```python\ndef status_for(order):"), ())

    def test_a_oneline_fence_still_counts_as_code(self) -> None:
        self.assertTrue(has_code_block("Use `x` here:\n```\nx = 1\n```"))

    def test_the_last_orbit_reply_is_the_one_that_counts(self) -> None:
        thread = [
            FakeMessage("Add status tracking.", False),
            FakeMessage(CODE_REPLY, True),
            FakeMessage("Thanks.", False),
        ]
        self.assertEqual(last_orbit_answer(thread), CODE_REPLY)
        self.assertEqual(followups_for(thread), CODE_FOLLOWUPS)

    def test_a_user_message_that_mentions_code_offers_nothing(self) -> None:
        thread = [FakeMessage(CODE_REPLY, False)]
        self.assertEqual(followups_for(thread), ())

    def test_a_proposed_action_card_is_not_read_as_a_reply(self) -> None:
        thread = [FakeMessage("Save a note: call the vendor", True, action=object())]
        self.assertEqual(last_orbit_answer(thread), "")
        self.assertEqual(followups_for(thread), ())

    def test_an_empty_thread_offers_nothing(self) -> None:
        self.assertEqual(followups_for([]), ())

    def test_chips_are_serialisable_for_the_stream(self) -> None:
        payload = [chip.as_dict() for chip in CODE_FOLLOWUPS]
        self.assertEqual(payload[0]["label"], "Explain this code")
        self.assertIn("prompt", payload[0])


if __name__ == "__main__":
    unittest.main()
