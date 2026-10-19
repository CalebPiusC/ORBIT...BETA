"""Queue-then-approve for tool calls the home chat proposes.

The model can ask for one tool call per reply. Nothing runs when it asks. The
call is queued here, shown to the person as a card, and runs only when they
click Approve. Declining also goes through the registry, so both outcomes are
audited the same way a terminal confirmation is.

The queue is in memory, like the rest of the chat state. A proposal that is
never answered simply does not run.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from tools.confirmation import DeferredConfirmationGate
from app.database import Database
from tools.registry import AuditLog, ToolOutcome, ToolRegistry


class ProposalRejected(ValueError):
    """The model asked for something that cannot be proposed. Nothing was queued."""


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: str
    tool_name: str
    arguments: dict[str, Any] = field(compare=False)
    summary: str
    # The account whose conversation proposed this call. Only that account
    # may approve or decline it — the decide route checks this *before*
    # anything can run, and answers the same 404 as a spent/unknown id.
    # Empty only for proposals made outside a request (unit tests of the
    # queue itself); the chat route always binds it.
    account_id: str = ""


def summarize(tool_name: str, arguments: dict[str, Any]) -> str:
    """One plain sentence for the card. It shows exactly what would run."""

    def text(key: str) -> str:
        return str(arguments.get(key, ""))

    if tool_name == "write_note":
        return f"Add this line to your notes: \u201c{text('content')}\u201d"
    if tool_name == "remember_fact":
        return f"Remember this about you: \u201c{text('fact')}\u201d"
    if tool_name == "forget_fact":
        return f"Forget saved facts that match: \u201c{text('fact')}\u201d"
    if tool_name == "save_plan":
        return f"Save today\u2019s plan: \u201c{text('plan')}\u201d"
    if tool_name == "pause_heartbeat":
        return "Pause reminders: the missing-plan and check-in notices, and phone push."
    if tool_name == "resume_heartbeat":
        return "Resume reminders: the missing-plan and check-in notices, and phone push."
    return f"Run {tool_name} with {json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"


class PendingActions:
    def __init__(
        self,
        registry: ToolRegistry,
        gate: DeferredConfirmationGate,
        *,
        audit: AuditLog | None = None,
        database: Database | None = None,
    ) -> None:
        self._registry = registry
        self._gate = gate
        self._audit = audit
        self._database = database
        self._pending: dict[str, PendingAction] = {}
        self._lock = threading.Lock()

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    def propose(
        self, tool_name: str, arguments: Any, *, account_id: str = ""
    ) -> PendingAction:
        """Queue one model-proposed call. Raises ProposalRejected, and audits it, if it cannot run."""
        problem = self._registry.validate(tool_name, arguments)
        if problem is not None:
            execution, message = problem
            self._registry.reject_call(
                tool_name if isinstance(tool_name, str) else "unknown_tool",
                arguments,
                execution=execution,
                message=message,
            )
            raise ProposalRejected(message)

        safe_arguments = copy.deepcopy(dict(arguments))
        action = PendingAction(
            id=f"act-{uuid.uuid4().hex[:12]}",
            tool_name=tool_name,
            arguments=safe_arguments,
            summary=summarize(tool_name, safe_arguments),
            account_id=account_id,
        )
        if self._database is not None:
            if not account_id:
                raise ValueError("account_id is required to persist a tool call")
            with self._database.connect() as db:
                db.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (account_id,))
                db.execute(
                    "INSERT INTO tool_calls(id, account_id, payload) VALUES (?, ?, ?)",
                    (
                        action.id,
                        account_id,
                        json.dumps(
                            {"tool_name": action.tool_name, "arguments": action.arguments, "summary": action.summary},
                            ensure_ascii=False,
                        ),
                    ),
                )
        with self._lock:
            self._pending[action.id] = action
        if self._audit is not None:
            self._audit.record_event("tool_proposed", action_id=action.id, tool_name=tool_name)
        return action

    def owner_of(self, action_id: str, *, account_id: str | None = None) -> str | None:
        """Peek at ownership without consuming a pending proposal."""
        with self._lock:
            action = self._pending.get(action_id)
        if action is not None:
            return action.account_id if not account_id or action.account_id == account_id else None
        if self._database is not None and account_id:
            with self._database.connect() as db:
                row = db.execute(
                    "SELECT account_id FROM tool_calls WHERE account_id = ? AND id = ?",
                    (account_id, action_id),
                ).fetchone()
            return row[0] if row else None
        return None

    def decide(
        self, action_id: str, *, approved: bool, account_id: str | None = None
    ) -> ToolOutcome:
        """Resolve one queued call. Persistent lookups are always owner-scoped."""
        with self._lock:
            action = self._pending.get(action_id)
            if action is not None and account_id and action.account_id != account_id:
                raise KeyError(action_id)
            if action is not None:
                self._pending.pop(action_id, None)
        if action is None and self._database is not None and account_id:
            with self._database.connect() as db:
                row = db.execute(
                    "SELECT payload FROM tool_calls WHERE account_id = ? AND id = ?",
                    (account_id, action_id),
                ).fetchone()
                if row is None:
                    raise KeyError(action_id)
                payload = json.loads(row[0])
                db.execute(
                    "DELETE FROM tool_calls WHERE account_id = ? AND id = ?",
                    (account_id, action_id),
                )
            action = PendingAction(
                id=action_id,
                tool_name=payload["tool_name"],
                arguments=payload["arguments"],
                summary=payload["summary"],
                account_id=account_id,
            )
        elif action is None:
            raise KeyError(action_id)
        elif self._database is not None:
            if not account_id:
                raise KeyError(action_id)
            with self._database.connect() as db:
                deleted = db.execute(
                    "DELETE FROM tool_calls WHERE account_id = ? AND id = ?",
                    (account_id, action_id),
                )
                if deleted.rowcount != 1:
                    raise KeyError(action_id)
        # The recorded decision is the only thing confirm() sees, so the call
        # runs with exactly the arguments the person was shown.
        with self._gate.decision(action.tool_name, action.arguments, approved=approved):
            return self._registry.dispatch(action.tool_name, copy.deepcopy(action.arguments))
