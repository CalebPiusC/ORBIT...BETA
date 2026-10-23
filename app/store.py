"""In-memory data behind the ORBIT UI.

Everything the mockup hard-codes as placeholder text lives here as real data:
conversation history, the connected-tools state, the user, and the coding-agent
task (steps, proposed changes, activity, and the side chat). The store is the
single source of truth the templates render from, so the two screens show live
state instead of static strings.

Three rules from Phase 2 are enforced here, not just displayed:
  * No integration is faked as active. GitHub, Linear and Vercel all render
    *not* connected, because none of them is a real integration yet.
  * A task's proposed changes are never applied/pushed unless ``approved`` is
    set by an explicit approval call. ``apply_changes`` refuses otherwise.
  * Records belong to an account. Every method that reads or writes a
    conversation, a task, or a message requires an ``account_id`` — there is
    no default and no unscoped read path — and a record owned by another
    account behaves exactly like a record that does not exist (``None`` /
    ``False`` / no-op, ``KeyError`` from ``apply_changes``), so routes can
    answer 404 without revealing that the id is real but someone else's.
    ``Store.for_account(account_id)`` returns the bound view route code uses.

Ownership is stored in each record's ``user_id`` field — the name predates
accounts (it is also the persisted key in ``app/projects.py``), and the value
is the account id that owns the record. When the launch has no account store
at all (the test suite's single-user mode), the server resolves every request
to ``DEFAULT_USER_ID``, the seeded legacy account that owns the example task.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.taskplan import TaskRun, summarize_request

# The seeded single user's id. It owns the example task, and it is the account
# every request resolves to when the launch has no account store attached —
# the pre-accounts behaviour, named instead of implied.
DEFAULT_USER_ID = os.getenv("ORBIT_USER_ID", "caleb")

ConnectionStatus = Literal["connected", "not_connected"]


@dataclass
class Connection:
    name: str
    status: ConnectionStatus
    # Display copy for the Connected apps screen — what this integration is
    # for and the smallest scope it would ask for. Never a claim about state:
    # ``status`` alone says connected or not.
    blurb: str = ""
    scope: str = ""


@dataclass
class Conversation:
    id: str
    title: str
    group: str  # "Today" | "Yesterday" | "Previous 7 days"
    # The account that owns this conversation (see the module docstring on
    # the field name). Every store read filters on it.
    user_id: str = DEFAULT_USER_ID
    # Whether the one-time naming call has been made. It is made once, after
    # the first completed exchange, whether or not it succeeds.
    titled: bool = False
    # Which agent brief the conversation runs under. "main" is the default;
    # anything else names an AgentType from app/agents.py and picks that
    # card's system prompt instead of the plain home prompt.
    agent_id: str = "main"
    # The project workspace this conversation belongs to, if any. A project
    # conversation is the Main Agent thread for that project; it renders in
    # the workspace, never in the home chat history.
    project_id: str | None = None


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
    # Which agent brief produced this reply, as a display label ("Main Agent",
    # "Coding", ...). Written when the reply lands — it is provenance, not
    # decoration. Empty for history that predates the label or has none.
    speaker: str = ""


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
    # The person rejected this draft. Like ``approved``, only an explicit
    # decision sets it — and a later finished turn clears it, because that
    # turn is a fresh proposal to review. It never touches files: it only
    # decides whether the draft is still waiting.
    rejected: bool = False
    last_turn_prompt: str | None = None
    last_turn_initial: bool = False
    # The project this task belongs to, if any. Tasks created from a project's
    # workspace carry its id, which is what the Project Map renders from.
    project_id: str | None = None
    # The stage of work the owner filed this task under on the project's map
    # ("Foundation", "Find & organize", ...). Set by the person, never
    # guessed: a task without one lands in the map's unlabelled group.
    stage: str | None = None
    # Another task's id, set by the owner: "this one builds on that one." The
    # map renders the link only because the owner said it exists.
    builds_on: str | None = None
    # Completion is a human assertion — the person marked it complete (or
    # reopened it). A turn finishing never sets it: that is the same rule as
    # the approval gate, applied to "done".
    done: bool = False


@dataclass
class User:
    name: str
    workspace: str
    id: str = DEFAULT_USER_ID


# No integration is built yet, so nothing is connected. Every row stays visibly
# "not connected" until it is a real integration — never faked as active.
# The four integrations the Connected apps frame draws, seeded honestly: none
# of them is built, so every one starts "not_connected" and stays that way
# until real integration code exists. The blurbs and scope notes are the
# frame's copy about what each one is for.
_SEED_CONNECTIONS: list[Connection] = [
    Connection(
        name="GitHub",
        status="not_connected",
        blurb="Bring repositories into Orbit and review proposed commits before they leave.",
        scope="Read only · Selected repositories",
    ),
    Connection(
        name="Figma",
        status="not_connected",
        blurb="Give agents design context with the files and frames you choose.",
        scope="Read only · Selected files",
    ),
    Connection(
        name="Notion",
        status="not_connected",
        blurb="Keep project notes close and turn approved plans into clear documentation.",
        scope="Selected pages only",
    ),
    Connection(
        name="Slack",
        status="not_connected",
        blurb="Receive gentle reminders and share updates only when you choose.",
        scope="Chosen channels",
    ),
]

_SEED_USER = User(name="Caleb", workspace="Personal space", id=DEFAULT_USER_ID)  # owner's design frames name the workspace


def _seed_task(user_id: str = DEFAULT_USER_ID, task_id: str = "T1") -> Task:
    """The example task, seeded the way the store seeds its user.

    Its request text is the only thing the Agent panel plans from. There are no
    steps, no file diffs and no activity lines here: those used to be seeded
    with invented numbers ("6 files scanned", "orders.py 38 → 61"), which read
    as progress the agent had made and never had. The breadcrumb names no
    repository either — no repo is connected, so naming one would be a claim
    the app cannot back.

    ``T1`` itself belongs to the seeded legacy account (``DEFAULT_USER_ID``):
    a real signed-in account asking for it gets the same 404 as any unknown
    task. ``Store.agent_task`` gives every other account its own copy of
    this example, created on demand — that, not ``T1``, is where Agent
    mode lands them.
    """
    return Task(
        id=task_id,
        user_id=user_id,
        breadcrumb="Example task",
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


@dataclass
class _AccountUI:
    """One account's ephemeral screen state: which conversation is open, the
    Chat/Agent and Quick/Deep selectors, and the Agents-page selection.

    This lived on the store as process-global fields while there was one
    user. Per account it can no longer point at another account's
    conversation — the active id is also re-checked for ownership on every
    read (``Store.thread``), so even a stale pointer cannot leak a thread.
    """

    active_conversation_id: str | None = None
    mode: Literal["chat", "agent"] = "chat"
    thinking_level: Literal["quick", "deep"] = "quick"
    # The specialty picked on the Agents page, in the "ready to assign" state
    # until a project is chosen for it (or it is deselected). A page selection
    # only — not a conversation yet.
    selected_agent: str | None = None


def _require_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id is required: store access is always scoped to an account.")
    return account_id


class Store:
    """Holds the live state the ORBIT UI renders and mutates.

    The containers (``conversations``, ``tasks``, ``threads``) hold every
    account's records; the methods are the only supported read/write path,
    and every one of them that touches records requires ``account_id``.
    Route code does not call them directly — it calls
    ``store.for_account(account_id)`` once per request and works through the
    returned :class:`AccountView`, whose methods carry the same names
    without the account argument.
    """

    def __init__(self) -> None:
        self.user = _SEED_USER
        # Copies, not the seed's own objects: mutating one store's connection
        # used to change the module-level seed, and with it every other store
        # in the process (and the next test).
        self.connections: list[Connection] = [
            Connection(name=c.name, status=c.status, blurb=c.blurb, scope=c.scope)
            for c in _SEED_CONNECTIONS
        ]
        # No seeded history: a conversation exists only once the person has
        # sent something. Invented titles in the rail read as work that
        # happened when it did not.
        self.conversations: list[Conversation] = []
        self.tasks: dict[str, Task] = {"T1": _seed_task()}
        # Home chat threads (user + Orbit turns), one per conversation id.
        self.threads: dict[str, list[ChatMessage]] = {}
        # Per-account screen state, created on first use of an account.
        self._ui: dict[str, _AccountUI] = {}

    # ----- account scoping -------------------------------------------------
    def for_account(self, account_id: str) -> AccountView:
        """The store as one account sees it. ``account_id`` is required."""
        return AccountView(self, _require_account_id(account_id))

    def _ui_for(self, account_id: str) -> _AccountUI:
        return self._ui.setdefault(account_id, _AccountUI())

    # ----- connections (workspace-global; no account owns these rows) -----
    def connected_count(self) -> int:
        return sum(1 for c in self.connections if c.status == "connected")

    # ----- per-account listing ---------------------------------------------
    def conversations_for(self, *, account_id: str) -> list[Conversation]:
        return [
            c
            for c in self.conversations
            if c.user_id == account_id and c.project_id is None
        ]

    def tasks_for(self, *, account_id: str) -> list[Task]:
        return [t for t in self.tasks.values() if t.user_id == account_id]

    def agent_task(self, *, account_id: str) -> Task:
        """The account's Agent-mode landing task: its example task, found or
        created on demand.

        The seeded ``T1`` is the legacy account's instance. Any other
        account gets its own copy the first time it switches to Agent
        mode, and the same task thereafter — the toggle must land the
        person on a task they own, never on a hardcoded id that may be
        another account's (which would now correctly 404).
        """
        for task in self.tasks_for(account_id=account_id):
            if task.breadcrumb == "Example task" and task.project_id is None:
                return task
        task = _seed_task(
            user_id=account_id, task_id=f"t-{uuid.uuid4().hex[:8]}"
        )
        self.tasks[task.id] = task
        return task

    def tasks_for_project(self, project_id: str, *, account_id: str) -> list[Task]:
        """The tasks one project's workspace renders, oldest first.

        ``self.tasks`` is insertion-ordered, so this is creation order — the
        map's stable reading order ("Next up" derives from it).
        """
        return [
            t
            for t in self.tasks.values()
            if t.project_id == project_id and t.user_id == account_id
        ]

    # ----- project conversations (the workspace's Main Agent thread) -------
    def project_conversations(
        self, project_id: str, *, account_id: str
    ) -> list[Conversation]:
        """Every agent conversation a project carries (main + assigned
        specialists). Rendering the workspace lists these; their agent ids are
        the only options the panel's agent switcher may offer."""
        return [
            c
            for c in self.conversations
            if c.project_id == project_id and c.user_id == account_id
        ]

    def project_conversation(
        self,
        project_id: str,
        *,
        account_id: str,
        create: bool = False,
        agent_id: str = "main",
    ) -> Conversation | None:
        """One project's conversation with one agent (``"main"`` or a specialty).

        Find-or-create: reading a workspace (GET) never creates anything — the
        conversation is born when the person assigns a specialist or sends the
        first message. It is titled on creation: naming calls belong to the
        home history, not to a thread that never renders there.
        """
        for conv in self.conversations:
            if (
                conv.project_id == project_id
                and conv.user_id == account_id
                and conv.agent_id == agent_id
            ):
                return conv
        if not create:
            return None
        conv = Conversation(
            id=f"pc-{uuid.uuid4().hex[:10]}",
            title="Project conversation",
            group="Today",
            user_id=account_id,
            titled=True,
            project_id=project_id,
            agent_id=agent_id,
        )
        self.conversations.insert(0, conv)
        self.threads[conv.id] = []
        return conv

    def project_thread(
        self, project_id: str, *, account_id: str, agent_id: str = "main"
    ) -> list[ChatMessage]:
        conv = self.project_conversation(
            project_id, account_id=account_id, agent_id=agent_id
        )
        if conv is None:
            return []
        return self.threads.get(conv.id, [])

    # ----- conversations -----------------------------------------------------
    def get_conversation(
        self, conv_id: str, *, account_id: str
    ) -> Conversation | None:
        for conv in self.conversations:
            if conv.id == conv_id and conv.user_id == account_id:
                return conv
        return None

    def start_conversation(
        self, *, account_id: str, agent_id: str = "main"
    ) -> Conversation:
        """Create the conversation the first send belongs to, and make it active.

        It goes to the top of the Today group. It keeps the placeholder title
        until the naming call lands. ``agent_id`` is the Agents-page card the
        conversation started from (or "main" for the default assistant) — the
        chat route picks the matching system prompt from it.
        """
        conv = Conversation(
            id=f"h-{uuid.uuid4().hex[:10]}",
            title=PLACEHOLDER_TITLE,
            group="Today",
            user_id=account_id,
            agent_id=agent_id,
        )
        self.conversations.insert(0, conv)
        self.threads[conv.id] = []
        self._ui_for(account_id).active_conversation_id = conv.id
        return conv

    def new_conversation(self, *, account_id: str) -> None:
        """\"+ New conversation\": the next view is a fresh, centered composer."""
        self._ui_for(account_id).active_conversation_id = None

    def thread(self, conv_id: str, *, account_id: str) -> list[ChatMessage]:
        """One owned conversation's live message list; ``[]`` for a
        conversation that is unknown or another account's. Returns the stored
        list itself — the regenerate path rewrites it in place — so ownership
        is decided here, once, before the caller touches it."""
        if self.get_conversation(conv_id, account_id=account_id) is None:
            return []
        return self.threads.get(conv_id, [])

    def home_thread(self, *, account_id: str) -> list[ChatMessage]:
        """The account's active conversation's messages. Empty when fresh."""
        active = self._ui_for(account_id).active_conversation_id
        if active is None:
            return []
        return self.thread(active, account_id=account_id)

    def append_message(
        self, conv_id: str, message: ChatMessage, *, account_id: str
    ) -> None:
        """Append to an owned conversation. Unknown or foreign conversation:
        a no-op — a message is never filed under a conversation its sender
        does not own (previously any id got a thread, owned or not)."""
        if self.get_conversation(conv_id, account_id=account_id) is None:
            return
        self.threads.setdefault(conv_id, []).append(message)
        self._assign_id(message)

    @staticmethod
    def _assign_id(message: ChatMessage) -> ChatMessage:
        """Give a message the id its row actions use, once."""
        if not message.id:
            message.id = f"m-{uuid.uuid4().hex[:10]}"
        return message

    def find_message(
        self, message_id: str, *, account_id: str
    ) -> tuple[str, ChatMessage] | None:
        """Locate a message by id across the account's conversations and
        task chats. Another account's message is not found."""
        if not message_id:
            return None
        for conv in self.conversations:
            if conv.user_id != account_id:
                continue
            for message in self.threads.get(conv.id, []):
                if message.id == message_id:
                    return conv.id, message
        for task in self.tasks.values():
            if task.user_id != account_id:
                continue
            for message in task.chat:
                if message.id == message_id:
                    return task.id, message
        return None

    def replace_message(
        self, owner_id: str, message_id: str, body: str, *, account_id: str
    ) -> bool:
        """Rewrite one stored message's text in place, keeping its position and id.

        Used when a reply is regenerated: the store must show what the screen
        shows, without reordering the conversation around it. ``owner_id`` is
        the conversation or task the message lives in; it must be the
        account's own, or nothing is rewritten.
        """
        messages: list[ChatMessage] | None = None
        if self.get_conversation(owner_id, account_id=account_id) is not None:
            messages = self.threads.get(owner_id)
        else:
            task = self.get_task(owner_id, account_id=account_id)
            messages = task.chat if task is not None else None
        for message in messages or []:
            if message.id == message_id:
                message.body = body
                return True
        return False

    # ----- dashboard roll-ups (read-only, per account) ----------------------
    def pending_approvals(self, *, account_id: str) -> list[dict[str, Any]]:
        """The still-pending proposals in the open home conversation, newest first.

        A proposal is a tool call the model asked for in the home chat. The
        store keeps its card on the message that proposed it, and only the home
        thread renders the Approve/Decline controls — the project chat offers no
        tools by design. The home screen shows one conversation at a time and
        there is no conversation switcher, so the open conversation is exactly
        the set a person can review right now; counting cards from a thread the
        chat cannot reach would put a number on the dashboard that its own
        button could not show.

        Read-only: nothing here decides, queues or expires anything.
        """
        ui = self._ui.get(account_id)
        conv_id = ui.active_conversation_id if ui is not None else None
        if conv_id is None:
            return []
        pending: list[dict[str, Any]] = []
        for message in self.threads.get(conv_id, []):
            card = message.action
            if card is not None and card.state == "pending":
                pending.append(
                    {
                        "id": card.id,
                        "summary": card.summary,
                        "conversation_id": conv_id,
                    }
                )
        pending.reverse()  # the last one appended is the most recent
        return pending

    def recent_activity(self, *, account_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """The account's most recently recorded task-activity lines.

        Each task keeps its own activity list, newest first, written when the
        event happened. The store records a clock label, not a date, so items
        are grouped by task in creation order (newest task first) instead of
        being merged into a cross-task chronology the data cannot prove.

        Read-only: returns copies, never the stored objects.
        """
        items: list[dict[str, Any]] = []
        tasks = [t for t in self.tasks.values() if t.user_id == account_id]
        for task in reversed(tasks):
            for entry in task.activity:
                items.append(
                    {
                        "text": entry.text,
                        "time": entry.time,
                        "task": task.title,
                        "task_id": task.id,
                        "project_id": task.project_id,
                    }
                )
                if len(items) >= limit:
                    return items
        return items

    def resolve_action(
        self, action_id: str, state: str, result: str, *, account_id: str
    ) -> bool:
        """Record the final state of a proposed action on its card. Only a pending card changes."""
        for conv in self.conversations:
            if conv.user_id != account_id:
                continue
            for message in self.threads.get(conv.id, []):
                card = message.action
                if card is not None and card.id == action_id and card.state == "pending":
                    card.state = state
                    card.result = result
                    return True
        return False

    def set_title(self, conv_id: str, title: str, *, account_id: str) -> None:
        conv = self.get_conversation(conv_id, account_id=account_id)
        if conv is not None:
            conv.title = title

    def conversations_grouped(
        self, *, account_id: str
    ) -> dict[str, list[Conversation]]:
        groups: dict[str, list[Conversation]] = {}
        for conv in self.conversations_for(account_id=account_id):
            groups.setdefault(conv.group, []).append(conv)
        return groups

    # ----- greeting (the seeded user's, for no-accounts launches) ----------
    def greeting(self) -> str:
        hour = datetime.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        return f"Good {part}, {self.user.name}."

    # ----- mode (per account) --------------------------------------------------
    def set_mode(self, mode: str, *, account_id: str) -> None:
        if mode not in ("chat", "agent"):
            raise ValueError("mode must be 'chat' or 'agent'.")
        self._ui_for(account_id).mode = mode  # type: ignore[assignment]

    def set_thinking_level(self, level: str, *, account_id: str) -> None:
        if level not in ("quick", "deep"):
            raise ValueError("thinking_level must be 'quick' or 'deep'.")
        self._ui_for(account_id).thinking_level = level  # type: ignore[assignment]

    # ----- tasks ----------------------------------------------------------------
    def get_task(self, task_id: str, *, account_id: str) -> Task | None:
        task = self.tasks.get(task_id)
        if task is None or task.user_id != account_id:
            return None
        return task

    def create_task(
        self,
        request: str,
        *,
        account_id: str,
        project_id: str | None = None,
        stage: str | None = None,
        builds_on: str | None = None,
    ) -> Task:
        """A task from a request the person typed.

        The title comes from the request itself (no model call for a heading),
        and the task starts with no run: the Agent panel appears once it runs
        and plans its checklist from this text. ``project_id`` attaches the
        task to a project workspace's map; ``stage`` files it under that
        map's stage; ``builds_on`` links it to the task the person said it
        follows. A ``builds_on`` id that is not one of the account's own
        tasks is refused — a dependency link may only point at something
        that exists *for this account*.
        """
        text = " ".join((request or "").split())
        if not text:
            raise ValueError("a task needs a request to work from.")
        if builds_on is not None and self.get_task(
            builds_on, account_id=account_id
        ) is None:
            raise ValueError(f"builds_on points at an unknown task: {builds_on}")
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        task = Task(
            id=task_id,
            user_id=account_id,
            breadcrumb="New task",
            title=summarize_request(text),
            description=text,
            activity=[Activity(text="Task created from your request.", time=now_label())],
            chat=[],
            project_id=project_id,
            stage=" ".join((stage or "").split()) or None,
            builds_on=builds_on,
        )
        self.tasks[task_id] = task
        return task

    def set_done(
        self, task_id: str, done: bool = True, *, account_id: str
    ) -> Task | None:
        """Mark a task complete, or reopen it. The person's call, recorded."""
        task = self.get_task(task_id, account_id=account_id)
        if task is None:
            return None
        task.done = bool(done)
        marker = "You marked this task complete." if task.done else "You reopened this task."
        task.activity.insert(0, Activity(text=marker, time=now_label()))
        return task

    def add_chat(
        self, task_id: str, message: ChatMessage, *, account_id: str
    ) -> None:
        task = self.get_task(task_id, account_id=account_id)
        if task is not None:
            task.chat.append(message)
            self._assign_id(message)

    def approve_task(self, task_id: str, *, account_id: str) -> bool:
        """Explicitly approve a task's proposed changes.

        Only an explicit call sets this. Nothing else flips it to True, which is
        the whole point of the approval gate.
        """
        task = self.get_task(task_id, account_id=account_id)
        if task is None:
            return False
        task.approved = True
        return True

    def reject_task(self, task_id: str, *, account_id: str) -> bool:
        """Explicitly reject a task's proposed draft.

        The decision is recorded and the draft stops waiting — but no file is
        touched and nothing is applied: a rejected draft is the opposite of an
        applied one. Like approval, only this explicit call flips it, and a
        later finished turn proposes a fresh draft (the flag clears there).
        """
        task = self.get_task(task_id, account_id=account_id)
        if task is None:
            return False
        task.rejected = True
        return True

    def apply_changes(self, task_id: str, *, account_id: str) -> Task:
        """Apply/push the proposed changes — but ONLY if explicitly approved.

        Raises RuntimeError when the task has not been approved, so a missing or
        declined approval can never silently result in a commit/push. A task
        that is another account's raises the same KeyError as an unknown id:
        the caller cannot tell the two apart.
        """
        task = self.get_task(task_id, account_id=account_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        if not task.approved:
            raise RuntimeError(
                "No commits will be pushed without your explicit approval."
            )
        task.applied = True
        return task


class AccountView:
    """The store scoped to one account — what route code works through.

    Obtained from ``Store.for_account(account_id)``. Method names mirror the
    store's, minus the ``account_id`` argument the view supplies. A foreign
    record answers exactly like a missing one, so a route holding this view
    cannot leak another account's data by forgetting a check: there is no
    unscoped getter to forget with.
    """

    def __init__(self, store: Store, account_id: str) -> None:
        self.store = store
        self.account_id = account_id

    # ----- seeded-user / global surface the chrome still reads -------------
    @property
    def user(self) -> User:
        """The seeded legacy user. Display fallback for no-accounts launches;
        signed-in chrome renders the account instead (``_user_view``)."""
        return self.store.user

    @property
    def connections(self) -> list[Connection]:
        return self.store.connections

    def connected_count(self) -> int:
        return self.store.connected_count()

    # ----- per-account screen state -------------------------------------------
    @property
    def _ui(self) -> _AccountUI:
        return self.store._ui_for(self.account_id)

    @property
    def active_conversation_id(self) -> str | None:
        return self._ui.active_conversation_id

    @active_conversation_id.setter
    def active_conversation_id(self, value: str | None) -> None:
        self._ui.active_conversation_id = value

    @property
    def mode(self) -> Literal["chat", "agent"]:
        return self._ui.mode

    @property
    def thinking_level(self) -> Literal["quick", "deep"]:
        return self._ui.thinking_level

    @property
    def selected_agent(self) -> str | None:
        return self._ui.selected_agent

    @selected_agent.setter
    def selected_agent(self, value: str | None) -> None:
        self._ui.selected_agent = value

    @property
    def home_thread(self) -> list[ChatMessage]:
        return self.store.home_thread(account_id=self.account_id)

    # ----- conversations ---------------------------------------------------------
    def conversations_for(self) -> list[Conversation]:
        return self.store.conversations_for(account_id=self.account_id)

    def conversations_grouped(self) -> dict[str, list[Conversation]]:
        return self.store.conversations_grouped(account_id=self.account_id)

    def get_conversation(self, conv_id: str) -> Conversation | None:
        return self.store.get_conversation(conv_id, account_id=self.account_id)

    def start_conversation(self, agent_id: str = "main") -> Conversation:
        return self.store.start_conversation(
            account_id=self.account_id, agent_id=agent_id
        )

    def new_conversation(self) -> None:
        self.store.new_conversation(account_id=self.account_id)

    def thread(self, conv_id: str) -> list[ChatMessage]:
        return self.store.thread(conv_id, account_id=self.account_id)

    def append_message(self, conv_id: str, message: ChatMessage) -> None:
        self.store.append_message(conv_id, message, account_id=self.account_id)

    def find_message(self, message_id: str) -> tuple[str, ChatMessage] | None:
        return self.store.find_message(message_id, account_id=self.account_id)

    def replace_message(self, owner_id: str, message_id: str, body: str) -> bool:
        return self.store.replace_message(
            owner_id, message_id, body, account_id=self.account_id
        )

    def resolve_action(self, action_id: str, state: str, result: str) -> bool:
        return self.store.resolve_action(
            action_id, state, result, account_id=self.account_id
        )

    def set_title(self, conv_id: str, title: str) -> None:
        self.store.set_title(conv_id, title, account_id=self.account_id)

    # ----- project conversations ---------------------------------------------------
    def project_conversations(self, project_id: str) -> list[Conversation]:
        return self.store.project_conversations(
            project_id, account_id=self.account_id
        )

    def project_conversation(
        self,
        project_id: str,
        *,
        create: bool = False,
        agent_id: str = "main",
    ) -> Conversation | None:
        return self.store.project_conversation(
            project_id, account_id=self.account_id, create=create, agent_id=agent_id
        )

    def project_thread(
        self, project_id: str, agent_id: str = "main"
    ) -> list[ChatMessage]:
        return self.store.project_thread(
            project_id, account_id=self.account_id, agent_id=agent_id
        )

    # ----- mode ----------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self.store.set_mode(mode, account_id=self.account_id)

    def set_thinking_level(self, level: str) -> None:
        self.store.set_thinking_level(level, account_id=self.account_id)

    # ----- tasks -----------------------------------------------------------------------
    def tasks_for(self) -> list[Task]:
        return self.store.tasks_for(account_id=self.account_id)

    def agent_task(self) -> Task:
        return self.store.agent_task(account_id=self.account_id)

    def tasks_for_project(self, project_id: str) -> list[Task]:
        return self.store.tasks_for_project(
            project_id, account_id=self.account_id
        )

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get_task(task_id, account_id=self.account_id)

    def create_task(
        self,
        request: str,
        project_id: str | None = None,
        stage: str | None = None,
        builds_on: str | None = None,
    ) -> Task:
        return self.store.create_task(
            request,
            account_id=self.account_id,
            project_id=project_id,
            stage=stage,
            builds_on=builds_on,
        )

    def set_done(self, task_id: str, done: bool = True) -> Task | None:
        return self.store.set_done(task_id, done, account_id=self.account_id)

    def add_chat(self, task_id: str, message: ChatMessage) -> None:
        self.store.add_chat(task_id, message, account_id=self.account_id)

    def approve_task(self, task_id: str) -> bool:
        return self.store.approve_task(task_id, account_id=self.account_id)

    def reject_task(self, task_id: str) -> bool:
        return self.store.reject_task(task_id, account_id=self.account_id)

    def apply_changes(self, task_id: str) -> Task:
        return self.store.apply_changes(task_id, account_id=self.account_id)

    # ----- dashboard roll-ups (read-only) ----------------------------------
    def pending_approvals(self) -> list[dict[str, Any]]:
        return self.store.pending_approvals(account_id=self.account_id)

    def recent_activity(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.store.recent_activity(account_id=self.account_id, limit=limit)
