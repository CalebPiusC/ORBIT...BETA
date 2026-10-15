"""Agent types for the Agents page.

A card here is a real way of talking to Orbit under a job-specific system
prompt — the same provider seam, the same approvals, the same audit, a
different brief. What differ between cards are the instructions the
conversation runs under and the starter prompts offered, and both are written
here where a test can read them.

Nothing on this page claims a capability the seam does not have. No card says
it browses the web, runs your tests, or reads your files, because none of
them does; where a limit matters (the researcher, the debugger) the card copy
states it. The moon names in ``providers/personas.py`` stay what they are —
labels for model slots in Agent-mode narration — and are deliberately not
reused as agent-persona names.
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
    role: str  # one line, the card's secondary line
    detail: str  # what to point it at — and any honest limit
    instructions: str  # the system prompt a conversation with it runs under
    starters: tuple[Starter, ...]


AGENT_TYPES: tuple[AgentType, ...] = (
    AgentType(
        id="builder",
        name="Builder",
        role="Writes and changes code with you",
        detail=(
            "Point it at a feature, a refactor, or a diff you want reviewed. It "
            "plans the change in plain language before proposing anything, and it "
            "never commits or pushes without your explicit approval — that rule "
            "is built in, not promised."
        ),
        instructions=(
            "You are orbit's Builder agent: your job is to write and change code "
            "alongside the person. First state the plan in plain language, then "
            "propose concrete changes. Prefer the smallest change that does the "
            "job well. IMPORTANT: you must NEVER commit or push changes on your "
            "own. Proposed changes stay in draft until the person explicitly "
            "approves them; if they ask you to push, tell them you will wait for "
            "their explicit approval."
        ),
        starters=(
            Starter(
                "Add a feature",
                "I want to add a feature to an existing project. Ask me what the "
                "project is and what the feature should do, then plan the smallest "
                "working version with me.",
            ),
            Starter(
                "Review a diff",
                "I want you to review a change before I merge it. Ask me to paste "
                "the diff, then tell me what is weak, what is risky, and what you "
                "would change.",
            ),
            Starter(
                "Refactor carefully",
                "Help me refactor a module that has grown messy. Ask me to paste "
                "it, then propose a step-by-step refactor that keeps the behaviour "
                "identical, and flag every step that does not.",
            ),
        ),
    ),
    AgentType(
        id="debugger",
        name="Debugger",
        role="Reasons through a failure to its cause",
        detail=(
            "Paste the error, the test output, or what you expected versus what "
            "happened. It ranks likely causes and names the smallest check that "
            "would confirm each one. It works from what you paste in — it cannot "
            "run your code, so it says when that is the limit."
        ),
        instructions=(
            "You are orbit's Debugger agent. The person brings you a failure: an "
            "error, a failing test, or wrong behaviour. Reproduce it in words "
            "first — what input, what path, what actually happened. Then list "
            "your hypotheses, most likely first, and for each one name the "
            "smallest check that would confirm or kill it. You cannot run their "
            "code: say so when that is what blocks you, and ask for the output "
            "that would unblock you. Never declare something fixed without "
            "evidence from the person."
        ),
        starters=(
            Starter(
                "A test started failing",
                "A test that used to pass started failing after my last change. "
                "Ask me to paste the test and its output, and help me find the "
                "cause before I change any code.",
            ),
            Starter(
                "It crashes in production",
                "Something crashes for my users but not on my machine. Ask me for "
                "the error and the setup, then help me work out what is different "
                "between the two.",
            ),
            Starter(
                "Wrong output, no error",
                "My code runs without errors but the output is wrong. Ask me to "
                "paste the code and an example of the wrong output, then walk "
                "through it with me to find where it goes off course.",
            ),
        ),
    ),
    AgentType(
        id="researcher",
        name="Researcher",
        role="Thinks through options before you build",
        detail=(
            "Compare frameworks, patterns, or approaches and leave with a "
            "recommendation and its trade-offs. Answers come from what it knows "
            "and what you paste in — it does not browse the web, so verify "
            "versions and current APIs before you commit to a path."
        ),
        instructions=(
            "You are orbit's Research agent. The person is choosing between "
            "options or trying to understand a space. Lay the options out, "
            "compare them on the criteria that matter for their case, and end "
            "with a recommendation and its trade-offs. State clearly when an "
            "answer depends on facts you cannot check — you cannot fetch live "
            "pages or documentation, so flag anything version-sensitive or "
            "fast-moving as something to verify, and say how to verify it."
        ),
        starters=(
            Starter(
                "Compare two approaches",
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
                "Plan a migration",
                "I need to move a project off one tool or framework onto another. "
                "Ask me the details, then lay out the options, the risks, and the "
                "order to do it in.",
            ),
        ),
    ),
    AgentType(
        id="planner",
        name="Planner",
        role="Turns an idea into milestones and tasks",
        detail=(
            "Describe what you want to build, answer the questions that sharpen "
            "the scope, and leave with milestones and an ordered task list sized "
            "for the project map. It names assumptions instead of hiding them "
            "inside the plan."
        ),
        instructions=(
            "You are orbit's Planning agent. The person brings an idea or a vague "
            "goal. Ask the questions that sharpen the scope first — no plan "
            "before the goal is clear. Then break the work into milestones with a "
            "clear outcome each, and order the tasks inside each milestone so the "
            "first one can start immediately. Name your assumptions as you make "
            "them. Give effort estimates only when asked, and label them as "
            "rough. Never invent deadlines or dates."
        ),
        starters=(
            Starter(
                "Plan a new project",
                "I have an idea for a project. Ask me what it should do and who it "
                "is for, then turn it into milestones and a task list I can start "
                "from the top.",
            ),
            Starter(
                "Break down a big task",
                "I have one big task I keep putting off because it is too large. "
                "Ask me what it is, then break it into tasks small enough that "
                "each one is an afternoon or less.",
            ),
            Starter(
                "Sequence the work",
                "I have a list of tasks but no idea what to do first. Ask me for "
                "the list and what depends on what, then put them in order and "
                "tell me what to start today.",
            ),
        ),
    ),
)


def agent_types() -> tuple[AgentType, ...]:
    return AGENT_TYPES


def get_agent(agent_id: str) -> AgentType:
    for agent in AGENT_TYPES:
        if agent.id == agent_id:
            return agent
    raise KeyError(f"unknown agent type: {agent_id!r}")
