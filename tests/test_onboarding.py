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
            store.verify_email(EMAIL, store.demo_code(EMAIL, "verify"))
            store.add_feedback(account["id"], "general", "Nice", "It is nice")
            reopened = AccountStore(path, iterations=1_000)
            self.assertTrue(reopened.by_id(account["id"])["email_verified"])
            self.assertEqual(len(reopened.feedback_for(account["id"])), 1)
            self.assertTrue(reopened.check_password(account["id"], PASSWORD))

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
        _, client, _ = make_client()
        text = client.get("/").get_data(as_text=True)
        self.assertIn("Open beta", text)
        self.assertIn("Create your free account", text)
        self.assertIn("How it works", text)
        self.assertIn("visibly disabled", text)  # the unfinished parts say so out loud


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
        app, client, _ = make_client()
        oauth(client)
        projects = app.config["PROJECTS"]
        st = app.config["STORE"]
        projects.create(user_id=st.user.id, name="Order bot", description="WhatsApp orders")
        text = client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Order bot", text)
        self.assertIn("Open workspace", text)      # continue strip
        self.assertIn("New project", text)          # quick actions
        self.assertNotIn("Three ways to start", text)  # zero-state stepped aside

    def test_a_second_visit_is_the_returning_state(self) -> None:
        app, client, _ = make_client()
        oauth(client)
        st = app.config["STORE"]
        app.config["PROJECTS"].create(user_id=st.user.id, name="Order bot", description="")
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
        app, client, _ = make_client()
        oauth(client)
        st = app.config["STORE"]
        resp = client.post("/projects", data={
            "name": "Order bot",
            "description": "WhatsApp ordering",
            "mode": "guided",
            "first_task": "Sketch the order flow",
        })
        self.assertEqual(resp.status_code, 303)
        self.assertIn("?guided=1", resp.headers["Location"])
        tasks = [t for t in st.tasks_for(st.user.id) if "Sketch the order flow" in t.description]
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
    def setUp(self) -> None:
        self.app, self.client, self.store = make_client()
        oauth(self.client)

    def test_settings_renders_every_section(self) -> None:
        text = self.client.get("/settings").get_data(as_text=True)
        for section in ("Profile", "Preferences", "Integrations", "Notifications", "Privacy &amp; security", "Account"):
            self.assertIn(section, text)
        self.assertIn("This device", text)
        self.assertIn("does not grant access to any repository", text.replace("&#39;", "'"))

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
        self.assertEqual(self.app.config["STORE"].thinking_level, "deep")  # applied, not just stored

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


if __name__ == "__main__":
    unittest.main()
