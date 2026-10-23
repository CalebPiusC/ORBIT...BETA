"""The Settings screen, on both stores.

What is pinned here:

  * only options that really work: the profile fields save through the
    real /settings/profile handler, the Product updates checkbox is the
    real account preference, the password/session/delete controls are the
    real handlers, and the theme choice is the real per-browser setting;
  * the frame's "Approval reminders" and "Weekly progress note" checkboxes
    promise notifications this build cannot send and do not appear at all
    (the behavioural rule wins over the mock — flagged in the report);
  * the Language and Time zone rows display the real values and say what
    they cannot do; "Explore plans" and "Export workspace" are disabled
    with "Coming soon";
  * the Notifications and Privacy tabs carry the heartbeat's real
    notification settings and the real password/session/delete forms
    (both derived — the frame draws only General);
  * empty/loading/error states; one copper action (Save preferences);
    zero inline styles; no hard-coded colours.
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


class SettingsScreenTests(unittest.TestCase):
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

    # -- only real options -------------------------------------------------

    def test_general_shows_the_real_account_and_saves_it(self) -> None:
        app, client, accounts = self._signed_in()
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Make Orbit feel like yours.", page)
        self.assertIn("Ada A", page)
        self.assertIn("ada@example.dev", page)
        resp = client.post(
            "/settings/profile",
            data={"name": "Ada Lovelace", "workspace": "Notes crew", "beta_updates": "on"},
        )
        self.assertIn("saved=profile", resp.headers["Location"])
        account = accounts.by_email("ada@example.dev")
        self.assertEqual(account["name"], "Ada Lovelace")
        self.assertEqual(account["workspace"], "Notes crew")
        self.assertTrue(account["prefs"]["beta_updates"])
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Ada Lovelace", page)
        self.assertIn("Notes crew", page)

    def test_the_unsendable_nudges_do_not_appear_as_options(self) -> None:
        """The frame draws them; this build cannot send them, so they are gone."""
        app, client, _accounts = self._signed_in()
        page = client.get("/settings").get_data(as_text=True)
        self.assertNotIn("Approval reminders", page)
        self.assertNotIn("Weekly progress note", page)
        # The one real preference is there, honestly described.
        self.assertIn("Product updates", page)
        self.assertIn("sends no mail in this build yet", page)

    def test_language_and_time_zone_display_what_is_real(self) -> None:
        app, client, _accounts = self._signed_in()
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Africa/Lagos", page)
        self.assertIn("English", page)
        self.assertIn("Changing it is not built yet", page)
        self.assertIn("22:00 – 08:00", page)

    def test_the_theme_choice_is_the_real_three_way_setting(self) -> None:
        app, client, _accounts = self._signed_in()
        page = client.get("/settings").get_data(as_text=True)
        for choice in ("light", "dark", "system"):
            self.assertIn(f'data-theme-choice="{choice}"', page)
        script = open(
            os.path.join(os.path.dirname(__file__), "..", "app", "static", "settings.js"),
            encoding="utf-8",
        ).read()
        self.assertIn('window.localStorage.setItem(THEME_KEY, choice)', script)
        self.assertIn("(prefers-color-scheme: dark)", script)

    # -- notifications and privacy (derived tabs) --------------------------

    def test_notifications_tab_carries_the_real_reminder_settings(self) -> None:
        app, client, _accounts = self._signed_in()
        page = client.get("/settings?tab=notifications").get_data(as_text=True)
        self.assertIn("Pause reminders", page)
        self.assertIn('id="stPause"', page)
        self.assertIn("href=\"/reminders\"", page)
        self.assertIn("22:00 – 08:00", page)

    def test_the_pause_switch_is_wired_to_the_real_endpoint(self) -> None:
        app, client, _accounts = self._signed_in()
        script = open(
            os.path.join(os.path.dirname(__file__), "..", "app", "static", "settings.js"),
            encoding="utf-8",
        ).read()
        self.assertIn('fetch("/api/heartbeat/pause"', script)
        self.assertIn("{ paused: pause.checked }", script)

    def test_privacy_tab_holds_the_real_password_session_and_delete(self) -> None:
        app, client, _accounts = self._signed_in()
        page = client.get("/settings?tab=privacy").get_data(as_text=True)
        self.assertIn('action="/settings/password"', page)
        self.assertIn('action="/settings/sessions/revoke-others"', page)
        self.assertIn('action="/settings/delete"', page)
        self.assertIn("this browser", page)
        # And the General tab's button leads here.
        general = client.get("/settings").get_data(as_text=True)
        self.assertIn('href="/settings?tab=privacy#signin"', general)
        self.assertIn('href="/settings?tab=privacy#delete"', general)

    # -- the rail ----------------------------------------------------------

    def test_the_rail_counts_are_real_and_the_promises_are_gated(self) -> None:
        app, client, accounts = self._signed_in()
        projects = app.config["PROJECTS"]
        ada = accounts.by_email("ada@example.dev")
        projects.create(user_id=ada["id"], name="Fieldnotes")
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Your approval is the rule.", page)
        self.assertIn("Always requires approval", page)
        self.assertIn("1 active project", page)
        self.assertIn("6 specialists", page)
        self.assertIn("Explore plans", page)
        self.assertIn("Export workspace", page)
        self.assertEqual(page.count("Coming soon"), 2)

    # -- states and constraints -------------------------------------------

    def test_the_three_states_render(self) -> None:
        app, client, _accounts = self._signed_in()
        loading = client.get("/settings?view=loading").get_data(as_text=True)
        self.assertIn("Loading your settings", loading)
        error = client.get("/settings?view=error").get_data(as_text=True)
        self.assertIn("couldn't load your settings", error)
        self.assertIn("Try again", error)

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        app, client, _accounts = self._signed_in()
        for url in ("/settings", "/settings?tab=notifications", "/settings?tab=privacy",
                    "/settings?view=loading", "/settings?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), url)
            self.assertIsNone(re.search(r"\brgba?\(", block), url)
            self.assertIsNone(re.search(r"\bhsla?\(", block), url)

    def test_one_primary_action_at_most_and_no_screen_copper(self) -> None:
        app, client, _accounts = self._signed_in()
        general = client.get("/settings").get_data(as_text=True)
        self.assertEqual(general.count("o-btn--primary"), 1)  # Save preferences
        self.assertIn("Save preferences", general)
        for url in ("/settings?tab=notifications", "/settings?tab=privacy"):
            page = client.get(url).get_data(as_text=True)
            self.assertLessEqual(page.count("o-btn--primary"), 1, url)
        error = client.get("/settings?view=error").get_data(as_text=True)
        self.assertEqual(error.count("o-btn--primary"), 1)
        loading = client.get("/settings?view=loading").get_data(as_text=True)
        self.assertEqual(loading.count("o-btn--primary"), 0)
        self.assertNotIn("--copper", page_style_block(general))
        markup = re.sub(r"<style>.*?</style>", "", general, flags=re.S)
        self.assertNotIn("copper", markup)

    def test_two_accounts_see_only_their_own_settings(self) -> None:
        app, client, accounts = self._signed_in()
        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        page = client.get("/settings").get_data(as_text=True)
        self.assertIn("Bob B", page)
        self.assertIn("bob@example.dev", page)
        self.assertNotIn("Ada A", page)
        self.assertNotIn("ada@example.dev", page)


class SettingsScreenSQLiteTests(SettingsScreenTests):
    """Every Settings check again, against the SQLite stores production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_store import SQLiteStore

        app, _client, accounts = self._make()
        self.assertIsInstance(app.config["STORE"], SQLiteStore)


if __name__ == "__main__":
    unittest.main()
