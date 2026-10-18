"""Characterization tests — Phase B, step 1.

These tests pin the behaviour that exists *today*, before any account
plumbing lands. They are the safety net for step 2 (``Store.for_account``):
everything here must keep passing with its assertions unchanged, except the
small ``SharedBucketTodayTests`` class at the bottom, which pins the one
behaviour step 2 exists to change — with accounts on, every signed-in
account currently reads and writes the single seeded user's data.

Focus is the surface step 2 can disturb: the in-memory store's scoping and
approval gate, and the exact status codes / bodies of every route that
takes an id. Where an older test file already pins a behaviour well, this
file does not repeat it.
"""

from __future__ import annotations

import json
import unittest
from collections.abc import Iterator

from providers.base import StreamChunk

from app.accounts import AccountStore
from app.projects import ProjectStore, SourceUnavailable
from app.server import create_app
from app.store import PLACEHOLDER_TITLE, ChatMessage, Store

PASSWORD = "password1"


class FakeProvider:
    """Deterministic streaming provider: one canned answer per turn."""

    def reply(self, history, system_prompt: str) -> str:
        return "A canned one-shot reply."

    def reply_stream(self, history, system_prompt: str, **kwargs) -> Iterator[StreamChunk]:
        if kwargs.get("think"):
            yield StreamChunk(kind="thinking", text="I am checking the repository context.")
        yield StreamChunk(kind="answer", text="Hello from Orbit.")


def parse_sse(raw: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:])
        events.append((event, "\n".join(data_lines)))
    return events


def msg(body: str, *, orbit: bool = False) -> ChatMessage:
    return ChatMessage(who="Orbit" if orbit else "You", time="10:00", body=body, is_orbit=orbit)


# ---------------------------------------------------------------------------
# The store as it stands: seeds, scoping seams, and the approval gate.
# ---------------------------------------------------------------------------


class StoreSeedTests(unittest.TestCase):
    def test_fresh_store_has_only_the_example_task_and_no_history(self) -> None:
        store = Store()
        self.assertEqual(list(store.tasks), ["T1"])
        self.assertEqual(store.conversations, [])
        self.assertEqual(store.threads, {})
        self.assertIsNone(store.active_conversation_id)
        self.assertEqual(store.home_thread, [])
        task = store.get_task("T1")
        assert task is not None
        self.assertEqual(task.user_id, store.user.id)
        self.assertFalse(task.approved)
        self.assertFalse(task.applied)
        self.assertIsNone(task.run)

    def test_connections_are_all_not_connected(self) -> None:
        store = Store()
        self.assertEqual(store.connected_count(), 0)
        self.assertEqual(
            [c.name for c in store.connections], ["GitHub", "Linear", "Vercel"]
        )
        self.assertTrue(all(c.status == "not_connected" for c in store.connections))

    def test_defaults_are_chat_mode_and_quick_depth(self) -> None:
        store = Store()
        self.assertEqual(store.mode, "chat")
        self.assertEqual(store.thinking_level, "quick")
        self.assertIsNone(store.selected_agent)


class StoreConversationTests(unittest.TestCase):
    def test_start_conversation_makes_it_active_with_placeholder_title(self) -> None:
        store = Store()
        conv = store.start_conversation()
        self.assertEqual(conv.title, PLACEHOLDER_TITLE)
        self.assertEqual(conv.group, "Today")
        self.assertEqual(conv.user_id, store.user.id)
        self.assertEqual(store.active_conversation_id, conv.id)
        self.assertEqual(store.conversations_grouped()["Today"][0].id, conv.id)

    def test_append_message_assigns_a_stable_id_once(self) -> None:
        store = Store()
        conv = store.start_conversation()
        message = msg("hello")
        store.append_message(conv.id, message)
        self.assertTrue(message.id.startswith("m-"))
        first_id = message.id
        store.append_message(conv.id, message)
        self.assertEqual(message.id, first_id)

    def test_new_conversation_clears_active_but_keeps_the_thread(self) -> None:
        store = Store()
        conv = store.start_conversation()
        store.append_message(conv.id, msg("kept"))
        store.new_conversation()
        self.assertIsNone(store.active_conversation_id)
        self.assertEqual(store.home_thread, [])
        self.assertIsNotNone(store.get_conversation(conv.id))
        self.assertEqual([m.body for m in store.threads[conv.id]], ["kept"])

    def test_find_and_replace_message_across_conversations_and_task_chat(self) -> None:
        store = Store()
        conv = store.start_conversation()
        home_msg = msg("home reply", orbit=True)
        store.append_message(conv.id, home_msg)
        task_msg = msg("task reply", orbit=True)
        store.add_chat("T1", task_msg)

        self.assertEqual(store.find_message(home_msg.id), (conv.id, home_msg))
        self.assertEqual(store.find_message(task_msg.id), ("T1", task_msg))
        self.assertIsNone(store.find_message("m-nope"))
        self.assertIsNone(store.find_message(""))

        self.assertTrue(store.replace_message(conv.id, home_msg.id, "rewritten"))
        self.assertEqual(home_msg.body, "rewritten")
        self.assertTrue(store.replace_message("T1", task_msg.id, "also rewritten"))
        self.assertFalse(store.replace_message(conv.id, "m-nope", "nope"))

    def test_project_conversation_is_find_or_create_and_hidden_from_home(self) -> None:
        store = Store()
        self.assertIsNone(store.project_conversation("p-1"))  # a read creates nothing
        conv = store.project_conversation("p-1", create=True)
        assert conv is not None
        self.assertIs(store.project_conversation("p-1", create=True), conv)
        self.assertNotIn(conv, store.conversations_for())
        self.assertEqual(store.conversations_grouped(), {})

    def test_scoping_seams_already_filter_by_user_id_when_asked(self) -> None:
        store = Store()
        store.start_conversation()
        store.project_conversation("p-1", create=True)
        self.assertEqual(store.conversations_for("someone-else"), [])
        self.assertEqual(store.tasks_for("someone-else"), [])
        self.assertIsNone(store.project_conversation("p-1", user_id="someone-else"))
        self.assertEqual(store.project_thread("p-1", user_id="someone-else"), [])

    def test_set_title_on_unknown_conversation_is_a_no_op(self) -> None:
        store = Store()
        store.set_title("h-nope", "Nothing")  # must not raise
        self.assertIsNone(store.get_conversation("h-nope"))

    def test_mode_and_thinking_level_reject_unknown_values(self) -> None:
        store = Store()
        with self.assertRaises(ValueError):
            store.set_mode("sideways")
        with self.assertRaises(ValueError):
            store.set_thinking_level("ultra")


class StoreTaskTests(unittest.TestCase):
    def test_create_task_titles_from_the_first_sentence_and_seeds_activity(self) -> None:
        store = Store()
        task = store.create_task("Fix the login crash. Then deploy it.")
        self.assertEqual(task.title, "Fix the login crash.")
        self.assertEqual(task.user_id, store.user.id)
        self.assertIsNone(task.run)
        self.assertFalse(task.approved)
        self.assertEqual([a.text for a in task.activity], ["Task created from your request."])

    def test_create_task_rejects_empty_request_and_dangling_builds_on(self) -> None:
        store = Store()
        with self.assertRaises(ValueError):
            store.create_task("   ")
        with self.assertRaises(ValueError) as ctx:
            store.create_task("Follow-up", builds_on="t-nope")
        self.assertIn("builds_on points at an unknown task: t-nope", str(ctx.exception))

    def test_approval_gate_is_enforced_by_the_store(self) -> None:
        store = Store()
        with self.assertRaises(KeyError):
            store.apply_changes("t-nope")
        with self.assertRaises(RuntimeError) as ctx:
            store.apply_changes("T1")
        self.assertEqual(
            str(ctx.exception),
            "No commits will be pushed without your explicit approval.",
        )
        self.assertFalse(store.approve_task("t-nope"))
        self.assertTrue(store.approve_task("T1"))
        applied = store.apply_changes("T1")
        self.assertTrue(applied.applied)

    def test_set_done_records_the_persons_assertion_and_unknown_is_none(self) -> None:
        store = Store()
        task = store.create_task("Ship it")
        done = store.set_done(task.id, True)
        assert done is not None
        self.assertTrue(done.done)
        self.assertEqual(done.activity[0].text, "You marked this task complete.")
        reopened = store.set_done(task.id, False)
        assert reopened is not None
        self.assertFalse(reopened.done)
        self.assertEqual(reopened.activity[0].text, "You reopened this task.")
        self.assertIsNone(store.set_done("t-nope", True))

    def test_add_chat_and_get_task_on_unknown_ids_are_safe(self) -> None:
        store = Store()
        self.assertIsNone(store.get_task("t-nope"))
        store.add_chat("t-nope", msg("lost"))  # must not raise
        self.assertIsNone(store.get_task("t-nope"))


class ProjectStoreScopingTests(unittest.TestCase):
    """ProjectStore already requires a user_id on every call; pin what that buys."""

    def test_get_list_and_counts_are_scoped_to_the_owner(self) -> None:
        projects = ProjectStore()
        mine = projects.create(user_id="caleb", name="Order bot")
        self.assertIsNotNone(projects.get_for(mine.id, "caleb"))
        self.assertIsNone(projects.get_for(mine.id, "someone-else"))
        self.assertEqual(projects.list_for("someone-else"), [])
        self.assertEqual(sum(projects.counts_for("someone-else").values()), 0)

    def test_mutations_by_another_user_change_nothing(self) -> None:
        projects = ProjectStore()
        mine = projects.create(user_id="caleb", name="Order bot")
        self.assertIsNone(projects.set_status(mine.id, "someone-else", "live"))
        self.assertIsNone(projects.set_summary(mine.id, "someone-else", "A summary."))
        unchanged = projects.get_for(mine.id, "caleb")
        assert unchanged is not None
        self.assertEqual(unchanged.status, "building")
        self.assertEqual(unchanged.summary, "")

    def test_only_the_new_source_creates_a_project(self) -> None:
        projects = ProjectStore()
        with self.assertRaises(SourceUnavailable):
            projects.create(user_id="caleb", name="Imported", source="github")
        with self.assertRaises(ValueError):
            projects.create(user_id="caleb", name="Nope", source="carrier-pigeon")


# ---------------------------------------------------------------------------
# HTTP surface, accounts off (the launch mode the whole suite uses).
# Exact statuses and bodies for every route that takes an id.
# ---------------------------------------------------------------------------


class IdRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.projects = ProjectStore()
        self.app = create_app(
            provider=FakeProvider(), store=self.store, projects=self.projects
        )
        self.client = self.app.test_client()

    def make_project(self, name: str = "Alpha"):
        return self.projects.create(user_id=self.store.user.id, name=name)

    # -- task routes ------------------------------------------------------

    def test_unknown_task_page_is_a_template_404_naming_the_id(self) -> None:
        resp = self.client.get("/task/t-nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("t-nope", resp.get_data(as_text=True))

    def test_seeded_example_task_page_renders(self) -> None:
        resp = self.client.get("/task/T1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Add order tracking", resp.get_data(as_text=True))

    def test_task_api_unknown_ids_are_json_404s(self) -> None:
        for path, payload in (
            ("/api/task/t-nope/stream", {"message": "hi"}),
            ("/api/task/t-nope/approve", {"approve": True}),
            ("/api/task/t-nope/apply", {}),
        ):
            resp = self.client.post(path, json=payload)
            self.assertEqual(resp.status_code, 404, path)
            self.assertEqual(resp.get_json(), {"error": "unknown task"}, path)

    def test_approve_requires_a_literal_true(self) -> None:
        resp = self.client.post("/api/task/T1/approve", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.get_json(), {"error": "explicit approval required", "approved": False}
        )
        task = self.store.get_task("T1")
        assert task is not None
        self.assertFalse(task.approved)

    def test_apply_without_approval_is_403_and_nothing_is_applied(self) -> None:
        resp = self.client.post("/api/task/T1/apply", json={})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(
            resp.get_json(),
            {
                "error": "No commits will be pushed without your explicit approval.",
                "approved": False,
            },
        )
        task = self.store.get_task("T1")
        assert task is not None
        self.assertFalse(task.applied)

    def test_approve_then_apply_round_trip(self) -> None:
        resp = self.client.post("/api/task/T1/approve", json={"approve": True})
        self.assertEqual((resp.status_code, resp.get_json()), (200, {"approved": True}))
        resp = self.client.post("/api/task/T1/apply", json={})
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (200, {"applied": True, "approved": True}),
        )

    def test_new_task_api_validates_and_returns_id_and_title(self) -> None:
        resp = self.client.post("/api/tasks", json={"request": "  "})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"error": "a task needs a request to work from."})
        resp = self.client.post("/api/tasks", json={"request": "Fix the login crash. Deploy."})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["title"], "Fix the login crash.")
        self.assertIsNotNone(self.store.get_task(body["id"]))

    def test_task_stream_stores_the_exchange_and_plans_the_run(self) -> None:
        resp = self.client.post("/api/task/T1/stream", json={"initial": True})
        self.assertEqual(resp.status_code, 200)
        events = parse_sse(resp.get_data(as_text=True))
        answers = [data.strip() for name, data in events if name == "answer"]
        self.assertEqual(answers, ["Hello from Orbit."])
        task = self.store.get_task("T1")
        assert task is not None
        self.assertIsNotNone(task.run)
        self.assertTrue(any(m.is_orbit and m.body == "Hello from Orbit." for m in task.chat))

    # -- project routes -----------------------------------------------------

    def test_unknown_project_page_is_a_template_404(self) -> None:
        resp = self.client.get("/projects/p-nope")
        self.assertEqual(resp.status_code, 404)
        # Unlike the task 404, this page does not echo the id back.
        self.assertIn("Project not found", resp.get_data(as_text=True))

    def test_project_api_unknown_ids_are_json_404s(self) -> None:
        cases = [
            self.client.post("/projects/p-nope/status", data={"status": "live"}),
            self.client.post("/projects/p-nope/tasks", data={"request": "Do it"}),
            self.client.post("/projects/p-nope/tasks/T1/complete", data={}),
            self.client.post("/api/project/p-nope/stream", json={"message": "hi"}),
        ]
        for resp in cases:
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.get_json(), {"error": "unknown project"})

    def test_project_status_rejects_unknown_status_values(self) -> None:
        project = self.make_project()
        resp = self.client.post(f"/projects/{project.id}/status", data={"status": "wild"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"error": "unknown status: 'wild'"})

    def test_completing_a_task_from_another_project_is_404(self) -> None:
        alpha = self.make_project("Alpha")
        beta = self.make_project("Beta")
        task = self.store.create_task("Alpha work", project_id=alpha.id)
        resp = self.client.post(
            f"/projects/{beta.id}/tasks/{task.id}/complete", data={}
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "unknown task"})
        self.assertFalse(task.done)

    def test_completing_a_task_toggles_done_and_redirects_to_details(self) -> None:
        alpha = self.make_project("Alpha")
        task = self.store.create_task("Alpha work", project_id=alpha.id)
        resp = self.client.post(
            f"/projects/{alpha.id}/tasks/{task.id}/complete", data={}
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("#details", resp.headers["Location"])
        stored = self.store.get_task(task.id)
        assert stored is not None
        self.assertTrue(stored.done)

    def test_new_project_task_with_dangling_builds_on_files_nothing(self) -> None:
        alpha = self.make_project("Alpha")
        resp = self.client.post(
            f"/projects/{alpha.id}/tasks",
            data={"request": "Follow-up", "builds_on": "t-nope"},
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("#add-task", resp.headers["Location"])
        self.assertEqual(self.store.tasks_for_project(alpha.id), [])

    def test_project_stream_pins_only_tasks_of_that_project(self) -> None:
        alpha = self.make_project("Alpha")
        beta = self.make_project("Beta")
        task = self.store.create_task("Alpha work", project_id=alpha.id)
        resp = self.client.post(
            f"/api/project/{beta.id}/stream",
            json={"message": "hi", "task_id": task.id},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "task does not belong to this project"})

    def test_project_stream_validates_message_and_agent(self) -> None:
        alpha = self.make_project("Alpha")
        resp = self.client.post(f"/api/project/{alpha.id}/stream", json={"message": ""})
        self.assertEqual(
            (resp.status_code, resp.get_json()), (400, {"error": "message is required"})
        )
        resp = self.client.post(
            f"/api/project/{alpha.id}/stream", json={"message": "hi", "agent": "nope"}
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "unknown agent type: 'nope'"})

    def test_project_stream_creates_the_workspace_conversation_on_first_send(self) -> None:
        alpha = self.make_project("Alpha")
        self.assertEqual(self.store.project_conversations(alpha.id), [])
        resp = self.client.post(
            f"/api/project/{alpha.id}/stream", json={"message": "Plan please"}
        )
        self.assertEqual(resp.status_code, 200)
        resp.get_data(as_text=True)
        conversations = self.store.project_conversations(alpha.id)
        self.assertEqual(len(conversations), 1)
        thread = self.store.project_thread(alpha.id)
        self.assertEqual([m.body for m in thread], ["Plan please", "Hello from Orbit."])

    # -- agents, messages, actions, heartbeat ------------------------------

    def test_agent_routes_404_on_unknown_catalog_ids(self) -> None:
        resp = self.client.post("/agents/nope/select", data={})
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (404, {"error": "unknown agent type: 'nope'"}),
        )
        resp = self.client.post("/agents/nope/assign", data={"project_id": "p-x"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "unknown agent type: 'nope'"})
        resp = self.client.post("/agents/review/assign", data={"project_id": "p-nope"})
        self.assertEqual(
            (resp.status_code, resp.get_json()), (404, {"error": "unknown project"})
        )

    def test_rating_validates_value_then_message_existence(self) -> None:
        resp = self.client.post("/api/messages/m-x/rating", json={"rating": "meh"})
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (400, {"error": "rating must be 'up' or 'down'"}),
        )
        resp = self.client.post("/api/messages/m-nope/rating", json={"rating": "up"})
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (404, {"error": "no message with that id"}),
        )

    def test_action_decision_without_a_queue_is_503(self) -> None:
        resp = self.client.post("/api/actions/act-x/approve", json={})
        self.assertEqual(
            (resp.status_code, resp.get_json()), (503, {"error": "tools are not enabled"})
        )

    def test_heartbeat_routes_without_a_service_are_503(self) -> None:
        self.assertEqual(self.client.get("/api/heartbeat").status_code, 503)
        resp = self.client.post("/api/heartbeat/notices/n-1/dismiss", json={})
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (503, {"error": "heartbeat is not running"}),
        )

    def test_mode_and_thinking_endpoints_validate(self) -> None:
        resp = self.client.post("/api/mode", json={"mode": "sideways"})
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (400, {"error": "mode must be 'chat' or 'agent'."}),
        )
        resp = self.client.post("/api/thinking-level", json={"thinking_level": "ultra"})
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (400, {"error": "thinking_level must be 'quick' or 'deep'."}),
        )


class ChatFlowTests(unittest.TestCase):
    """The home chat's stored state, which step 2 re-homes per account."""

    def setUp(self) -> None:
        self.store = Store()
        self.app = create_app(provider=FakeProvider(), store=self.store)
        self.client = self.app.test_client()

    def test_first_send_creates_the_active_conversation_and_stores_both_turns(self) -> None:
        resp = self.client.post("/api/chat", json={"message": "Hello there"})
        self.assertEqual(resp.status_code, 200)
        events = parse_sse(resp.get_data(as_text=True))
        started = [data for name, data in events if name == "conversation"]
        self.assertEqual(len(started), 1)
        conv_id = json.loads(started[0])["id"]
        self.assertEqual(self.store.active_conversation_id, conv_id)
        self.assertEqual(
            [(m.who, m.body) for m in self.store.home_thread],
            [("You", "Hello there"), ("Orbit", "Hello from Orbit.")],
        )
        self.assertTrue(all(m.id for m in self.store.home_thread))

    def test_second_send_reuses_the_same_conversation(self) -> None:
        self.client.post("/api/chat", json={"message": "One"}).get_data(as_text=True)
        conv_id = self.store.active_conversation_id
        resp = self.client.post("/api/chat", json={"message": "Two"})
        events = parse_sse(resp.get_data(as_text=True))
        self.assertEqual([name for name, _ in events if name == "conversation"], [])
        self.assertEqual(self.store.active_conversation_id, conv_id)
        self.assertEqual(len(self.store.home_thread), 4)

    def test_empty_message_is_400_and_regenerate_without_history_is_409(self) -> None:
        resp = self.client.post("/api/chat", json={"message": "   "})
        self.assertEqual(
            (resp.status_code, resp.get_json()), (400, {"error": "message is required"})
        )
        resp = self.client.post("/api/chat", json={"regenerate": True})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.get_json(), {"error": "there is no completed response to regenerate"}
        )
        self.assertIsNone(self.store.active_conversation_id)

    def test_rating_a_stored_reply_is_recorded_with_its_id(self) -> None:
        self.client.post("/api/chat", json={"message": "Rate me"}).get_data(as_text=True)
        message_id = self.store.home_thread[-1].id
        resp = self.client.post(
            f"/api/messages/{message_id}/rating", json={"rating": "up"}
        )
        self.assertEqual(
            (resp.status_code, resp.get_json()),
            (200, {"message_id": message_id, "rating": "up"}),
        )

    def test_new_conversation_endpoint_resets_to_a_fresh_composer(self) -> None:
        self.client.post("/api/chat", json={"message": "Hi"}).get_data(as_text=True)
        resp = self.client.post("/api/conversations/new", json={})
        self.assertEqual(resp.get_json(), {"active_conversation_id": None})
        self.assertIsNone(self.store.active_conversation_id)
        self.assertEqual(self.store.home_thread, [])


# ---------------------------------------------------------------------------
# Accounts on: the single-account happy paths step 2 must preserve.
# ---------------------------------------------------------------------------


def make_accounts_client():
    accounts = AccountStore(iterations=1_000)
    store = Store()
    projects = ProjectStore()
    app = create_app(
        provider=FakeProvider(), store=store, projects=projects, accounts=accounts
    )
    return app, store, projects, accounts


def oauth_client(app, email: str, name: str):
    client = app.test_client()
    client.post("/auth/oauth/github", data={"name": name, "email": email})
    return client


class AccountsOnSingleAccountTests(unittest.TestCase):
    def test_signed_out_id_routes_redirect_to_login_first(self) -> None:
        app, _, _, _ = make_accounts_client()
        client = app.test_client()
        resp = client.get("/projects/p-anything")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.headers["Location"])
        resp = client.get("/task/T1")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/login", resp.headers["Location"])

    def test_signed_in_account_creates_a_project_and_sees_it_everywhere(self) -> None:
        app, store, projects, accounts = make_accounts_client()
        client = oauth_client(app, "grace@example.dev", "Grace Hopper")
        resp = client.post(
            "/projects",
            data={"name": "Compiler notes", "description": "", "mode": "blank"},
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Compiler notes", client.get("/projects").get_data(as_text=True))
        self.assertIn("Compiler notes", client.get("/dashboard").get_data(as_text=True))
        project_id = resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
        self.assertEqual(client.get(f"/projects/{project_id}").status_code, 200)

    def test_guided_setup_lands_its_first_task_on_the_new_project(self) -> None:
        app, store, projects, accounts = make_accounts_client()
        client = oauth_client(app, "grace@example.dev", "Grace Hopper")
        resp = client.post(
            "/projects",
            data={
                "name": "Order bot",
                "description": "",
                "mode": "guided",
                "first_task": "Sketch the order flow",
            },
        )
        self.assertEqual(resp.status_code, 303)
        body = client.get(resp.headers["Location"].split("?")[0]).get_data(as_text=True)
        self.assertIn("Sketch the order flow", body)


# ---------------------------------------------------------------------------
# The status quo step 2 exists to change. These tests describe today
# faithfully: with accounts on, data is still filed under the seeded
# single user, so accounts can see and mutate each other's work.
# Step 2 flips exactly these assertions to 404-isolation.
# ---------------------------------------------------------------------------


class SharedBucketTodayTests(unittest.TestCase):
    def test_signed_in_account_opens_the_seeded_example_task(self) -> None:
        app, store, projects, accounts = make_accounts_client()
        client = oauth_client(app, "grace@example.dev", "Grace Hopper")
        self.assertEqual(client.get("/task/T1").status_code, 200)

    def test_projects_created_while_signed_in_are_filed_under_the_seeded_user(self) -> None:
        app, store, projects, accounts = make_accounts_client()
        client = oauth_client(app, "grace@example.dev", "Grace Hopper")
        grace_id = accounts.by_email("grace@example.dev")["id"]
        client.post(
            "/projects",
            data={"name": "Grace project", "description": "", "mode": "blank"},
        )
        self.assertEqual([p.name for p in projects.list_for(store.user.id)], ["Grace project"])
        self.assertEqual(projects.list_for(grace_id), [])

    def test_a_second_account_sees_the_first_accounts_project(self) -> None:
        app, store, projects, accounts = make_accounts_client()
        grace = oauth_client(app, "grace@example.dev", "Grace Hopper")
        resp = grace.post(
            "/projects",
            data={"name": "Grace project", "description": "", "mode": "blank"},
        )
        project_id = resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]

        ada = oauth_client(app, "ada@example.dev", "Ada Osei")
        self.assertEqual(ada.get(f"/projects/{project_id}").status_code, 200)
        self.assertIn("Grace project", ada.get("/dashboard").get_data(as_text=True))

    def test_a_second_account_can_mutate_the_first_accounts_task(self) -> None:
        app, store, projects, accounts = make_accounts_client()
        grace = oauth_client(app, "grace@example.dev", "Grace Hopper")
        resp = grace.post("/api/tasks", json={"request": "Grace task"})
        task_id = resp.get_json()["id"]

        ada = oauth_client(app, "ada@example.dev", "Ada Osei")
        self.assertEqual(ada.get(f"/task/{task_id}").status_code, 200)
        approve = ada.post(f"/api/task/{task_id}/approve", json={"approve": True})
        self.assertEqual(approve.status_code, 200)
        task = store.get_task(task_id)
        assert task is not None
        self.assertTrue(task.approved)


if __name__ == "__main__":
    unittest.main()
