"""The Reminders screen, on both stores.

What is pinned here:

  * every reminder card is one the heartbeat really fired, with its real
    date, its real sentence, and its real delivery state;
  * the settings block is the heartbeat's own — quiet hours 22:00–08:00,
    the Africa/Lagos clock, the plan-hour and check-in-gap rules, and the
    pause switch wired to POST /api/heartbeat/pause;
  * Mark done dismisses through the existing notices API; another account
    cannot dismiss it or see it (404 / absent);
  * "New reminder" and "Snooze" are disabled with "Coming soon" — nothing
    pretends to create or snooze a reminder;
  * empty, loading and error states render; exactly one copper (primary)
    action — the real "Check now"; zero inline styles; no hard-coded colours.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import datetime, timezone

from config import HeartbeatSettings
from heartbeat.service import Heartbeat
from tools.registry import AuditLog

from app.accounts import AccountStore
from app.server import create_app


class FakeProvider:
    def reply(self, history, system_prompt: str) -> str:
        return "canned"

    def reply_stream(self, history, system_prompt: str, **kwargs):
        from providers.base import StreamChunk

        yield StreamChunk(kind="answer", text="canned")


class PerAccountHeartbeats:
    """The manager contract (``for_account``) over real per-account Heartbeats.

    Production with a database uses ``SQLiteHeartbeatManager``; this is the
    same seam for the in-memory factory, with one state file per account so
    two accounts cannot share reminders.
    """

    def __init__(self, directory: str, settings: HeartbeatSettings) -> None:
        self._directory = directory
        self._settings = settings
        self._by_account: dict[str, Heartbeat] = {}

    def for_account(self, account_id: str) -> Heartbeat:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id is required for heartbeat access")
        if account_id not in self._by_account:
            self._by_account[account_id] = Heartbeat(
                settings=self._settings,
                state_path=os.path.join(self._directory, f"{account_id}.json"),
                plans=_MemoryPlans(),
                audit=AuditLog(os.path.join(self._directory, f"{account_id}.audit")),
            )
        return self._by_account[account_id]


class _MemoryPlans:
    """No plans saved, so the plan-missing check is always honestly due."""

    def has_plan(self, _day) -> bool:
        return False


def settings() -> HeartbeatSettings:
    return HeartbeatSettings(
        plan_hour=10, checkin_gap_days=2, interval_seconds=60.0, ntfy_topic=""
    )


def make_memory_client():
    directory = tempfile.TemporaryDirectory()
    audit_dir = tempfile.TemporaryDirectory()
    heartbeats = PerAccountHeartbeats(directory.name, settings())
    app = create_app(
        provider=FakeProvider(),
        accounts=AccountStore(iterations=1_000),
        heartbeat=heartbeats,
        model_audit=AuditLog(os.path.join(audit_dir.name, "audit.log")),
    )
    app.config["TESTING"] = True
    return directory, audit_dir, app, app.test_client(), app.config["ACCOUNTS"]


def make_sqlite_client():
    from app.sqlite_accounts import SQLiteAccountStore
    from app.sqlite_projects import SQLiteProjectStore
    from app.sqlite_store import SQLiteStore
    from heartbeat.sqlite_store import SQLiteHeartbeatManager

    directory = tempfile.TemporaryDirectory()
    store = SQLiteStore(os.path.join(directory.name, "orbit.sqlite3"))
    app = create_app(provider=FakeProvider())
    app.config["TESTING"] = True
    app.config["STORE"] = store
    accounts = SQLiteAccountStore(store.database, iterations=1_000)
    app.config["ACCOUNTS"] = accounts
    app.config["PROJECTS"] = SQLiteProjectStore(store.database)
    app.config["HEARTBEAT"] = SQLiteHeartbeatManager(
        settings=settings(), database=store.database
    )
    return directory, None, app, app.test_client(), accounts


def sign_in(client, email: str = "ada@example.dev", name: str = "Ada A"):
    return client.post("/auth/oauth/github", data={"name": name, "email": email})


def page_style_block(html: str) -> str:
    blocks = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    assert blocks, "the page has no style block"
    return blocks[-1]


def fire(heartbeat: Heartbeat, day: int) -> list[dict]:
    """Run one due check on a fixed day (11:00 UTC = past the 10:00 plan hour)."""
    return heartbeat.tick(now=datetime(2026, 10, day, 11, 0, tzinfo=timezone.utc))


class RemindersScreenTests(unittest.TestCase):
    use_sqlite = False

    def _make(self):
        if not self.use_sqlite:
            directory, audit_dir, app, client, accounts = make_memory_client()
            self.addCleanup(directory.cleanup)
            self.addCleanup(audit_dir.cleanup)
            return app, client, accounts
        directory, _audit_dir, app, client, accounts = make_sqlite_client()
        store = app.config["STORE"]

        def _release_database() -> None:
            self.assertEqual(store.database.open_connection_count, 0)
            store.database.close()

        self.addCleanup(directory.cleanup)
        self.addCleanup(_release_database)
        return app, client, accounts

    def _heartbeat(self, app, account_id: str) -> Heartbeat:
        return app.config["HEARTBEAT"].for_account(account_id)

    def _one_open_one_done(self):
        """Ada: one open notice and one completed one, both really fired."""
        app, client, accounts = self._make()
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        hb = self._heartbeat(app, ada["id"])
        (first,) = fire(hb, 21)
        hb.dismiss(first["id"], source="card")
        (second,) = fire(hb, 22)
        return app, client, accounts, first, second

    # -- real notices with dates ------------------------------------------

    def test_each_card_is_a_real_fired_notice_with_its_date(self) -> None:
        app, client, _accounts, first, second = self._one_open_one_done()
        page = client.get("/reminders?tab=upcoming").get_data(as_text=True)
        self.assertIn(second["text"], page)  # the notice's own sentence
        self.assertIn("No plan for today", page)
        # 11:00 UTC on the Lagos clock — with a real date label on it,
        # whatever the wall calendar says today is.
        self.assertRegex(page, r"(Today|Yesterday|Oct 22, 2026) · 12:00")
        self.assertIn("In-app · In-app only", page)
        # Cards are distinguishable by their real notice ids (two days of
        # the same missing-plan check share one sentence).
        self.assertIn(f'data-notice="{second["id"]}"', page)
        self.assertNotIn(f'data-notice="{first["id"]}"', page)

    def test_the_tabs_split_open_completed_and_all(self) -> None:
        app, client, _accounts, first, second = self._one_open_one_done()
        upcoming = client.get("/reminders?tab=upcoming").get_data(as_text=True)
        self.assertIn(f'data-notice="{second["id"]}"', upcoming)
        self.assertIn(f'data-dismiss="{second["id"]}"', upcoming)  # open → actionable
        self.assertNotIn(first["id"], upcoming)
        completed = client.get("/reminders?tab=completed").get_data(as_text=True)
        self.assertIn(f'data-notice="{first["id"]}"', completed)
        self.assertNotIn(f'data-dismiss="{first["id"]}"', completed)  # done → no button
        self.assertIn("Completed", completed)
        self.assertNotIn(second["id"], completed)
        everything = client.get("/reminders?tab=all").get_data(as_text=True)
        self.assertIn(f'data-notice="{first["id"]}"', everything)
        self.assertIn(f'data-notice="{second["id"]}"', everything)
        self.assertIn("Upcoming · 1", upcoming)

    def test_two_accounts_never_see_each_others_reminders(self) -> None:
        app, client, accounts, _first, second = self._one_open_one_done()
        client.get("/auth/logout")
        sign_in(client, "bob@example.dev", "Bob B")
        page = client.get("/reminders").get_data(as_text=True)
        self.assertNotIn(second["text"], page)
        self.assertIn("Nothing is nudging you right now.", page)
        # Bob cannot dismiss Ada's notice: unknown there, exactly like a
        # missing id.
        resp = client.post(f"/api/heartbeat/notices/{second['id']}/dismiss", json={})
        self.assertEqual(resp.status_code, 404)

    # -- real actions only -------------------------------------------------

    def test_mark_done_is_wired_to_the_real_dismiss_endpoint(self) -> None:
        app, client, _accounts, _first, second = self._one_open_one_done()
        page = client.get("/reminders?tab=upcoming").get_data(as_text=True)
        self.assertIn(f'data-dismiss="{second["id"]}"', page)
        resp = client.post(f"/api/heartbeat/notices/{second['id']}/dismiss", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"dismissed": True})
        page = client.get("/reminders?tab=upcoming").get_data(as_text=True)
        self.assertIn("Nothing is nudging you right now.", page)

    def test_pause_really_pauses_and_the_switch_shows_it(self) -> None:
        app, client, _accounts, _first, _second = self._one_open_one_done()
        self.assertIn('id="rmPause"', client.get("/reminders").get_data(as_text=True))
        resp = client.post("/api/heartbeat/pause", json={"paused": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"paused": True})
        page = client.get("/reminders").get_data(as_text=True)
        self.assertIn("paused right now", page)
        # Paused really means paused: a manual check runs nothing.
        check = client.post("/api/heartbeat/check", json={}).get_json()
        self.assertEqual(check["status"], "paused")
        client.post("/api/heartbeat/pause", json={"paused": False})

    def test_check_now_runs_the_real_checks(self) -> None:
        app, client, _accounts, _first, _second = self._one_open_one_done()
        resp = client.post("/api/heartbeat/check", json={})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn(body["status"], ("fired", "nothing_due", "paused"))
        self.assertIsInstance(body["fired"], list)
        script = open(
            os.path.join(os.path.dirname(__file__), "..", "app", "static", "reminders.js"),
            encoding="utf-8",
        ).read()
        self.assertIn('post("/api/heartbeat/check", {})', script)
        self.assertIn('post("/api/heartbeat/pause", { paused: pause.checked })', script)
        self.assertIn('/dismiss", {})', script)

    def test_the_settings_block_is_the_heartbeats_real_settings(self) -> None:
        app, client, _accounts, _first, _second = self._one_open_one_done()
        page = client.get("/reminders").get_data(as_text=True)
        self.assertIn("22:00 – 08:00", page)  # the real quiet hours
        self.assertIn("Africa/Lagos", page)  # the real clock
        self.assertIn("after 10:00", page)  # the real plan hour
        self.assertIn("check-in gap of 2 days", page)  # the real gap
        self.assertIn("phone push off", page)  # no NTFY_TOPIC is set here
        self.assertIn("href=\"/settings?tab=notifications\"", page)

    def test_unbuilt_actions_are_disabled_not_fake(self) -> None:
        app, client, _accounts, _first, _second = self._one_open_one_done()
        page = client.get("/reminders").get_data(as_text=True)
        self.assertIn("New reminder", page)
        self.assertIn("Snooze", page)
        self.assertEqual(page.count("Coming soon"), 2)
        for title in ("Creating your own reminders opens in a later build",
                      "Snoozing opens in a later build"):
            self.assertIn(title, page)

    # -- states and constraints -------------------------------------------

    def test_the_empty_state_is_honest(self) -> None:
        app, client, accounts = self._make()
        sign_in(client)
        page = client.get("/reminders").get_data(as_text=True)
        self.assertIn("Nothing is nudging you right now.", page)
        self.assertIn("with its date on it", page)

    def test_the_three_states_render(self) -> None:
        app, client, _accounts, _first, _second = self._one_open_one_done()
        loading = client.get("/reminders?view=loading").get_data(as_text=True)
        self.assertIn("Loading your reminders", loading)
        error = client.get("/reminders?view=error").get_data(as_text=True)
        self.assertIn("couldn't load your reminders", error)
        self.assertIn("Try again", error)

    def test_the_page_has_no_inline_styles_or_hard_coded_colours(self) -> None:
        app, client, _accounts, _first, _second = self._one_open_one_done()
        for url in ("/reminders", "/reminders?tab=all", "/reminders?view=loading",
                    "/reminders?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertNotIn('style="', page, url)
            block = page_style_block(page)
            self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", block), url)
            self.assertIsNone(re.search(r"\brgba?\(", block), url)
            self.assertIsNone(re.search(r"\bhsla?\(", block), url)

    def test_exactly_one_primary_action_per_state(self) -> None:
        app, client, _accounts, _first, _second = self._one_open_one_done()
        for url in ("/reminders", "/reminders?tab=completed", "/reminders?view=error"):
            page = client.get(url).get_data(as_text=True)
            self.assertEqual(page.count("o-btn--primary"), 1, url)
        loading = client.get("/reminders?view=loading").get_data(as_text=True)
        self.assertEqual(loading.count("o-btn--primary"), 0)

    def test_the_screen_itself_adds_no_copper(self) -> None:
        app, client, _accounts, _first, _second = self._one_open_one_done()
        page = client.get("/reminders").get_data(as_text=True)
        self.assertNotIn("--copper", page_style_block(page))
        markup = re.sub(r"<style>.*?</style>", "", page, flags=re.S)
        self.assertNotIn("copper", markup)


class RemindersScreenSQLiteTests(RemindersScreenTests):
    """Every Reminders check again, against the SQLite stores production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_stores(self) -> None:
        from app.sqlite_store import SQLiteStore
        from heartbeat.sqlite_store import SQLiteHeartbeatManager

        app, _client, accounts = self._make()
        self.assertIsInstance(app.config["STORE"], SQLiteStore)
        self.assertIsInstance(app.config["HEARTBEAT"], SQLiteHeartbeatManager)

    def test_reminders_persist_across_a_reload(self) -> None:
        from heartbeat.sqlite_store import SQLiteHeartbeatManager

        directory, _audit_dir, app, client, accounts = make_sqlite_client()
        self.addCleanup(directory.cleanup)
        sign_in(client)
        ada = accounts.by_email("ada@example.dev")
        hb = app.config["HEARTBEAT"].for_account(ada["id"])
        (notice,) = fire(hb, 22)
        app.config["STORE"].database.close()

        # A fresh manager over the same database still knows the notice.
        from app.sqlite_store import SQLiteStore as FreshStore

        fresh_store = FreshStore(os.path.join(directory.name, "orbit.sqlite3"))
        self.addCleanup(fresh_store.database.close)
        manager = SQLiteHeartbeatManager(settings=settings(), database=fresh_store.database)
        history = manager.for_account(ada["id"]).history()
        self.assertEqual([n["id"] for n in history], [notice["id"]])
        self.assertIsNone(history[0]["dismissed_at"])


if __name__ == "__main__":
    unittest.main()
