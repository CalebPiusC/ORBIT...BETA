"""Offline tests for the Projects backbone (app/projects.py) and its routes.

Everything here runs without a model key: the store is exercised directly, and
the routes through Flask's test client with an audit log pointed at a temp
file. The pinned behaviours are the product rules, not the formatting:

  * only source="new" creates; every other entry point refuses honestly, is
    audited, and stores nothing (brief: imports are visible, nothing is faked
    as a working import);
  * the four filter buckets are exactly the brief's Live / Build in Progress /
    Archived / Saved Ideas;
  * a summary is never fabricated — it starts empty and only set_summary (the
    Main Agent's seam) writes one;
  * projects persist through a real file round-trip (restarts keep them);
  * every read is scoped by user_id.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.projects import (
    IMPORT_SOURCES,
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    SOURCE_LABELS,
    STATUS_LABELS,
    STATUSES,
    ProjectStore,
    SourceUnavailable,
)
from app.server import create_app
from app.store import DEFAULT_USER_ID, Store
from tools.registry import AuditLog


class ProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProjectStore()  # in-memory: the routing-test default

    def test_create_assigns_defaults(self) -> None:
        project = self.store.create(user_id="caleb", name="  My   Bot ")
        self.assertTrue(project.id.startswith("p-"))
        self.assertEqual(project.name, "My Bot")  # whitespace collapses
        self.assertEqual(project.status, "building")
        self.assertEqual(project.source, "new")
        self.assertEqual(project.summary, "")  # never fabricated
        self.assertTrue(project.created_at)
        self.assertTrue(project.updated_at)

    def test_name_is_required_and_bounded(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create(user_id="caleb", name="   ")
        with self.assertRaises(ValueError):
            self.store.create(user_id="caleb", name="x" * (MAX_NAME_CHARS + 1))
        with self.assertRaises(ValueError):
            self.store.create(
                user_id="caleb", name="ok", description="d" * (MAX_DESCRIPTION_CHARS + 1)
            )
        # the description is optional — a blank one is fine
        self.store.create(user_id="caleb", name="ok")

    def test_import_sources_are_visible_but_not_connected(self) -> None:
        # Every entry point the brief lists is a known source...
        for source in IMPORT_SOURCES:
            self.assertIn(source, SOURCE_LABELS)
        # ...and each one refuses honestly rather than pretending to import.
        for source in IMPORT_SOURCES:
            with self.assertRaises(SourceUnavailable) as ctx:
                self.store.create(user_id="caleb", name="Imported", source=source)
            self.assertEqual(ctx.exception.source, source)
        # nothing was stored for the refused attempts
        self.assertEqual(self.store.list_for("caleb"), [])

    def test_unknown_source_and_status_raise(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create(user_id="caleb", name="x", source="sourceforge")
        project = self.store.create(user_id="caleb", name="x")
        with self.assertRaises(ValueError):
            self.store.set_status(project.id, "caleb", "deleted")

    def test_status_labels_are_the_briefs_filter(self) -> None:
        # The filter chips read exactly Live / Build in Progress / Archived /
        # Saved Ideas — no invention, no paraphrase.
        self.assertEqual(
            STATUS_LABELS,
            {
                "live": "Live",
                "building": "Build in Progress",
                "archived": "Archived",
                "idea": "Saved Ideas",
            },
        )
        self.assertEqual(STATUSES, ("live", "building", "archived", "idea"))

    def test_list_scopes_to_user_and_filters(self) -> None:
        mine = self.store.create(user_id="caleb", name="Order bot", description="WhatsApp orders")
        other = self.store.create(user_id="caleb", name="Portfolio site")
        self.store.create(user_id="someone-else", name="Not mine")
        self.store.set_status(mine.id, "caleb", "live")
        self.store.set_status(other.id, "caleb", "idea")

        self.assertEqual(
            {p.name for p in self.store.list_for("caleb")},
            {"Order bot", "Portfolio site"},
        )
        self.assertEqual([p.name for p in self.store.list_for("caleb", status="live")], ["Order bot"])
        self.assertEqual([p.name for p in self.store.list_for("caleb", status="idea")], ["Portfolio site"])
        whatsapp_hits = self.store.list_for("caleb", query="whatsapp")
        self.assertEqual([p.id for p in whatsapp_hits], [mine.id])
        self.assertEqual(whatsapp_hits[0].status, "live")
        self.assertEqual([p.id for p in self.store.list_for("caleb", query="BOT")], [mine.id])
        # an unrecognized status value matches nothing instead of everything
        self.assertEqual(self.store.list_for("caleb", status="deleted"), [])

    def test_counts_cover_all_buckets(self) -> None:
        self.store.create(user_id="caleb", name="a")
        b = self.store.create(user_id="caleb", name="b")
        self.store.set_status(b.id, "caleb", "archived")
        self.assertEqual(
            self.store.counts_for("caleb"),
            {"live": 0, "building": 1, "archived": 1, "idea": 0},
        )

    def test_summary_only_comes_from_the_agent_seam(self) -> None:
        project = self.store.create(user_id="caleb", name="a")
        self.assertEqual(project.summary, "")
        with self.assertRaises(ValueError):
            self.store.set_summary(project.id, "caleb", "   ")
        updated = self.store.set_summary(project.id, "caleb", "A bot that takes orders on WhatsApp.")
        self.assertEqual(updated.summary, "A bot that takes orders on WhatsApp.")
        self.assertGreaterEqual(updated.updated_at, project.updated_at)

    def test_cannot_touch_another_users_project(self) -> None:
        project = self.store.create(user_id="caleb", name="mine")
        self.assertIsNone(self.store.get_for(project.id, "someone-else"))
        self.assertIsNone(self.store.set_status(project.id, "someone-else", "archived"))
        self.assertEqual(self.store.get_for(project.id, "caleb").status, "building")

    def test_file_round_trip_survives_a_restart(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "projects.json"
        first = ProjectStore(path)
        self.assertTrue(first.persistent)
        first.create(user_id="caleb", name="Keeps", description="still here")
        # a fresh instance on the same path is the restart case
        second = ProjectStore(path)
        names = [p.name for p in second.list_for("caleb")]
        self.assertEqual(names, ["Keeps"])
        self.assertIn('"version": 1', Path(path).read_text(encoding="utf-8"))

    def test_in_memory_store_is_not_persistent(self) -> None:
        # create_app's default: touching the app must never write runtime/.
        self.assertFalse(ProjectStore().persistent)


class ProjectRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit_path = Path(self.tmp.name) / "audit.log"
        self.projects_path = Path(self.tmp.name) / "projects.json"
        self.project_store = ProjectStore(self.projects_path)
        self.app = create_app(
            provider=None,  # unused: these routes never call a model
            store=Store(),
            projects=self.project_store,
            model_audit=AuditLog(self.audit_path),
        )
        self.client = self.app.test_client()

    def _audit_events(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines() if line.strip()]

    def test_projects_page_renders_the_first_run_hero(self) -> None:
        html = self.client.get("/projects").get_data(as_text=True)
        # The design's two real paths out of the empty state.
        self.assertIn("What would you like to make?", html)
        self.assertIn("Start with an idea", html)
        self.assertIn("Bring your own project", html)
        self.assertIn('href="/create"', html)
        self.assertNotIn("Good ideas, moving forward", html)  # that's the list view

    def test_create_then_list(self) -> None:
        resp = self.client.post(
            "/projects",
            data={"name": "whatsapp-ordering-bot", "description": "Takes orders.", "source": "new"},
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(len(self.project_store.list_for("caleb")), 1)
        events = [e for e in self._audit_events() if e.get("event") == "project_created"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "new")
        self.assertNotIn("name", events[0])  # ids and kinds, never the text
        html = self.client.get("/projects").get_data(as_text=True)
        self.assertIn("whatsapp-ordering-bot", html)
        self.assertIn("Takes orders.", html)
        self.assertIn("Build in Progress", html)

    def test_import_entry_points_render_disabled_and_honest(self) -> None:
        html = self.client.get("/create").get_data(as_text=True)
        # Visible in the design, disabled because they are not connected.
        for name in ("GitHub", "GitLab", "Bitbucket"):
            self.assertIn(name, html, name)
        self.assertIn("is-disabled", html)
        self.assertIn("Not connected", html)
        self.assertIn("File import isn't built yet", html)
        self.assertNotIn("fetched and added", html)  # no pretend success anywhere
        # The create form only ever posts the idea path.
        self.assertIn('value="new"', html)
        # A source smuggled into the POST is ignored: an idea is what the
        # route creates, so "new" is what gets stored.
        resp = self.client.post(
            "/projects",
            data={"name": "from github", "source": "github", "mode": "blank"},
        )
        self.assertEqual(resp.status_code, 303)
        project = self.project_store.list_for("caleb")[0]
        self.assertEqual(project.source, "new")

    def test_invalid_create_is_rejected(self) -> None:
        resp = self.client.post("/projects", data={"name": "   ", "source": "new"})
        self.assertEqual(resp.status_code, 303)
        self.assertIn("notice=invalid", resp.headers["Location"])
        self.assertEqual(self.project_store.list_for("caleb"), [])

    def test_plan_mode_lands_on_the_workspace_with_the_hint(self) -> None:
        resp = self.client.post("/projects", data={"name": "Scoped", "mode": "plan"})
        self.assertEqual(resp.status_code, 303)
        project = self.project_store.list_for("caleb")[0]
        self.assertEqual(
            resp.headers["Location"],
            f"/projects/{project.id}?plan=1#agent",
        )
        html = self.client.get(resp.headers["Location"].split("#")[0]).get_data(as_text=True)
        self.assertIn("You chose", html)  # the plan hint over the composer
        self.assertIn('data-plan-hint="1"', html)

    def test_the_continue_strip_names_real_recent_work(self) -> None:
        html = self.client.get("/create").get_data(as_text=True)
        # no projects → no strip (the CSS class name ships on every page;
        # the rendered element is what must be absent)
        self.assertNotIn('class="continue-strip"', html)
        project = self.project_store.create(user_id="caleb", name="Fieldnotes")
        self.app.config["STORE"].for_account(DEFAULT_USER_ID).create_task("Sort by recent", project_id=project.id)
        html = self.client.get("/create").get_data(as_text=True)
        self.assertIn('class="continue-strip"', html)
        strip = html[html.index('class="continue-strip"'):]
        self.assertIn("Fieldnotes", strip)
        self.assertIn("Sort by recent", strip)
        self.assertIn("Updated just now", strip)

    def _grid_only(self, html: str) -> str:
        """The card grid on its own.

        Assertions scope to it because the sidebar's RECENT PROJECTS list
        legitimately shows every project, filtered view or not — a plain
        haystack search would find filtered-out names there.
        """
        start = html.index('class="projects-grid"')
        end = html.index("grid-caption", start)
        return html[start:end]

    def test_search_and_status_filter(self) -> None:
        self.project_store.create(user_id="caleb", name="Order bot")
        archived = self.project_store.create(user_id="caleb", name="Old site")
        self.project_store.set_status(archived.id, "caleb", "archived")
        grid = self._grid_only(self.client.get("/projects?status=archived").get_data(as_text=True))
        self.assertIn("Old site", grid)
        self.assertNotIn("Order bot", grid)
        # chips carry real counts, including the zeroes
        html = self.client.get("/projects").get_data(as_text=True)
        self.assertIn("Archived <span", html)
        grid = self._grid_only(html)
        self.assertIn("Order bot", grid)
        self.assertIn("Old site", grid)
        grid = self._grid_only(self.client.get("/projects?q=order").get_data(as_text=True))
        self.assertIn("Order bot", grid)
        self.assertNotIn("Old site", grid)
        # an unknown status param is ignored rather than emptying the grid
        grid = self._grid_only(self.client.get("/projects?status=bogus").get_data(as_text=True))
        self.assertIn("Order bot", grid)

    def test_sort_links_really_sort(self) -> None:
        self.project_store.create(user_id="caleb", name="Zebra")
        self.project_store.create(user_id="caleb", name="Alpha")
        grid = self._grid_only(self.client.get("/projects?sort=name").get_data(as_text=True))
        self.assertLess(grid.index("Alpha"), grid.index("Zebra"))
        grid = self._grid_only(self.client.get("/projects").get_data(as_text=True))
        self.assertLess(grid.index("Zebra"), grid.index("Alpha"))  # recent first
        self.assertNotIn("sort=zebra", grid)

    def test_selecting_and_deselecting_a_specialist(self) -> None:
        store = self.app.config["STORE"].for_account(DEFAULT_USER_ID)
        self.assertIsNone(store.selected_agent)
        resp = self.client.post("/agents/debug/select")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(store.selected_agent, "debug")
        # the sticky bar names the pick and its real next step
        html = self.client.get("/agents").get_data(as_text=True)
        self.assertIn("Debug", html)
        self.assertIn("selected — no project is assigned yet", html)
        self.assertIn('class="assign-bar"', html)  # the bar element, not just its CSS
        events = [e for e in self._audit_events() if e.get("event") == "agent_specialist_selected"]
        self.assertEqual(len(events), 1)
        resp = self.client.post("/agents/deselect")
        self.assertEqual(resp.status_code, 303)
        self.assertIsNone(store.selected_agent)
        self.assertNotIn('class="assign-bar"', self.client.get("/agents").get_data(as_text=True))
        resp = self.client.post("/agents/not-an-agent/select")
        self.assertEqual(resp.status_code, 404)

    def test_agent_assignment_creates_a_project_conversation(self) -> None:
        store = self.app.config["STORE"].for_account(DEFAULT_USER_ID)
        project = self.project_store.create(user_id="caleb", name="Fieldnotes")
        resp = self.client.post("/agents/review/assign", data={"project_id": project.id})
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/projects/{project.id}?agent=review", resp.headers["Location"])
        conv = store.project_conversation(project.id, agent_id="review")
        self.assertIsNotNone(conv)
        self.assertEqual(conv.agent_id, "review")
        events = [e for e in self._audit_events() if e.get("event") == "agent_specialist_assigned"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["project_id"], project.id)
        resp = self.client.post("/agents/not-an-agent/assign", data={"project_id": project.id})
        self.assertEqual(resp.status_code, 404)

    def test_agents_page_lists_every_card(self) -> None:
        html = self.client.get("/agents").get_data(as_text=True)
        for name in ("Coding", "Research", "Debug", "Review", "Testing", "Documentation"):
            self.assertIn(name, html)
        for quote in ("Add the no-results search state.", "Compare local search approaches.",
                      "Why are duplicate notes appearing?", "Review this search implementation.",
                      "Test search with an empty notebook.", "Explain how notes are stored."):
            self.assertIn(quote, html)
        self.assertIn("6 specialties", html)
        self.assertIn("Find an agent by specialty", html)

    def test_agents_search_filters_by_specialty(self) -> None:
        html = self.client.get("/agents?q=libraries").get_data(as_text=True)
        self.assertIn("Research", html)
        self.assertNotIn("Debug<", html)
        html = self.client.get("/agents?q=zzz").get_data(as_text=True)
        self.assertIn("No specialty named", html)
        self.assertIn("Coding, Research, Debug, Review, Testing and Documentation", html)


class ProjectTaskLinkTests(unittest.TestCase):
    def test_tasks_attach_to_projects(self) -> None:
        store = Store().for_account(DEFAULT_USER_ID)
        project = ProjectStore().create(user_id="caleb", name="bot")
        task = store.create_task("Fix the crash in checkout.py", project_id=project.id)
        store.create_task("Unrelated standalone task")
        self.assertEqual(task.project_id, project.id)
        self.assertEqual(
            [t.title for t in store.tasks_for_project(project.id)],
            ["Fix the crash in checkout.py"],
        )


if __name__ == "__main__":
    unittest.main()
