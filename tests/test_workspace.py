"""Offline tests for the project workspace: the map's data, the Main Agent
stream, status changes, task creation, and the once-per-project AI summary.

Nothing here calls a live model: the provider is scripted, the audit log and
the project store point at temp files, and SSE bodies are parsed into events.
The pinned behaviours are the rules, not the copy:

  * the map is derived from real run states — a task that never ran is
    "Not started", and no bucket is ever called "Done";
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

from app.projectmap import bucket_for, map_for
from app.projects import ProjectStore
from app.server import create_app
from app.store import Store
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
    def test_buckets_are_derived_from_the_run_only(self) -> None:
        store = Store()
        never = store.create_task("Fix login", project_id="p1")
        self.assertEqual(bucket_for(never), "not_started")
        never.run = new_run(never.description)  # planned, status RUNNING
        self.assertEqual(bucket_for(never), "in_progress")
        never.run.status = FAILED
        self.assertEqual(bucket_for(never), "needs_attention")
        never.run.status = IDLE
        self.assertEqual(bucket_for(never), "waiting")

    def test_map_groups_and_counts_real_steps(self) -> None:
        store = Store()
        a = store.create_task("Fix the crash in checkout.py", project_id="p1")
        b = store.create_task("Review the diff in orders.py", project_id="p1")
        b.run = new_run(b.description)
        buckets = {b_["key"]: b_ for b_ in map_for([a, b])}
        self.assertEqual([t["title"] for t in buckets["not_started"]["tasks"]], [a.title])
        cell = buckets["in_progress"]["tasks"][0]
        self.assertEqual(cell["id"], b.id)
        self.assertEqual(cell["steps_done"], 0)
        self.assertGreater(cell["steps_total"], 0)  # real plan from the request
        # no bucket claims work is done — a finished turn is "waiting on you"
        from app.projectmap import BUCKETS

        self.assertNotIn("Done", [label for _, label in BUCKETS])

    def test_empty_map_maps_empty(self) -> None:
        buckets = map_for([])
        self.assertEqual(len(buckets), 4)
        self.assertTrue(all(not b_["tasks"] for b_ in buckets))


class ProjectConversationTests(unittest.TestCase):
    def test_find_or_create_is_once_and_scoped(self) -> None:
        store = Store()
        self.assertIsNone(store.project_conversation("p-1"))  # GET creates nothing
        conv = store.project_conversation("p-1", create=True)
        again = store.project_conversation("p-1", create=True)
        self.assertIs(conv, again)
        self.assertTrue(conv.titled)  # no naming call for workspace threads
        self.assertIsNone(store.project_conversation("p-1", user_id="someone-else"))
        # project threads never pollute the home history
        self.assertNotIn(conv, store.conversations_for())

    def test_project_thread_empty_without_conversation(self) -> None:
        self.assertEqual(Store().project_thread("p-x"), [])


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


class WorkspaceRouteTests(WorkspaceFixture):
    def test_workspace_renders_project_and_empty_map(self) -> None:
        html = self.client.get(f"/projects/{self.project.id}").get_data(as_text=True)
        self.assertIn("whatsapp-bot", html)
        self.assertIn("Project map", html)
        self.assertIn("Main Agent", html)
        self.assertIn("Not started", html)
        self.assertIn("No conversation yet", html)

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
        task = self.store.tasks_for_project(self.project.id)[0]
        self.assertEqual(task.project_id, self.project.id)
        html = self.client.get(f"/projects/{self.project.id}").get_data(as_text=True)
        self.assertIn("Fix the crash in checkout.py", html)

    def test_details_only_open_for_this_projects_task(self) -> None:
        own = self.store.create_task("Own task", project_id=self.project.id)
        other = self.store.create_task("Someone else's task")  # no project
        html = self.client.get(f"/projects/{self.project.id}?task={own.id}").get_data(as_text=True)
        self.assertIn("Task details", html)
        self.assertIn("Own task", html)
        # a task that is not this project's renders no details at all
        html = self.client.get(f"/projects/{self.project.id}?task={other.id}").get_data(as_text=True)
        self.assertNotIn("Task details", html)

    def test_stream_appends_project_scoped_thread_and_audits(self) -> None:
        events = self.stream("What is this project?")
        kinds = [name for name, _ in events]
        self.assertIn("answer", kinds)
        self.assertIn("message", kinds)
        self.assertEqual(kinds[-1], "done")
        threads = self.store.threads
        conv = self.store.project_conversation(self.project.id)
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
        self.store.set_mode("agent")
        events = self.stream("Plan the tasks.")
        kinds = [name for name, _ in events]
        self.assertIn("thinking", kinds)
        self.assertIn("answer", kinds)
        self.store.set_mode("chat")

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
