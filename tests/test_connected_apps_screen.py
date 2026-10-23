"""The Connected apps screen, on both stores.

What is pinned here:

  * the rows come from the store's connections — the frame's four apps
    (GitHub, Figma, Notion, Slack), each with an honest status; nothing is
    ever faked as connected;
  * every action that is not built is disabled with "Coming soon" — the
    connect buttons and the privacy-guide link alike;
  * a status that really is "connected" renders as connected (the
    conditional is not a lie), with "Manage access" and no "Connect";
  * empty, loading and error states render; the content states carry zero
    copper because nothing on them works yet; zero inline styles; no
    hard-coded colours.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from app.accounts import AccountStore
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


class ConnectedAppsScreenTests(unittest.TestCase):
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

    # -- honest status per app --------------------------------------------

    def test_every_row_is_honest_about_not_being_built(self) -> None:
        app, client, _accounts = self._signed_in()
        page = client.get("/connected").get_data(as_text=True)
        for name in ("GitHub", "Figma", "Notion", "Slack"):
            self.assertIn(name, page)
        self.assertEqual(page.count("Not connected"), 4)
        # The frame's real copy: what each is for, and the smallest scope.
        self.assertIn("Bring repositories into Orbit", page)
        self.assertIn("Read only · Selected files", page)
        self.assertIn("Chosen channels", page)
        # Every connect button is disabled with the honest label.
        for name in ("GitHub", "Figma", "Notion", "Slack"):
            self.assertIn(f"Connect {name}", page)
        self.assertEqual(page.count("Coming soon"), 5)  # 4 connects + the guide
        self.assertIn("A connection isn't permission to change things.", page)

    def test_a_real_status_still_renders_honestly(self) -> None:
        """The conditional is not a lie: connected renders as connected."""
        app, client, _accounts = self._signed_in()
        app.config["STORE"].connections[0].status = "connected"
        page = client.get("/connected").get_data(as_text=True)
        self.assertIn("Connected · Read only · Selected repositories", page)
        self.assertIn("Manage access", page)
        self.assertEqual(page.count("Connect GitHub"), 0)
        self.assertEqual(page.count("Not connected"), 3)

    def test_no_row_invents_a_connection_or_a_brand_mark(self) -> None:
        app, client, _accounts = self._signed_in()
        page = client.get("/connected").get_data(as_text=True)
        self.assertNotIn('class="tool-pill', page)
        # No brand artwork ships: nothing pretends to be a product logo.
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("octocat", markup.lower())
        self.assertNotIn("logo", markup.lower())

    def test_two_accounts_see_the_same_honest_rows_and_no_each_others_data(self) -> None:
        app, client, accounts = self._signed_in()
        store = app.config["STORE"]
        ada = accounts.by_email("ada@example.dev")
        store.for_account(ada["id"]).create_task("Ada's private idea")
        page = client.get("/connected").get_data(as_text=True)
        self.assertNotIn("Ada&#39;s private idea", page)
        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        bob_page = client.get("/connected").get_data(as_text=True)
        self.assertNotIn("Ada", bob_page)
        self.assertEqual(bob_page.count("Not connected"), 4)

    # -- states and constraints -------------------------------------------

    def test_an_empty_registry_says_so(self) -> None:
        app, client, _accounts = self._signed_in()
        app.config["STORE"].connections = []
        page = client.get("/connected").get_data(as_text=True)
        self.assertIn("No connections are offered yet.", page)

    def test_the_three_states_render(self) -> None:
        app, client, _accounts = self._signed_in()
        loading = client.get("/connected?view=loading").get_data(as_text=True)
        self.assertIn("Loading your connections", loading)
        error = client.get("/connected?view=error").get_data(as_text=True)
        self.assertIn("couldn't load your connections", error)
        self.assertIn("Try again", error)

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        app, client, _accounts = self._signed_in()
        for url in ("/connected", "/connected?view=loading", "/connected?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), url)
            self.assertIsNone(re.search(r"\brgba?\(", block), url)
            self.assertIsNone(re.search(r"\bhsla?\(", block), url)

    def test_nothing_works_so_nothing_gets_copper(self) -> None:
        """The frame's copper "Connect Figma" cannot survive: none connects."""
        app, client, _accounts = self._signed_in()
        page = client.get("/connected").get_data(as_text=True)
        self.assertEqual(page.count("o-btn--primary"), 0)
        self.assertNotIn("--copper", page_style_block(page))
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)
        # The error state's way back is its one primary action.
        error = client.get("/connected?view=error").get_data(as_text=True)
        self.assertEqual(error.count("o-btn--primary"), 1)


class ConnectedAppsScreenSQLiteTests(ConnectedAppsScreenTests):
    """Every Connected apps check again, against the SQLite stores production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_store import SQLiteStore

        app, _client, accounts = self._make()
        self.assertIsInstance(app.config["STORE"], SQLiteStore)


if __name__ == "__main__":
    unittest.main()
