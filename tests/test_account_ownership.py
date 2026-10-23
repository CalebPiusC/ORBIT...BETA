"""Account plumbing — Phase B, step 2.

What is pinned here:

  * the contract: ``account_id`` is required to touch stored records —
    ``Store.for_account`` / ``ProjectStore.for_account`` refuse to work
    without one, and every record method on the raw stores takes it with
    no default (checked by signature, so a future method cannot silently
    reintroduce an unscoped path);
  * store-level isolation: through another account's view, a record is
    indistinguishable from a missing one, for reads and mutations alike;
  * HTTP isolation with accounts on: every route taking an id answers 404
    (never 403) for another account's record, and lists/dashboards never
    show it — while the owner keeps the exact behaviour characterization
    pinned in step 1, including the approval gate's 403 on her *own*
    unapproved task;
  * tool-call ownership: only the proposing account may decide a queued
    action, and a foreign decision does not consume it;
  * the guard: every url_map rule taking an id is either marked
    ``@ownership_checked`` or listed in ``OWNERSHIP_EXEMPTIONS`` with a
    reason. Adding an id route without either fails this file.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from providers.base import StreamChunk, ToolProposal

from app.accounts import AccountStore
from app.actions import PendingActions
from app.projects import ProjectStore
from app.server import OWNERSHIP_EXEMPTIONS, create_app
from app.store import (
    DEFAULT_USER_ID,
    ActionCard,
    ChatMessage,
    Store,
)
from config import HeartbeatSettings
from heartbeat.service import Heartbeat
from memory.facts import FactStore
from memory.plans import PlanStore
from tools import build_tool_registry
from tools.confirmation import DeferredConfirmationGate
from tools.registry import AuditLog

GRACE_EMAIL = "grace@example.dev"
ADA_EMAIL = "ada@example.dev"


class FakeProvider:
    def reply(self, history, system_prompt: str) -> str:
        return "A canned one-shot reply."

    def reply_stream(self, history, system_prompt: str, **kwargs) -> Iterator[StreamChunk]:
        yield StreamChunk(kind="answer", text="Hello from Orbit.")


class ToolProvider:
    """Streams one tool proposal, like a model asking to save a plan."""

    def reply(self, history, system_prompt: str) -> str:
        return "A canned one-shot reply."

    def title(self, user_text: str, answer: str) -> str:
        return "Plan saving"

    def reply_stream(self, history, system_prompt: str, **kwargs):
        yield ToolProposal("save_plan", {"plan": "Ship the release"})


def msg(body: str, *, orbit: bool = False, action: ActionCard | None = None) -> ChatMessage:
    return ChatMessage(
        who="Orbit" if orbit else "You",
        time="10:00",
        body=body,
        is_orbit=orbit,
        action=action,
    )


def make_accounts_app(provider=None, **kwargs):
    accounts = AccountStore(iterations=1_000)
    store = Store()
    projects = ProjectStore()
    app = create_app(
        provider=provider or FakeProvider(),
        store=store,
        projects=projects,
        accounts=accounts,
        **kwargs,
    )
    return app, store, projects, accounts


def oauth_client(app, email: str, name: str):
    client = app.test_client()
    resp = client.post("/auth/oauth/github", data={"name": name, "email": email})
    assert resp.status_code == 303, resp.get_data(as_text=True)
    return client


# ---------------------------------------------------------------------------
# The contract: no account_id, no records.
# ---------------------------------------------------------------------------


class ForAccountContractTests(unittest.TestCase):
    def test_for_account_requires_a_real_account_id(self) -> None:
        for factory in (Store(), ProjectStore()):
            with self.assertRaises(TypeError):
                factory.for_account()  # type: ignore[call-arg]
            for bad in ("", "   ", None):
                with self.assertRaises(ValueError, msg=f"{factory!r} {bad!r}"):
                    factory.for_account(bad)  # type: ignore[arg-type]

    def test_view_is_bound_to_its_account_and_store(self) -> None:
        raw = Store()
        view = raw.for_account("u-123")
        self.assertEqual(view.account_id, "u-123")
        self.assertIs(view.store, raw)
        projects = ProjectStore().for_account("u-123")
        self.assertEqual(projects.account_id, "u-123")

    def test_every_record_method_on_store_requires_account_id(self) -> None:
        # The only public Store methods exempt from the account argument are
        # the two that touch no records: connected_count (workspace-global
        # connector rows) and greeting (the seeded user's display name).
        exempt = {"connected_count", "greeting"}
        checked = 0
        for name, func in inspect.getmembers(Store, predicate=inspect.isfunction):
            if name.startswith("_"):
                continue
            params = inspect.signature(func).parameters
            if name in exempt:
                self.assertNotIn("account_id", params, name)
                continue
            self.assertIn("account_id", params, f"Store.{name} has no account_id")
            self.assertIs(
                params["account_id"].default,
                inspect.Parameter.empty,
                f"Store.{name} account_id must have no default",
            )
            checked += 1
        self.assertGreaterEqual(checked, 20)  # the sweep really covered the API

    def test_project_store_methods_require_the_owner_id(self) -> None:
        # ProjectStore's spelling of the account argument is user_id — the
        # persisted field name — and it has been required since the store
        # landed; for_account is the new bound face of the same contract.
        for name in ("list_for", "counts_for", "get_for", "create", "set_status", "set_summary"):
            params = inspect.signature(getattr(ProjectStore, name)).parameters
            self.assertIn("user_id", params, name)
            self.assertIs(params["user_id"].default, inspect.Parameter.empty, name)

    def test_calling_the_raw_store_without_an_account_raises(self) -> None:
        raw = Store()
        with self.assertRaises(TypeError):
            raw.get_task("T1")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            raw.create_task("Do a thing")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            raw.conversations_for()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Store-level isolation between two accounts sharing one raw store.
# ---------------------------------------------------------------------------


class StoreIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = Store()
        self.a = self.raw.for_account("acct-a")
        self.b = self.raw.for_account("acct-b")
        # A's world: a home conversation with a reply and a pending action
        # card, a project conversation, and two tasks (one in a project).
        self.conv = self.a.start_conversation()
        self.a.append_message(self.conv.id, msg("A question"))
        self.reply = msg("A answer", orbit=True)
        self.a.append_message(self.conv.id, self.reply)
        self.card = ActionCard(id="act-a1", summary="Save a plan")
        self.a.append_message(self.conv.id, msg("card", orbit=True, action=self.card))
        self.a.project_conversation("p-a", create=True)
        self.task = self.a.create_task("A standalone task")
        self.a.add_chat(self.task.id, msg("A task note"))
        self.task_reply = msg("A task reply", orbit=True)
        self.a.add_chat(self.task.id, self.task_reply)
        self.project_task = self.a.create_task("A project task", project_id="p-a")

    def test_lists_show_only_the_owners_records(self) -> None:
        self.assertEqual([c.id for c in self.a.conversations_for()], [self.conv.id])
        self.assertEqual(self.b.conversations_for(), [])
        self.assertEqual(self.b.conversations_grouped(), {})
        self.assertEqual(self.b.tasks_for(), [])
        self.assertEqual(self.b.tasks_for_project("p-a"), [])
        self.assertEqual(self.b.project_conversations("p-a"), [])
        self.assertEqual(len(self.a.tasks_for()), 2)

    def test_getters_treat_foreign_records_as_missing(self) -> None:
        self.assertIsNone(self.b.get_conversation(self.conv.id))
        self.assertIsNone(self.b.get_task(self.task.id))
        self.assertIsNone(self.b.project_conversation("p-a"))
        self.assertEqual(self.b.thread(self.conv.id), [])
        self.assertEqual(self.b.project_thread("p-a"), [])
        self.assertEqual(self.b.home_thread, [])

    def test_foreign_appends_and_titles_change_nothing(self) -> None:
        stray = msg("B stray")
        self.b.append_message(self.conv.id, stray)
        self.assertEqual(stray.id, "")  # never filed, so never given an id
        self.assertEqual(len(self.a.thread(self.conv.id)), 3)
        self.b.set_title(self.conv.id, "Hijacked")
        conv = self.a.get_conversation(self.conv.id)
        assert conv is not None
        self.assertNotEqual(conv.title, "Hijacked")
        self.b.add_chat(self.task.id, msg("B task stray"))
        task = self.a.get_task(self.task.id)
        assert task is not None
        self.assertEqual(len(task.chat), 2)

    def test_messages_are_invisible_and_unwritable_across_accounts(self) -> None:
        self.assertIsNone(self.b.find_message(self.reply.id))
        self.assertIsNone(self.b.find_message(self.task_reply.id))
        self.assertFalse(self.b.replace_message(self.conv.id, self.reply.id, "rewritten"))
        self.assertFalse(self.b.replace_message(self.task.id, self.task_reply.id, "rewritten"))
        self.assertEqual(self.reply.body, "A answer")
        self.assertEqual(self.task_reply.body, "A task reply")
        self.assertFalse(self.b.resolve_action("act-a1", "approved", "ran"))
        self.assertEqual(self.card.state, "pending")
        # …while the owner can do all three.
        self.assertIsNotNone(self.a.find_message(self.reply.id))
        self.assertTrue(self.a.replace_message(self.conv.id, self.reply.id, "A better answer"))
        self.assertTrue(self.a.resolve_action("act-a1", "declined", "Declined."))

    def test_task_mutations_are_refused_across_accounts(self) -> None:
        self.assertIsNone(self.b.set_done(self.task.id, True))
        self.assertFalse(self.b.approve_task(self.task.id))
        # Foreign and unknown are the same error — no existence leak.
        with self.assertRaises(KeyError):
            self.b.apply_changes(self.task.id)
        with self.assertRaises(KeyError):
            self.b.apply_changes("t-unknown")
        task = self.a.get_task(self.task.id)
        assert task is not None
        self.assertFalse(task.done)
        self.assertFalse(task.approved)
        self.assertFalse(task.applied)
        # The owner's approval gate is unchanged: unapproved still refuses.
        with self.assertRaises(RuntimeError):
            self.a.apply_changes(self.task.id)

    def test_builds_on_cannot_point_at_another_accounts_task(self) -> None:
        with self.assertRaises(ValueError):
            self.b.create_task("Follow-up", builds_on=self.task.id)
        own = self.a.create_task("Follow-up", builds_on=self.task.id)
        self.assertEqual(own.builds_on, self.task.id)

    def test_project_conversations_are_created_per_account(self) -> None:
        b_conv = self.b.project_conversation("p-a", create=True)
        a_conv = self.a.project_conversation("p-a")
        assert b_conv is not None and a_conv is not None
        self.assertNotEqual(b_conv.id, a_conv.id)

    def test_screen_state_is_per_account(self) -> None:
        b_conv = self.b.start_conversation()
        self.a.set_mode("agent")
        self.a.set_thinking_level("deep")
        self.a.selected_agent = "review"
        self.assertEqual(self.b.mode, "chat")
        self.assertEqual(self.b.thinking_level, "quick")
        self.assertIsNone(self.b.selected_agent)
        self.assertEqual(self.b.active_conversation_id, b_conv.id)
        self.assertEqual(self.a.active_conversation_id, self.conv.id)
        self.a.new_conversation()
        self.assertIsNone(self.a.active_conversation_id)
        self.assertEqual(self.b.active_conversation_id, b_conv.id)

    def test_the_seeded_example_task_belongs_to_the_legacy_account_only(self) -> None:
        legacy = self.raw.for_account(DEFAULT_USER_ID)
        self.assertIsNotNone(legacy.get_task("T1"))
        self.assertIsNone(self.a.get_task("T1"))
        self.assertEqual(self.b.tasks_for(), [])


# ---------------------------------------------------------------------------
# HTTP isolation with accounts on.
# ---------------------------------------------------------------------------


class HttpIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.store, self.projects, self.accounts = make_accounts_app()
        self.grace = oauth_client(self.app, GRACE_EMAIL, "Grace Hopper")
        self.ada = oauth_client(self.app, ADA_EMAIL, "Ada Osei")
        self.grace_id = self.accounts.by_email(GRACE_EMAIL)["id"]
        self.ada_id = self.accounts.by_email(ADA_EMAIL)["id"]
        self.grace_view = self.store.for_account(self.grace_id)

        # Grace's world, built through the real routes.
        created = self.grace.post(
            "/projects",
            data={"name": "Grace project", "description": "", "mode": "blank"},
        )
        self.project_id = created.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
        task_resp = self.grace.post(
            f"/projects/{self.project_id}/tasks",
            data={"request": "Grace project task"},
        )
        location = task_resp.headers["Location"]
        self.project_task_id = location.split("task=", 1)[1].split("#")[0].split("&")[0]
        standalone = self.grace.post("/api/tasks", json={"request": "Grace standalone"})
        self.task_id = standalone.get_json()["id"]
        self.grace.post(
            "/api/chat", json={"message": "Grace secret hello"}
        ).get_data(as_text=True)
        self.grace_message_id = self.grace_view.home_thread[-1].id

    # -- the other account gets 404 everywhere, and nothing changes ---------

    def test_workspace_is_404_for_another_account(self) -> None:
        resp = self.ada.get(f"/projects/{self.project_id}")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("Project not found", resp.get_data(as_text=True))

    def test_project_mutations_are_404_for_another_account(self) -> None:
        resp = self.ada.post(
            f"/projects/{self.project_id}/status", data={"status": "archived"}
        )
        self.assertEqual(
            (resp.status_code, resp.get_json()), (404, {"error": "unknown project"})
        )
        resp = self.ada.post(
            f"/projects/{self.project_id}/tasks", data={"request": "Ada was here"}
        )
        self.assertEqual(resp.status_code, 404)
        resp = self.ada.post(
            f"/projects/{self.project_id}/tasks/{self.project_task_id}/complete",
            data={},
        )
        self.assertEqual(resp.status_code, 404)

        project = self.projects.for_account(self.grace_id).get(self.project_id)
        assert project is not None
        self.assertEqual(project.status, "building")
        tasks = self.grace_view.tasks_for_project(self.project_id)
        self.assertEqual([t.id for t in tasks], [self.project_task_id])
        self.assertFalse(tasks[0].done)

    def test_project_stream_is_404_for_another_account(self) -> None:
        resp = self.ada.post(
            f"/api/project/{self.project_id}/stream", json={"message": "hi"}
        )
        self.assertEqual(
            (resp.status_code, resp.get_json()), (404, {"error": "unknown project"})
        )
        self.assertEqual(self.grace_view.project_conversations(self.project_id), [])

    def test_pinning_another_accounts_task_is_404_with_the_project_body(self) -> None:
        ada_project = self.projects.for_account(self.ada_id).create(name="Ada project")
        resp = self.ada.post(
            f"/api/project/{ada_project.id}/stream",
            json={"message": "hi", "task_id": self.project_task_id},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "task does not belong to this project"})

    def test_task_routes_are_404_for_another_account(self) -> None:
        for task_id in (self.task_id, self.project_task_id):
            self.assertEqual(self.ada.get(f"/task/{task_id}").status_code, 404, task_id)
            resp = self.ada.post(f"/api/task/{task_id}/stream", json={"message": "hi"})
            self.assertEqual(
                (resp.status_code, resp.get_json()),
                (404, {"error": "unknown task"}),
                task_id,
            )

    def test_approve_and_apply_are_404_not_403_for_another_account(self) -> None:
        resp = self.ada.post(
            f"/api/task/{self.task_id}/approve", json={"approve": True}
        )
        self.assertEqual(resp.status_code, 404)
        self.assertNotEqual(resp.status_code, 403)
        resp = self.ada.post(f"/api/task/{self.task_id}/apply", json={})
        self.assertEqual(resp.status_code, 404)
        self.assertNotEqual(resp.status_code, 403)
        task = self.grace_view.get_task(self.task_id)
        assert task is not None
        self.assertFalse(task.approved)
        self.assertFalse(task.applied)

    def test_rating_another_accounts_message_is_404(self) -> None:
        resp = self.ada.post(
            f"/api/messages/{self.grace_message_id}/rating", json={"rating": "down"}
        )
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (404, {"error": "no message with that id"}),
        )

    def test_regenerating_another_accounts_reply_is_refused(self) -> None:
        before = self.grace_view.home_thread[-1].body
        resp = self.ada.post(
            "/api/chat",
            json={"regenerate": True, "message_id": self.grace_message_id},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.grace_view.home_thread[-1].body, before)

    def test_assigning_an_agent_to_another_accounts_project_is_404(self) -> None:
        resp = self.ada.post(
            "/agents/review/assign", data={"project_id": self.project_id}
        )
        self.assertEqual(
            (resp.status_code, resp.get_json()), (404, {"error": "unknown project"})
        )
        ada_view = self.store.for_account(self.ada_id)
        self.assertEqual(ada_view.project_conversations(self.project_id), [])

    def test_builds_on_another_accounts_task_files_nothing(self) -> None:
        ada_project = self.projects.for_account(self.ada_id).create(name="Ada project")
        resp = self.ada.post(
            f"/projects/{ada_project.id}/tasks",
            data={"request": "Follow-up", "builds_on": self.task_id},
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("#add-task", resp.headers["Location"])
        ada_view = self.store.for_account(self.ada_id)
        self.assertEqual(ada_view.tasks_for_project(ada_project.id), [])

    def test_workspace_task_query_cannot_open_another_accounts_task(self) -> None:
        ada_project = self.projects.for_account(self.ada_id).create(name="Ada project")
        body = self.ada.get(
            f"/projects/{ada_project.id}?task={self.project_task_id}"
        ).get_data(as_text=True)
        self.assertNotIn("Grace project task", body)

    def test_lists_and_home_never_show_another_accounts_work(self) -> None:
        self.assertNotIn(
            "Grace project", self.ada.get("/projects").get_data(as_text=True)
        )
        self.assertNotIn(
            "Grace project", self.ada.get("/dashboard").get_data(as_text=True)
        )
        self.assertNotIn(
            "Grace secret hello", self.ada.get("/").get_data(as_text=True)
        )

    # -- and the owner's behaviour is exactly what step 1 pinned -------------

    def test_the_owner_keeps_full_access(self) -> None:
        self.assertEqual(self.grace.get(f"/projects/{self.project_id}").status_code, 200)
        self.assertEqual(self.grace.get(f"/task/{self.task_id}").status_code, 200)
        resp = self.grace.post(
            f"/api/messages/{self.grace_message_id}/rating", json={"rating": "up"}
        )
        self.assertEqual(resp.status_code, 200)

    def test_the_approval_gate_still_answers_403_to_the_owner(self) -> None:
        # 404 is the ownership answer; 403 remains the *gate's* answer on a
        # task the caller owns but has not approved.
        resp = self.grace.post(f"/api/task/{self.task_id}/apply", json={})
        self.assertEqual(resp.status_code, 403)
        resp = self.grace.post(
            f"/api/task/{self.task_id}/approve", json={"approve": True}
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.grace.post(f"/api/task/{self.task_id}/apply", json={})
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (200, {"applied": True, "approved": True}),
        )

    def test_the_owner_can_complete_her_project_task(self) -> None:
        resp = self.grace.post(
            f"/projects/{self.project_id}/tasks/{self.project_task_id}/complete",
            data={},
        )
        self.assertEqual(resp.status_code, 303)
        task = self.grace_view.get_task(self.project_task_id)
        assert task is not None
        self.assertTrue(task.done)


# ---------------------------------------------------------------------------
# Agent mode lands on a task the account owns (the /task/T1 regression).
# ---------------------------------------------------------------------------


class AgentModeTaskTests(unittest.TestCase):
    def test_fresh_signed_in_account_agent_mode_reaches_its_own_task(self) -> None:
        app, store, projects, accounts = make_accounts_app()
        grace = oauth_client(app, GRACE_EMAIL, "Grace Hopper")
        grace_id = accounts.by_email(GRACE_EMAIL)["id"]
        self.assertEqual(store.for_account(grace_id).tasks_for(), [])

        # This is exactly what the mode toggle does before navigating.
        resp = grace.post("/api/mode", json={"mode": "agent"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["mode"], "agent")
        task_url = body["task_url"]
        self.assertNotEqual(task_url, "/task/T1")

        page = grace.get(task_url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Add order tracking", page.get_data(as_text=True))

        view = store.for_account(grace_id)
        task = view.get_task(body["task_id"])
        assert task is not None
        self.assertEqual(task.user_id, grace_id)
        self.assertEqual(task_url, f"/task/{task.id}")

        # On demand means once: toggling again hands back the same task.
        again = grace.post("/api/mode", json={"mode": "agent"}).get_json()
        self.assertEqual(again["task_id"], body["task_id"])
        self.assertEqual(len(view.tasks_for()), 1)

        # The frontend carries no hardcoded task id any more.
        app_js = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("/task/T1", app_js)
        self.assertIn("task_url", app_js)

    def test_second_account_still_gets_404_on_the_first_accounts_agent_task(self) -> None:
        app, store, projects, accounts = make_accounts_app()
        grace = oauth_client(app, GRACE_EMAIL, "Grace Hopper")
        body = grace.post("/api/mode", json={"mode": "agent"}).get_json()
        grace_task_url = body["task_url"]
        self.assertEqual(grace.get(grace_task_url).status_code, 200)

        ada = oauth_client(app, ADA_EMAIL, "Ada Osei")
        self.assertEqual(ada.get(grace_task_url).status_code, 404)
        # Ada's own Agent mode lands on a different task that is hers.
        ada_body = ada.post("/api/mode", json={"mode": "agent"}).get_json()
        self.assertNotEqual(ada_body["task_id"], body["task_id"])
        self.assertEqual(ada.get(ada_body["task_url"]).status_code, 200)
        self.assertEqual(grace.get(ada_body["task_url"]).status_code, 404)


# ---------------------------------------------------------------------------
# Tool-call ownership: only the proposing account may decide.
# ---------------------------------------------------------------------------


class ActionOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plans = root / "plans.json"
        audit = root / "audit.log"
        gate = DeferredConfirmationGate()
        heartbeat = Heartbeat(
            settings=HeartbeatSettings(
                plan_hour=10,
                checkin_gap_days=2,
                interval_seconds=60.0,
                ntfy_topic="",
                ntfy_server="https://ntfy.sh",
            ),
            state_path=root / "heartbeat.json",
            plans=PlanStore(self.plans),
            audit=AuditLog(audit),
        )
        registry = build_tool_registry(
            confirmation_gate=gate,
            audit_log_path=audit,
            notes_path=root / "notes.txt",
            facts_path=root / "facts.txt",
            plans_path=self.plans,
            heartbeat_path=root / "heartbeat.json",
            heartbeat=heartbeat,
        )
        self.queue = PendingActions(registry, gate, audit=AuditLog(audit))
        self.app, self.store, self.projects, self.accounts = make_accounts_app(
            provider=ToolProvider(),
            facts=FactStore(root / "facts.txt"),
            actions=self.queue,
        )
        self.grace = oauth_client(self.app, GRACE_EMAIL, "Grace Hopper")
        self.ada = oauth_client(self.app, ADA_EMAIL, "Ada Osei")
        self.grace_id = self.accounts.by_email(GRACE_EMAIL)["id"]

    def propose_as_grace(self) -> str:
        resp = self.grace.post("/api/chat", json={"message": "Save my plan"})
        raw = resp.get_data(as_text=True)
        action_line = next(
            line for line in raw.splitlines() if '"state": "pending"' in line
        )
        return json.loads(action_line.removeprefix("data:"))["id"]

    def test_only_the_proposing_account_can_decide(self) -> None:
        action_id = self.propose_as_grace()
        self.assertEqual(self.queue.owner_of(action_id), self.grace_id)

        for decision in ("approve", "decline"):
            resp = self.ada.post(f"/api/actions/{action_id}/{decision}", json={})
            self.assertEqual(resp.status_code, 404, decision)
            self.assertEqual(
                resp.get_json(),
                {"error": "This request is no longer pending. Nothing ran."},
            )
        # Nothing ran, and the proposal was not consumed by the refusals.
        self.assertFalse(self.plans.exists())
        self.assertEqual(self.queue.owner_of(action_id), self.grace_id)

        resp = self.grace.post(f"/api/actions/{action_id}/approve", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["state"], "approved")
        self.assertTrue(self.plans.exists())
        self.assertIsNone(self.queue.owner_of(action_id))

    def test_owner_of_an_unknown_action_is_none(self) -> None:
        self.assertIsNone(self.queue.owner_of("act-nope"))


# ---------------------------------------------------------------------------
# The guard: no id-taking route without an ownership decision.
# ---------------------------------------------------------------------------


# Every endpoint that must carry @ownership_checked. Adding an id route
# means adding it here *and* adding cross-account coverage above;
# removing a marker fails the equality test below even before behaviour
# is considered.
EXPECTED_OWNERSHIP_CHECKED = {
    "assign_agent",          # project_id in the form body
    "agent_conversation",    # project_id (+ agent_id from the catalog)
    "project_workspace",     # project_id (+ ?task=)
    "project_set_status",    # project_id
    "project_new_task",      # project_id (+ builds_on in the form)
    "project_task_complete", # project_id + task_id
    "api_project_stream",    # project_id (+ task_id in the body)
    "task",                  # task_id
    "api_task_stream",       # task_id
    "api_approve",           # task_id
    "api_reject",            # task_id
    "api_apply",             # task_id
    "approvals",             # ?task= (query id)
    "api_message_rating",    # message_id
    "api_action_decide",     # action_id
    "api_chat",              # message_id in the body (regenerate)
}


class UrlMapGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(provider=FakeProvider())

    def test_every_id_route_has_an_ownership_decision(self) -> None:
        checked_rules = 0
        for rule in self.app.url_map.iter_rules():
            if not rule.arguments:
                continue
            checked_rules += 1
            if rule.endpoint in OWNERSHIP_EXEMPTIONS:
                self.assertTrue(
                    OWNERSHIP_EXEMPTIONS[rule.endpoint].strip(),
                    f"exemption for {rule.endpoint} needs a reason",
                )
                continue
            func = self.app.view_functions[rule.endpoint]
            params = getattr(func, "__ownership_params__", None)
            self.assertTrue(
                params,
                f"{rule.rule} ({rule.endpoint}) takes ids "
                f"{sorted(rule.arguments)} but has no ownership check: "
                "add @ownership_checked(...) with 404-for-foreign "
                "behaviour, or an OWNERSHIP_EXEMPTIONS entry with a reason.",
            )
        self.assertGreaterEqual(checked_rules, 10)  # the walk is not vacuous

    def test_the_marked_set_is_exactly_the_expected_set(self) -> None:
        marked = {
            endpoint
            for endpoint, func in self.app.view_functions.items()
            if getattr(func, "__ownership_params__", None)
        }
        self.assertEqual(marked, EXPECTED_OWNERSHIP_CHECKED)

    def test_exemptions_are_not_stale(self) -> None:
        id_endpoints = {
            rule.endpoint
            for rule in self.app.url_map.iter_rules()
            if rule.arguments
        }
        for endpoint in OWNERSHIP_EXEMPTIONS:
            self.assertIn(
                endpoint,
                id_endpoints,
                f"OWNERSHIP_EXEMPTIONS[{endpoint!r}] names no id-taking route",
            )
            func = self.app.view_functions[endpoint]
            self.assertFalse(
                getattr(func, "__ownership_params__", None),
                f"{endpoint} is both exempt and marked — pick one story",
            )


if __name__ == "__main__":
    unittest.main()
