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
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.taskplan import TaskRun, summarize_request

# Every record in the store carries this id. It is a constant today because
# there is one user; it exists now because threading it in later means editing
# every read and write path, and that is the rewrite we are avoiding.
DEFAULT_USER_ID = os.getenv("ORBIT_USER_ID", "caleb")

ConnectionStatus = Literal["connected", "not_connected"]


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
    # Whether the one-time naming call has been made. It is made once, after
    # the first completed exchange, whether or not it succeeds.
    titled: bool = False


@dataclass
class Activity:
    text: str
    time: str


PLACEHOLDER_TITLE = "New conversation"


def now_label() -> str:
    """Local wall-clock "HH:MM" for a chat message's time line."""
    return datetime.now().strftime("%H:%M")


@dataclass
class ActionCard:
    """A tool call proposed in the home chat. ``state`` moves pending -> one final state, once."""

    id: str
    summary: str
    state: str = "pending"  # pending | approved | declined | failed | expired
    result: str = ""


@dataclass
class ChatMessage:
    who: str
    time: str
    body: str
    is_orbit: bool
    action: ActionCard | None = None
    # Stable id assigned by the store when the message is appended. It is what
    # the row's actions carry: a rating is logged against it, and regenerating a
    # reply names the message to replace. Seeded history has none, so the id is
    # empty until the store assigns one.
    id: str = ""


@dataclass
class Task:
    """One Agent task: the request, the chat around it, and its run (if any).

    ``run`` is ``None`` until the task runs. The Agent panel is rendered from it
    and from nothing else, so a task that has never run has no panel — no
    standing empty checklist, and no seeded numbers standing in for progress.
    """

    id: str
    user_id: str
    breadcrumb: str
    title: str
    description: str
    activity: list[Activity]
    chat: list[ChatMessage]
    run: TaskRun | None = None
    approved: bool = False
    applied: bool = False
    last_turn_prompt: str | None = None
    last_turn_initial: bool = False


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
    """The example task, seeded the way conversations are.

    Its request text is the only thing the Agent panel plans from. There are no
    steps, no file diffs and no activity lines here: those used to be seeded
    with invented numbers ("6 files scanned", "orders.py 38 → 61"), which read
    as progress the agent had made and never had.
    """
    return Task(
        id="T1",
        user_id=DEFAULT_USER_ID,
        breadcrumb="GitHub / whatsapp-ordering-bot / main",
        title="Add order tracking to WhatsApp bot",
        description=(
            "Add a status field to each order in orders.py, expose it through the "
            "existing order-lookup command in bot.py, and update the vendor-facing "
            'summary so a customer can ask "where\'s my order" and get a real '
            "answer. Do not push anything."
        ),
        activity=[],
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

    @classmethod
    def blank(cls, *, user: User | None = None) -> "Store":
        """A workspace with nothing in it, for an account that just signed up.

        ``Store()`` keeps its seeded sample data because the demo workspace and the
        offline suite both depend on it. A real new account must not open on
        somebody else's conversations, so the onboarding layer builds one of these
        instead: no conversations, no tasks, and GitHub genuinely *not* connected
        until the person connects it. Faking an active connection for a brand-new
        account would break the same rule Linear and Vercel are held to.
        """
        store = cls()
        store.user = user or User(name="You", workspace="Personal workspace")
        store.connections = [
            Connection(name="GitHub", status="not_connected"),
            Connection(name="Linear", status="not_connected"),
            Connection(name="Vercel", status="not_connected"),
        ]
        store.conversations = []
        store.tasks = {}
        store.threads = {}
        store.active_conversation_id = None
        return store

    def __init__(self) -> None:
        self.user = _SEED_USER
        self.connections: list[Connection] = list(_SEED_CONNECTIONS)
        self.conversations: list[Conversation] = list(_SEED_CONVERSATIONS)
        # None is a fresh, empty conversation: the composer is centered and no
        # history item is highlighted. The first send creates one.
        self.active_conversation_id: str | None = None
        # Chat/Agent and Quick/Deep selectors are shared across the two screens.
        self.mode: Literal["chat", "agent"] = "chat"
        self.thinking_level: Literal["quick", "deep"] = "quick"
        self.tasks: dict[str, Task] = {"T1": _seed_task()}
        # Home chat threads (user + Orbit turns), one per conversation id.
        self.threads: dict[str, list[ChatMessage]] = {}

    # ----- connections -----
    def connected_count(self) -> int:
        return sum(1 for c in self.connections if c.status == "connected")

    # ----- per-user scoping (single-user today, every read uses it) -----
    def conversations_for(self, user_id: str = DEFAULT_USER_ID) -> list[Conversation]:
        return [c for c in self.conversations if c.user_id == user_id]

    def tasks_for(self, user_id: str = DEFAULT_USER_ID) -> list[Task]:
        return [t for t in self.tasks.values() if t.user_id == user_id]

    # ----- conversations -----
    @property
    def home_thread(self) -> list[ChatMessage]:
        """The active conversation's messages. Empty on a fresh conversation."""
        if self.active_conversation_id is None:
            return []
        return self.threads.get(self.active_conversation_id, [])

    def get_conversation(self, conv_id: str) -> Conversation | None:
        for conv in self.conversations:
            if conv.id == conv_id:
                return conv
        return None

    def start_conversation(self) -> Conversation:
        """Create the conversation the first send belongs to, and make it active.

        It goes to the top of the Today group. It keeps the placeholder title
        until the naming call lands.
        """
        conv = Conversation(
            id=f"h-{uuid.uuid4().hex[:10]}",
            title=PLACEHOLDER_TITLE,
            group="Today",
            user_id=self.user.id,
        )
        self.conversations.insert(0, conv)
        self.threads[conv.id] = []
        self.active_conversation_id = conv.id
        return conv

    def new_conversation(self) -> None:
        """"+ New conversation": the next view is a fresh, centered composer."""
        self.active_conversation_id = None

    def append_message(self, conv_id: str, message: ChatMessage) -> None:
        self.threads.setdefault(conv_id, []).append(message)
        self._assign_id(message)

    @staticmethod
    def _assign_id(message: ChatMessage) -> ChatMessage:
        """Give a message the id its row actions use, once."""
        if not message.id:
            message.id = f"m-{uuid.uuid4().hex[:10]}"
        return message

    def find_message(self, message_id: str) -> tuple[str, ChatMessage] | None:
        """Locate a message by id across every conversation and task chat."""
        if not message_id:
            return None
        for threads in (self.threads, {t.id: t.chat for t in self.tasks.values()}):
            for owner_id, messages in threads.items():
                for message in messages:
                    if message.id == message_id:
                        return owner_id, message
        return None

    def replace_message(self, owner_id: str, message_id: str, body: str) -> bool:
        """Rewrite one stored message's text in place, keeping its position and id.

        Used when a reply is regenerated: the store must show what the screen
        shows, without reordering the conversation around it.
        """
        for messages in (self.threads.get(owner_id), getattr(self.tasks.get(owner_id), "chat", None)):
            for message in messages or []:
                if message.id == message_id:
                    message.body = body
                    return True
        return False

    def resolve_action(self, action_id: str, state: str, result: str) -> bool:
        """Record the final state of a proposed action on its card. Only a pending card changes."""
        for messages in self.threads.values():
            for message in messages:
                card = message.action
                if card is not None and card.id == action_id and card.state == "pending":
                    card.state = state
                    card.result = result
                    return True
        return False

    def set_title(self, conv_id: str, title: str) -> None:
        conv = self.get_conversation(conv_id)
        if conv is not None:
            conv.title = title

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

    def set_thinking_level(self, level: str) -> None:
        if level not in ("quick", "deep"):
            raise ValueError("thinking_level must be 'quick' or 'deep'.")
        self.thinking_level = level  # type: ignore[assignment]

    # ----- tasks -----
    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def create_task(self, request: str) -> Task:
        """A task from a request the person typed.

        The title comes from the request itself (no model call for a heading),
        and the task starts with no run: the Agent panel appears once it runs
        and plans its checklist from this text.
        """
        text = " ".join((request or "").split())
        if not text:
            raise ValueError("a task needs a request to work from.")
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        task = Task(
            id=task_id,
            user_id=self.user.id,
            breadcrumb="New task",
            title=summarize_request(text),
            description=text,
            activity=[Activity(text="Task created from your request.", time=now_label())],
            chat=[],
        )
        self.tasks[task_id] = task
        return task

    def add_chat(self, task_id: str, message: ChatMessage) -> None:
        task = self.tasks.get(task_id)
        if task is not None:
            task.chat.append(message)
            self._assign_id(message)

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
