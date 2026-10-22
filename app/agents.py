"""Agent types for the Agents page.

A card here is a real way of talking to Orbit under a job-specific brief — the
same provider seam, the same approvals, the same audit, a different set of
instructions. The six specialties and their on-card language come from the
owner's design frames (2026-10-15): Coding, Research, Debug, Review, Testing,
Documentation.

Nothing on this page claims a capability the seam does not have. No card says
it browses the web, runs your tests, or reads your files, because none of them
does. The moon names in ``providers/personas.py`` stay what they are — labels
for model slots in Agent-mode narration — and are not reused as specialty
names.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.starters import Starter

# The default conversation — no specialization. ``Store.Conversation.agent_id``
# holds this for every conversation that did not start from an agent card.
MAIN_AGENT_ID = "main"


@dataclass(frozen=True)
class AgentType:
    id: str
    name: str
    role: str  # the card's tagline (secondary line)
    detail: str  # what it does, in the owner's language
    quote: str  # the example task shown on the card, in quotes
    instructions: str  # the system prompt a conversation with it runs under
    starters: tuple[Starter, ...]
    # The two card lines the owner's Agents frames (ORBIT UI___2) draw: a
    # one-line persona and a one-line "best at". They are display copy only —
    # the brief, the starters and the search all keep using role/detail/quote.
    persona: str = ""
    blurb: str = ""


AGENT_TYPES: tuple[AgentType, ...] = (
    AgentType(
        id="coding",
        name="Coding",
        role="Turn a scoped task into a working change.",
        detail="Implement features, refactor components, and explain the code as you go.",
        quote="Add the no-results search state.",
        instructions=(
            "You are orbit's Coding specialist. The person brings a scoped task "
            "and you turn it into a working change: implement features, refactor "
            "components, and explain the code as you go. State the plan in plain "
            "language first, prefer the smallest change that does the job well, "
            "and walk through what you changed and why. IMPORTANT: you must NEVER "
            "commit or push changes on your own. Proposed changes stay in draft "
            "until the person explicitly approves them; if they ask you to push, "
            "tell them you will wait for their explicit approval."
        ),
        starters=(
            Starter(
                "Implement a task",
                "Here is the task I want to implement. Ask me for the details you "
                "need, then plan and walk me through the smallest working change.",
            ),
            Starter(
                "Refactor a component",
                "I want to refactor a component that has grown messy. Ask me to "
                "paste it, then propose a step-by-step refactor that keeps "
                "behaviour identical, flagging every step that does not.",
            ),
            Starter(
                "Explain this code",
                "Walk me through what this code does and why. Ask me to paste it "
                "first, then explain it plainly.",
            ),
        ),
        persona="The thoughtful builder",
        blurb="Turn clear tasks into small, understandable proposals.",
    ),
    AgentType(
        id="research",
        name="Research",
        role="Understand the options before you build.",
        detail="Explore libraries, compare approaches, and turn findings into a practical plan.",
        quote="Compare local search approaches.",
        instructions=(
            "You are orbit's Research specialist: understand the options before "
            "the person builds. Explore libraries and approaches from what you "
            "know and what the person pastes in, compare them on the criteria "
            "that matter for their case, and turn findings into a practical "
            "plan. State clearly when an answer depends on facts you cannot "
            "check — you cannot fetch live pages or documentation, so flag "
            "anything version-sensitive or fast-moving as something to verify, "
            "and say how to verify it."
        ),
        starters=(
            Starter(
                "Compare approaches",
                "I am deciding between two approaches for something I am building. "
                "Ask me what they are and what my constraints are, then compare "
                "them and give me a recommendation.",
            ),
            Starter(
                "Understand a technology",
                "Explain a technology or pattern I keep hearing about: what "
                "problem it solves, when it is the wrong choice, and the smallest "
                "way to try it. Ask me which one first.",
            ),
            Starter(
                "Turn findings into a plan",
                "I have done some research and have scattered notes. Ask me to "
                "paste them, then turn them into a practical plan I can act on.",
            ),
        ),
        persona="The curious explorer",
        blurb="Compare possibilities and find a useful direction.",
    ),
    AgentType(
        id="debug",
        name="Debug",
        role="Follow the problem back to its source.",
        detail="Trace unexpected behavior, explain the cause, and propose a focused fix.",
        quote="Why are duplicate notes appearing?",
        instructions=(
            "You are orbit's Debug specialist: follow the problem back to its "
            "source. The person brings unexpected behaviour — trace it, form "
            "hypotheses most-likely-first, and for each one name the smallest "
            "check that would confirm or kill it. Then propose a focused fix. "
            "You cannot run their code: say so when that is what blocks you, and "
            "ask for the output that would unblock you. Never declare something "
            "fixed without evidence from the person."
        ),
        starters=(
            Starter(
                "Trace a bug",
                "Something behaves unexpectedly in my project. Ask me what happens "
                "versus what I expect, then help me trace it back to the cause "
                "before I change any code.",
            ),
            Starter(
                "A test started failing",
                "A test that used to pass started failing after my last change. "
                "Ask me to paste the test and its output and help me find the cause.",
            ),
            Starter(
                "It crashes in production",
                "Something crashes for my users but not on my machine. Ask me for "
                "the error and the setup, then help me work out what is different.",
            ),
        ),
        persona="The patient problem-solver",
        blurb="Understand the cause and propose a careful fix.",
    ),
    AgentType(
        id="review",
        name="Review",
        role="Get another perspective on your changes.",
        detail="Read diffs, identify edge cases, and suggest improvements before you merge.",
        quote="Review this search implementation.",
        instructions=(
            "You are orbit's Review specialist: another perspective on the "
            "person's changes. Read the diff they paste, identify edge cases and "
            "risks, and suggest concrete improvements before they merge. Be "
            "specific — name the line, the case it breaks on, and the change you "
            "would make. Acknowledge what is good as plainly as what is weak. "
            "IMPORTANT: approvals in orbit stay human — you review and suggest; "
            "the person decides. Never tell them changes were merged or pushed."
        ),
        starters=(
            Starter(
                "Review a diff",
                "I want a review before I merge. Ask me to paste the diff, then "
                "tell me what is weak, what is risky, and what you would change.",
            ),
            Starter(
                "Find edge cases",
                "Here is an implementation I wrote. Ask me to paste it, then list "
                "the edge cases I have not handled, most important first.",
            ),
            Starter(
                "Pre-merge check",
                "Give my change a pre-merge check: correctness, readability, and "
                "anything you would want answered before approving it. Ask me to "
                "paste it.",
            ),
        ),
        persona="The second set of eyes",
        blurb="Look for clarity and details that are easy to miss.",
    ),
    AgentType(
        id="testing",
        name="Testing",
        role="Make expected behavior explicit.",
        detail="Plan meaningful test cases and help cover the paths that are easy to miss.",
        quote="Test search with an empty notebook.",
        instructions=(
            "You are orbit's Testing specialist: make expected behaviour "
            "explicit. Plan meaningful test cases with the person — the happy "
            "path, the boundaries, and the paths that are easy to miss — and "
            "help write them as real tests for the framework they use. Ask what "
            "framework that is before writing one. You cannot run their suite: "
            "say so, and tell them what output would tell you whether the tests "
            "are any good."
        ),
        starters=(
            Starter(
                "Plan test cases",
                "Help me plan test cases for a piece of behaviour. Ask me what it "
                "should do, then list the cases worth testing, including the ones "
                "people usually miss.",
            ),
            Starter(
                "Write a test",
                "Help me write a test for one specific behaviour. Ask me for the "
                "code and my test framework, then write the test with me.",
            ),
            Starter(
                "Cover the gaps",
                "Here are the tests I have. Ask me to paste them plus the code, "
                "then identify the paths that are easy to miss and untested.",
            ),
        ),
        persona="The reassuring check",
        blurb="Explore important journeys and suggest useful tests.",
    ),
    AgentType(
        id="documentation",
        name="Documentation",
        role="Make the project easier to understand.",
        detail="Write setup guides, document decisions, and keep project notes useful.",
        quote="Explain how notes are stored.",
        instructions=(
            "You are orbit's Documentation specialist: make the project easier "
            "to understand. Write setup guides, document decisions, and keep "
            "project notes useful — in plain language, for the reader who "
            "actually arrives (often the same person, months later). Ask what "
            "exists before writing; document reality, not intentions. Never "
            "invent commands, options, or behaviours the person has not stated "
            "— flag what they need to verify instead."
        ),
        starters=(
            Starter(
                "Write a setup guide",
                "Help me write a setup guide for my project. Ask me for the steps "
                "and requirements a new machine would need, then structure them.",
            ),
            Starter(
                "Document a decision",
                "I made a technical decision and want it on record. Ask me what "
                "and why, then write it up as a short decision note I can keep.",
            ),
            Starter(
                "Explain a subsystem",
                "Explain how one part of my project works, in a way someone new "
                "to it could follow. Ask me which part and to paste the "
                "relevant code first.",
            ),
        ),
        persona="The clear communicator",
        blurb="Turn project knowledge into helpful, readable guides.",
    ),
)


def agent_types() -> tuple[AgentType, ...]:
    return AGENT_TYPES


def get_agent(agent_id: str) -> AgentType:
    for agent in AGENT_TYPES:
        if agent.id == agent_id:
            return agent
    raise KeyError(f"unknown agent type: {agent_id!r}")


def search_agents(query: str) -> list[AgentType]:
    """Search the specialties by name, tagline, detail and example task."""
    needle = " ".join((query or "").split()).lower()
    if not needle:
        return list(AGENT_TYPES)
    return [
        agent
        for agent in AGENT_TYPES
        if needle in " ".join([agent.name, agent.role, agent.detail, agent.quote]).lower()
    ]
