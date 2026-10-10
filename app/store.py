"""In-memory data behind the ORBIT UI.

Everything the mockup hard-codes as placeholder text lives here as real data:
conversation history, the connected-tools state, the user, and the coding-agent
task (steps, proposed changes, activity, and the side chat). The store is the
single source of truth the templates render from, so the two screens show live
state instead of static strings.

Two rules from Phase 2 are enforced here, not just displayed:
  * Only GitHub is a real connection. Linear/Vercel are present but explicitly
    *not* connected, and never faked as active.
  * A task's proposed changes are never applied/pushed unless ``approved`` is
    set by an explicit approval call. ``apply_changes`` refuses otherwise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# Every record in the store carries this id. It is a constant today because
# there is one user; it exists now because threading it in later means editing
# every read and write path, and that is the rewrite we are avoiding.
DEFAULT_USER_ID = os.getenv("ORBIT_USER_ID", "caleb")

ConnectionStatus = Literal["connected", "not_connected"]
StepStatus = Literal["done", "active", "pending"]
ChangeStatus = Literal["proposed", "checking"]


@dataclass
class Connection:
    name: str
    status: ConnectionStatus


@dataclass
class Conversation:
    id: str
    title: str
    group: str  # "Today" | "Yesterday" | "Previous 7 days"
    # Namespaced per person from the start, while there is still only one real
    # user, so that adding "friends" later is an extension and not a rewrite.
    user_id: str = DEFAULT_USER_ID


@dataclass
class Step:
    title: str
    status: StepStatus
    detail: str


@dataclass
class Change:
    file: str
    delta: str
    desc: str
    status: ChangeStatus


@dataclass
class Activity:
    text: str
    time: str


@dataclass
class ChatMessage:
    who: str
    time: str
    body: str
    is_orbit: bool


@dataclass
class Task:
    id: str
    user_id: str
    breadcrumb: str
    title: str
    description: str
    steps: list[Step]
    changes: list[Change]
    activity: list[Activity]
    chat: list[ChatMessage]
    approved: bool = False
    applied: bool = False


@dataclass
class User:
    name: str
    workspace: str
    id: str = DEFAULT_USER_ID


# GitHub is the only real, built connection. Anything else stays visibly
# "not connected" until it is a real integration — never faked as active.
_SEED_CONNECTIONS: list[Connection] = [
    Connection(name="GitHub", status="connected"),
    Connection(name="Linear", status="not_connected"),
    Connection(name="Vercel", status="not_connected"),
]

_SEED_CONVERSATIONS: list[Conversation] = [
    Conversation(id="c1", title="Add order tracking to WhatsApp bot", group="Today", user_id=DEFAULT_USER_ID),
    Conversation(id="c2", title="Review portfolio site PR", group="Today", user_id=DEFAULT_USER_ID),
    Conversation(id="c3", title="Sync SEN106 issues with plan", group="Yesterday", user_id=DEFAULT_USER_ID),
    Conversation(id="c4", title="Explain this diff before merge", group="Yesterday", user_id=DEFAULT_USER_ID),
    Conversation(id="c5", title="Run branch tests for ordering bot", group="Previous 7 days", user_id=DEFAULT_USER_ID),
    Conversation(id="c6", title="Draft pricing page copy", group="Previous 7 days", user_id=DEFAULT_USER_ID),
]

_SEED_USER = User(name="Caleb", workspace="Personal workspace", id=DEFAULT_USER_ID)


def _seed_task() -> Task:
    return Task(
        id="T1",
        user_id=DEFAULT_USER_ID,
        breadcrumb="GitHub / whatsapp-ordering-bot / main",
        title="Add order tracking to WhatsApp bot",
        description=(
            'Add a status field to each order, expose it through the existing '
            'order-lookup command, and update the vendor-facing summary so a '
            'customer can ask "where\'s my order" and get a real answer.'
        ),
        steps=[
            Step(
                title="Read repository context",
                status="done",
                detail="6 files scanned · order model located",
            ),
            Step(
                title="Extract order status logic",
                status="done",
                detail="1 shared status helper created",
            ),
            Step(
                title="Run tests",
                status="active",
                detail="4 tests passing · 2 in progress",
            ),
            Step(
                title="Ready for your review",
                status="pending",
                detail="1 file changed · 1 approval required",
            ),
        ],
        changes=[
            Change(
                file="orders.py",
                delta="38 → 61",
                desc="Added <code>status</code> field and lookup-by-status helper",
                status="proposed",
            ),
            Change(
                file="bot.py",
                delta="22 → 30",
                desc='Wired status into the "where\'s my order" reply',
                status="checking",
            ),
        ],
        activity=[
            Activity(text="Running tests against the updated order lookup…", time="just now"),
            Activity(text="Added status field to the order model.", time="1m ago"),
            Activity(text="Read orders.py and bot.py for current structure.", time="3m ago"),
        ],
        chat=[
            ChatMessage(
                who="You",
                time="09:41",
                body=(
                    "Add order status tracking. Keep the branch as main and don't "
                    "push anything — I want to review the diff first."
                ),
                is_orbit=False,
            ),
        ],
        approved=False,
        applied=False,
    )


class Store:
    """Holds the live state the ORBIT UI renders and mutates."""

    def __init__(self) -> None:
        self.user = _SEED_USER
        self.connections: list[Connection] = list(_SEED_CONNECTIONS)
        self.conversations: list[Conversation] = list(_SEED_CONVERSATIONS)
        self.active_conversation_id: str = "c1"
        # Chat/Agent mode toggle — the same toggle surfaced in the UI.
        self.mode: Literal["chat", "agent"] = "chat"
        self.tasks: dict[str, Task] = {"T1": _seed_task()}
        # Home chat thread (user + Orbit turns) for the streaming chat.
        self.home_thread: list[ChatMessage] = []

    # ----- connections -----
    def connected_count(self) -> int:
        return sum(1 for c in self.connections if c.status == "connected")

    # ----- per-user scoping (single-user today, every read uses it) -----
    def conversations_for(self, user_id: str = DEFAULT_USER_ID) -> list[Conversation]:
        return [c for c in self.conversations if c.user_id == user_id]

    def tasks_for(self, user_id: str = DEFAULT_USER_ID) -> list[Task]:
        return [t for t in self.tasks.values() if t.user_id == user_id]

    # ----- conversations -----
    def conversations_grouped(self) -> dict[str, list[Conversation]]:
        groups: dict[str, list[Conversation]] = {}
        # Grouping the *current user's* conversations (all of them, while there
        # is one user) rather than the raw list, so the scoping call is already
        # on the read path when a second person exists.
        for conv in self.conversations_for(self.user.id):
            groups.setdefault(conv.group, []).append(conv)
        return groups

    # ----- greeting (real, time-based) -----
    def greeting(self) -> str:
        hour = datetime.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        return f"Good {part}, {self.user.name}."

    # ----- mode -----
    def set_mode(self, mode: str) -> None:
        if mode not in ("chat", "agent"):
            raise ValueError("mode must be 'chat' or 'agent'.")
        self.mode = mode  # type: ignore[assignment]

    # ----- tasks -----
    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def add_chat(self, task_id: str, message: ChatMessage) -> None:
        task = self.tasks.get(task_id)
        if task is not None:
            task.chat.append(message)

    def approve_task(self, task_id: str) -> bool:
        """Explicitly approve a task's proposed changes.

        Only an explicit call sets this. Nothing else flips it to True, which is
        the whole point of the approval gate.
        """
        task = self.tasks.get(task_id)
        if task is None:
            return False
        task.approved = True
        return True

    def apply_changes(self, task_id: str) -> Task:
        """Apply/push the proposed changes — but ONLY if explicitly approved.

        Raises RuntimeError when the task has not been approved, so a missing or
        declined approval can never silently result in a commit/push.
        """
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        if not task.approved:
            raise RuntimeError(
                "No commits will be pushed without your explicit approval."
            )
        task.applied = True
        return task
