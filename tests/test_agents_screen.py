"""The Agents screen, on both stores.

Same two-layer harness as test_projects_screen.py. What is pinned:

  * the six registry specialties render with their frame persona and
    best-at lines and a Start button each — no rates, no usage, no toggles;
  * choosing a specialist only ever offers this account's own projects, and
    a project id from another account opens no picker and assigns nothing;
  * the pick-up card names only this account's own recent specialist
    conversation, and only when one really has messages;
  * empty, loading and error states render, derived from the dashboard's;
  * exactly one copper (primary) action, zero inline styles, no hard-coded
    colours in the screen's own style block.
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
    blocks = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    assert blocks, "the page has no style block"
    return blocks[-1]


class AgentsScreenTests(unittest.TestCase):
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

    def _two_accounts(self):
        app, client, accounts = self._make()
        projects = app.config["PROJECTS"]
        sign_in(client, "ada@example.dev", "Ada A")
        ada = accounts.by_email("ada@example.dev")
        fieldnotes = projects.create(user_id=ada["id"], name="Fieldnotes")
        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        bob = accounts.by_email("bob@example.dev")
        budgets = projects.create(user_id=bob["id"], name="Budgets")
        return app, client, accounts, fieldnotes, budgets

    # -- the cards ---------------------------------------------------------

    def test_every_specialty_gets_its_frame_card(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        page = client.get("/agents").get_data(as_text=True)
        for agent_id, persona in (
            ("coding", "The thoughtful builder"),
            ("research", "The curious explorer"),
            ("debug", "The patient problem-solver"),
            ("review", "The second set of eyes"),
            ("testing", "The reassuring check"),
            ("documentation", "The clear communicator"),
        ):
            self.assertIn(persona, page, agent_id)
            self.assertIn(f'href="/agents?pick={agent_id}#pick"', page, agent_id)
        # no rates, no usage figures, no on/off toggles
        for banned in ("success", "usage", "toggle", "enabled"):
            self.assertNotIn(banned, page.lower())

    # -- isolation ---------------------------------------------------------

    def test_the_picker_offers_only_this_accounts_projects(self) -> None:
        _app, client, _accounts, fieldnotes, budgets = self._two_accounts()
        page = client.get("/agents?pick=coding").get_data(as_text=True)
        self.assertIn("Budgets", page)
        self.assertNotIn("Fieldnotes", page)
        self.assertIn(f'name="project_id" value="{budgets.id}"', page)
        choose = client.get("/agents?choose=1").get_data(as_text=True)
        self.assertIn(f'href="/agents?project={budgets.id}#pick"', choose)
        self.assertNotIn("Fieldnotes", choose)

    def test_another_accounts_project_opens_no_picker(self) -> None:
        _app, client, _accounts, fieldnotes, budgets = self._two_accounts()
        page = client.get(f"/agents?project={fieldnotes.id}").get_data(as_text=True)
        self.assertNotIn("Who should work on", page)
        self.assertNotIn("Fieldnotes", page)
        # and assigning into it is refused, same as before this screen
        resp = client.post("/agents/coding/assign", data={"project_id": fieldnotes.id})
        self.assertEqual(resp.status_code, 404)

    def test_the_pick_up_card_names_only_own_conversations(self) -> None:
        from app.store import ChatMessage

        app, client, accounts, fieldnotes, budgets = self._two_accounts()
        ada = accounts.by_email("ada@example.dev")
        ada_store = app.config["STORE"].for_account(ada["id"])
        conv = ada_store.project_conversation(fieldnotes.id, create=True, agent_id="coding")
        ada_store.append_message(
            conv.id,
            ChatMessage(who="Ada", time="09:00", body="Keep saved articles between visits", is_orbit=False),
        )
        page = client.get("/agents").get_data(as_text=True)
        self.assertNotIn("Keep saved articles between visits", page)
        self.assertIn("No specialist conversations yet.", page)

        bob = accounts.by_email("bob@example.dev")
        bob_store = app.config["STORE"].for_account(bob["id"])
        bob_conv = bob_store.project_conversation(budgets.id, create=True, agent_id="review")
        bob_store.append_message(
            bob_conv.id,
            ChatMessage(who="Bob", time="09:30", body="Review the budget import", is_orbit=False),
        )
        page = client.get("/agents").get_data(as_text=True)
        self.assertIn("Review the budget import", page)
        self.assertIn("Your recent conversation with the Review agent is ready to pick up.", page)
        self.assertIn("Budgets", page)
        self.assertNotIn("Keep saved articles between visits", page)

    # -- states ------------------------------------------------------------

    def test_an_account_without_projects_gets_the_guiding_picker(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        page = client.get("/agents?pick=coding").get_data(as_text=True)
        self.assertIn("No projects yet", page)
        self.assertIn('href="/create"', page)
        self.assertIn('href="/projects/import"', page)
        self.assertIn("No specialist conversations yet.", page)

    def test_the_loading_and_error_views_render(self) -> None:
        _app, client, _accounts = self._make()
        sign_in(client)
        loading = client.get("/agents?view=loading").get_data(as_text=True)
        self.assertIn("ag-skel", loading)
        self.assertIn("Loading the specialists", loading)
        error = client.get("/agents?view=error").get_data(as_text=True)
        self.assertIn("couldn't load the specialists", error)
        self.assertIn("Try again", error)

    # -- the screen's own constraints --------------------------------------

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        _app, client, _accounts, _fieldnotes, budgets = self._two_accounts()
        for url in ("/agents", "/agents?view=error", f"/agents?project={budgets.id}"):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), "hex colour")
            self.assertIsNone(re.search(r"\brgba?\(", block), "rgb colour")
            self.assertIsNone(re.search(r"\bhsla?\(", block), "hsl colour")

    def test_the_page_has_exactly_one_primary_action(self) -> None:
        _app, client, _accounts, _fieldnotes, budgets = self._two_accounts()
        for url in ("/agents", f"/agents?pick=coding", f"/agents?project={budgets.id}", "/agents?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertEqual(page.count("o-btn--primary"), 1, url)

    def test_the_screen_itself_adds_no_copper(self) -> None:
        _app, client, _accounts, _fieldnotes, _budgets = self._two_accounts()
        page = client.get("/agents").get_data(as_text=True)
        block = page_style_block(page)
        self.assertNotIn("--copper", block)
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)


class AgentsScreenSQLiteTests(AgentsScreenTests):
    """Every Agents check again, against the SQLite store production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_accounts import SQLiteAccountStore
        from app.sqlite_projects import SQLiteProjectStore

        app, _client, _accounts = self._make()
        self.assertIsInstance(app.config["PROJECTS"], SQLiteProjectStore)
        self.assertIsInstance(app.config["ACCOUNTS"], SQLiteAccountStore)


if __name__ == "__main__":
    unittest.main()
