"""The Analytics screen, on both stores.

What is pinned here:

  * every figure on the page comes from real records — completions from the
    audited ``task_completion`` marks, decision counts from the store, the
    project rows from the store's own tasks;
  * two accounts never see each other's counts (the tagged audit lines are
    filtered per account exactly like the SQLite analytics rows);
  * the window tabs select a real span and the comparison line appears only
    when there is a real previous window to compare against;
  * Export summary downloads a plain-text file of exactly those numbers;
  * empty, loading and error states render; one copper (primary) action;
    zero inline styles; no hard-coded colours.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from tools.registry import AuditLog

from app.accounts import AccountStore
from app.server import create_app


class FakeProvider:
    def reply(self, history, system_prompt: str) -> str:
        return "canned"

    def reply_stream(self, history, system_prompt: str, **kwargs):
        from providers.base import StreamChunk

        yield StreamChunk(kind="answer", text="canned")


def make_memory_client():
    """In-memory stores plus a per-test audit file, so nothing global is read."""
    audit_dir = tempfile.TemporaryDirectory()
    audit_path = os.path.join(audit_dir.name, "audit.log")
    app = create_app(
        provider=FakeProvider(),
        accounts=AccountStore(iterations=1_000),
        model_audit=AuditLog(audit_path),
    )
    app.config["TESTING"] = True
    return audit_dir, app, app.test_client(), app.config["ACCOUNTS"]


def make_sqlite_client():
    """Production wiring: SQLite stores write per-account analytics rows."""
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
    return client.post("/auth/oauth/github", data={"name": name, "email": email})


def page_style_block(html: str) -> str:
    blocks = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    assert blocks, "the page has no style block"
    return blocks[-1]


class AnalyticsScreenTests(unittest.TestCase):
    use_sqlite = False

    def _make(self):
        if not self.use_sqlite:
            audit_dir, app, client, accounts = make_memory_client()
            self.addCleanup(audit_dir.cleanup)
            return audit_dir, app, client, accounts
        directory, app, client, accounts = make_sqlite_client()
        store = app.config["STORE"]

        def _release_database() -> None:
            self.assertEqual(store.database.open_connection_count, 0)
            store.database.close()

        self.addCleanup(directory.cleanup)
        self.addCleanup(_release_database)
        return directory, app, client, accounts

    def _two_accounts_with_work(self):
        """Ada completes one task; Bob completes two. Counts must not mix."""
        _dir, app, client, accounts = self._make()
        projects = app.config["PROJECTS"]
        store = app.config["STORE"]

        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        fieldnotes = projects.create(user_id=ada["id"], name="Fieldnotes")
        ada_tasks = store.for_account(ada["id"])
        t1 = ada_tasks.create_task("Save an article", project_id=fieldnotes.id)
        t2 = ada_tasks.create_task("Search saved articles", project_id=fieldnotes.id)
        for t in (t1, t2):
            store.for_account(ada["id"]).set_done(t.id, done=False)
        self.assertEqual(
            client.post(
                f"/projects/{fieldnotes.id}/tasks/{t1.id}/complete", data={}
            ).status_code,
            303,
        )
        client.get("/auth/logout")

        sign_in(client, "bob@example.dev", "Bob B")
        bob = accounts.by_email("bob@example.dev")
        budgets = projects.create(user_id=bob["id"], name="Budgets")
        bob_tasks = store.for_account(bob["id"])
        b1 = bob_tasks.create_task("Trim the budget", project_id=budgets.id)
        b2 = bob_tasks.create_task("Split the rent", project_id=budgets.id)
        for t in (b1, b2):
            client.post(f"/projects/{budgets.id}/tasks/{t.id}/complete", data={})
        client.get("/auth/logout")

        sign_in(client)
        return app, client, accounts, fieldnotes, budgets

    # -- real data only ----------------------------------------------------

    def test_the_counts_come_from_the_real_marks(self) -> None:
        app, client, _accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        page = client.get("/analytics?window=all").get_data(as_text=True)
        # Ada completed one task (the audited mark), approved nothing.
        self.assertIn("1 / Tasks completed", page)
        self.assertIn("0 / Changes approved", page)
        self.assertIn("0 / Pending proposals", page)
        self.assertIn("Fieldnotes", page)
        self.assertIn("1 of 2", page)
        self.assertIn("50%", page)
        self.assertIn("Progress reflects completed tasks", page)

    def test_two_accounts_never_see_each_others_counts(self) -> None:
        app, client, accounts, _fieldnotes, budgets = self._two_accounts_with_work()
        ada_page = client.get("/analytics?window=all").get_data(as_text=True)
        self.assertIn("1 / Tasks completed", ada_page)
        self.assertNotIn("Budgets", ada_page)
        self.assertNotIn("Trim the budget", ada_page)

        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        bob_page = client.get("/analytics?window=all").get_data(as_text=True)
        self.assertIn("2 / Tasks completed", bob_page)
        self.assertIn("Budgets", bob_page)
        self.assertNotIn("Fieldnotes", bob_page)

    def test_the_decisions_counts_are_store_state(self) -> None:
        app, client, _accounts, fieldnotes, _budgets = self._two_accounts_with_work()
        store = app.config["STORE"]
        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        task = store.for_account(ada["id"]).create_task(
            "Save an article to the reading list.", project_id=fieldnotes.id
        )
        from tests.test_approval_screen import draft_run

        task.run = draft_run()
        page = client.get("/analytics?window=all").get_data(as_text=True)
        self.assertIn("1 / Pending proposals", page)
        self.assertIn("1 pending · Waiting for your decision", page)
        # Approve it: the approved count moves, pending drops.
        self.assertEqual(
            client.post(f"/api/task/{task.id}/approve", json={"approve": True}).status_code,
            200,
        )
        page = client.get("/analytics?window=all").get_data(as_text=True)
        self.assertIn("1 / Changes approved", page)
        self.assertIn("0 / Pending proposals", page)

    def test_the_ring_share_is_computed_not_sample(self) -> None:
        app, client, _accounts, fieldnotes, _budgets = self._two_accounts_with_work()
        store = app.config["STORE"]
        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        from tests.test_approval_screen import draft_run

        for i in range(3):
            task = store.for_account(ada["id"]).create_task(
                f"Draft {i}", project_id=fieldnotes.id
            )
            task.run = draft_run()
        # 3 pending, 0 approved → 0% approved share (the frame's 67% is sample).
        page = client.get("/analytics?window=all").get_data(as_text=True)
        self.assertIn("0%", page)
        self.assertIn("3 pending", page)

    # -- windows and comparison -------------------------------------------

    def test_the_window_tabs_select_a_real_span(self) -> None:
        app, client, _accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        for window in ("week", "month", "all"):
            page = client.get(f"/analytics?window={window}").get_data(as_text=True)
            self.assertIn("Tasks completed", page, window)
            self.assertIn("ALL PROJECTS", page.upper(), window)
        # An unknown window falls back to the default rather than lying.
        page = client.get("/analytics?window=fortnight").get_data(as_text=True)
        self.assertIn("This week", page)

    def test_the_comparison_line_only_appears_against_real_history(self) -> None:
        app, client, _accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        page = client.get("/analytics?window=week").get_data(as_text=True)
        # One completion today, nothing in the previous week: the line says
        # so in plain words, never "3 more than last week".
        self.assertIn("1 more than last week", page)
        self.assertNotIn("0 more than", page)

    # -- export ------------------------------------------------------------

    def test_export_summary_downloads_the_same_real_numbers(self) -> None:
        app, client, _accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        resp = client.get("/analytics/export?window=all")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["Content-Type"].startswith("text/plain"))
        self.assertIn("orbit-summary-", resp.headers["Content-Disposition"])
        body = resp.get_data(as_text=True)
        self.assertIn("orbit — activity summary", body)
        self.assertIn("Tasks completed (in window): 1", body)
        self.assertIn("Fieldnotes", body)
        self.assertIn("Progress reflects completed tasks", body)

    def test_export_of_another_account_is_that_account_only(self) -> None:
        app, client, accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        body = client.get("/analytics/export?window=all").get_data(as_text=True)
        self.assertIn("Fieldnotes", body)
        self.assertNotIn("Budgets", body)

    # -- states and constraints -------------------------------------------

    def test_the_three_states_render(self) -> None:
        app, client, _accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        loading = client.get("/analytics?view=loading").get_data(as_text=True)
        self.assertIn("Loading your progress", loading)
        error = client.get("/analytics?view=error").get_data(as_text=True)
        self.assertIn("couldn't load your progress", error)
        self.assertIn("Try again", error)

    def test_an_empty_account_is_honest(self) -> None:
        _dir, app, client, accounts = self._make()
        sign_in(client)
        page = client.get("/analytics?window=all").get_data(as_text=True)
        self.assertIn("0 / Tasks completed", page)
        self.assertIn("No completions recorded", page)
        self.assertIn("No projects yet", page)

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        app, client, _accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        for url in ("/analytics", "/analytics?window=month", "/analytics?window=all",
                    "/analytics?view=loading", "/analytics?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), url)
            self.assertIsNone(re.search(r"\brgba?\(", block), url)
            self.assertIsNone(re.search(r"\bhsla?\(", block), url)

    def test_exactly_one_primary_action_per_state(self) -> None:
        app, client, _accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        for url in ("/analytics", "/analytics?window=all", "/analytics?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertEqual(page.count("o-btn--primary"), 1, url)
        loading = client.get("/analytics?view=loading").get_data(as_text=True)
        self.assertEqual(loading.count("o-btn--primary"), 0)

    def test_the_screen_itself_adds_no_copper(self) -> None:
        app, client, _accounts, _fieldnotes, _budgets = self._two_accounts_with_work()
        page = client.get("/analytics").get_data(as_text=True)
        self.assertNotIn("--copper", page_style_block(page))
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)


class AnalyticsScreenSQLiteTests(AnalyticsScreenTests):
    """Every Analytics check again, against the SQLite stores production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_store import SQLiteStore

        _dir, app, _client, accounts = self._make()
        self.assertIsInstance(app.config["STORE"], SQLiteStore)


if __name__ == "__main__":
    unittest.main()
