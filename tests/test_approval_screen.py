"""The Approval review screen, on both stores.

Follows test_projects_screen.py's two-layer harness (in-memory factory, then
the SQLite stores production boots) and pins what the group's rules ask of
this screen — the most important one of group 3:

  * the change review shows only stored facts: the task's request in plain
    language, the before/after the gate really guarantees, the files the run
    named (with where their names came from), and the task it belongs to;
  * Approve is wired to the existing ``/api/task/<id>/approve`` then
    ``/apply`` endpoints and Reject to ``/api/task/<id>/reject`` — the
    "approve: true" gate is unchanged, and nothing is applied before
    approval (403, flag stays false);
  * another account can neither see the review (404, no leak of titles,
    requests or file names) nor decide it (approve/apply/reject all 404);
  * reject leaves files untouched: the run's file list is byte-identical,
    nothing is approved or applied, and apply still answers 403;
  * empty, loading and error states render; exactly one copper (primary)
    action per rendered state that has one; zero inline styles and no
    hard-coded colours.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from app.accounts import AccountStore
from app.server import create_app
from app.taskplan import IDLE, add_files, finish_run, new_run


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


def draft_run(description: str = "Save an article to the reading list."):
    """A finished run with named files — exactly what a proposal waits on."""
    run = new_run(description)
    add_files(run, ["src/components/ReadingList.tsx"], "request")
    add_files(run, ["src/lib/articleStorage.ts"], "orbit")
    finish_run(run.steps)
    run.status = IDLE
    return run


class ApprovalScreenTests(unittest.TestCase):
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
        """Ada owns Fieldnotes with a finished draft; Bob owns Budgets.

        The client is left signed in as Ada (the owner under test); Bob's
        draft is the foreign one the isolation checks reach for.
        """
        app, client, accounts = self._make()
        projects = app.config["PROJECTS"]

        sign_in(client, "ada@example.dev", "Ada A")
        ada = accounts.by_email("ada@example.dev")
        fieldnotes = projects.create(
            user_id=ada["id"], name="Fieldnotes", description="A reading home"
        )
        ada_store = app.config["STORE"].for_account(ada["id"])
        task = ada_store.create_task(
            "Save an article to the reading list.", project_id=fieldnotes.id
        )
        task.run = draft_run()
        client.get("/auth/logout")

        sign_in(client, "bob@example.dev", "Bob B")
        bob = accounts.by_email("bob@example.dev")
        budgets = projects.create(user_id=bob["id"], name="Budgets")
        bob_store = app.config["STORE"].for_account(bob["id"])
        bob_task = bob_store.create_task(
            "Keep the budget hidden.", project_id=budgets.id
        )
        bob_task.run = draft_run("Keep the budget hidden.")
        client.get("/auth/logout")

        sign_in(client, "ada@example.dev", "Ada A")
        return app, client, accounts, fieldnotes, task, budgets, bob_task

    def _as_bob(self, app, client, accounts):
        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        return accounts.by_email("bob@example.dev")

    # -- isolation ---------------------------------------------------------

    def test_another_accounts_draft_is_not_a_review(self) -> None:
        app, client, accounts, fieldnotes, task, _budgets, bob_task = self._two_accounts()
        self._as_bob(app, client, accounts)
        resp = client.get(f"/approvals?task={task.id}")
        self.assertEqual(resp.status_code, 404)
        page = resp.get_data(as_text=True)
        for leak in ("Save an article", "ReadingList", "Fieldnotes", "reading home"):
            self.assertNotIn(leak, page)
        # Bob's own draft is the one the queue serves.
        own = client.get("/approvals").get_data(as_text=True)
        self.assertIn("Keep the budget hidden.", own)

    def test_another_account_cannot_approve_apply_or_reject(self) -> None:
        app, client, accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        self._as_bob(app, client, accounts)
        for path, body in (
            (f"/api/task/{task.id}/approve", {"approve": True}),
            (f"/api/task/{task.id}/apply", {}),
            (f"/api/task/{task.id}/reject", {}),
        ):
            resp = client.post(path, json=body)
            self.assertEqual(resp.status_code, 404, path)
            self.assertEqual(resp.get_json(), {"error": "unknown task"}, path)
        # And Ada's draft was not decided by any of it.
        client.get("/auth/logout")
        sign_in(client, "ada@example.dev", "Ada A")
        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        fresh = app.config["STORE"].for_account(ada["id"]).get_task(task.id)
        self.assertFalse(fresh.approved)
        self.assertFalse(fresh.applied)
        self.assertFalse(fresh.rejected)

    # -- the approval gate, unchanged --------------------------------------

    def test_nothing_is_applied_before_approval(self) -> None:
        app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        # The review page states it and shows the un-applied before/after.
        page = client.get(f"/approvals?task={task.id}").get_data(as_text=True)
        self.assertIn("Nothing changes until you approve.", page)
        self.assertIn("Before / current", page)
        self.assertIn("Not applied", page)
        # Apply before approval is refused, and the flag stays false.
        resp = client.post(f"/api/task/{task.id}/apply", json={})
        self.assertEqual(resp.status_code, 403)
        body = resp.get_json()
        self.assertFalse(body["approved"])
        self.assertIn("explicit approval", body["error"])
        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        fresh = app.config["STORE"].for_account(ada["id"]).get_task(task.id)
        self.assertFalse(fresh.applied)

    def test_the_gate_still_requires_the_literal_true(self) -> None:
        app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        for body in ({}, {"approve": "yes"}, {"approve": 1}):
            resp = client.post(f"/api/task/{task.id}/approve", json=body)
            self.assertEqual(resp.status_code, 400, body)
            self.assertEqual(resp.get_json()["approved"], False)
        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        fresh = app.config["STORE"].for_account(ada["id"]).get_task(task.id)
        self.assertFalse(fresh.approved)

    def test_the_approve_flow_is_the_two_endpoints_in_order(self) -> None:
        """What the Approve button runs: approve (with the flag) then apply."""
        app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        approved = client.post(
            f"/api/task/{task.id}/approve", json={"approve": True}
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.get_json(), {"approved": True})
        applied = client.post(f"/api/task/{task.id}/apply", json={})
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.get_json(), {"applied": True, "approved": True})
        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        fresh = app.config["STORE"].for_account(ada["id"]).get_task(task.id)
        self.assertTrue(fresh.approved)
        self.assertTrue(fresh.applied)
        # After the decision the page reports it instead of offering again.
        page = client.get(f"/approvals?task={task.id}").get_data(as_text=True)
        self.assertIn("You approved these changes.", page)
        self.assertIn("Decision recorded", page)
        self.assertNotIn("Reject proposal", page)

    def test_reject_leaves_files_untouched(self) -> None:
        app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        before_files = [(item.name, item.source) for item in task.run.files]
        self.assertTrue(before_files)

        resp = client.post(f"/api/task/{task.id}/reject", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"rejected": True, "approved": False})

        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        fresh = app.config["STORE"].for_account(ada["id"]).get_task(task.id)
        self.assertEqual(
            [(item.name, item.source) for item in fresh.run.files], before_files
        )
        self.assertFalse(fresh.approved)
        self.assertFalse(fresh.applied)
        self.assertTrue(fresh.rejected)
        # Applying a rejected draft is still behind the same gate.
        self.assertEqual(client.post(f"/api/task/{task.id}/apply", json={}).status_code, 403)
        self.assertFalse(fresh.applied)
        # And the page reports the decision.
        page = client.get(f"/approvals?task={task.id}").get_data(as_text=True)
        self.assertIn("You rejected this proposal.", page)
        self.assertIn("exactly as you left them", page)

    def test_a_rejected_draft_leaves_the_queue(self) -> None:
        app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        self.assertIn("Proposal 1 of 1", client.get("/approvals").get_data(as_text=True))
        client.post(f"/api/task/{task.id}/reject", json={})
        page = client.get("/approvals").get_data(as_text=True)
        self.assertIn("Nothing waits for your approval.", page)
        # The rail's count follows the same queue.
        self.assertNotIn('class="app-nav__count"', page)

    def test_a_finished_turn_proposes_a_fresh_draft(self) -> None:
        """A rejection decides one draft; the next turn is a new proposal."""
        app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        client.post(f"/api/task/{task.id}/reject", json={})
        resp = client.post(
            f"/api/task/{task.id}/stream", json={"message": "Here is a revision."}
        )
        self.assertEqual(resp.status_code, 200)
        # The test client streams lazily: reading the body runs the turn to
        # its end (where the fresh-draft reset happens).
        self.assertIn("Waiting for your input", resp.get_data(as_text=True))
        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        fresh = app.config["STORE"].for_account(ada["id"]).get_task(task.id)
        self.assertFalse(fresh.rejected)
        page = client.get("/approvals").get_data(as_text=True)
        self.assertIn("Proposal 1 of 1", page)

    # -- the screen shows real data ----------------------------------------

    def test_the_review_shows_the_request_files_and_task(self) -> None:
        app, client, _accounts, fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        page = client.get(f"/approvals?task={task.id}").get_data(as_text=True)
        # Plain language: the real request and title.
        self.assertIn("Save an article", page)
        self.assertIn("Save an article to the reading list.", page)
        # The files it names, with where each name came from.
        self.assertIn("src/components/ReadingList.tsx", page)
        self.assertIn("named in your request", page)
        self.assertIn("src/lib/articleStorage.ts", page)
        self.assertIn("mentioned by Orbit", page)
        # The task it belongs to, and the project crumb.
        self.assertIn(f'href="/task/{task.id}"', page)
        self.assertIn("Fieldnotes", page)
        self.assertIn("Proposal 1 of 1", page)
        # No diffs exist in this build, so the screen says so instead of
        # drawing sample code blocks.
        self.assertIn("stores no diffs", page)
        # The rail's guarantees are the gate's real guarantees.
        self.assertIn("Nothing is applied until you approve", page)
        self.assertIn("Nothing is committed, pushed or published", page)

    def test_the_rail_count_is_the_real_pending_queue(self) -> None:
        app, client, _accounts, fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        page = client.get("/approvals").get_data(as_text=True)
        self.assertIn('class="app-nav__count">1</span>', page)
        self.assertIn("Proposal 1 of 1", page)
        # A second draft raises the count and makes the queue navigable.
        ada = app.config["ACCOUNTS"].by_email("ada@example.dev")
        ada_store = app.config["STORE"].for_account(ada["id"])
        second = ada_store.create_task("Search saved articles", project_id=fieldnotes.id)
        second.run = draft_run("Add search over saved articles.")
        page = client.get("/approvals").get_data(as_text=True)
        self.assertIn('class="app-nav__count">2</span>', page)
        self.assertIn("Proposal 1 of 2", page)
        self.assertIn("Next", page)

    def test_a_task_without_a_run_says_so(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        task = app.config["STORE"].for_account(ada["id"]).create_task("An idea")
        page = client.get(f"/approvals?task={task.id}").get_data(as_text=True)
        self.assertIn("This task has no draft yet.", page)
        self.assertIn(f'href="/task/{task.id}"', page)

    # -- states ------------------------------------------------------------

    def test_the_three_states_render(self) -> None:
        _app, client, _accounts, _fieldnotes, _task, _budgets, _bob_task = self._two_accounts()
        loading = client.get("/approvals?view=loading").get_data(as_text=True)
        self.assertIn("Loading the change review", loading)
        error = client.get("/approvals?view=error").get_data(as_text=True)
        self.assertIn("couldn't load the change review", error)
        self.assertIn("Try again", error)
        self.assertIn("no files were touched", error)

    def test_an_empty_queue_is_honest(self) -> None:
        app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        client.post(f"/api/task/{task.id}/reject", json={})
        page = client.get("/approvals").get_data(as_text=True)
        self.assertIn("Nothing waits for your approval.", page)
        self.assertIn("Nothing runs without your yes.", page)

    # -- the screens' own constraints -------------------------------------

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        _app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        for url in (
            "/approvals",
            f"/approvals?task={task.id}",
            "/approvals?view=loading",
            "/approvals?view=error",
        ):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), url)
            self.assertIsNone(re.search(r"\brgba?\(", block), url)
            self.assertIsNone(re.search(r"\bhsla?\(", block), url)

    def test_exactly_one_primary_action_per_state(self) -> None:
        _app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        # Review: the calm Approve. Error: Try again. Empty: Open projects.
        for url in (
            "/approvals",
            f"/approvals?task={task.id}",
            "/approvals?view=error",
        ):
            page = client.get(url).get_data(as_text=True)
            self.assertEqual(page.count("o-btn--primary"), 1, url)
        # The loading skeleton has no actions at all.
        loading = client.get("/approvals?view=loading").get_data(as_text=True)
        self.assertEqual(loading.count("o-btn--primary"), 0)

    def test_the_screen_itself_adds_no_copper(self) -> None:
        """Copper here is the shell's nav bar plus the one primary button."""
        _app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        page = client.get(f"/approvals?task={task.id}").get_data(as_text=True)
        block = page_style_block(page)
        self.assertNotIn("--copper", block)
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)

    def test_the_buttons_are_wired_to_the_real_endpoints(self) -> None:
        _app, client, _accounts, _fieldnotes, task, _budgets, _bob_task = self._two_accounts()
        page = client.get(f"/approvals?task={task.id}").get_data(as_text=True)
        self.assertIn(f'data-approve-url="/api/task/{task.id}/approve"', page)
        self.assertIn(f'data-apply-url="/api/task/{task.id}/apply"', page)
        self.assertIn(f'data-reject-url="/api/task/{task.id}/reject"', page)
        # The Approve button is gated by the review checkbox, not by a fake
        # confirmation: it starts disabled and the client enables it.
        self.assertIn('id="arApprove" disabled', page)
        self.assertIn('id="arConfirm"', page)
        script = open(
            os.path.join(os.path.dirname(__file__), "..", "app", "static", "approvals.js"),
            encoding="utf-8",
        ).read()
        # Approve then apply, in that order, with the literal approval flag.
        self.assertLess(
            script.index("post(approveUrl, { approve: true })"),
            script.index("return post(applyUrl, {})"),
        )
        self.assertIn("post(rejectUrl, {})", script)


class ApprovalScreenSQLiteTests(ApprovalScreenTests):
    """Every Approval review check again, against the SQLite store production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_accounts import SQLiteAccountStore
        from app.sqlite_store import SQLiteStore

        app, _client, accounts = self._make()
        self.assertIsInstance(app.config["STORE"], SQLiteStore)
        self.assertIsInstance(accounts, SQLiteAccountStore)

    def test_the_decision_survives_a_reload_from_the_database(self) -> None:
        """Reject persists: a restart must not resurrect the draft."""
        from app.sqlite_projects import SQLiteProjectStore
        from app.sqlite_store import SQLiteStore as FreshStore

        directory, app, client, accounts = make_sqlite_client()
        self.addCleanup(directory.cleanup)
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        store = app.config["STORE"]
        task = store.for_account(ada["id"]).create_task(
            "Save an article to the reading list."
        )
        task.run = draft_run()
        self.assertEqual(client.post(f"/api/task/{task.id}/reject", json={}).status_code, 200)
        db_path = store.database.path
        store.database.close()

        fresh_store = FreshStore(db_path)
        self.addCleanup(fresh_store.database.close)
        fresh = fresh_store.for_account(ada["id"]).get_task(task.id)
        self.assertIsNotNone(fresh)
        assert fresh is not None
        self.assertTrue(fresh.rejected)
        self.assertFalse(fresh.approved)
        self.assertFalse(fresh.applied)
        self.assertEqual(
            [item.name for item in fresh.run.files],
            ["src/components/ReadingList.tsx", "src/lib/articleStorage.ts"],
        )


if __name__ == "__main__":
    unittest.main()
