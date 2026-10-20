"""The Projects screen, on both stores.

Two layers, following the dashboard's own pattern:

  * the in-memory app factory (``create_app()`` with an in-memory
    ``ProjectStore``) — the same harness the rest of the route tests use;
  * the SQLite subclass, which re-runs every check against the stores
    production boots, so the two implementations cannot drift apart.

What is pinned here is the product rule, not the formatting:

  * two accounts with different projects see only their own, and neither
    search nor the status filter can leak the other's names;
  * search matches name and description, the four statuses filter exactly,
    the no-results panel is distinct from the new-account empty state;
  * the screens carry no inline styles and no hard-coded colours, and
    exactly one copper (primary) action.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from app.accounts import AccountStore
from app.server import create_app


class FakeProvider:
    """The smallest provider the app needs; these screens never call it."""

    def reply(self, history, system_prompt: str) -> str:
        return "canned"

    def reply_stream(self, history, system_prompt: str, **kwargs):
        from providers.base import StreamChunk

        yield StreamChunk(kind="answer", text="canned")


def make_memory_client() -> tuple:
    app = create_app(provider=FakeProvider(), accounts=AccountStore(iterations=1_000))
    app.config["TESTING"] = True
    return app, app.test_client(), app.config["ACCOUNTS"]


def make_sqlite_client():
    """The same app, wired the way production boots: SQLite-backed stores."""
    from app.sqlite_accounts import SQLiteAccountStore
    from app.sqlite_projects import SQLiteProjectStore
    from app.sqlite_store import SQLiteStore

    directory = tempfile.TemporaryDirectory()
    store = SQLiteStore(os.path.join(directory.name, "orbit.sqlite3"))
    app = create_app(provider=FakeProvider())
    app.config["TESTING"] = True
    app.config["STORE"] = store
    accounts = SQLiteAccountStore(store.database, iterations=1_000)
    app.config["ACCOUNTS"] = accounts
    app.config["PROJECTS"] = SQLiteProjectStore(store.database)
    return directory, app, app.test_client(), accounts


def sign_in(client, email: str = "ada@example.dev", name: str = "Ada A"):
    return client.post(f"/auth/oauth/github", data={"name": name, "email": email})


def page_style_block(html: str) -> str:
    """The screen's own <style> block (the shell ships the first one)."""
    blocks = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    assert blocks, "the page has no style block"
    return blocks[-1]


class ProjectsScreenTests(unittest.TestCase):
    use_sqlite = False

    def _make(self):
        if not self.use_sqlite:
            return make_memory_client()
        directory, app, client, accounts = make_sqlite_client()
        store = app.config["STORE"]

        def _release_database() -> None:
            # addCleanup is LIFO: this runs before the directory is removed.
            self.assertEqual(store.database.open_connection_count, 0)
            store.database.close()

        self.addCleanup(directory.cleanup)
        self.addCleanup(_release_database)
        return app, client, accounts

    def _two_accounts(self):
        """Ada has two projects, Bob has one; the client is signed in as Bob."""
        app, client, accounts = self._make()
        projects = app.config["PROJECTS"]

        sign_in(client, "ada@example.dev", "Ada A")
        ada = accounts.by_email("ada@example.dev")
        projects.create(
            user_id=ada["id"],
            name="Alpha notes",
            description="Ada's private reading list",
        )
        archived = projects.create(user_id=ada["id"], name="Alpha archive")
        projects.set_status(archived.id, ada["id"], "archived")
        client.get("/auth/logout")

        sign_in(client, "bob@example.dev", "Bob B")
        bob = accounts.by_email("bob@example.dev")
        projects.create(
            user_id=bob["id"],
            name="Beta budget",
            description="Bob's monthly numbers",
        )
        return app, client, accounts

    # -- isolation ---------------------------------------------------------

    def test_each_account_sees_only_its_own_projects(self) -> None:
        _app, client, _accounts = self._two_accounts()
        page = client.get("/projects").get_data(as_text=True)
        self.assertIn("Beta budget", page)
        self.assertNotIn("Alpha notes", page)
        self.assertNotIn("Alpha archive", page)
        self.assertIn("monthly numbers", page)

    def test_search_never_reaches_another_accounts_names(self) -> None:
        _app, client, _accounts = self._two_accounts()
        for needle in ("Alpha", "alpha", "reading list", "private"):
            page = client.get(f"/projects?q={needle}").get_data(as_text=True)
            self.assertIn("Nothing matches", page, needle)
            for leak in ("Alpha notes", "Alpha archive", "Ada's private reading list"):
                self.assertNotIn(leak, page, needle)

    def test_the_status_filter_never_reaches_another_accounts_names(self) -> None:
        _app, client, _accounts = self._two_accounts()
        page = client.get("/projects?status=archived").get_data(as_text=True)
        self.assertNotIn("Alpha archive", page)
        # Bob has nothing archived, so this is the no-results panel, not an error.
        self.assertIn("Nothing matches", page)
        self.assertIn("Archived", page)  # the status chosen is named

    # -- search and filter -------------------------------------------------

    def test_search_matches_name_and_description(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        projects = app.config["PROJECTS"]
        projects.create(
            user_id=ada["id"], name="Fieldnotes", description="A reading home"
        )
        projects.create(user_id=ada["id"], name="Ledger", description="Monthly budgets")

        by_name = client.get("/projects?q=field").get_data(as_text=True)
        self.assertIn("Fieldnotes", by_name)
        self.assertNotIn("Monthly budgets", by_name)

        by_description = client.get("/projects?q=budget").get_data(as_text=True)
        self.assertIn("Ledger", by_description)
        self.assertNotIn("Fieldnotes", by_description)

    def test_status_filter_uses_the_four_real_statuses(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        projects = app.config["PROJECTS"]
        live = projects.create(user_id=ada["id"], name="Shipped thing")
        projects.set_status(live.id, ada["id"], "live")
        idea = projects.create(user_id=ada["id"], name="Someday thing")
        projects.set_status(idea.id, ada["id"], "idea")
        projects.create(user_id=ada["id"], name="Building thing")

        page = client.get("/projects").get_data(as_text=True)
        for label in ("Live", "Build in Progress", "Archived", "Saved Ideas"):
            self.assertIn(label, page)

        live_page = client.get("/projects?status=live").get_data(as_text=True)
        self.assertIn("Shipped thing", live_page)
        self.assertNotIn("Someday thing", live_page)
        idea_page = client.get("/projects?status=idea").get_data(as_text=True)
        self.assertIn("Someday thing", idea_page)
        self.assertNotIn("Shipped thing", idea_page)

        # An unknown status is ignored rather than emptying the grid.
        bogus = client.get("/projects?status=bogus").get_data(as_text=True)
        self.assertIn("Building thing", bogus)

    def test_a_new_account_gets_the_guiding_empty_state(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        page = client.get("/projects").get_data(as_text=True)
        self.assertIn("No projects yet.", page)
        self.assertNotIn("Nothing matches", page)
        # Both paths out, and the guidance names them.
        self.assertIn('href="/create"', page)
        self.assertIn('href="/projects/import"', page)

    def test_a_search_that_matches_nothing_gets_its_own_state(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        app.config["PROJECTS"].create(user_id=ada["id"], name="Fieldnotes")
        page = client.get("/projects?q=nothing-like-this").get_data(as_text=True)
        self.assertIn("Nothing matches", page)
        self.assertIn("No project matches", page)
        self.assertIn('href="/projects"', page)  # clear the filters
        self.assertNotIn("No projects yet.", page)
        self.assertNotIn("Fieldnotes", page)

    def test_the_loading_and_error_views_render(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        loading = client.get("/projects?view=loading").get_data(as_text=True)
        self.assertIn("pj-skel", loading)
        self.assertIn("Loading your projects", loading)
        error = client.get("/projects?view=error").get_data(as_text=True)
        self.assertIn("couldn't load your projects", error)
        self.assertIn("Try again", error)

    def test_the_card_shows_only_stored_values(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        projects = app.config["PROJECTS"]
        with_description = projects.create(
            user_id=ada["id"], name="Fieldnotes", description="A reading home"
        )
        projects.create(user_id=ada["id"], name="Bare project", description="")
        view = app.config["STORE"].for_account(ada["id"])
        view.create_task("Save the first article", project_id=with_description.id)

        page = client.get("/projects").get_data(as_text=True)
        self.assertIn("A reading home", page)
        # A project without a description gets no invented line.
        self.assertNotIn("No description yet.", page)
        self.assertIn("0 of 1 tasks complete", page)
        # The frame's label and its arrow, into the real workspace route.
        self.assertIn('Open project <span class="pj-card__arrow" aria-hidden="true">↗</span>', page)
        self.assertNotIn("Open Project Map", page)
        self.assertIn(f'href="/projects/{with_description.id}"', page)
        # Proposals have no project link in this build, so no pill is drawn.
        self.assertNotIn("proposal", page.lower())

    # -- the screens' own constraints -------------------------------------

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        _app, client, _accounts = self._two_accounts()
        page = client.get("/projects").get_data(as_text=True)
        self.assertNotIn('style="', page)
        block = page_style_block(page)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), "hex colour")
        self.assertIsNone(re.search(r"\brgba?\(", block), "rgb colour")
        self.assertIsNone(re.search(r"\bhsla?\(", block), "hsl colour")

    def test_the_page_has_exactly_one_primary_action(self) -> None:
        _app, client, _accounts = self._two_accounts()
        for url in ("/projects", "/projects?status=archived", "/projects?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertEqual(page.count("o-btn--primary"), 1, url)


class ProjectsScreenSQLiteTests(ProjectsScreenTests):
    """Every Projects check again, against the SQLite store production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_accounts import SQLiteAccountStore
        from app.sqlite_projects import SQLiteProjectStore

        app, _client, accounts = self._make()
        self.assertIsInstance(app.config["PROJECTS"], SQLiteProjectStore)
        self.assertIsInstance(accounts, SQLiteAccountStore)
