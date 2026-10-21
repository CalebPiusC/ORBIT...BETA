"""The Agent conversation screen, on both stores.

Same two-layer harness as the other screen tests (in-memory factory, then
the SQLite stores production boots). What is pinned:

  * the thread follows the chat rules — the person's messages right, the
    agent's left, and no row carries a name label;
  * two accounts never see each other's conversations: another account's
    project 404s, and another account's specialist thread never renders
    on this account's screen;
  * the workspace's ?agent=<specialist> deep link and the Agents page's
    pick-up card both land here;
  * sending goes through the workspace's own stream endpoint and grows
    this specialist's thread — and only this specialist's thread;
  * empty, loading and error states render, derived from the dashboard's;
  * exactly one copper (primary) action — Send — zero inline styles, no
    hard-coded colours in the screen's own style block, and Quick/Deep
    stays a separate control from Chat/Agent in the client.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

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


class ConversationScreenTests(unittest.TestCase):
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

    def _project_with_thread(self, agent_id: str = "coding"):
        """One signed-in account, one project, one specialist thread."""
        from app.store import ChatMessage

        app, client, accounts = self._make()
        projects = app.config["PROJECTS"]
        sign_in(client)
        account = accounts.by_email("ada@example.dev")
        project = projects.create(user_id=account["id"], name="Fieldnotes")
        store = app.config["STORE"].for_account(account["id"])
        conv = store.project_conversation(project.id, create=True, agent_id=agent_id)
        store.append_message(
            conv.id,
            ChatMessage(who="Ada", time="09:00", body="Keep saved articles between visits", is_orbit=False),
        )
        store.append_message(
            conv.id,
            ChatMessage(who="Coding", time="09:01", body="Here is a small proposal for saved articles.", is_orbit=True),
        )
        return app, client, store, project

    # -- the screen --------------------------------------------------------

    def test_the_thread_has_no_name_labels_and_sides_by_speaker(self) -> None:
        _app, client, _store, project = self._project_with_thread()
        page = client.get(f"/projects/{project.id}/agents/coding").get_data(as_text=True)
        self.assertIn('<h1 class="cv-title">Coding agent</h1>', page)
        self.assertIn('data-agent="coding"', page)
        # the two real messages, on their two sides
        self.assertIn('class="cv-msg cv-msg--user"', page)
        self.assertIn('class="cv-msg cv-msg--orbit"', page)
        self.assertIn("Keep saved articles between visits", page)
        self.assertIn("Here is a small proposal for saved articles.", page)
        # and no per-row name label anywhere — the heading says who speaks
        self.assertNotIn("speaker", page)
        self.assertNotIn(">Ada<", page)
        self.assertNotIn(">Coding<", page)

    def test_an_empty_thread_offers_the_specialtys_real_starters(self) -> None:
        from app.agents import get_agent

        app, client, accounts = self._make()
        projects = app.config["PROJECTS"]
        sign_in(client)
        account = accounts.by_email("ada@example.dev")
        project = projects.create(user_id=account["id"], name="Fieldnotes")
        page = client.get(f"/projects/{project.id}/agents/research").get_data(as_text=True)
        self.assertIn("No conversation yet", page)
        for starter in get_agent("research").starters:
            self.assertIn(starter.prompt, page)
        # the composer is honest about who it talks to
        self.assertIn('data-agent="research"', page)
        self.assertIn("Ask the Research agent about Fieldnotes…", page)

    def test_an_unknown_agent_id_falls_back_to_the_main_agent(self) -> None:
        _app, client, _store, project = self._project_with_thread()
        page = client.get(f"/projects/{project.id}/agents/bogus").get_data(as_text=True)
        self.assertIn('data-agent="main"', page)
        # and the coding thread is NOT on the fallback page
        self.assertNotIn("Here is a small proposal", page)

    def test_the_states_render(self) -> None:
        _app, client, _store, project = self._project_with_thread()
        loading = client.get(f"/projects/{project.id}/agents/coding?view=loading").get_data(as_text=True)
        self.assertIn("cv-skel", loading)
        self.assertIn("Loading the conversation", loading)
        error = client.get(f"/projects/{project.id}/agents/coding?view=error").get_data(as_text=True)
        self.assertIn("couldn't load this conversation", error)
        self.assertIn("Try again", error)
        self.assertIn(f'href="/projects/{project.id}"', error)

    # -- isolation ---------------------------------------------------------

    def test_another_accounts_project_404s_and_leaks_nothing(self) -> None:
        app, client, accounts = self._make()
        projects = app.config["PROJECTS"]
        sign_in(client, "ada@example.dev", "Ada A")
        ada = accounts.by_email("ada@example.dev")
        fieldnotes = projects.create(user_id=ada["id"], name="Fieldnotes")
        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        resp = client.get(f"/projects/{fieldnotes.id}/agents/coding")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("Fieldnotes", resp.get_data(as_text=True))

    def test_another_accounts_thread_never_renders_here(self) -> None:
        from app.store import ChatMessage

        app, client, accounts = self._make()
        projects = app.config["PROJECTS"]
        sign_in(client, "ada@example.dev", "Ada A")
        ada = accounts.by_email("ada@example.dev")
        fieldnotes = projects.create(user_id=ada["id"], name="Fieldnotes")
        ada_store = app.config["STORE"].for_account(ada["id"])
        conv = ada_store.project_conversation(fieldnotes.id, create=True, agent_id="coding")
        ada_store.append_message(
            conv.id,
            ChatMessage(who="Ada", time="09:00", body="Ada's secret coding plan", is_orbit=False),
        )
        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        bob = accounts.by_email("bob@example.dev")
        budgets = projects.create(user_id=bob["id"], name="Budgets")
        page = client.get(f"/projects/{budgets.id}/agents/coding").get_data(as_text=True)
        self.assertNotIn("Ada's secret coding plan", page)
        self.assertNotIn("Fieldnotes", page)
        self.assertIn("No conversation yet", page)

    # -- the routes around it ----------------------------------------------

    def test_the_workspace_deep_link_and_the_pick_up_card_land_here(self) -> None:
        _app, client, _store, project = self._project_with_thread()
        resp = client.get(f"/projects/{project.id}?agent=coding")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["Location"], f"/projects/{project.id}/agents/coding")
        agents_page = client.get("/agents").get_data(as_text=True)
        self.assertIn("Your recent conversation with the Coding agent is ready to pick up.", agents_page)
        self.assertIn(f'href="/projects/{project.id}/agents/coding"', agents_page)

    def test_sending_grows_only_this_specialists_thread(self) -> None:
        app, client, store, project = self._project_with_thread()
        resp = client.post(
            f"/api/project/{project.id}/stream",
            json={"message": "What would the coding agent change?", "agent": "coding"},
        )
        self.assertEqual(resp.status_code, 200)
        events = resp.get_data(as_text=True)  # drain the SSE turn to completion
        self.assertIn("event: answer", events)
        self.assertIn("event: done", events)
        coding_page = client.get(f"/projects/{project.id}/agents/coding").get_data(as_text=True)
        self.assertIn("What would the coding agent change?", coding_page)
        self.assertIn("canned", coding_page)
        # the Main Agent's own workspace thread stays untouched
        workspace = client.get(f"/projects/{project.id}").get_data(as_text=True)
        self.assertNotIn("What would the coding agent change?", workspace)
        self.assertIn("No conversation yet", workspace)

    # -- the screen's own constraints --------------------------------------

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        _app, client, _store, project = self._project_with_thread()
        for suffix in ("", "?view=loading", "?view=error"):
            url = f"/projects/{project.id}/agents/coding{suffix}"
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), "hex colour")
            self.assertIsNone(re.search(r"\brgba?\(", block), "rgb colour")
            self.assertIsNone(re.search(r"\bhsla?\(", block), "hsl colour")

    def test_the_page_has_exactly_one_primary_action(self) -> None:
        _app, client, _store, project = self._project_with_thread()
        for suffix in ("", "?view=error"):
            url = f"/projects/{project.id}/agents/coding{suffix}"
            page = client.get(url).get_data(as_text=True)
            self.assertEqual(page.count("o-btn--primary"), 1, url)
        # Send is the normal page's copper action; nothing else claims it
        normal = client.get(f"/projects/{project.id}/agents/coding").get_data(as_text=True)
        self.assertIn('class="o-btn o-btn--primary" id="cvSend">Send</button>', normal)

    def test_the_screen_itself_adds_no_copper(self) -> None:
        _app, client, _store, project = self._project_with_thread()
        page = client.get(f"/projects/{project.id}/agents/coding").get_data(as_text=True)
        block = page_style_block(page)
        self.assertNotIn("--copper", block)
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)

    def test_quick_deep_stays_a_separate_control_from_chat_agent(self) -> None:
        # The client renders the two toggles as two separate elements: a
        # segmented Chat/Agent group and its own Quick/Deep button.
        script = Path("app/static/conversation.js").read_text(encoding="utf-8")
        self.assertIn('seg.className = "cv-seg";', script)
        self.assertIn('think.className = "cv-depth";', script)
        self.assertIn("modeZone.appendChild(seg);", script)
        self.assertIn("modeZone.appendChild(think);", script)
        seg_at = script.index('seg.className = "cv-seg";')
        depth_at = script.index('think.className = "cv-depth";')
        self.assertLess(seg_at, depth_at)
        # and the two never share one element
        self.assertNotIn('"cv-seg cv-depth"', script)


class ConversationScreenSQLiteTests(ConversationScreenTests):
    """Every conversation check again, against the SQLite store production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_accounts import SQLiteAccountStore
        from app.sqlite_projects import SQLiteProjectStore

        app, _client, _accounts = self._make()
        self.assertIsInstance(app.config["PROJECTS"], SQLiteProjectStore)
        self.assertIsInstance(app.config["ACCOUNTS"], SQLiteAccountStore)


if __name__ == "__main__":
    unittest.main()
