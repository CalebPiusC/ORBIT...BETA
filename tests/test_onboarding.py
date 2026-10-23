"""Offline tests for the onboarding & first-run experience.

Two layers, following the repo's conventions:

  * AccountStore — the account/onboarding database itself (registration rules,
    codes with expiry and attempt limits, sessions, personalization filtering,
    feedback, persistence), exercised in memory;
  * the Flask routes — the guard, the auth flows, the adaptive dashboard
    states, settings, feedback and the tour/checklist APIs, exercised through
    the test client with a low-cost pbkdf2 iteration count (the real code
    path, a fraction of the hashing cost).

The pre-existing suite creates ``create_app()`` with no accounts and must be
untouched by all of this: an earlier test in this file pins that decision
("no store → no wall").
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.accounts import (
    AccountError,
    AccountStore,
    checklist_for,
    recommendations_for,
)
from app.server import create_app

PASSWORD = "password1"
EMAIL = "ada@example.com"


def make_store() -> AccountStore:
    return AccountStore(iterations=1_000)


class AccountStoreTests(unittest.TestCase):
    def test_register_email_creates_an_unverified_account_with_a_code(self) -> None:
        store = make_store()
        account = store.register_email("Ada Osei", EMAIL, PASSWORD)
        self.assertFalse(account["email_verified"])
        self.assertEqual(account["provider"], "email")
        self.assertIsNotNone(store.demo_code(EMAIL, "verify"))
        self.assertTrue(store.check_password(account["id"], PASSWORD))
        self.assertFalse(store.check_password(account["id"], "wrong-password"))

    def test_registration_rejects_bad_input_and_duplicates(self) -> None:
        store = make_store()
        with self.assertRaises(AccountError):
            store.register_email("Ada", "not-an-email", PASSWORD)
        with self.assertRaises(AccountError):
            store.register_email("Ada", EMAIL, "short")
        store.register_email("Ada", EMAIL, PASSWORD)
        with self.assertRaises(AccountError):
            store.register_email("Ada Two", " ADA@example.com ", PASSWORD)  # same person, case/space-insensitively

    def test_verify_consumes_attempts_and_confirms(self) -> None:
        store = make_store()
        account = store.register_email("Ada", EMAIL, PASSWORD)
        code = store.demo_code(EMAIL, "verify")
        wrong = "000000" if code != "000000" else "111111"
        self.assertEqual(store.verify_email(EMAIL, wrong), "wrong")
        self.assertEqual(store.attempts_left(EMAIL, "verify"), 5)
        self.assertEqual(store.verify_email(EMAIL, code), "ok")
        self.assertTrue(store.by_id(account["id"])["email_verified"])
        self.assertIsNone(store.demo_code(EMAIL, "verify"))  # consumed

    def test_verify_locks_out_after_six_misses(self) -> None:
        store = make_store()
        store.register_email("Ada", EMAIL, PASSWORD)
        code = store.demo_code(EMAIL, "verify")
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(6):
            store.verify_email(EMAIL, wrong)
        self.assertEqual(store.verify_email(EMAIL, code), "locked")

    def test_expired_codes_are_rejected(self) -> None:
        store = make_store()
        store.register_email("Ada", EMAIL, PASSWORD)
        code = store.demo_code(EMAIL, "verify")

        def backdate(data):
            data["codes"][f"verify:{EMAIL}"]["expires_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=11)
            ).isoformat(timespec="seconds")

        store._update(backdate)
        self.assertEqual(store.verify_email(EMAIL, code), "expired")

    def test_oauth_signin_is_verified_immediately_and_reusable(self) -> None:
        store = make_store()
        account, is_new = store.oauth_signin("github", "Grace Hopper", "grace@example.dev")
        self.assertTrue(is_new)
        self.assertTrue(account["email_verified"])
        self.assertEqual(account["provider"], "github")
        again, is_new_again = store.oauth_signin("github", "Grace Hopper", "grace@example.dev")
        self.assertFalse(is_new_again)
        self.assertEqual(again["id"], account["id"])
        with self.assertRaises(AccountError):
            store.oauth_signin("gitlab", "X", "x@example.com")

    def test_reset_flow_rotates_the_password_and_ends_sessions(self) -> None:
        store = make_store()
        account = store.register_email("Ada", EMAIL, PASSWORD)
        sid = store.start_session(account["id"], "agent")
        store.start_reset(EMAIL)
        code = store.demo_code(EMAIL, "reset")
        self.assertEqual(store.complete_reset(EMAIL, code, "newpassword9"), "ok")
        self.assertFalse(store.check_password(account["id"], PASSWORD))
        self.assertTrue(store.check_password(account["id"], "newpassword9"))
        self.assertFalse(store.session_valid(account["id"], sid))  # every session ended
        # Resetting also confirms the address — inbox access was just proven.
        self.assertTrue(store.by_id(account["id"])["email_verified"])

    def test_reset_is_silent_for_unknown_addresses(self) -> None:
        store = make_store()
        store.start_reset("nobody@example.com")  # no exception, and no code either
        self.assertIsNone(store.demo_code("nobody@example.com", "reset"))

    def test_sessions_are_tracked_and_revocable(self) -> None:
        store = make_store()
        account, _ = store.oauth_signin("google", "Ada", "ada2@example.com")
        sid1 = store.start_session(account["id"], "a")
        sid2 = store.start_session(account["id"], "b")
        self.assertTrue(store.session_valid(account["id"], sid1))
        store.revoke_other_sessions(account["id"], sid2)
        self.assertFalse(store.session_valid(account["id"], sid1))
        self.assertTrue(store.session_valid(account["id"], sid2))
        store.end_session(account["id"], sid2)
        self.assertFalse(store.session_valid(account["id"], sid2))

    def test_personalization_filters_invented_options_and_skip_is_a_decision(self) -> None:
        store = make_store()
        account, _ = store.oauth_signin("google", "Ada", "ada2@example.com")
        store.update_personalization(
            account["id"],
            role="Professional developer",
            experience="3–5 years",
            interests=["Web apps", "Time travel"],  # the second does not exist
            languages=["Python", "Klingon"],
        )
        profile = store.by_id(account["id"])["profile"]
        self.assertEqual(profile["interests"], ["Web apps"])
        self.assertEqual(profile["languages"], ["Python"])
        self.assertTrue(store.by_id(account["id"])["onboarding"]["personalized"])

        store.update_personalization(account["id"], skipped=True)
        onboarding = store.by_id(account["id"])["onboarding"]
        self.assertTrue(onboarding["personalize_skipped"])
        # …and the checklist counts a skip as resolved, not as unfinished work.
        items = checklist_for(store.by_id(account["id"]), 0)
        personalize = next(item for item in items if item["key"] == "personalize")
        self.assertTrue(personalize["done"])

    def test_prefs_guard_unknown_keys(self) -> None:
        store = make_store()
        account, _ = store.oauth_signin("google", "Ada", "ada2@example.com")
        store.set_pref(account["id"], "default_depth", "deep")
        store.set_pref(account["id"], "theme", "purple")  # not a real pref: dropped
        prefs = store.by_id(account["id"])["prefs"]
        self.assertEqual(prefs["default_depth"], "deep")
        self.assertNotIn("theme", prefs)

    def test_recommendations_point_only_at_real_pages(self) -> None:
        cards = recommendations_for({"profile": {"interests": ["Data & ML"], "languages": ["Python"]}})
        hrefs = {card["href"] for card in cards}
        self.assertIn("/agents", hrefs)
        self.assertIn("/create#describe", hrefs)
        bare = [card.copy() for card in recommendations_for({"profile": {}})]
        self.assertTrue(any("Personalize" in card["title"] for card in bare))

    def test_feedback_validates_and_lists_per_account(self) -> None:
        store = make_store()
        account, _ = store.oauth_signin("google", "Ada", "ada2@example.com")
        with self.assertRaises(ValueError):
            store.add_feedback(account["id"], "feature", "", "no title")
        with self.assertRaises(ValueError):
            store.add_feedback(account["id"], "rant", "title", "body")
        entry = store.add_feedback(account["id"], "bug", "Import broke", "It did a thing", page="/create")
        self.assertTrue(entry["id"].startswith("f-"))
        entries = store.feedback_for(account["id"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Import broke")

    def test_file_persistence_survives_a_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            store = AccountStore(path, iterations=1_000)
            account = store.register_email("Ada", EMAIL, PASSWORD)
            code = store.demo_code(EMAIL, "verify")
            persisted = json.loads(path.read_text(encoding="utf-8"))
            code_record = persisted["codes"][f"verify:{EMAIL}"]
            self.assertNotIn("code", code_record)
            self.assertTrue(code_record["token_hash"])
            self.assertTrue(code_record["salt"])
            self.assertEqual(store.verify_email(EMAIL, code), "ok")
            store.add_feedback(account["id"], "general", "Nice", "It is nice")
            reopened = AccountStore(path, iterations=1_000)
            self.assertTrue(reopened.by_id(account["id"])["email_verified"])
            self.assertEqual(len(reopened.feedback_for(account["id"])), 1)
            self.assertTrue(reopened.check_password(account["id"], PASSWORD))

    def test_reopen_hashes_legacy_plaintext_verification_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            store = AccountStore(path, iterations=1_000)
            store.register_email("Ada", EMAIL, PASSWORD)
            legacy_code = store.demo_code(EMAIL, "verify")
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data["codes"][f"verify:{EMAIL}"]
            entry.pop("salt", None)
            entry.pop("token_hash", None)
            entry["code"] = legacy_code
            path.write_text(json.dumps(data), encoding="utf-8")

            reopened = AccountStore(path, iterations=1_000)
            migrated = json.loads(path.read_text(encoding="utf-8"))["codes"][f"verify:{EMAIL}"]
            self.assertNotIn("code", migrated)
            self.assertIn("token_hash", migrated)
            self.assertEqual(reopened.verify_email(EMAIL, legacy_code), "ok")

    def test_delete_account_scrubs_account_codes_and_feedback(self) -> None:
        store = make_store()
        account = store.register_email("Ada", EMAIL, PASSWORD)
        store.add_feedback(account["id"], "bug", "B", "D")
        store.delete_account(account["id"])
        self.assertIsNone(store.by_id(account["id"]))
        self.assertIsNone(store.by_email(EMAIL))
        self.assertEqual(store.feedback_for(account["id"]), [])
        self.assertIsNone(store.demo_code(EMAIL, "verify"))


class FakeProvider:
    """The smallest streaming provider the app needs for template tests."""

    def reply(self, history, system_prompt: str) -> str:
        return "canned"

    def reply_stream(self, history, system_prompt: str, **kwargs):
        from providers.base import StreamChunk

        yield StreamChunk(kind="answer", text="canned")


def make_client() -> tuple:
    store = make_store()
    app = create_app(provider=FakeProvider(), accounts=store)
    app.config["TESTING"] = True
    return app, app.test_client(), store


def make_sqlite_client():
    """The same app, wired the way production boots: SQLite-backed stores.

    Mirrors ``run_app`` without the server: the chat store, accounts and
    projects all share one database file, so these tests exercise the store
    adapter the shipped app actually uses.
    """
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


def signup(client, email: str = EMAIL, name: str = "Ada Osei"):
    return client.post(
        "/auth/signup",
        data={"name": name, "email": email, "password": PASSWORD, "confirm": PASSWORD},
    )


def verify(client, store: AccountStore, email: str = EMAIL):
    return client.post("/auth/verify", data={"code": store.demo_code(email, "verify")})


def oauth(client, provider: str = "github", email: str = "grace@example.dev", name: str = "Grace Hopper"):
    return client.post(f"/auth/oauth/{provider}", data={"name": name, "email": email})


class GuardTests(unittest.TestCase):
    def test_app_pages_redirect_signed_out_visitors_to_login(self) -> None:
        _, client, _ = make_client()
        for path in ("/dashboard", "/projects", "/create", "/agents", "/settings", "/feedback", "/welcome", "/onboarding"):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 302, path)
            self.assertIn("/auth/login", resp.headers["Location"], path)

    def test_public_pages_are_really_public(self) -> None:
        _, client, _ = make_client()
        for path in ("/", "/landing", "/auth/login", "/auth/signup", "/auth/forgot", "/auth/oauth/github", "/auth/oauth/google"):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200, path)
        resp = client.get("/static/onboarding.js")
        self.assertEqual(resp.status_code, 200)
        resp = client.get("/static/public.js")
        self.assertEqual(resp.status_code, 200)

    def test_a_launch_without_accounts_has_no_wall_at_all(self) -> None:
        # The pre-existing suite's launch mode: no store → everything behaves
        # as the single-user app, and auth pages honestly say they're off.
        app = create_app(provider=FakeProvider())
        client = app.test_client()
        self.assertEqual(client.get("/dashboard").status_code, 200)
        self.assertEqual(client.get("/settings").status_code, 302)  # redirects home, not an error
        resp = client.get("/auth/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("accounts off", resp.get_data(as_text=True))

    def test_landing_page_is_the_signed_out_front_door(self) -> None:
        # Copy follows the ORBIT UI___2 design frames, which superseded the
        # earlier long-form beta page.
        _, client, _ = make_client()
        text = client.get("/").get_data(as_text=True)
        self.assertIn("Sign in", text)
        self.assertIn("How it works", text)
        self.assertIn("Start your project", text)
        self.assertIn("Create your free workspace", text)

    def test_landing_page_never_presents_a_provider_as_connected(self) -> None:
        """The landing page may name import entry points, never live connections.

        The ORBIT UI___2 frames replaced the long-form beta copy, so the older
        "No Git provider is connected yet" paragraph is gone. What survives is
        the part that matters: no provider may be described as already
        connected, and no attachment or voice capability may be claimed.
        """
        _, client, _ = make_client()
        text = client.get("/landing").get_data(as_text=True)

        self.assertNotIn("GitHub is connected", text)
        self.assertNotIn("a GitHub connection", text)
        self.assertNotIn("Connected to GitHub", text)

        # Attachments are never uploaded to the model, so they are not offered.
        self.assertNotIn("Attach files", text)

        # Voice is not presented as shipping.
        self.assertNotIn("voice is ready", text.lower())


class AuthFlowTests(unittest.TestCase):
    def test_signup_validation_errors_rerender_with_values(self) -> None:
        _, client, _ = make_client()
        resp = client.post("/auth/signup", data={"name": "", "email": "bad", "password": "x", "confirm": "y"})
        self.assertEqual(resp.status_code, 422)
        resp = client.post("/auth/signup", data={"name": "Ada", "email": "bad", "password": PASSWORD, "confirm": PASSWORD})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("valid email", resp.get_data(as_text=True))
        resp = client.post("/auth/signup", data={"name": "Ada", "email": EMAIL, "password": PASSWORD, "confirm": "different1"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.get_data(as_text=True).count("Ada"), 2)  # name value was kept, not wiped

    def test_the_full_email_path_signup_verify_welcome(self) -> None:
        _, client, store = make_client()
        resp = signup(client)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/auth/verify", resp.headers["Location"])
        # Unverified: every app page funnels to the code screen.
        resp = client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/verify", resp.headers["Location"])
        resp = client.post("/auth/verify", data={"code": "999999"})
        self.assertEqual(resp.status_code, 422)
        resp = verify(client, store)
        self.assertIn("/welcome", resp.headers["Location"])
        text = client.get("/welcome").get_data(as_text=True)
        for option in ("Create a new project", "Import an existing project", "Connect a Git provider", "Explore ORBIT"):
            self.assertIn(option, text)
        self.assertIn("skip", text.lower())  # the way out is visible

    def test_login_errors_and_success(self) -> None:
        _, client, store = make_client()
        signup(client)
        verify(client, store)
        client.get("/auth/logout")
        resp = client.post("/auth/login", data={"email": "nobody@example.com", "password": PASSWORD})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("couldn&#39;t find an account", resp.get_data(as_text=True))
        resp = client.post("/auth/login", data={"email": EMAIL, "password": "wrong-password"})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("doesn&#39;t match", resp.get_data(as_text=True))
        resp = client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD})
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/dashboard", resp.headers["Location"])

    def test_login_with_unverified_email_lands_back_on_verify(self) -> None:
        _, client, store = make_client()
        signup(client)
        client.get("/auth/logout")
        resp = client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD})
        self.assertIn("/auth/verify", resp.headers["Location"])

    def test_forgot_reset_round_trip(self) -> None:
        _, client, store = make_client()
        signup(client)
        client.get("/auth/logout")
        resp = client.post("/auth/forgot", data={"email": EMAIL})
        self.assertIn("/auth/reset", resp.headers["Location"])
        code = store.demo_code(EMAIL, "reset")
        resp = client.post("/auth/reset", data={"code": code, "password": "newpassword9", "confirm": "newpassword9"})
        self.assertIn("/auth/login", resp.headers["Location"])
        self.assertIn("reset=1", resp.headers["Location"])
        resp = client.post("/auth/login", data={"email": EMAIL, "password": "newpassword9"})
        self.assertIn("/dashboard", resp.headers["Location"])

    def test_oauth_pages_are_honest_about_scope(self) -> None:
        _, client, _ = make_client()
        github = client.get("/auth/oauth/github").get_data(as_text=True)
        self.assertIn("Demo flow", github)
        self.assertIn("not grants access to any repository".replace("not", "never"), github)
        self.assertIn("no real request is made", github)
        google = client.get("/auth/oauth/google").get_data(as_text=True)
        self.assertIn("Demo flow", google)

    def test_oauth_signin_new_vs_returning(self) -> None:
        _, client, store = make_client()
        resp = oauth(client, "github")
        self.assertIn("/welcome", resp.headers["Location"])
        account = store.by_email("grace@example.dev")
        self.assertTrue(account["email_verified"])  # provider-confirmed: no code step
        client.get("/auth/logout")
        resp = oauth(client, "github")
        self.assertIn("/dashboard", resp.headers["Location"])  # known identity: no re-welcoming

    def test_open_redirect_guards(self) -> None:
        _, client, store = make_client()
        signup(client)
        verify(client, store)
        client.get("/auth/logout")
        resp = client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD, "next": "//evil.example"})
        self.assertNotIn("evil.example", resp.headers["Location"])


class DashboardStateTests(unittest.TestCase):
    def test_new_account_gets_guidance_not_an_empty_room(self) -> None:
        _, client, _ = make_client()
        oauth(client)
        text = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Getting set up", text)          # the checklist carries the page
        self.assertIn("Three ways to start", text)     # direct / guided / describe
        self.assertIn("get your first project", text)

    def test_personalization_answers_turn_into_real_recommendations(self) -> None:
        _, client, _ = make_client()
        oauth(client)
        client.post("/onboarding", data={
            "role": "Professional developer",
            "experience": "3–5 years",
            "interests": ["Data & ML"],
            "languages": ["Python"],
        })
        text = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Research specialist", text)   # Data & ML maps to the Research agent
        self.assertIn("Python", text)
        # …and the checklist tracks the resolution honestly (verify + personalize done).
        self.assertIn("2 of 4 done", text)

    def test_skip_marks_the_item_done_without_nagging(self) -> None:
        _, client, store = make_client()
        oauth(client)
        client.post("/onboarding", data={"skip": "1"})
        account = store.by_email("grace@example.dev")
        self.assertTrue(account["onboarding"]["personalize_skipped"])

    def test_dashboard_adapts_when_projects_exist(self) -> None:
        app, client, accounts = make_client()
        oauth(client)
        projects = app.config["PROJECTS"]
        grace_id = accounts.by_email("grace@example.dev")["id"]
        projects.create(user_id=grace_id, name="Order bot", description="WhatsApp orders")
        text = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Order bot", text)
        self.assertIn("Open workspace", text)      # continue strip
        self.assertIn("New project", text)          # quick actions
        self.assertNotIn("Three ways to start", text)  # zero-state stepped aside

    def test_a_second_visit_is_the_returning_state(self) -> None:
        app, client, accounts = make_client()
        oauth(client)
        grace_id = accounts.by_email("grace@example.dev")["id"]
        app.config["PROJECTS"].create(user_id=grace_id, name="Order bot", description="")
        client.get("/dashboard")  # first visit marks it
        text = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Welcome back", text)
        self.assertIn("Jump back in", text)

    def test_checklist_dismiss_persists(self) -> None:
        _, client, store = make_client()
        oauth(client)
        resp = client.post("/api/onboarding/checklist", json={"dismissed": True})
        self.assertEqual(resp.get_json(), {"ok": True, "dismissed": True})
        self.assertNotIn("Getting set up", client.get("/dashboard").get_data(as_text=True))
        account = store.by_email("grace@example.dev")
        self.assertTrue(account["onboarding"]["checklist_dismissed"])

    def test_tour_state_round_trip(self) -> None:
        _, client, store = make_client()
        oauth(client)
        text = client.get("/dashboard?tour=1").get_data(as_text=True)
        self.assertIn("data-tour-autostart", text)
        resp = client.post("/api/onboarding/tour", json={"state": "done"})
        self.assertTrue(resp.get_json()["ok"])
        account = store.by_email("grace@example.dev")
        self.assertEqual(account["onboarding"]["tour"], "done")
        # Finished tours do not auto-start again.
        self.assertNotIn("data-tour-autostart", client.get("/dashboard?tour=1").get_data(as_text=True))
        # …unless the person explicitly asks for a replay from Settings.
        client.post("/settings/tour-replay")
        self.assertIn("data-tour-autostart", client.get("/dashboard").get_data(as_text=True) if False else client.get("/dashboard?tour=1").get_data(as_text=True))

    def test_tour_rejects_invented_states(self) -> None:
        _, client, _ = make_client()
        oauth(client)
        resp = client.post("/api/onboarding/tour", json={"state": "exploded"})
        self.assertEqual(resp.status_code, 400)


class ProjectEntryTests(unittest.TestCase):
    def test_guided_setup_creates_the_project_with_its_first_task(self) -> None:
        app, client, accounts = make_client()
        oauth(client)
        grace_id = accounts.by_email("grace@example.dev")["id"]
        st = app.config["STORE"].for_account(grace_id)
        resp = client.post("/projects", data={
            "name": "Order bot",
            "description": "WhatsApp ordering",
            "mode": "guided",
            "first_task": "Sketch the order flow",
        })
        self.assertEqual(resp.status_code, 303)
        self.assertIn("?guided=1", resp.headers["Location"])
        tasks = [t for t in st.tasks_for() if "Sketch the order flow" in t.description]
        self.assertEqual(len(tasks), 1)
        self.assertIsNotNone(tasks[0].project_id)

    def test_describe_mode_lands_on_a_primed_workspace(self) -> None:
        _, client, _ = make_client()
        oauth(client)
        resp = client.post("/projects", data={
            "name": "Reading tracker",
            "description": "Track books I want to read",
            "mode": "describe",
        })
        self.assertIn("plan=1", resp.headers["Location"])

    def test_create_page_offers_all_three_modes_and_the_tip(self) -> None:
        _, client, _ = make_client()
        oauth(client)
        text = client.get("/create").get_data(as_text=True)
        for phrase in ("Create directly", "Guided setup", "Describe to the Main Agent", "Not connected", "Not built yet"):
            self.assertIn(phrase, text)


class SettingsPageTests(unittest.TestCase):
    WIZARD_EMAIL = "grace@example.dev"   # the account oauth() signs in

    def setUp(self) -> None:
        self.app, self.client, self.store = make_client()
        oauth(self.client)

    def test_settings_renders_every_section(self) -> None:
        text = self.client.get("/settings").get_data(as_text=True)
        # The frame's three tabs, and the real sections under them.
        for section in ("General", "Notifications", "Privacy",
                        "Your surroundings", "Only the useful nudges",
                        "Your approval is the rule.", "Your data, your choice."):
            self.assertIn(section, text)
        privacy = self.client.get("/settings?tab=privacy").get_data(as_text=True)
        self.assertIn("Password &amp; sign-in", privacy)
        self.assertIn("Signed-in browsers", privacy)
        self.assertIn("Delete your account", privacy)
        self.assertIn("this browser", privacy)

    def test_integrations_reports_nothing_as_connected(self) -> None:
        """No integration is built, so none may read as connected.

        GitHub used to be seeded as "connected" here while no GitHub code
        existed anywhere in the app. Settings now hands integrations to the
        Connected apps screen — and that screen keeps the honesty.
        """
        text = self.client.get("/settings").get_data(as_text=True)
        self.assertNotIn("Connected to your workspace.", text)
        connected = self.client.get("/connected").get_data(as_text=True)
        self.assertEqual(connected.count(">Not connected<"), 0)  # rendered as meta text
        self.assertEqual(connected.count("Not connected"), 4)  # GitHub, Figma, Notion, Slack

    def test_profile_and_preferences_save(self) -> None:
        self.client.post("/settings/profile", data={"name": "Grace M. Hopper", "workspace": "Compiler crew"})
        account = self.store.by_email("grace@example.dev")
        self.assertEqual(account["name"], "Grace M. Hopper")
        self.assertEqual(account["workspace"], "Compiler crew")
        self.client.post("/settings/preferences", data={
            "role": "Student", "experience": "New to coding",
            "interests": ["Web apps"], "languages": ["Python"], "default_depth": "deep",
        })
        account = self.store.by_email("grace@example.dev")
        self.assertEqual(account["profile"]["role"], "Student")
        self.assertEqual(account["prefs"]["default_depth"], "deep")
        grace_id = self.store.by_email("grace@example.dev")["id"]
        self.assertEqual(self.app.config["STORE"].for_account(grace_id).thinking_level, "deep")  # applied, not just stored

    def test_password_change_requires_and_rotates(self) -> None:
        self.client.post("/settings/password", data={"new": "newpassword9", "confirm": "newpassword9"})
        account = self.store.by_email("grace@example.dev")
        self.assertTrue(self.store.check_password(account["id"], "newpassword9"))
        resp = self.client.post("/settings/password", data={
            "current": "wrong-current", "new": "anotherpass9", "confirm": "anotherpass9",
        })
        self.assertIn("password-current", resp.headers["Location"])
        resp = self.client.post("/settings/password", data={
            "current": "newpassword9", "new": "anotherpass9", "confirm": "differentpass9",
        })
        self.assertIn("password-mismatch", resp.headers["Location"])

    def test_revoke_others_keeps_this_browser_signed_in(self) -> None:
        account = self.store.by_email("grace@example.dev")
        self.store.start_session(account["id"], "another browser")
        self.client.post("/settings/sessions/revoke-others")
        self.assertEqual(self.client.get("/settings").status_code, 200)
        account = self.store.by_email("grace@example.dev")
        self.assertEqual(len(account["sessions"]), 1)

    def test_delete_requires_the_email_and_then_logs_out(self) -> None:
        resp = self.client.post("/settings/delete", data={"confirm": "wrong@example.dev"})
        self.assertIn("delete-confirm", resp.headers["Location"])
        resp = self.client.post("/settings/delete", data={"confirm": "grace@example.dev"})
        self.assertIn("deleted=1", resp.headers["Location"])
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)


class FeedbackTests(unittest.TestCase):
    WIZARD_EMAIL = "grace@example.dev"   # the account oauth() signs in

    def setUp(self) -> None:
        self.app, self.client, self.store = make_client()
        oauth(self.client)

    def test_page_form_posts_and_lists(self) -> None:
        resp = self.client.post("/feedback", data={"kind": "feature", "title": "ZIP import", "body": "Let me drop a zip"})
        self.assertIn("sent=1", resp.headers["Location"])
        text = self.client.get("/feedback").get_data(as_text=True)
        self.assertIn("ZIP import", text)
        self.assertIn("Thank you", text)

    def test_widget_api_never_navigates_and_validates(self) -> None:
        resp = self.client.post("/api/feedback", json={"kind": "bug", "title": "X", "body": "Y", "page": "/create"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["id"].startswith("f-"))
        resp = self.client.post("/api/feedback", json={"kind": "bug", "title": "", "body": ""})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_empty_state_is_honest(self) -> None:
        text = self.client.get("/feedback").get_data(as_text=True)
        self.assertIn("No notes yet", text)


class OnboardingWizardTests(unittest.TestCase):
    """The rebuilt onboarding wizard: its step contract, its keyboard hooks and
    the save-before-advance rule that keeps a typed name from being dropped.

    These are structural and server-side assertions on purpose: the repo's
    suite runs offline with no browser, so anything that depends on paint is
    asserted through the markup the script binds to, and the failure path is
    proven against the real HTTP endpoint.
    """

    WIZARD_EMAIL = "grace@example.dev"   # the account oauth() signs in

    def setUp(self) -> None:
        self.app, self.client, self.store = make_client()
        oauth(self.client)

    def page(self) -> str:
        resp = self.client.get("/onboarding")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    # ----- the three drawn steps -------------------------------------------

    def test_three_steps_and_only_the_first_is_open(self) -> None:
        text = self.page()
        # three step sections, exactly one active
        self.assertEqual(text.count('class="onb-step is-active"'), 1)
        self.assertEqual(text.count('class="onb-step"'), 2)
        # and only the first is not hidden
        self.assertEqual(text.count('data-step="0" aria-labelledby'), 1)
        self.assertIn('data-step="1" aria-labelledby="step-1-title" hidden', text)
        self.assertIn('data-step="2" aria-labelledby="step-2-title" hidden', text)

    def test_stepper_labels_start_on_step_one(self) -> None:
        text = self.page()
        self.assertIn('data-step-label="0" class="is-current"', text)
        self.assertIn("01 / Your workspace", text)
        self.assertIn("02 / Starting point", text)
        self.assertIn("03 / Ready to build", text)

    def test_frame_copy_and_controls_are_present(self) -> None:
        text = self.page()
        for phrase in (
            "A workspace, shaped around you.",
            "A few small details help Orbit meet you where you are.",
            "What should we call you?",
            "Workspace name",
            "How familiar is building software?",
            "A fresh start",
            "Finding my feet",
            "Comfortably curious",
            "Continue to your starting point",
            "Save &amp; exit",
            "You don’t need all the answers.",
        ):
            self.assertIn(phrase, text)

    # ----- keyboard reachability ------------------------------------------

    def test_every_step_control_is_a_real_button(self) -> None:
        """The stepper is a script, so its controls must be natively tabbable
        and Enter/Space-activatable: real <button> elements, never divs."""
        text = self.page()
        for hook in ("data-next=", "data-back="):
            # each occurrence sits on a <button ...>
            for fragment in text.split(hook)[1:]:
                opening = text[max(0, text.index(fragment) - 200):text.index(fragment)]
                self.assertIn("<button", opening, hook)

    def test_steps_are_labelled_for_assistive_tech(self) -> None:
        text = self.page()
        # the list of steps is an ordered list; the script sets aria-current
        self.assertIn('<ol class="onb-steps" id="onbSteps">', text)
        self.assertIn("aria-current", text)  # the script's contract

    def test_headings_receive_focus_after_advancing(self) -> None:
        """Focus is moved to the new step's heading, and headings are made
        focusable, so a keyboard user is never dropped back at the top of the
        document with no announcement."""
        text = self.page()
        self.assertIn('setAttribute("tabindex", "-1")', text)
        self.assertIn('heading.focus({ preventScroll: true })', text)

    # ----- save-before-advance --------------------------------------------

    def test_advance_is_gated_on_a_successful_save(self) -> None:
        text = self.page()
        # the failure branch returns before the step changes
        self.assertIn("if (!saved) { showSaveError(true); return; }", text)
        # and the error is a live region with real text
        self.assertIn('id="profile-save-error" role="alert"', text)
        self.assertIn("We couldn’t save your details just now", text)

    def test_save_refused_degrades_to_false_not_true(self) -> None:
        """A network failure must resolve false — never fall through as a
        successful save, which is what let the wizard advance into a dropped
        name before this test existed."""
        text = self.page()
        self.assertIn("return false;   // offline or blocked", text)
        self.assertNotIn("catch(function () { /* offline: the wizard answers still save */ })", text)

    def test_submit_is_blocked_while_the_save_is_in_flight(self) -> None:
        text = self.page()
        self.assertIn('if (form.dataset.saving === "1") return;', text)
        self.assertIn('if (skip.dataset.saving === "1") return;', text)

    def test_profile_endpoint_reports_failure_the_way_the_page_reads_it(self) -> None:
        """The page treats a non-2xx as a refused save. Prove the endpoint can
        actually answer that way, so the branch is not unreachable in practice:
        an empty name is rejected with a 303 back to settings#profile, while a
        real name is accepted."""
        resp = self.client.post("/settings/profile", data={"name": "", "workspace": "X"})
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error=name", resp.headers["Location"])

        resp = self.client.post("/settings/profile", data={"name": "Ada Osei", "workspace": "Fieldnotes"})
        self.assertEqual(resp.status_code, 303)
        self.assertIn("saved=profile", resp.headers["Location"])
        self.assertEqual(self.store.by_email(self.WIZARD_EMAIL)["name"], "Ada Osei")
        self.assertEqual(self.store.by_email(self.WIZARD_EMAIL)["workspace"], "Fieldnotes")

    def test_save_and_exit_records_a_real_skip(self) -> None:
        resp = self.client.post("/onboarding", data={"skip": "1"})
        self.assertEqual(resp.status_code, 303)
        account = self.store.by_email(self.WIZARD_EMAIL)
        self.assertTrue(account["onboarding"]["personalize_skipped"])

    def test_wizard_answers_still_land_through_the_new_form(self) -> None:
        resp = self.client.post("/onboarding", data={
            "experience": "1–2 years",
            "interests": ["Data & ML"],
            "languages": ["Python"],
        })
        self.assertEqual(resp.status_code, 303)
        profile = self.store.by_email(self.WIZARD_EMAIL)["profile"]
        self.assertEqual(profile["experience"], "1–2 years")
        self.assertEqual(profile["interests"], ["Data & ML"])
        self.assertEqual(profile["languages"], ["Python"])


class DerivedAuthScreenTests(unittest.TestCase):
    """Sign up, verify, forgot and reset have no frames of their own.

    They were built on the Sign in frame's shell, so these tests pin the two
    things that make that claim true: every one of them renders through the
    shared shell (same card, same chrome, one primary button), and the copy
    that existed because the old pages made a promise or held a privacy line
    still exists after the rebuild.
    """

    def setUp(self) -> None:
        self.app, self.client, self.store = make_client()

    def shell(self, text: str) -> None:
        self.assertIn('class="auth-card"', text)
        self.assertIn('id="authTitle"', text)          # the shared shell's heading id
        self.assertIn("A little clarity goes a long way.", text)   # the frame's marketing column
        self.assertIn("A calmer way to build.", text)              # the frame's footer
        self.assertEqual(text.count("o-btn--primary"), 1, "exactly one primary button per screen")

    def test_signup_renders_through_the_sign_in_shell(self) -> None:
        text = self.client.get("/auth/signup").get_data(as_text=True)
        self.shell(text)
        # Provider rows are drawn, but honestly: no real OAuth exists yet, so
        # they are disabled buttons marked coming soon, never links.
        self.assertIn("Continue with Google", text)
        self.assertIn("Continue with GitHub", text)
        self.assertIn("Coming soon", text)
        self.assertNotIn('href="/auth/oauth/google', text)
        self.assertNotIn('href="/auth/oauth/github', text)
        self.assertIn("or use email", text)
        # every field the server actually reads
        for field in ('name="name"', 'name="email"', 'name="password"', 'name="confirm"'):
            self.assertIn(field, text)

    def test_verify_renders_through_the_shell_and_stays_honest(self) -> None:
        signup(self.client)
        text = self.client.get("/auth/verify").get_data(as_text=True)
        self.shell(text)
        self.assertIn('id="code"', text)
        # the sandbox really does send no mail, and the page says so
        self.assertIn("Demo build — no email is sent.", text)
        self.assertIn("Resend the code", text)

    def test_forgot_renders_through_the_shell_without_revealing_accounts(self) -> None:
        text = self.client.get("/auth/forgot").get_data(as_text=True)
        self.shell(text)
        self.assertIn("the next page answers the same either way", text)
        self.assertIn('name="email"', text)

    def test_reset_renders_through_the_shell(self) -> None:
        signup(self.client)
        verify(self.client, self.store)
        self.client.post("/auth/forgot", data={"email": EMAIL})
        text = self.client.get("/auth/reset").get_data(as_text=True)
        self.shell(text)
        for field in ('id="code"', 'id="password"', 'id="confirm"'):
            self.assertIn(field, text)
        self.assertIn("Update password", text)

    def test_no_auth_screen_offers_a_provider_link_that_cannot_work(self) -> None:
        """No auth screen may link to a provider flow it cannot really perform.

        The /auth/oauth routes still exist and are still honest when visited
        directly (they say "Demo flow"), but a control labelled "Continue with
        Google" must not pretend to start one. Each is a disabled <button>.
        """
        # Guests only: once signed in, both routes correctly redirect to the app.
        for path in ("/auth/login", "/auth/signup"):
            text = self.client.get(path).get_data(as_text=True)
            with self.subTest(path=path):
                self.assertNotIn('href="/auth/oauth/', text)
                self.assertEqual(text.count('class="o-provider o-provider--soon"'), 2)
                self.assertEqual(text.count('class="o-provider o-provider--soon"'), 2)
                self.assertEqual(text.count('disabled aria-disabled="true"'), 2)

    def test_no_auth_screen_ships_a_dead_legal_link(self) -> None:
        """Terms and Privacy are drawn as text in the frame's footer. There are
        no such routes, so no auth page may render them as links."""
        signup(self.client)
        for path in ("/auth/login", "/auth/signup", "/auth/forgot", "/auth/verify"):
            text = self.client.get(path).get_data(as_text=True)
            with self.subTest(path=path):
                self.assertNotIn('href="/terms"', text)
                self.assertNotIn('href="/privacy"', text)

    def test_accounts_off_still_renders_every_auth_page(self) -> None:
        """The pre-existing suite launches with no account store; every auth
        page must still answer 200 with the honest banner."""
        app = create_app(provider=FakeProvider())
        client = app.test_client()
        for path in ("/auth/login", "/auth/signup", "/auth/forgot"):
            resp = client.get(path)
            with self.subTest(path=path):
                self.assertEqual(resp.status_code, 200)
                self.assertIn("accounts off", resp.get_data(as_text=True))


class ButtonContrastTests(unittest.TestCase):
    """The one place a colour decision is asserted rather than eyeballed.

    Copper is the brand accent and the primary button is one of its four
    allowed uses, so the label on it must clear 4.5:1 in both themes — and in
    the hover state, which is where a darker fill silently broke AA.
    """

    @staticmethod
    def _relative_luminance(value: str) -> float:
        value = value.lstrip("#")
        channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def ratio(self, fg: str, bg: str) -> float:
        a, b = self._relative_luminance(fg), self._relative_luminance(bg)
        hi, lo = max(a, b), min(a, b)
        return (hi + 0.05) / (lo + 0.05)

    def test_primary_label_clears_aa_in_both_themes(self) -> None:
        pairs = {
            "light": ("#1B1208", "#C2692F"),
            "light hover": ("#1B1208", "#CC7839"),
            "dark": ("#0F0F10", "#D9814A"),
            "dark hover": ("#0F0F10", "#C9763F"),
        }
        for name, (fg, bg) in pairs.items():
            with self.subTest(state=name):
                self.assertGreaterEqual(self.ratio(fg, bg), 4.5, name)

    def test_the_tokens_match_what_is_asserted(self) -> None:
        """Guard against the CSS drifting away from the tested values."""
        css = (Path(__file__).resolve().parents[1] / "app" / "static" / "orbit.css").read_text()
        for token in (
            "--copper: #C2692F;",
            "--copper-hover: #CC7839;",
            "--copper-ink: #1B1208;",
            "--copper: #D9814A;",
            "--copper-hover: #C9763F;",
            "--copper-ink: #0F0F10;",
        ):
            self.assertIn(token, css)


class LayoutPassTests(unittest.TestCase):
    """Keep the cross-screen layout decisions from regressing silently.

    The templates carry their screen-specific CSS, so these checks assert the
    desktop scroll model and the measured frame rhythm without requiring a
    browser in the offline suite.
    """

    ROOT = Path(__file__).resolve().parents[1]

    @classmethod
    def _template(cls, name: str) -> str:
        return (cls.ROOT / "app" / "templates" / name).read_text(encoding="utf-8")

    def test_auth_sticky_panel_and_independent_form_scroller(self) -> None:
        css = self._template("auth_shell.html")
        desktop = css[css.index("@media (min-width: 1001px)"):]
        self.assertRegex(desktop, r"\.auth-marketing\s*\{[^}]*position:\s*sticky;")
        self.assertIn(".auth-card-col", desktop)
        self.assertIn("overflow-y: auto;", desktop)
        self.assertIn(".auth-field + .auth-field { margin-top: 24px; }", css)

    def test_onboarding_sticky_side_panel_and_independent_form_scroller(self) -> None:
        css = self._template("personalize.html")
        bounded = css[css.index("@media (min-width: 1001px) and (min-height: 640px)"):]
        self.assertRegex(bounded, r"\.onb-aside\s*\{[^}]*position:\s*sticky;")
        self.assertRegex(bounded, r"\.onb-card\s*\{[^}]*overflow-y:\s*auto;")
        # Onboarding's frame measures 16px between its two fields; the auth
        # frame's 24px rhythm is intentionally not carried across here.
        self.assertIn(".onb-field + .onb-field { margin-top: 16px; }", css)

    def test_short_window_gutters_and_app_shell_spacing_are_clamped(self) -> None:
        auth = self._template("auth_shell.html")
        onboarding = self._template("personalize.html")
        app_shell = self._template("app_shell.html")
        self.assertIn("--auth-gap: clamp(24px, 8vh, 104px);", auth)
        self.assertIn("clamp(24px, 5vh, 64px)", onboarding)
        self.assertIn("--app-pad-y: clamp(24px, 4.4vh, 40px);", app_shell)
        self.assertIn("--app-pad-bottom: clamp(24px, 5vh, 48px);", app_shell)
        self.assertIn("padding: var(--app-pad-y) var(--app-pad-x) var(--app-pad-bottom);", app_shell)


if __name__ == "__main__":
    unittest.main()


class DashboardDesignTests(unittest.TestCase):
    """The Overview screen as drawn in Dashboard · Light/Dark · Desktop/Mobile.

    These pin the promises the frame makes structurally: one primary action,
    no literal colour in the markup, a rail whose order matches the frame, and
    an honest treatment for the three destinations this build does not have.
    """

    def _active(self):
        app, client, accounts = make_client()
        oauth(client)
        account = accounts.by_email("grace@example.dev")
        project = app.config["PROJECTS"].create(
            user_id=account["id"], name="Fieldnotes", description="A quieter home for everything you want to read."
        )
        view = app.config["STORE"].for_account(account["id"])
        view.create_task("Save your first article", project_id=project.id)
        client.get("/dashboard")  # the first visit marks the return
        return app, client, project

    def test_one_primary_action_on_the_screen(self) -> None:
        _, client, _ = self._active()
        page = client.get("/dashboard").get_data(as_text=True)
        self.assertEqual(page.count("o-btn--primary"), 1)
        self.assertIn("New project", page)

    def test_markup_carries_no_literal_colour_or_inline_style(self) -> None:
        _, client, _ = self._active()
        page = client.get("/dashboard").get_data(as_text=True)
        self.assertNotIn('style="', page)
        self.assertNotIn("rgba(", page)
        self.assertIsNone(re.search(r"#[0-9A-Fa-f]{3,6}\b", page))

    def test_the_rail_follows_the_frame_order(self) -> None:
        _, client, _ = self._active()
        page = client.get("/dashboard").get_data(as_text=True)
        rail = page[page.index('class="app-nav"'):page.index("</aside>")]
        order = ["Overview", "Projects", "Agents", "Approvals", "Analytics", "Reminders", "Connected apps", "Settings"]
        positions = [rail.index(label) for label in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('class="app-nav__item is-active" href="/dashboard" aria-current="page"', rail)

    def test_every_rail_destination_is_a_real_link(self) -> None:
        _, client, _ = self._active()
        page = client.get("/dashboard").get_data(as_text=True)
        # Every rail destination is built and linked now — including the
        # three group-3 screens. Nothing lingers as a dead "Soon" row.
        for path in ("/approvals", "/reminders", "/connected"):
            self.assertIn(f'href="{path}"', page)
        self.assertEqual(page.count('class="app-nav__item is-soon"'), 0)  # rail
        self.assertEqual(page.count("app-nav__item app-nav__item--drawer is-soon"), 0)  # drawer

    def test_the_phone_menu_is_a_real_dialog_with_the_same_rail(self) -> None:
        _, client, _ = self._active()
        page = client.get("/dashboard").get_data(as_text=True)
        self.assertIn('id="appDrawer"', page)
        self.assertIn('aria-controls="appDrawer"', page)
        self.assertIn('aria-expanded="false"', page)
        self.assertIn("data-drawer-close", page)
        drawer = page[page.index('id="appDrawer"'):]
        self.assertIn('href="/projects"', drawer)
        self.assertIn('href="/settings"', drawer)

    def test_the_three_states_render(self) -> None:
        _, client, _ = self._active()
        loading = client.get("/dashboard?view=loading").get_data(as_text=True)
        self.assertIn("dash-skel", loading)
        self.assertIn("Loading your workspace", loading)
        error = client.get("/dashboard?view=error").get_data(as_text=True)
        self.assertIn("couldn't load your workspace", error)
        self.assertIn("Try again", error)
        self.assertIn("Open projects", error)

    def test_the_project_card_and_strip_name_real_work(self) -> None:
        _, client, project = self._active()
        page = client.get("/dashboard").get_data(as_text=True)
        card_start = page.index("dash-card__project")
        card = page[card_start:]
        self.assertIn("Fieldnotes", card)
        self.assertIn("Open Project Map", card)
        self.assertIn(f'href="/projects/{project.id}"', card)
        strip = page[page.index('class="continue-strip"'):]
        self.assertIn("Fieldnotes", strip)
        self.assertIn("Open workspace", strip)

    def test_the_drawer_and_rail_share_no_missing_page(self) -> None:
        _, client, _ = self._active()
        page = client.get("/dashboard").get_data(as_text=True)
        # Every href in the chrome resolves to a route this build serves.
        for href in re.findall(r'href="(/[^"#?]*)"', page):
            if href.startswith("/projects/") or href.startswith("/static/"):
                continue
            self.assertIn(
                href, {"/dashboard", "/projects", "/agents", "/approvals", "/analytics", "/reminders", "/connected", "/settings", "/create", "/", "/onboarding"},
                href,
            )


class DashboardDataTests(unittest.TestCase):
    """The Overview roll-ups: real counts, honest empties, account isolation.

    Every figure the route adds is read through the store view bound to the
    signed-in account, so these tests drive two accounts with different work
    and assert that neither screen can see the other's rows.

    The SQLite subclass below re-runs every one of these checks against the
    production store, so the two implementations cannot drift apart.
    """

    use_sqlite = False

    def _make(self):
        if not self.use_sqlite:
            return make_client()
        directory, app, client, accounts = make_sqlite_client()
        store = app.config["STORE"]

        def _release_database() -> None:
            # addCleanup is LIFO, so this runs before the directory is removed.
            # A store call must leave no connection behind: on Windows a live
            # handle makes the temp directory undeletable, which is how these
            # tests failed there. Closing is a no-op when nothing leaked; the
            # count is what catches a regression.
            self.assertEqual(store.database.open_connection_count, 0)
            store.database.close()

        self.addCleanup(directory.cleanup)
        self.addCleanup(_release_database)
        return app, client, accounts

    @staticmethod
    def _stat(page: str, label: str) -> int | None:
        match = re.search(
            r"<span>(\d+)</span><span class=\"dash-stat__sep\">/</span>"
            r"<span>" + re.escape(label) + r"</span>",
            page,
        )
        return int(match.group(1)) if match else None

    @staticmethod
    def _proposal(view, summary: str, card_id: str) -> None:
        from app.store import ActionCard, ChatMessage

        conv = view.start_conversation()
        view.append_message(
            conv.id,
            ChatMessage(
                who="Orbit",
                time="09:12",
                body=summary,
                is_orbit=True,
                action=ActionCard(id=card_id, summary=summary),
            ),
        )

    def test_counts_read_the_signed_in_accounts_own_records(self) -> None:
        app, client, accounts = self._make()
        oauth(client)
        account = accounts.by_email("grace@example.dev")
        project = app.config["PROJECTS"].create(
            user_id=account["id"], name="Fieldnotes", description=""
        )
        view = app.config["STORE"].for_account(account["id"])
        view.create_task("Save your first article", project_id=project.id)
        second = view.create_task("Build the reading list", project_id=project.id)
        view.set_done(second.id)
        self._proposal(view, "Add this line to your notes: “Keep going”", "act-count")

        page = client.get("/dashboard").get_data(as_text=True)
        self.assertEqual(self._stat(page, "Active projects"), 1)
        self.assertEqual(self._stat(page, "Tasks completed"), 1)
        self.assertEqual(self._stat(page, "Pending proposals"), 1)
        # The proposal itself is named, and the proposal count is spelled out.
        self.assertIn("Keep going", page)
        self.assertIn("1 proposal waiting", page)
        # The activity feed carries the recorded line and its project.
        self.assertIn("Task created from your request.", page)
        self.assertIn("Fieldnotes · ", page)

    def test_one_accounts_dashboard_never_shows_anothers_work(self) -> None:
        app, client, accounts = self._make()
        oauth(client, email="ada@example.dev", name="Ada A")
        ada = accounts.by_email("ada@example.dev")
        ada_project = app.config["PROJECTS"].create(
            user_id=ada["id"], name="Alpha project", description=""
        )
        ada_view = app.config["STORE"].for_account(ada["id"])
        ada_task = ada_view.create_task("Build the alpha thing", project_id=ada_project.id)
        ada_view.set_done(ada_task.id)
        self._proposal(ada_view, "Alpha proposal: write the alpha note", "act-alpha")
        client.get("/auth/logout")

        oauth(client, email="bob@example.dev", name="Bob B")
        bob = accounts.by_email("bob@example.dev")
        bob_project = app.config["PROJECTS"].create(
            user_id=bob["id"], name="Beta project", description=""
        )
        bob_view = app.config["STORE"].for_account(bob["id"])
        bob_view.create_task("Build the beta thing", project_id=bob_project.id)
        self._proposal(bob_view, "Beta proposal: write the beta note", "act-beta")

        bob_page = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Beta project", bob_page)
        self.assertIn("Beta proposal", bob_page)
        self.assertEqual(self._stat(bob_page, "Tasks completed"), 0)
        for leak in ("Alpha project", "Alpha proposal", "Build the alpha thing"):
            self.assertNotIn(leak, bob_page)

        client.get("/auth/logout")
        oauth(client, email="ada@example.dev", name="Ada A")
        ada_page = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Alpha project", ada_page)
        self.assertIn("Alpha proposal", ada_page)
        self.assertEqual(self._stat(ada_page, "Tasks completed"), 1)
        for leak in ("Beta project", "Beta proposal", "Build the beta thing"):
            self.assertNotIn(leak, ada_page)

    def test_empty_states_are_named_not_faked(self) -> None:
        app, client, accounts = self._make()
        oauth(client)
        account = accounts.by_email("grace@example.dev")
        app.config["PROJECTS"].create(user_id=account["id"], name="A quiet project", description="")
        page = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("No pending approvals.", page)
        self.assertIn("No activity yet.", page)
        self.assertEqual(self._stat(page, "Pending proposals"), 0)
        self.assertEqual(self._stat(page, "Tasks completed"), 0)

    def test_activity_is_capped_at_ten_and_follows_task_creation(self) -> None:
        app, client, accounts = self._make()
        oauth(client)
        account = accounts.by_email("grace@example.dev")
        project = app.config["PROJECTS"].create(user_id=account["id"], name="Long list", description="")
        view = app.config["STORE"].for_account(account["id"])
        for index in range(12):
            view.create_task(f"Task number {index}", project_id=project.id)
        page = client.get("/dashboard").get_data(as_text=True)
        self.assertEqual(page.count("Task created from your request."), 10)
        # Every row names the task's project; the cap is the feed's, not the store's.
        self.assertEqual(page.count("Long list · "), 10)
        self.assertEqual(len(view.recent_activity(limit=10)), 10)
        self.assertEqual(len(view.recent_activity(limit=50)), 12)

    def test_resolving_a_proposal_removes_it_from_the_count(self) -> None:
        app, client, accounts = self._make()
        oauth(client)
        account = accounts.by_email("grace@example.dev")
        app.config["PROJECTS"].create(user_id=account["id"], name="Resolve project", description="")
        view = app.config["STORE"].for_account(account["id"])
        self._proposal(view, "Add this line to your notes: “Resolve me”", "act-resolve")
        self.assertEqual(
            self._stat(client.get("/dashboard").get_data(as_text=True), "Pending proposals"), 1
        )
        view.resolve_action("act-resolve", "approved", "ok")
        page = client.get("/dashboard").get_data(as_text=True)
        self.assertEqual(self._stat(page, "Pending proposals"), 0)
        self.assertIn("No pending approvals.", page)

    def test_a_proposal_the_chat_cannot_show_is_not_counted(self) -> None:
        """Scope by what the home screen can actually put in front of you.

        The chat shows one conversation and has no switcher, so a card left in
        an earlier conversation has no Approve control on screen. Counting it
        would make the dashboard's own "Review proposals" button unable to show
        what the number promised.
        """
        app, client, accounts = self._make()
        oauth(client)
        account = accounts.by_email("grace@example.dev")
        app.config["PROJECTS"].create(user_id=account["id"], name="Scope project", description="")
        view = app.config["STORE"].for_account(account["id"])
        self._proposal(view, "Add this line to your notes: “Behind you”", "act-old")
        self.assertEqual(
            self._stat(client.get("/dashboard").get_data(as_text=True), "Pending proposals"), 1
        )
        view.new_conversation()  # "+" opens a fresh, empty thread
        page = client.get("/dashboard").get_data(as_text=True)
        self.assertEqual(self._stat(page, "Pending proposals"), 0)
        self.assertIn("No pending approvals.", page)


class DashboardDataSQLiteTests(DashboardDataTests):
    """Every dashboard-data check, run against the SQLite store production uses."""

    use_sqlite = True

    def test_the_harness_really_uses_the_sqlite_store(self) -> None:
        from app.sqlite_accounts import SQLiteAccountStore
        from app.sqlite_store import SQLiteStore

        app, _client, accounts = self._make()
        self.assertIsInstance(app.config["STORE"], SQLiteStore)
        self.assertIsInstance(accounts, SQLiteAccountStore)
