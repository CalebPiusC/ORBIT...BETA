"""The Empty, loading & error states screen, on both stores.

The frame is a spec sheet (wordmark, no rail, the patterns in cards). What
is pinned here:

  * each card carries the app's real state copy and real destinations —
    the same strings the live Projects screen renders (kept in sync here,
    so the gallery cannot drift from the app);
  * none of the frame's sample specifics is reproduced (no "26 of 38 files
    read", no IMPORT-004, no "fieldnotes.zip", no "garden" query, no
    sample project names);
  * exactly one copper action — the real "New project" starting point;
    zero inline styles; no hard-coded colours; both themes pre-paint.
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


class StatesScreenTests(unittest.TestCase):
    use_sqlite = False

    def _make(self):
        if not self.use_sqlite:
            app, client, accounts = make_memory_client()
        else:
            directory, app, client, accounts = make_sqlite_client()
            store = app.config["STORE"]

            def _release_database() -> None:
                self.assertEqual(store.database.open_connection_count, 0)
                store.database.close()

            self.addCleanup(directory.cleanup)
            self.addCleanup(_release_database)
        # The states screen is a signed-in screen, like every other one.
        sign_in(client)
        return app, client, accounts

    # -- the four real patterns -------------------------------------------

    def test_the_four_cards_carry_the_real_state_copy(self) -> None:
        app, client, _accounts = self._make()
        page = client.get("/states").get_data(as_text=True)
        self.assertIn("A little guidance, in every state.", page)
        self.assertIn("Projects / Empty", page)
        self.assertIn("Projects / Loading", page)
        self.assertIn("Projects / Error", page)
        self.assertIn("Projects / Search", page)
        # Real copy: the empty state, the failure panel, the no-results panel.
        self.assertIn("No projects yet.", page)
        self.assertIn("We couldn't load your projects.", page)
        self.assertIn("Nothing has changed and no files were touched.", page)
        self.assertIn("Nothing matches", page)
        self.assertIn("No files are being changed. You can safely leave this page.", page)

    def test_every_destination_is_real(self) -> None:
        app, client, _accounts = self._make()
        page = client.get("/states").get_data(as_text=True)
        for href in ("/create", "/projects/import", "/projects?view=loading",
                     "/projects", "/dashboard"):
            self.assertIn(f'href="{href}"', page)
        self.assertIn("New project", page)
        self.assertIn("See the live state", page)

    def test_the_gallery_cannot_drift_from_the_live_states(self) -> None:
        """The strings shown are the strings the Projects screen renders."""
        app, client, _accounts = self._make()
        page = client.get("/states").get_data(as_text=True)
        live_error = client.get("/projects?view=error").get_data(as_text=True)
        self.assertIn("We couldn't load your projects.", live_error)
        self.assertIn("We couldn't load your projects.", page)
        live_loading = client.get("/projects?view=loading").get_data(as_text=True)
        self.assertIn("Loading your projects", live_loading)
        self.assertIn("Loading", page)

    def test_none_of_the_frames_sample_content_is_reproduced(self) -> None:
        app, client, _accounts = self._make()
        page = client.get("/states").get_data(as_text=True)
        for sample in ("26 of 38", "IMPORT-004", "fieldnotes.zip", "garden",
                       "Fieldnotes", "Step 2 of 3", "100 MB"):
            self.assertNotIn(sample, page)

    # -- constraints -------------------------------------------------------

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        app, client, _accounts = self._make()
        page = client.get("/states").get_data(as_text=True)
        self.assertNotIn('style="', page)
        block = page_style_block(page)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block))
        self.assertIsNone(re.search(r"\brgba?\(", block))
        self.assertIsNone(re.search(r"\bhsla?\(", block))

    def test_one_copper_action_and_no_screen_copper(self) -> None:
        app, client, _accounts = self._make()
        page = client.get("/states").get_data(as_text=True)
        self.assertEqual(page.count("o-btn--primary"), 1)  # New project
        self.assertNotIn("--copper", page_style_block(page))
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)

    def test_both_themes_pre_paint_and_the_layout_is_responsive(self) -> None:
        app, client, _accounts = self._make()
        page = client.get("/states").get_data(as_text=True)
        self.assertIn("prefers-color-scheme", page)
        self.assertIn('localStorage.getItem("orbit.theme")', page)
        block = page_style_block(page)
        self.assertIn("@media (max-width: 1000px)", block)

    def test_the_wordmark_is_the_only_chrome(self) -> None:
        """The frame is a spec sheet: mark and content, no rail."""
        app, client, _accounts = self._make()
        page = client.get("/states").get_data(as_text=True)
        self.assertIn('class="stg-brand"', page)
        self.assertNotIn('class="app-nav"', page)
        self.assertNotIn('id="appSidebar"', page)


class StatesScreenSQLiteTests(StatesScreenTests):
    """Every states-screen check again, against the SQLite stores."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_store import SQLiteStore

        app, _client, accounts = self._make()
        self.assertIsInstance(app.config["STORE"], SQLiteStore)


if __name__ == "__main__":
    unittest.main()
