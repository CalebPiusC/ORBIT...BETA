"""Offline tests for the project workspace: the map's data, the Main Agent
stream, status changes, task creation, and the once-per-project AI summary.

Nothing here calls a live model: the provider is scripted, the audit log and
the project store point at temp files, and SSE bodies are parsed into events.
The pinned behaviours are the rules, not the copy:

  * the map is grouped by stages the person filed, and a row's state is
    derived; "Complete" exists only because the person marked it — never
    because a turn ended;
  * Task Details only open for a task of THIS project, same as the approval
    gate being enforced in the store rather than in copy;
  * the Main Agent conversation is project-scoped (its own thread, its own
    audit surface) and never appears in the home history;
  * the AI summary is written once, from model output, on the first completed
    exchange; a failed call writes nothing and a later turn does not retry.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from providers.base import StreamChunk

from app.projectmap import UNSTAGED, latest_task, map_stages, next_up, progress, row_state
from app.projects import ProjectStore
from app.server import create_app
from app.store import DEFAULT_USER_ID, Activity, Store
from app.summaries import clean_summary, request_summary
from app.taskplan import IDLE, FAILED, new_run
from tools.registry import AuditLog


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
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip(" "))
        events.append((event, "\n".join(data_lines)))
    return events


class ProjectProvider:
    """Scripted provider: canned streams, plus a summarize() to exercise the
    once-per-project summary seam (and a failure mode for it)."""

    def __init__(self, summaries: list[str | Exception] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.summary_calls: list[tuple[str, str]] = []
        self._summaries = list(summaries or [])

    def reply_stream(self, history, system_prompt: str, *, think=False, **kwargs):
        self.calls.append({"history": list(history), "prompt": system_prompt, "think": think})
        if think:
            yield StreamChunk(kind="thinking", text="Looking at the project map.")
        yield StreamChunk(kind="answer", text="This project tracks orders on WhatsApp.")

    def summarize(self, project_name: str, description: str, user_text: str, answer: str) -> str:
        self.summary_calls.append((project_name, user_text))
        outcome = self._summaries.pop(0) if self._summaries else "A scripted summary."
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class WorkspaceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit_path = Path(self.tmp.name) / "audit.log"
        self.projects = ProjectStore(Path(self.tmp.name) / "projects.json")
        self.store = Store()
        self.view = self.store.for_account(DEFAULT_USER_ID)
        self.provider = ProjectProvider()
        self.app = create_app(
            provider=self.provider,
            store=self.store,
            projects=self.projects,
            model_audit=AuditLog(self.audit_path),
        )
        self.client = self.app.test_client()
        self.project = self.projects.create(
            user_id=self.store.user.id, name="whatsapp-bot", description="Takes orders."
        )

    def audit_events(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines() if line.strip()]

    def stream(self, text: str) -> list[tuple[str, str]]:
        resp = self.client.post(
            f"/api/project/{self.project.id}/stream",
            json={"message": text},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return parse_sse(resp.get_data(as_text=True))


class ProjectMapTests(unittest.TestCase):
    def test_row_state_is_derived_and_completion_is_the_persons(self) -> None:
        store = Store().for_account(DEFAULT_USER_ID)
        task = store.create_task("Fix login", project_id="p1")
        self.assertEqual(row_state(task), ("planned", "Planned"))
        task.run = new_run(task.description)  # planned, status RUNNING
        self.assertEqual(row_state(task), ("in_progress", "In progress"))
        task.run.status = FAILED
        self.assertEqual(row_state(task), ("needs_attention", "Needs attention"))
        task.run.status = IDLE  # a turn that ended is not "Complete"
        self.assertEqual(row_state(task), ("finished", "Turn finished"))
        # only the person's mark says Complete (and "reopen" undoes it)
        store.set_done(task.id, True)
        self.assertEqual(row_state(task), ("done", "Complete"))
        self.assertTrue(task.activity[0].text.startswith("You marked this task complete"))
        store.set_done(task.id, False)
        self.assertNotEqual(row_state(task)[0], "done")
        self.assertIsNone(store.set_done("t-nope", True))

    def test_stages_group_in_filed_order_with_real_counts(self) -> None:
        store = Store().for_account(DEFAULT_USER_ID)
        a = store.create_task("Fix the crash", project_id="p1", stage="Foundation")
        b = store.create_task("Shape search", project_id="p1", stage="Foundation")
        c = store.create_task("Sort by recent", project_id="p1")  # no stage filed
        store.set_done(a.id, True)
        b.run = new_run(b.description)
        stages = map_stages([a, b, c])
        # the named stage leads; the unstaged group renders last under "Tasks"
        self.assertEqual([s["name"] for s in stages], ["Foundation", UNSTAGED])
        self.assertEqual(stages[0]["done"], 1)
        self.assertEqual(stages[0]["total"], 2)
        cell = stages[0]["tasks"][1]
        self.assertEqual(cell["id"], b.id)
        self.assertEqual(cell["state"], "in_progress")
        self.assertGreater(cell["steps_total"], 0)  # real plan from the request
        self.assertEqual(progress([a, b, c]), (1, 3))
        self.assertIs(latest_task([a, b, c]), c)
        self.assertIsNone(latest_task([]))
        self.assertEqual(map_stages([]), [])

    def test_next_up_is_within_the_same_stage_and_skips_completion(self) -> None:
        store = Store().for_account(DEFAULT_USER_ID)
        a = store.create_task("One", project_id="p1", stage="S")
        b = store.create_task("Two", project_id="p1", stage="S")
        c = store.create_task("Three", project_id="p1", stage="S")
        other = store.create_task("Elsewhere", project_id="p1", stage="T")
        self.assertIs(next_up([a, b, c, other], a), b)
        self.assertIs(next_up([a, b, c, other], c), None)
        # "builds on" names a real task or nothing at all
        linked = store.create_task("Follow-up", project_id="p1", builds_on=a.id)
        self.assertEqual(linked.builds_on, a.id)
        with self.assertRaises(ValueError):
            store.create_task("Dangling", project_id="p1", builds_on="t-not-real")


class ProjectConversationTests(unittest.TestCase):
    def test_find_or_create_is_once_and_scoped(self) -> None:
        raw = Store()
        store = raw.for_account(DEFAULT_USER_ID)
        self.assertIsNone(store.project_conversation("p-1"))  # GET creates nothing
        conv = store.project_conversation("p-1", create=True)
        again = store.project_conversation("p-1", create=True)
        self.assertIs(conv, again)
        self.assertTrue(conv.titled)  # no naming call for workspace threads
        self.assertIsNone(raw.for_account("someone-else").project_conversation("p-1"))
        # project threads never pollute the home history
        self.assertNotIn(conv, store.conversations_for())

    def test_project_thread_empty_without_conversation(self) -> None:
        self.assertEqual(Store().for_account(DEFAULT_USER_ID).project_thread("p-x"), [])


class SummaryTests(unittest.TestCase):
    def test_clean_summary(self) -> None:
        self.assertEqual(clean_summary(' "Summary: A bot.  It takes orders." '), "A bot. It takes orders.")
        self.assertEqual(len(clean_summary("x " * 400)), 280)  # capped with the ellipsis
        self.assertTrue(clean_summary("x " * 400).endswith("…"))
        with self.assertRaises(ValueError):
            clean_summary("  ''  ")

    def test_request_summary_never_raises(self) -> None:
        class Raises:
            def summarize(self, *args):
                raise RuntimeError("model died")

        self.assertIsNone(request_summary(Raises(), "n", "d", "u", "a"))
        self.assertIsNone(request_summary(object(), "n", "d", "u", "a"))  # no method

    def test_request_summary_cleans(self) -> None:
        class OK:
            def summarize(self, *args):
                return ' "Summary: A tidy one." '

        self.assertEqual(request_summary(OK(), "n", "d", "u", "a"), "A tidy one.")


class AgentConversationTests(WorkspaceFixture):
    def test_assign_creates_a_specialist_conversation_and_audits(self) -> None:
        resp = self.client.post(
            "/agents/code-review/select".replace("code-review", "review")
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.view.selected_agent, "review")
        resp = self.client.post(
            "/agents/review/assign", data={"project_id": self.project.id}
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("agent=review", resp.headers["Location"])
        self.assertIsNone(self.view.selected_agent)  # consumed by the assignment
        conv = self.view.project_conversation(self.project.id, agent_id="review")
        self.assertIsNotNone(conv)
        events = [e for e in self.audit_events() if e.get("event") == "agent_specialist_assigned"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["agent_id"], "review")
        # assigning without a project is refused rather than faked
        resp = self.client.post("/agents/review/assign", data={"project_id": "p-nope"})
        self.assertEqual(resp.status_code, 404)

    def test_specialist_stream_uses_its_own_brief_and_thread(self) -> None:
        resp = self.client.post(
            f"/api/project/{self.project.id}/stream",
            json={"message": "Review this", "agent": "review"},
        )
        self.assertEqual(resp.status_code, 200)
        resp.get_data(as_text=True)
        # the specialist's instructions replace the Main Agent prompt
        self.assertIn("Review specialist", self.provider.calls[-1]["prompt"])
        self.assertIn("PROJECT YOU ARE ASSIGNED TO", self.provider.calls[-1]["prompt"])
        self.assertNotIn("Main Agent", self.provider.calls[-1]["prompt"])
        main = self.view.project_conversation(self.project.id, agent_id="main")
        self.assertIsNone(main)  # an untouched conversation is not created
        review = self.view.project_conversation(self.project.id, agent_id="review")
        thread = self.store.threads[review.id]
        self.assertEqual(thread[-1].speaker, "Review")

    def test_task_pin_joins_real_state_to_the_prompt(self) -> None:
        task = self.view.create_task(
            "Pin this discussion", project_id=self.project.id, stage="Foundation"
        )
        task.run = new_run(task.description)
        resp = self.client.post(
            f"/api/project/{self.project.id}/stream",
            json={"message": "What remains?", "task_id": task.id},
        )
        self.assertEqual(resp.status_code, 200)
        resp.get_data(as_text=True)
        prompt = self.provider.calls[-1]["prompt"]
        self.assertIn("TASK THE PERSON IS DISCUSSING RIGHT NOW", prompt)
        self.assertIn("Pin this discussion", prompt)
        self.assertIn("[ ]", prompt)  # the real, unchecked plan steps
        # a pin that belongs to another project is refused
        other_store_task = self.view.create_task("Elsewhere")
        resp = self.client.post(
            f"/api/project/{self.project.id}/stream",
            json={"message": "hi", "task_id": other_store_task.id},
        )
        self.assertEqual(resp.status_code, 404)

    def test_replies_carry_the_speaker_label(self) -> None:
        resp = self.client.post(
            f"/api/project/{self.project.id}/stream", json={"message": "Hi"}
        )
        resp.get_data(as_text=True)
        conv = self.view.project_conversation(self.project.id, agent_id="main")
        self.assertEqual(self.store.threads[conv.id][-1].speaker, "Main Agent")
        # and the rendered thread carries the reply with no name label: the
        # column heading already says who is speaking (chat rules).
        html = self.client.get(f"/projects/{self.project.id}").get_data(as_text=True)
        self.assertNotIn("chat-speaker", html)
        self.assertIn('class="ws-msg ws-msg--orbit"', html)


class WorkspaceRouteTests(WorkspaceFixture):
    def test_workspace_renders_project_and_empty_map(self) -> None:
        html = self.client.get(f"/projects/{self.project.id}").get_data(as_text=True)
        self.assertIn("whatsapp-bot", html)
        self.assertIn("Project Map", html)
        self.assertIn("Main Agent", html)
        self.assertIn("0 of 0 tasks complete", html)
        self.assertIn("No conversation yet", html)
        self.assertIn("The map is empty", html)
        # the frame's four tabs, all real views
        for label in ("Tasks", "Files", "Activity"):
            self.assertIn(f"?tab={label.lower()}", html)
        # no fake completion claim ever slips in through empty states
        self.assertNotIn("All done", html)

    def test_map_renders_stages_rows_and_the_add_task_form(self) -> None:
        done = self.view.create_task("Set up the project", project_id=self.project.id, stage="Foundation")
        self.view.set_done(done.id, True)
        open_ = self.view.create_task("Wire the provider", project_id=self.project.id, stage="Foundation")
        html = self.client.get(f"/projects/{self.project.id}").get_data(as_text=True)
        self.assertIn("Foundation", html)
        # the meta line counts person-marked completes; the open row is the
        # map's one "Up next" (and its stage carries the capsule), the closed
        # row reads "Done" with a tick.
        self.assertIn("1 of 2 tasks complete", html)
        self.assertIn("Up next", html)
        self.assertIn(">Done<", html)
        self.assertIn('action="/projects/' + self.project.id + '/tasks"', html)
        self.assertIn('name="stage"', html)
        self.assertIn('name="builds_on"', html)
        self.assertIn('value="' + open_.id + '"', html)

    def test_mark_complete_toggles_and_audits(self) -> None:
        task = self.view.create_task("Ship the map", project_id=self.project.id)
        resp = self.client.post(f"/projects/{self.project.id}/tasks/{task.id}/complete")
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(self.view.get_task(task.id).done)
        events = [e for e in self.audit_events() if e.get("event") == "task_completion"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "complete")
        self.assertEqual(events[0]["project_id"], self.project.id)
        # reopen uses the same route, same toggle
        self.client.post(f"/projects/{self.project.id}/tasks/{task.id}/complete")
        self.assertFalse(self.view.get_task(task.id).done)
        # a task of another project cannot be toggled through this one
        other = self.view.create_task("Not yours", project_id=None)
        resp = self.client.post(f"/projects/{self.project.id}/tasks/{other.id}/complete")
        self.assertEqual(resp.status_code, 404)

    def test_the_other_tabs_list_only_stored_rows(self) -> None:
        task = self.view.create_task(
            "Follow the orders flow in checkout.py", project_id=self.project.id, stage="Build"
        )
        # an empty files tab says so honestly before anything names a file
        empty_files = self.client.get(
            f"/projects/{self.project.id}?tab=files"
        ).get_data(as_text=True)
        self.assertIn("No files named yet", empty_files)
        task.run = new_run(task.description)
        task.activity.insert(0, Activity(
            text="You marked this task complete.", time="09:12"))
        files = self.client.get(f"/projects/{self.project.id}?tab=files").get_data(as_text=True)
        self.assertIn("checkout.py", files)
        self.assertIn("Named in the request", files)
        activity = self.client.get(f"/projects/{self.project.id}?tab=activity").get_data(as_text=True)
        self.assertIn("You marked this task complete.", activity)
        rows = self.client.get(f"/projects/{self.project.id}?tab=tasks").get_data(as_text=True)
        self.assertIn("Follow the orders flow in checkout.py", rows)
        self.assertIn("Build ·", rows)
        # an unknown tab falls back to the map rather than emptying the page
        fallback = self.client.get(f"/projects/{self.project.id}?tab=bogus").get_data(as_text=True)
        self.assertIn("Project Map", fallback)
        # activity rows carry the task they belong to, newest task first
        self.assertLess(activity.index("You marked this task complete."),
                        activity.index("Task created from your request."))

    def test_unknown_project_404s(self) -> None:
        resp = self.client.get("/projects/p-nope")
        self.assertEqual(resp.status_code, 404)

    def test_status_change_persists_and_audits(self) -> None:
        resp = self.client.post(f"/projects/{self.project.id}/status", data={"status": "idea"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.projects.get_for(self.project.id, "caleb").status, "idea")
        events = [e for e in self.audit_events() if e.get("event") == "project_status_changed"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from_status"], "building")
        self.assertEqual(events[0]["to_status"], "idea")

    def test_status_change_rejects_unknown_values(self) -> None:
        resp = self.client.post(f"/projects/{self.project.id}/status", data={"status": "banana"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.projects.get_for(self.project.id, "caleb").status, "building")
        resp = self.client.post("/projects/p-nope/status", data={"status": "live"})
        self.assertEqual(resp.status_code, 404)

    def test_task_creation_attaches_and_shows_on_map(self) -> None:
        resp = self.client.post(
            f"/projects/{self.project.id}/tasks",
            data={"request": "Fix the crash in checkout.py"},
        )
        self.assertEqual(resp.status_code, 303)
        task = self.view.tasks_for_project(self.project.id)[0]
        self.assertEqual(task.project_id, self.project.id)
        html = self.client.get(f"/projects/{self.project.id}").get_data(as_text=True)
        self.assertIn("Fix the crash in checkout.py", html)

    def test_details_only_open_for_this_projects_task(self) -> None:
        own = self.view.create_task("Own task", project_id=self.project.id)
        other = self.view.create_task("Someone else's task")  # no project
        html = self.client.get(f"/projects/{self.project.id}?task={own.id}").get_data(as_text=True)
        self.assertIn("Own task", html)
        self.assertIn("Mark complete", html)
        # the open task is what the conversation focuses on
        self.assertIn("Focused on: Own task", html)
        # a task that is not this project's renders nowhere on the page
        html = self.client.get(f"/projects/{self.project.id}?task={other.id}").get_data(as_text=True)
        self.assertNotIn("Someone else's task", html)

    def test_details_render_the_run_state_and_a_waiting_proposal(self) -> None:
        from app.taskplan import IDLE

        a = self.view.create_task("Foundation task", project_id=self.project.id, stage="Foundation")
        b = self.view.create_task(
            "Follow the orders flow in checkout.py",
            project_id=self.project.id,
            stage="Foundation",
            builds_on=a.id,
        )
        b.run = new_run(b.description)
        b.run.status = IDLE  # the turn finished: a draft exists, nothing applied
        html = self.client.get(f"/projects/{self.project.id}?task={b.id}").get_data(as_text=True)
        self.assertIn("FOUNDATION", html)  # the eyebrow is the filed stage
        self.assertIn("What done looks like", html)
        self.assertIn("A proposal is ready.", html)
        self.assertIn("Open change review", html)
        self.assertIn("Needs approval", html)
        # with nothing selected in the query, the waiting draft opens by default
        default = self.client.get(f"/projects/{self.project.id}").get_data(as_text=True)
        self.assertIn("Focused on: Follow the orders flow in checkout.py", default)
        # the stage capsule carries the copper-dot approval count
        self.assertIn("1 needs approval", default)
        # approving moves the row and the details out of the waiting state
        b.approved = True
        approved = self.client.get(f"/projects/{self.project.id}?task={b.id}").get_data(as_text=True)
        self.assertIn("You approved this change.", approved)
        self.assertNotIn("A proposal is ready.", approved)

    def test_stream_appends_project_scoped_thread_and_audits(self) -> None:
        events = self.stream("What is this project?")
        kinds = [name for name, _ in events]
        self.assertIn("answer", kinds)
        self.assertIn("message", kinds)
        self.assertEqual(kinds[-1], "done")
        threads = self.store.threads
        conv = self.view.project_conversation(self.project.id)
        self.assertIsNotNone(conv)
        thread = threads[conv.id]
        self.assertEqual(len(thread), 2)
        self.assertFalse(thread[0].is_orbit)
        self.assertTrue(thread[1].is_orbit)
        audits = [e for e in self.audit_events() if e.get("event") == "chat_message_received"]
        self.assertEqual(audits[0]["surface"], "project")
        self.assertEqual(audits[0]["project_id"], self.project.id)
        # the prompt carried the project context as ground truth
        self.assertIn("whatsapp-bot", self.provider.calls[0]["prompt"])
        self.assertIn("PROJECT CONTEXT", self.provider.calls[0]["prompt"])

    def test_stream_agent_mode_narrates(self) -> None:
        self.view.set_mode("agent")
        events = self.stream("Plan the tasks.")
        kinds = [name for name, _ in events]
        self.assertIn("thinking", kinds)
        self.assertIn("answer", kinds)
        self.view.set_mode("chat")

    def test_stream_requires_message_and_project(self) -> None:
        resp = self.client.post(f"/api/project/{self.project.id}/stream", json={"message": "  "})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/project/p-nope/stream", json={"message": "hi"})
        self.assertEqual(resp.status_code, 404)

    def test_summary_written_once_from_the_first_exchange(self) -> None:
        self.stream("What is it?")
        project = self.projects.get_for(self.project.id, "caleb")
        self.assertEqual(project.summary, "A scripted summary.")
        events = [e for e in self.audit_events() if e.get("event") == "project_summary_written"]
        self.assertEqual(len(events), 1)
        # a second exchange never re-asks
        self.stream("And now?")
        self.assertEqual(len(self.provider.summary_calls), 1)

    def test_summary_failure_writes_nothing(self) -> None:
        self.provider._summaries = [RuntimeError("model died")]
        self.stream("What is it?")
        project = self.projects.get_for(self.project.id, "caleb")
        self.assertEqual(project.summary, "")
        self.assertNotIn(
            "project_summary_written", [e.get("event") for e in self.audit_events()]
        )

    def test_empty_stream_is_surfaced_and_audited(self) -> None:
        class Empty:
            def reply_stream(self, *args, **kwargs):
                if False:
                    yield

        app = create_app(provider=Empty(), store=self.store, projects=self.projects)
        client = app.test_client()
        resp = client.post(f"/api/project/{self.project.id}/stream", json={"message": "hi"})
        events = parse_sse(resp.get_data(as_text=True))
        kinds = [name for name, _ in events]
        self.assertIn("error", kinds)  # a silent stream is indistinguishable from a hang
        self.assertIn("done", kinds)


if __name__ == "__main__":
    unittest.main()
