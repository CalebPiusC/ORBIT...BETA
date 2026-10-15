"""Offline tests for the Agents page registry (app/agents.py).

The page is a grid of agent *types*, not one agent per project, and every card
must earn its place: a real brief (system prompt), real starter prompts a
person could have typed, and honest detail copy that never claims a capability
the provider seam does not have.
"""

from __future__ import annotations

import unittest

from app.agents import AGENT_TYPES, MAIN_AGENT_ID, agent_types, get_agent, search_agents
from providers.personas import RESERVED_NAMES, SLOT_NAMES


class AgentRegistryTests(unittest.TestCase):
    def test_ids_are_unique_and_stable(self) -> None:
        ids = [a.id for a in AGENT_TYPES]
        self.assertEqual(len(ids), len(set(ids)))
        # The six job specialties from the owner's design frames.
        self.assertEqual(ids, ["coding", "research", "debug", "review", "testing", "documentation"])

    def test_every_card_is_complete(self) -> None:
        for agent in AGENT_TYPES:
            self.assertTrue(agent.name, agent.id)
            self.assertTrue(agent.role, agent.id)
            self.assertTrue(agent.detail, agent.id)
            self.assertTrue(agent.quote, agent.id)  # the card's example task
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
        # The limit lives in the brief the conversation actually runs under.
        research = get_agent("research").instructions
        self.assertIn("cannot fetch live pages", research)
        debug = get_agent("debug").instructions
        self.assertIn("cannot run their code", debug)
        testing = get_agent("testing").instructions
        self.assertIn("cannot run their suite", testing)

    def test_agent_names_stay_separate_from_model_moon_names(self) -> None:
        # Moon names label which model answered in Agent mode; agent cards are
        # job types. Reusing a moon name here would conflate the two.
        moons = set(SLOT_NAMES) | set(RESERVED_NAMES)
        for agent in AGENT_TYPES:
            self.assertNotIn(agent.name, moons)

    def test_get_agent_and_main_default(self) -> None:
        self.assertEqual(MAIN_AGENT_ID, "main")
        self.assertEqual(get_agent("coding").name, "Coding")
        with self.assertRaises(KeyError):
            get_agent("oracle")
        self.assertIs(agent_types(), AGENT_TYPES)

    def test_search_covers_the_card_copy(self) -> None:
        self.assertEqual(search_agents(""), list(AGENT_TYPES))
        hits = search_agents("empty notebook")
        self.assertEqual([a.id for a in hits], ["testing"])
        self.assertIn("research", [a.id for a in search_agents("libraries")])
        self.assertEqual(search_agents("nonox"), [])


if __name__ == "__main__":
    unittest.main()
