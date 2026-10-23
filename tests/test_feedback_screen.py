"""The Feedback screen, on both stores — derived from the Settings frame.

There is no Feedback frame in design/ORBIT UI___2; the group's rule is to
derive it from the Settings frame and flag it. What is pinned here:

  * the screen keeps the Settings frame's bones (headline block, field
    cards, two-column body with an honest side rail, one copper button)
    and says in its own template that it is derived;
  * the form posts to the real /feedback handler and the list is the
    account's own sent notes — the floating widget writes to the same
    list, and "No notes yet" is the honest empty state;
  * two accounts never see each other's notes;
  * empty, loading and error states render; exactly one copper (primary)
    action; zero inline styles; no hard-coded colours.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from app.accounts import FEEDBACK_KINDS, AccountStore
from app.server import create_app


class FakeProvider:
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


class FeedbackScreenTests(unittest.TestCase):
    use_sqlite = False

    def _make(self):
        if not self.use_sqlite:
            return make_memory_client()
        directory, app, client, accounts = make_sqlite_client()
        store = app.config["STORE"]

        def _release_database() -> None:
            self.assertEqual(store.database.open_connection_count, 0)
            store.database.close()

        self.addCleanup(directory.cleanup)
        self.addCleanup(_release_database)
        return app, client, accounts

    def _signed_in(self):
        app, client, accounts = self._make()
        sign_in(client)
        return app, client, accounts

    # -- derived from Settings, and honest about it ------------------------

    def test_the_template_says_it_is_derived_from_the_settings_frame(self) -> None:
        source = open(
            os.path.join(
                os.path.dirname(__file__), "..", "app", "templates", "feedback.html"
            ),
            encoding="utf-8",
        ).read()
        self.assertIn("DERIVED from Settings", source)
        self.assertIn("Feedback frame in design/ORBIT UI___2", source)

    def test_the_form_and_list_are_the_real_store(self) -> None:
        app, client, _accounts = self._signed_in()
        page = client.get("/feedback").get_data(as_text=True)
        for label in FEEDBACK_KINDS.values():
            self.assertIn(label, page)
        self.assertIn('action="/feedback"', page)
        self.assertIn("No notes yet", page)
        resp = client.post(
            "/feedback",
            data={"kind": "bug", "title": "Dark mode flashes", "body": "On first paint."},
        )
        self.assertIn("sent=1", resp.headers["Location"])
        page = client.get("/feedback?sent=1").get_data(as_text=True)
        self.assertIn("Thank you — your feedback is in.", page)
        self.assertIn("Dark mode flashes", page)
        self.assertIn("On first paint.", page)
        self.assertNotIn("No notes yet", page)

    def test_the_widget_writes_to_the_same_list(self) -> None:
        app, client, _accounts = self._signed_in()
        resp = client.post(
            "/api/feedback",
            json={"kind": "feature", "title": "Search that remembers", "body": "Please.", "page": "/projects"},
        )
        self.assertEqual(resp.status_code, 200)
        page = client.get("/feedback").get_data(as_text=True)
        self.assertIn("Search that remembers", page)

    def test_two_accounts_never_see_each_others_notes(self) -> None:
        app, client, accounts = self._signed_in()
        client.post(
            "/feedback",
            data={"kind": "general", "title": "Ada's private note", "body": "Just mine."},
        )
        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        page = client.get("/feedback").get_data(as_text=True)
        self.assertNotIn("Ada&#39;s private note", page)
        self.assertNotIn("Ada's private note", page)
        self.assertIn("No notes yet", page)
        # And Bob's own send lands only on Bob's list.
        client.post(
            "/feedback",
            data={"kind": "general", "title": "Bob's note", "body": "Bob only."},
        )
        bob_page = client.get("/feedback").get_data(as_text=True)
        self.assertTrue(
            "Bob's note" in bob_page or "Bob&#39;s note" in bob_page, bob_page[:200]
        )
        client.get("/auth/logout")
        sign_in(client)
        ada_page = client.get("/feedback").get_data(as_text=True)
        self.assertNotIn("Bob", ada_page)

    # -- states and constraints -------------------------------------------

    def test_the_three_states_render(self) -> None:
        app, client, _accounts = self._signed_in()
        loading = client.get("/feedback?view=loading").get_data(as_text=True)
        self.assertIn("Loading your feedback", loading)
        error = client.get("/feedback?view=error").get_data(as_text=True)
        self.assertIn("couldn't load your feedback", error)
        self.assertIn("Try again", error)

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        app, client, _accounts = self._signed_in()
        for url in ("/feedback", "/feedback?view=loading", "/feedback?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), url)
            self.assertIsNone(re.search(r"\brgba?\(", block), url)
            self.assertIsNone(re.search(r"\bhsla?\(", block), url)

    def test_exactly_one_primary_action_per_state(self) -> None:
        app, client, _accounts = self._signed_in()
        for url in ("/feedback", "/feedback?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertEqual(page.count("o-btn--primary"), 1, url)
        loading = client.get("/feedback?view=loading").get_data(as_text=True)
        self.assertEqual(loading.count("o-btn--primary"), 0)

    def test_the_screen_itself_adds_no_copper(self) -> None:
        app, client, _accounts = self._signed_in()
        page = client.get("/feedback").get_data(as_text=True)
        self.assertNotIn("--copper", page_style_block(page))
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)


class FeedbackScreenSQLiteTests(FeedbackScreenTests):
    """Every Feedback check again, against the SQLite stores production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_store import SQLiteStore

        app, _client, accounts = self._make()
        self.assertIsInstance(app.config["STORE"], SQLiteStore)


if __name__ == "__main__":
    unittest.main()
