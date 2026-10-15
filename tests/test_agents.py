"""Offline tests for the Agents page registry (app/agents.py).

The page is a grid of agent *types*, not one agent per project, and every card
must earn its place: a real brief (system prompt), real starter prompts a
person could have typed, and honest detail copy that never claims a capability
the provider seam does not have.
"""

from __future__ import annotations

import unittest

from app.agents import AGENT_TYPES, MAIN_AGENT_ID, agent_types, get_agent
from providers.personas import RESERVED_NAMES, SLOT_NAMES


class AgentRegistryTests(unittest.TestCase):
    def test_ids_are_unique_and_stable(self) -> None:
        ids = [a.id for a in AGENT_TYPES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, ["builder", "debugger", "researcher", "planner"])

    def test_every_card_is_complete(self) -> None:
        for agent in AGENT_TYPES:
            self.assertTrue(agent.name, agent.id)
            self.assertTrue(agent.role, agent.id)
            self.assertTrue(agent.detail, agent.id)
            self.assertIn("orbit", agent.instructions.lower(), agent.id)
            self.assertGreaterEqual(len(agent.starters), 3, agent.id)

    def test_starters_are_real_prompts_not_labels(self) -> None:
        # Same rule as the welcome screen: a chip sends a prompt the person
        # could have typed; it is never a bare label left in the box.
        for agent in AGENT_TYPES:
            for starter in agent.starters:
                self.assertGreater(len(starter.prompt), len(starter.label) + 20, starter.label)
                self.assertNotEqual(starter.prompt.strip().lower(), starter.label.strip().lower())

    def test_no_card_claims_capabilities_the_seam_lacks(self) -> None:
        # "No placeholder is faked as live" applied to copy: the cards may not
        # promise live browsing, local file access, or test execution.
        banned = ("i browse", "i can browse", "browses the web", "i run your tests",
                  "reads your files automatically", "access to your filesystem")
        for agent in AGENT_TYPES:
            copy = f"{agent.detail} {agent.instructions}".lower()
            for phrase in banned:
                self.assertNotIn(phrase, copy, f"{agent.id} claims {phrase!r}")

    def test_honest_limits_are_stated_where_they_matter(self) -> None:
        self.assertIn("does not browse the web", get_agent("researcher").detail)
        self.assertIn("cannot run your code", get_agent("debugger").detail)

    def test_agent_names_stay_separate_from_model_moon_names(self) -> None:
        # Moon names label which model answered in Agent mode; agent cards are
        # job types. Reusing a moon name here would conflate the two.
        moons = set(SLOT_NAMES) | set(RESERVED_NAMES)
        for agent in AGENT_TYPES:
            self.assertNotIn(agent.name, moons)

    def test_get_agent_and_main_default(self) -> None:
        self.assertEqual(MAIN_AGENT_ID, "main")
        self.assertEqual(get_agent("builder").name, "Builder")
        with self.assertRaises(KeyError):
            get_agent("oracle")
        self.assertIs(agent_types(), AGENT_TYPES)


if __name__ == "__main__":
    unittest.main()
