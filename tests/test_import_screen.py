"""The Import project screen, on both stores.

Two layers, following the dashboard's own pattern:

  * the in-memory app factory (``create_app()`` with an in-memory
    ``ProjectStore``) — the same harness the rest of the route tests use;
  * the SQLite subclass, which re-runs every check against the stores
    production boots, so the two implementations cannot drift apart.

What is pinned here is the product rule, not the formatting:

  * every import option that is not built renders as a disabled button —
    no link, no form, nothing to submit — and only "start new" leads
    anywhere (the existing /create flow);
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


class ImportScreenTests(unittest.TestCase):
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

    def test_the_screen_renders_with_every_source_drawn(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        page = client.get("/projects/import").get_data(as_text=True)
        self.assertIn("Bring your work into orbit.", page)
        for label in (
            "GitHub repository",
            "GitLab repository",
            "Bitbucket repository",
            "Local folder",
            "Drop your project ZIP here",
            "Start a new project",
        ):
            self.assertIn(label, page)

    def test_every_unbuilt_option_is_disabled_with_no_link_or_form(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        page = client.get("/projects/import").get_data(as_text=True)
        # Four source buttons plus the ZIP button, all disabled.
        rows = re.findall(
            r'<button class="o-provider o-provider--soon" type="button" disabled[^>]*>(.*?)</button>',
            page,
            flags=re.S,
        )
        self.assertEqual(len(rows), 4, rows)
        for row in rows:
            self.assertIn("Coming soon", row)
        self.assertEqual(page.count('disabled aria-disabled="true"'), 4)
        self.assertRegex(
            page,
            r'<button class="o-btn o-btn--secondary" type="button" disabled[^>]*>Choose ZIP file</button>',
        )
        # Nothing on this screen submits or navigates except "start new".
        self.assertNotIn("<form", page)
        self.assertNotIn("<input", page)
        self.assertNotIn('type="file"', page)
        # No link wears a "Coming soon" label.
        self.assertIsNone(re.search(r"<a[^>]*>[^<]*Coming soon", page))
        # "Start new" is the existing create flow.
        self.assertIn('<a class="o-btn o-btn--primary imp-start" href="/create">Start a new project</a>', page)

    def test_github_reads_as_optional(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        page = client.get("/projects/import").get_data(as_text=True)
        self.assertIn("GitHub is optional", page)
        self.assertIn("never requires connecting a repository", page)

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        page = client.get("/projects/import").get_data(as_text=True)
        self.assertNotIn('style="', page)
        block = page_style_block(page)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block))
        self.assertIsNone(re.search(r"\brgba?\(", block))
        self.assertIsNone(re.search(r"\bhsla?\(", block))

    def test_the_page_has_exactly_one_primary_action(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        page = client.get("/projects/import").get_data(as_text=True)
        self.assertEqual(page.count("o-btn--primary"), 1)

    def test_the_import_url_is_a_page_not_a_project(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        self.assertEqual(client.get("/projects/import").status_code, 200)
        # The dynamic route still owns real ids (and 404s for unknown ones).
        self.assertEqual(client.get("/projects/p-nope").status_code, 404)
        project = app.config["PROJECTS"].create(user_id=ada["id"], name="Fieldnotes")
        self.assertEqual(client.get(f"/projects/{project.id}").status_code, 200)


class ImportScreenSQLiteTests(ImportScreenTests):
    """Every Import screen check again, against the SQLite store."""

    use_sqlite = True
