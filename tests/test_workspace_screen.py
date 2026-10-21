"""The Workspace screen, on both stores.

Follows test_projects_screen.py's two-layer harness (in-memory factory, then
the SQLite stores production boots) and pins what the group's rules ask of
every screen:

  * two accounts never see each other's projects, tasks or conversations —
    the workspace 404s on another account's project, and a task id from the
    other account's map renders nothing here;
  * empty, loading and error states all render, derived from the dashboard's
    own set;
  * exactly one copper (primary) action per rendered state that has actions;
  * zero inline styles and no hard-coded colours in the screen's style block.
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


class WorkspaceScreenTests(unittest.TestCase):
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
        """Ada owns Fieldnotes with a task and a thread; Bob owns Budgets."""
        app, client, accounts = self._make()
        projects = app.config["PROJECTS"]

        sign_in(client, "ada@example.dev", "Ada A")
        ada = accounts.by_email("ada@example.dev")
        fieldnotes = projects.create(
            user_id=ada["id"], name="Fieldnotes", description="A reading home"
        )
        ada_store = app.config["STORE"].for_account(ada["id"])
        task = ada_store.create_task("Save an article", project_id=fieldnotes.id)
        ada_store.project_conversation(fieldnotes.id, create=True, agent_id="main")
        client.get("/auth/logout")

        sign_in(client, "bob@example.dev", "Bob B")
        bob = accounts.by_email("bob@example.dev")
        budgets = projects.create(user_id=bob["id"], name="Budgets")
        bob_store = app.config["STORE"].for_account(bob["id"])
        bob_task = bob_store.create_task("Bob's private task", project_id=budgets.id)
        return app, client, accounts, fieldnotes, task, budgets, bob_task

    # -- isolation ---------------------------------------------------------

    def test_another_accounts_project_is_not_a_workspace(self) -> None:
        app, client, _accounts, fieldnotes, _task, _budgets, _bob_task = self._two_accounts()
        resp = client.get(f"/projects/{fieldnotes.id}")
        self.assertEqual(resp.status_code, 404)
        page = resp.get_data(as_text=True)
        for leak in ("Fieldnotes", "Save an article", "A reading home"):
            self.assertNotIn(leak, page)

    def test_another_accounts_task_opens_no_details(self) -> None:
        app, client, _accounts, fieldnotes, _task, budgets, bob_task = self._two_accounts()
        page = client.get(f"/projects/{budgets.id}?task={_task.id}").get_data(as_text=True)
        self.assertIn("Budgets", page)
        self.assertNotIn("Save an article", page)
        # and Bob's own task does open, with its own focus line
        own = client.get(
            f"/projects/{budgets.id}?task={bob_task.id}"
        ).get_data(as_text=True)
        self.assertIn("Focused on: Bob&#39;s private task", own)

    def test_another_accounts_conversation_never_renders_here(self) -> None:
        app, client, accounts, fieldnotes, _task, budgets, _bob_task = self._two_accounts()
        ada = accounts.by_email("ada@example.dev")
        ada_store = app.config["STORE"].for_account(ada["id"])
        conv = ada_store.project_conversation(fieldnotes.id, agent_id="main")
        from app.store import ChatMessage

        ada_store.append_message(
            conv.id, ChatMessage(who="Ada", time="09:00", body="Ada's private thread line", is_orbit=False)
        )
        page = client.get(f"/projects/{budgets.id}").get_data(as_text=True)
        self.assertNotIn("Ada's private thread line", page)

    # -- states ------------------------------------------------------------

    def test_a_project_with_nothing_yet_gets_honest_empty_states(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        project = app.config["PROJECTS"].create(user_id=ada["id"], name="Blank")
        page = client.get(f"/projects/{project.id}").get_data(as_text=True)
        self.assertIn("The map is empty.", page)
        self.assertIn("No conversation yet", page)
        self.assertIn("Nothing selected yet.", page)
        empty_lines = {
            "tasks": "No tasks yet.",
            "files": "No files named yet.",
            "activity": "No activity yet.",
        }
        for tab, line in empty_lines.items():
            tabpage = client.get(
                f"/projects/{project.id}?tab={tab}"
            ).get_data(as_text=True)
            self.assertIn(line, tabpage)

    def test_the_loading_and_error_views_render(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        project = app.config["PROJECTS"].create(user_id=ada["id"], name="Fieldnotes")
        loading = client.get(f"/projects/{project.id}?view=loading").get_data(as_text=True)
        self.assertIn("ws-skel", loading)
        self.assertIn("Loading the workspace", loading)
        error = client.get(f"/projects/{project.id}?view=error").get_data(as_text=True)
        self.assertIn("couldn't load this workspace", error)
        self.assertIn("Try again", error)
        self.assertIn('href="/projects"', error)

    def test_a_waiting_draft_makes_the_review_button_the_primary(self) -> None:
        from app.taskplan import IDLE

        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        project = app.config["PROJECTS"].create(user_id=ada["id"], name="Fieldnotes")
        store = app.config["STORE"].for_account(ada["id"])
        one = store.create_task("Save an article", project_id=project.id, stage="Build")
        one.run = __import__("app.taskplan", fromlist=["new_run"]).new_run(one.description)
        one.run.status = IDLE
        page = client.get(f"/projects/{project.id}").get_data(as_text=True)
        self.assertIn("Review 1 proposal", page)
        self.assertIn("A proposal is ready.", page)
        self.assertIn("Needs approval", page)
        # the stage capsule carries the copper dot and the count
        self.assertIn("1 needs approval", page)

    # -- the screens' own constraints -------------------------------------

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        _app, client, _accounts, fieldnotes, _task, budgets, _bob_task = self._two_accounts()
        for url in (f"/projects/{budgets.id}", f"/projects/{budgets.id}?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), "hex colour")
            self.assertIsNone(re.search(r"\brgba?\(", block), "rgb colour")
            self.assertIsNone(re.search(r"\bhsla?\(", block), "hsl colour")

    def test_the_page_has_exactly_one_primary_action(self) -> None:
        _app, client, _accounts, fieldnotes, _task, budgets, _bob_task = self._two_accounts()
        for url in (
            f"/projects/{budgets.id}",
            f"/projects/{budgets.id}?tab=tasks",
            f"/projects/{budgets.id}?view=error",
        ):
            page = client.get(url).get_data(as_text=True)
            self.assertEqual(page.count("o-btn--primary"), 1, url)

    def test_the_screen_itself_adds_no_copper(self) -> None:
        """Copper on this screen is the shell's nav bar plus the shared dot.

        The screen's own style block never reaches for the copper token, and
        outside the style blocks the word appears nowhere: the capsule dot
        and the primary button carry it through shared classes only.
        """
        _app, client, _accounts, _fieldnotes, _task, budgets, _bob_task = self._two_accounts()
        page = client.get(f"/projects/{budgets.id}").get_data(as_text=True)
        block = page_style_block(page)
        self.assertNotIn("--copper", block)
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)


class WorkspaceScreenSQLiteTests(WorkspaceScreenTests):
    """Every Workspace check again, against the SQLite store production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_accounts import SQLiteAccountStore
        from app.sqlite_projects import SQLiteProjectStore

        app, _client, _accounts = self._make()
        self.assertIsInstance(app.config["PROJECTS"], SQLiteProjectStore)
        self.assertIsInstance(app.config["ACCOUNTS"], SQLiteAccountStore)


if __name__ == "__main__":
    unittest.main()
