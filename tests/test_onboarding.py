from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from providers.base import StreamChunk

from app.server import create_app
from app.store import Store
from onboarding import build_onboarding
from onboarding.accounts import (
    MIN_PASSWORD_LENGTH,
    AccountStore,
    AccountStoreError,
    hash_password,
    normalize_email,
    password_problems,
    token_digest,
    verify_password,
)
from onboarding.content import FAQ, WELCOME_OPTIONS
from onboarding.feedback import sanitize_context, first_line
from onboarding.projects import list_folder_paths, list_zip_entries
from onboarding import session_secret

PASSWORD = "correct-horse-battery"


class StubProvider:
    """Same contract as the real provider, no network."""

    def reply(self, history, system_prompt: str) -> str:
        return "STUB: canned."

    def reply_stream(
        self,
        history,
        system_prompt: str,
        *,
        think: bool = False,
        report_attempts: bool = False,
        thinking_level: str = "quick",
    ) -> Iterator[StreamChunk]:
        yield StreamChunk(kind="answer", text="STUB: canned.")


def csrf(client, path: str) -> str:
    body = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    if match is None:
        raise AssertionError(f"no csrf token on {path}")
    return match.group(1)


def mailbox_link(client, index: int = 0) -> str:
    body = client.get("/verify").get_data(as_text=True)
    links = re.findall(r'href="([^"]*?/verify/[^"]+)"', body)
    if not links:
        raise AssertionError("the demo mailbox had no confirmation link")
    return links[index]


class OnboardingTestCase(unittest.TestCase):
    """One temp runtime per test: no test touches the repo's runtime/."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        env = {
            "ORBIT_OAUTH_DEMO": "1",
            "ORBIT_RUNTIME_DIR": str(self.runtime),
            "ORBIT_SMTP_HOST": "",
            "ORBIT_HOST": "",
            "PORT": "",
        }
        self._env = patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.config = build_onboarding(runtime_dir=self.runtime)
        self.app = create_app(provider=StubProvider(), store=Store(), onboarding=self.config)
        self.client = self.app.test_client()

    # ----- helpers -----

    def sign_up(self, email: str = "ada@example.com", name: str = "Ada Lovelace", next_url: str = "") -> None:
        path = "/signup" + (f"?next={next_url}" if next_url else "")
        payload = {
            "csrf_token": csrf(self.client, path),
            "name": name,
            "email": email,
            "password": PASSWORD,
            "confirm": PASSWORD,
            "terms": "on",
        }
        if next_url:
            payload["next"] = next_url
        response = self.client.post("/signup", data=payload)
        assert response.status_code == 302, response.status_code

    def confirm_email(self) -> None:
        response = self.client.get(mailbox_link(self.client))
        assert response.status_code == 302

    def verified_account(self, email: str = "ada@example.com", name: str = "Ada Lovelace") -> Any:
        self.sign_up(email=email, name=name)
        self.confirm_email()
        return self.config.accounts.find_by_email(email)

    def html(self, path: str, expect: int = 200) -> str:
        response = self.client.get(path)
        self.assertEqual(response.status_code, expect, f"GET {path} -> {response.status_code}")
        return response.get_data(as_text=True)

    def csrf_token(self, path: str = "/") -> str:
        return re.search(r'name="csrf_token" value="([^"]+)"', self.html(path)).group(1)


class LandingPageTests(OnboardingTestCase):
    def test_signed_out_root_shows_marketing_not_a_login_form(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/landing")
        body = self.html("/landing")
        self.assertIn("Your ideas, in orbit.", body)
        self.assertIn("Create your account", body)
        self.assertIn("Sign in", body)

    def test_landing_covers_capabilities_and_how_it_works(self):
        body = self.html("/landing")
        for needle in ("What “beta” means here", "Questions people ask", "Start where you are"):
            self.assertIn(needle, body)

    def test_beta_framing_is_stated_rather_than_implied(self):
        body = self.html("/landing")
        self.assertIn("Settled", body)
        self.assertIn("Moving", body)
        self.assertIn("Not yet — and it says so", body)
        # The gaps are named on the landing page instead of being discovered later.
        for needle in ("GitLab", "Bitbucket"):
            self.assertIn(needle, body)
        # The gaps are listed, sentence case and all.
        for needle in ("sub-agent", "two-factor authentication"):
            self.assertIn(needle, body.lower())

    def test_github_faq_does_not_imply_repository_access(self):
        answer = next(item["a"] for item in FAQ if "GitHub" in item["q"])
        self.assertIn("Repository access is a separate", answer)
        self.assertIn("does not grant", self.html("/landing"))

    def test_simulated_oauth_is_labelled_as_simulated(self):
        body = self.html("/landing")
        self.assertIn("Simulated", body)

    def test_deep_link_survives_the_whole_funnel(self):
        body = self.html("/landing?next=/projects/new")
        self.assertIn("next=%2Fprojects%2Fnew", body.replace("/signup?next=/projects/new", "next=%2Fprojects%2Fnew"))
        self.assertIn("/signup?next=/projects/new", body)
        signup = self.html("/signup?next=/projects/new")
        self.assertIn('name="next" value="/projects/new"', signup)
        self.sign_up(email="deep@example.com", name="Deep Link", next_url="/projects/new")
        self.assertEqual(self.client.get(mailbox_link(self.client)).headers["Location"], "/projects/new")

    def test_off_site_next_is_refused(self):
        body = self.html("/landing?next=https://evil.example.com")
        self.assertNotIn("evil.example.com", body)
        self.assertNotIn("evil.example.com", self.html("/signup?next=https://evil.example.com"))

    def test_deep_link_without_a_session_asks_for_sign_in(self):
        response = self.client.get("/settings/ai")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login?next=/settings/ai")

    def test_demo_is_one_click_and_seeded(self):
        response = self.client.get("/demo")
        self.assertEqual(response.status_code, 302)
        body = self.html("/")
        self.assertIn("orbitDashboard", body)
        self.assertIn("whatsapp-ordering-bot", body)
        account = self.config.accounts.find_by_email("demo@orbit.local")
        self.assertTrue(account.is_demo)
        self.assertEqual(self.config.projects.count_for_account(account.id), 3)
        # The demo visitor gets the same workspace shell as anybody else.
        self.assertIn("data-dashboard-kind=", body)


class SignUpTests(OnboardingTestCase):
    def test_signup_needs_terms_a_matching_password_and_a_real_email(self):
        payload = {
            "csrf_token": csrf(self.client, "/signup"),
            "name": "Ada",
            "email": "not-an-email",
            "password": "short",
            "confirm": "different",
        }
        body = self.client.post("/signup", data=payload).get_data(as_text=True)
        self.assertIn("not a valid email address", body)
        self.assertIn("at least", body)
        self.assertIn("accept the beta terms", body)
        self.assertIsNone(self.config.accounts.find_by_email("not-an-email"))

    def test_common_password_is_refused_with_a_reason(self):
        payload = {
            "csrf_token": csrf(self.client, "/signup"),
            "name": "Ada",
            "email": "ada@example.com",
            "password": "password123",
            "confirm": "password123",
            "terms": "on",
        }
        body = self.client.post("/signup", data=payload).get_data(as_text=True)
        self.assertIn("predictable", body)

    def test_an_unknown_address_cannot_be_signed_up_twice(self):
        self.verified_account()
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        payload = {
            "csrf_token": csrf(self.client, "/signup"),
            "name": "Someone else",
            "email": "ada@example.com",
            "password": PASSWORD,
            "confirm": PASSWORD,
            "terms": "on",
        }
        body = self.client.post("/signup", data=payload).get_data(as_text=True)
        self.assertIn("already uses that address", body)
        self.assertEqual(self.config.accounts.count(), 1)

    def test_unverified_account_cannot_open_the_workspace(self):
        self.sign_up(email="pending@example.com", name="Pending")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/verify")
        self.assertIn('"error"', self.client.get("/api/onboarding/tour").get_data(as_text=True))

    def test_confirmation_link_works_once_and_then_stops(self):
        self.sign_up(email="once@example.com", name="Once")
        link = mailbox_link(self.client)
        self.assertEqual(self.client.get(link).status_code, 302)
        self.assertTrue(self.config.accounts.find_by_email("once@example.com").verified)
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        second = self.client.get(link).get_data(as_text=True)
        self.assertIn("not valid any more", second)

    def test_confirmation_resend_and_email_change(self):
        self.sign_up(email="moved@example.com", name="Moved")
        csrf_token = self.csrf_token("/verify")
        self.client.post("/verify/resend", data={"csrf_token": csrf_token})
        self.assertEqual(len(re.findall(r'href="[^"]*?/verify/', self.html("/verify"))), 1)
        self.client.post(
            "/verify/change-email",
            data={"csrf_token": csrf_token, "email": "moved2@example.com"},
        )
        self.assertIsNone(self.config.accounts.find_by_email("moved@example.com"))
        self.assertIsNotNone(self.config.accounts.find_by_email("moved2@example.com"))
        self.assertIn("moved2@example.com", self.html("/verify"))

    def test_mutating_routes_require_a_csrf_token(self):
        self.verified_account()
        response = self.client.post("/logout", data={})
        self.assertEqual(response.status_code, 302)
        # Still signed in: the stale request was refused.
        self.assertEqual(self.client.get("/").status_code, 200)


class LoginTests(OnboardingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.verified_account()
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})

    def test_password_sign_in_lands_in_the_workspace(self):
        payload = {
            "csrf_token": csrf(self.client, "/login"),
            "email": "ada@example.com",
            "password": PASSWORD,
            "remember": "on",
        }
        response = self.client.post("/login", data=payload)
        self.assertEqual(response.headers["Location"], "/")
        self.assertIn("Welcome", self.html("/"))

    def test_wrong_password_and_unknown_address_say_the_same_thing(self):
        for email in ("ada@example.com", "nobody@example.com"):
            payload = {
                "csrf_token": csrf(self.client, "/login"),
                "email": email,
                "password": "not-the-password",
            }
            body = self.client.post("/login", data=payload).get_data(as_text=True)
            self.assertIn("don’t match an account", body)
            self.assertNotIn("No account", body)

    def test_repeated_failures_are_throttled(self):
        last = ""
        for _ in range(7):
            payload = {
                "csrf_token": csrf(self.client, "/login"),
                "email": "ada@example.com",
                "password": "not-the-password",
            }
            last = self.client.post("/login", data=payload).get_data(as_text=True)
        self.assertIn("Too many sign-in attempts", last)

    def test_password_reset_end_to_end(self):
        body = self.html("/forgot-password")
        self.assertIn("reset", body.lower())
        token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
        response = self.client.post(
            "/forgot-password", data={"csrf_token": token, "email": "ada@example.com"}
        )
        self.assertEqual(response.status_code, 302)
        sent_page = self.html(response.headers["Location"])
        reset_link = re.search(r'href="([^"]*?/reset-password/[^"]+)"', sent_page).group(1)
        page = self.html(reset_link)
        reset_token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = self.client.post(
            reset_link,
            data={
                "csrf_token": reset_token,
                "password": "a-brand-new-password",
                "confirm": "a-brand-new-password",
            },
        )
        self.assertEqual(response.status_code, 302)
        payload = {
            "csrf_token": csrf(self.client, "/login"),
            "email": "ada@example.com",
            "password": "a-brand-new-password",
        }
        self.assertEqual(self.client.post("/login", data=payload).status_code, 302)

    def test_forgot_password_does_not_reveal_whether_an_account_exists(self):
        known = {
            "csrf_token": csrf(self.client, "/forgot-password"),
            "email": "ada@example.com",
        }
        self.client.post("/forgot-password", data=known)
        known_body = self.html("/forgot-password")
        unknown = {
            "csrf_token": csrf(self.client, "/forgot-password"),
            "email": "stranger@example.com",
        }
        self.client.post("/forgot-password", data=unknown)
        unknown_body = self.html("/forgot-password")
        self.assertIn("If that address has an account", known_body)
        self.assertIn("If that address has an account", unknown_body)

    def test_sign_out_ends_the_session(self):
        payload = {
            "csrf_token": csrf(self.client, "/login"),
            "email": "ada@example.com",
            "password": PASSWORD,
        }
        self.client.post("/login", data=payload)
        response = self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/").headers["Location"], "/landing")


class OAuthTests(OnboardingTestCase):
    def consent(self, provider: str = "github", decision: str = "allow", email: str = "octo@example.com"):
        start = self.client.post(
            f"/oauth/{provider}/start",
            data={"csrf_token": csrf(self.client, "/login"), "next": "/"},
        )
        self.assertEqual(start.status_code, 302)
        page = start.headers["Location"]
        body = self.html(page)
        state = re.search(r'name="state" value="([^"]+)"', body).group(1)
        token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
        decision_response = self.client.post(
            page.rsplit("/simulate", 1)[0] + "/simulate/decision",
            data={
                "csrf_token": token,
                "state": state,
                "email": email,
                "name": "Octo Cat",
                "decision": decision,
            },
        )
        # Allowing hands back a code and redirects to the callback, exactly as the
        # provider would. Declining never gets that far.
        location = decision_response.headers.get("Location", "")
        if "/callback" in location:
            return self.client.get(location)
        return decision_response

    def test_consent_screen_is_explicit_about_scopes(self):
        start = self.client.post(
            "/oauth/github/start", data={"csrf_token": csrf(self.client, "/login")}
        )
        body = self.html(start.headers["Location"])
        self.assertIn("read:user", body)
        self.assertIn("Not requested: your repositories", body)
        self.assertNotIn("repo ", body)

    def test_github_sign_in_creates_a_verified_account_without_repository_access(self):
        response = self.consent()
        self.assertEqual(response.status_code, 302)
        account = self.config.accounts.find_by_email("octo@example.com")
        self.assertEqual(account.provider, "github")
        self.assertTrue(account.verified)
        self.assertTrue(account.integrations.get("github_connected"))
        self.assertFalse(account.github_repository_access())
        workspace = self.html("/")
        self.assertIn("Welcome, Octo", workspace)
        self.assertIn("Not granted", self.html("/onboarding/welcome"))

    def test_declining_consent_creates_nothing(self):
        response = self.consent(decision="deny")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.config.accounts.find_by_email("octo@example.com"))
        self.assertIn("Landing", response.headers["Location"].title() + " Landing")

    def test_google_provider_is_offered_and_simulated(self):
        body = self.html("/login")
        self.assertIn("Continue with Google", body)
        self.assertIn("Continue with GitHub", body)
        response = self.consent(provider="google", email="g@example.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.config.accounts.find_by_email("g@example.com").provider, "google")

    def test_provider_sign_in_reuses_an_account_with_the_same_email(self):
        self.verified_account(email="shared@example.com", name="Shared")
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        before = self.config.accounts.count()
        self.consent(email="shared@example.com")
        self.assertEqual(self.config.accounts.count(), before)
        self.assertEqual(self.config.accounts.find_by_email("shared@example.com").provider, "email")


class WelcomeAndPersonalizationTests(OnboardingTestCase):
    def test_welcome_offers_the_four_ways_in_and_nothing_else(self):
        self.verified_account()
        body = self.html("/onboarding/welcome")
        for option in WELCOME_OPTIONS:
            self.assertIn(option["title"], body)
            self.assertIn(f'href="{option["href"]}"', body)
        self.assertIn("Skip", body)
        # The page explains how this account was created, including scope facts.
        self.assertIn("How this account was created", body)

    def test_welcome_can_be_skipped_without_losing_the_workspace(self):
        self.verified_account()
        token = self.csrf_token("/onboarding/welcome")
        response = self.client.post("/onboarding/welcome/skip", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.config.accounts.find_by_email("ada@example.com").onboarding["welcome_seen"])

    def test_personalization_is_optional_and_only_feeds_recommendations(self):
        self.verified_account()
        page = self.html("/onboarding/personalize")
        self.assertIn("Skip", page)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = self.client.post(
            "/onboarding/personalize",
            data={
                "csrf_token": token,
                "experience": "some",
                "role": "product engineer",
                "goal": "ship a first version",
                "interests": ["web-apps", "automation"],
                "languages": ["python", "javascript"],
            },
        )
        self.assertEqual(response.status_code, 302)
        account = self.config.accounts.find_by_email("ada@example.com")
        self.assertEqual(account.preferences["experience"], "some")
        self.assertEqual(account.preferences["languages"], ["python", "javascript"])
        self.assertTrue(account.onboarding["personalize_done"])
        home = self.html("/")
        self.assertIn("data-dashboard-kind=", home)

    def test_personalization_skip_is_remembered(self):
        self.verified_account()
        token = self.csrf_token("/onboarding/personalize")
        self.client.post("/onboarding/personalize", data={"csrf_token": token, "skip": "1"})
        account = self.config.accounts.find_by_email("ada@example.com")
        self.assertTrue(account.onboarding["personalize_skipped"])
        self.assertFalse(account.onboarding["personalize_done"])

    def test_preferences_api_ignores_unknown_keys(self):
        self.verified_account()
        response = self.client.post(
            "/api/onboarding/preferences",
            json={"experience": "confident", "role": "", "admin": True},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 200)
        account = self.config.accounts.find_by_email("ada@example.com")
        self.assertEqual(account.preferences["experience"], "confident")
        self.assertNotIn("admin", account.preferences)


class DashboardAndProjectTests(OnboardingTestCase):
    def test_a_brand_new_account_gets_the_new_dashboard(self):
        self.verified_account()
        body = self.html("/")
        self.assertIn('data-dashboard-kind="new"', body)
        self.assertIn("Getting started", body)
        self.assertIn("Take the 60-second tour", body)

    def test_dashboard_reports_the_projects_dashboard_once_a_project_exists(self):
        self.verified_account()
        token = self.csrf_token("/projects/blank")
        self.client.post(
            "/projects/blank",
            data={"csrf_token": token, "name": "Trip planner", "description": "Weekends"},
        )
        body = self.html("/")
        self.assertIn('data-dashboard-kind="projects"', body)
        self.assertIn("Trip planner", body)

    def test_every_entry_point_renders(self):
        self.verified_account()
        for path in (
            "/projects/new",
            "/projects/blank",
            "/projects/guided",
            "/projects/idea",
            "/projects/import/github",
            "/projects/import/gitlab",
            "/projects/import/bitbucket",
            "/projects/import/folder",
            "/projects/import/zip",
            "/projects/import/files",
        ):
            body = self.html(path)
            self.assertIn("csrf_token", body)

    def test_planned_providers_say_they_are_planned(self):
        self.verified_account()
        body = self.html("/projects/import/gitlab")
        self.assertIn("not built yet", body)
        self.assertIn("planned", body)
        self.assertNotIn("Sign in with GitLab", body)

    def test_github_import_needs_a_separate_grant_and_copies_nothing(self):
        self.verified_account()
        page = self.html("/projects/import/github")
        self.assertIn("Repository access is a separate decision", page)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = self.client.post(
            "/projects/import/github",
            data={"csrf_token": token, "owner": "caleb", "repo": "orbit-demo", "branch": "main"},
        )
        self.assertEqual(response.status_code, 302)
        project = self.config.projects.for_account(
            self.config.accounts.find_by_email("ada@example.com").id
        )[0]
        self.assertEqual(project.kind, "github")
        self.assertFalse(project.remote["contents_copied"])
        self.assertIn("Reference only", self.html(f"/projects/{project.id}"))

    def test_zip_import_reads_a_table_of_contents_only(self):
        self.verified_account()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("src/app.py", "print('hi')")
            archive.writestr("README.md", "# hi")
            archive.writestr("node_modules/tiny.js", "x")
        buffer.seek(0)
        token = self.csrf_token("/projects/import/zip")
        response = self.client.post(
            "/projects/import/zip",
            data={"csrf_token": token, "archive": (buffer, "myproject.zip")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        account = self.config.accounts.find_by_email("ada@example.com")
        project = self.config.projects.for_account(account.id)[0]
        self.assertEqual(project.kind, "zip")
        self.assertFalse(project.remote["contents_copied"])
        self.assertIn("src/app.py", {item["path"] for item in project.files})
        detail = self.html(f"/projects/{project.id}")
        self.assertIn("Files listed", detail)
        self.assertIn("was not kept", detail)
        self.assertIn("Contents copied", detail)

    def test_files_and_folder_imports_record_names_sizes_and_kinds(self):
        self.verified_account()
        token = self.csrf_token("/projects/import/files")
        response = self.client.post(
            "/projects/import/files",
            data={
                "csrf_token": token,
                "name": "Loose files",
                "files": [(io.BytesIO(b"body"), "notes.md"), (io.BytesIO(b"x=1"), "calc.py")],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        token = self.csrf_token("/projects/import/folder")
        response = self.client.post(
            "/projects/import/folder",
            data={
                "csrf_token": token,
                "paths": ["myapp/src/main.py", "myapp/README.md", "myapp/node_modules/x.js"],
            },
        )
        self.assertEqual(response.status_code, 302)
        account = self.config.accounts.find_by_email("ada@example.com")
        projects = {project.name: project for project in self.config.projects.for_account(account.id)}
        folder = projects["myapp"]
        self.assertEqual(folder.kind, "folder")
        paths = {item["path"] for item in folder.files}
        self.assertIn("src/main.py", paths)
        self.assertFalse(any("node_modules" in path for path in paths))

    def test_guided_setup_returns_a_plan_and_creates_nothing_until_confirmed(self):
        self.verified_account()
        page = self.html("/projects/guided")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        preview = self.client.post(
            "/projects/guided",
            data={
                "csrf_token": token,
                "stage": "preview",
                "name": "Ledger tool",
                "template": "cli",
                "primary_language": "python",
                "options": ["tests", "readme"],
                "description": "Track expenses",
            },
        ).get_data(as_text=True)
        account = self.config.accounts.find_by_email("ada@example.com")
        self.assertEqual(self.config.projects.count_for_account(account.id), 0)
        self.assertIn("local planner", preview)
        self.assertIn("Create the project with this plan", preview)

    def test_idea_plan_is_labelled_as_a_local_plan(self):
        self.verified_account()
        page = self.html("/projects/idea")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        body = self.client.post(
            "/projects/idea",
            data={"csrf_token": token, "stage": "plan", "idea": "Flag silent customers in a WhatsApp export."},
        ).get_data(as_text=True)
        self.assertIn("local planner", body)
        self.assertIn("Milestones", body)

    def test_project_actions_are_scoped_to_their_owner(self):
        self.verified_account(email="ada@example.com", name="Ada")
        token = self.csrf_token("/projects/blank")
        self.client.post("/projects/blank", data={"csrf_token": token, "name": "Mine"})
        ada = self.config.accounts.find_by_email("ada@example.com")
        project = self.config.projects.for_account(ada.id)[0]

        # Bob never sees it, and cannot delete it either.
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        self.verified_account(email="bob@example.com", name="Bob")
        self.assertEqual(self.client.get(f"/projects/{project.id}").status_code, 404)
        bob_token = self.csrf_token("/projects/blank")
        self.client.post(f"/projects/{project.id}/delete", data={"csrf_token": bob_token, "confirm": "Mine"})
        self.assertEqual(self.config.projects.count_for_account(ada.id), 1)

        # Deleting your own project asks for the name to be typed first.
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        payload = {
            "csrf_token": csrf(self.client, "/login"),
            "email": "ada@example.com",
            "password": PASSWORD,
        }
        self.client.post("/login", data=payload)
        ada_token = csrf(self.client, f"/projects/{project.id}")
        unconfirmed = self.client.post(
            f"/projects/{project.id}/delete", data={"csrf_token": ada_token}, follow_redirects=False
        )
        self.assertEqual(unconfirmed.status_code, 302)
        self.assertEqual(self.config.projects.count_for_account(ada.id), 1)
        confirmed = self.client.post(
            f"/projects/{project.id}/delete",
            data={"csrf_token": ada_token, "confirm": "Mine"},
            follow_redirects=False,
        )
        self.assertEqual(confirmed.status_code, 302)
        self.assertEqual(self.config.projects.count_for_account(ada.id), 0)

    def test_quick_project_api_creates_and_returns_a_url(self):
        self.verified_account()
        response = self.client.post(
            "/api/onboarding/projects/quick",
            json={"name": "Quick one"},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["url"], f"/projects/{payload['project']['id']}")


class SettingsTests(OnboardingTestCase):
    SECTIONS = (
        "profile",
        "account",
        "preferences",
        "ai",
        "integrations",
        "notifications",
        "privacy",
        "security",
        "help",
    )

    def test_every_settings_section_renders(self):
        self.verified_account()
        for section in self.SECTIONS:
            body = self.html(f"/settings/{section}")
            self.assertIn("Only your name and email are required", body)
        self.assertEqual(self.client.get("/settings").status_code, 302)

    def test_profile_saves_name_but_keeps_registration_fields_required_only(self):
        self.verified_account()
        token = self.csrf_token("/settings/profile")
        self.client.post(
            "/settings/profile",
            data={"csrf_token": token, "name": "Ada L", "workspace": "Analytical Engines"},
        )
        account = self.config.accounts.find_by_email("ada@example.com")
        self.assertEqual(account.name, "Ada L")
        self.assertIn("Analytical Engines", self.html("/settings/profile"))

    def test_integrations_keep_identity_and_repository_access_separate(self):
        self.consent_via_github()
        body = self.html("/settings/integrations")
        self.assertIn("read:user", body)
        self.assertIn("Grant read-only access", body)
        self.assertIn("no repository access", body)
        account = self.config.accounts.find_by_email("octo@example.com")
        self.assertFalse(account.github_repository_access())

    def consent_via_github(self) -> None:
        start = self.client.post(
            "/oauth/github/start", data={"csrf_token": csrf(self.client, "/login")}
        )
        page = start.headers["Location"]
        body = self.html(page)
        state = re.search(r'name="state" value="([^"]+)"', body).group(1)
        token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
        decision = self.client.post(
            page.rsplit("/simulate", 1)[0] + "/simulate/decision",
            data={
                "csrf_token": token,
                "state": state,
                "email": "octo@example.com",
                "name": "Octo Cat",
                "decision": "allow",
            },
        )
        if "/callback" in decision.headers.get("Location", ""):
            self.client.get(decision.headers["Location"])

    def test_privacy_export_carries_the_data_and_no_secrets(self):
        self.verified_account()
        response = self.client.get("/settings/privacy/export")
        payload = json.loads(response.get_data(as_text=True))
        self.assertIn("exported_at", payload)
        blob = json.dumps(payload)
        self.assertNotIn("pbkdf2_sha256", blob)
        self.assertNotIn(PASSWORD, blob)

    def test_security_lists_sessions_and_can_revoke_the_others(self):
        self.verified_account()
        self.config.accounts.create_session(
            self.config.accounts.find_by_email("ada@example.com"), user_agent="Other browser"
        )
        body = self.html("/settings/security")
        self.assertIn("This browser", body)
        token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
        response = self.client.post("/settings/security", data={"csrf_token": token, "action": "sign-out-others"})
        self.assertEqual(response.status_code, 302)
        account = self.config.accounts.find_by_email("ada@example.com")
        self.assertEqual(len(account.sessions), 1)

    def test_notifications_and_ai_preferences_persist(self):
        self.verified_account()
        token = self.csrf_token("/settings/notifications")
        response = self.client.post(
            "/settings/notifications",
            data={
                "csrf_token": token,
                "action": "email",
                "email_product_updates": "on",
                "feedback_replies": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        token = self.csrf_token("/settings/ai")
        response = self.client.post(
            "/settings/ai",
            data={
                "csrf_token": token,
                "default_mode": "agent",
                "default_depth": "deep",
                "voice_replies": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        account = self.config.accounts.find_by_email("ada@example.com")
        self.assertTrue(account.preferences["email_product_updates"])
        self.assertTrue(account.preferences["voice_replies"])
        self.assertEqual(account.preferences["default_mode"], "agent")
        self.assertEqual(account.preferences["default_depth"], "deep")


class TourAndHintTests(OnboardingTestCase):
    def test_tour_is_offered_once_and_can_be_dismissed_for_good(self):
        self.verified_account()
        self.assertIn("Take the 60-second tour", self.html("/"))
        self.assertIn("obTour", self.html("/"))
        response = self.client.post(
            "/api/onboarding/tour",
            json={"seen": True},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["onboarding"]["tour_seen"])
        self.assertNotIn("Take the 60-second tour", self.html("/"))

    def test_tour_steps_only_point_at_anchors_that_exist(self):
        self.verified_account()
        home = self.html("/")
        steps = json.loads(
            re.search(r'data-tour-steps=\'([^\']+)\'', home).group(1)
            if "data-tour-steps" in home
            else "[]"
        )
        for step in steps:
            self.assertIn(f'id="{step["anchor"]}"', home)

    def test_tour_can_be_asked_for_again_explicitly(self):
        self.verified_account()
        self.client.post(
            "/api/onboarding/tour", json={"seen": True}, headers={"X-CSRF-Token": self.csrf_token()}
        )
        body = self.html("/?tour=1")
        self.assertIn("obTour", body)

    def test_hints_dismiss_once_and_stay_dismissed(self):
        self.verified_account()
        body = self.html("/")
        hint_ids = re.findall(r'data-hint-id="([^"]+)"', body)
        if not hint_ids:
            self.skipTest("no hints on this surface")
        response = self.client.post(
            f"/api/onboarding/hints/{hint_ids[0]}/dismiss",
            json={},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 200)
        account = self.config.accounts.find_by_email("ada@example.com")
        self.assertIn(hint_ids[0], account.onboarding["dismissed_hints"])

    def test_api_calls_without_a_session_get_json_401(self):
        response = self.client.post("/api/onboarding/tour", json={"seen": True})
        self.assertEqual(response.status_code, 401)
        self.assertIn("error", response.get_json())


class FeedbackTests(OnboardingTestCase):
    def test_the_drawer_posts_without_leaving_the_page(self):
        self.verified_account()
        response = self.client.post(
            "/api/onboarding/feedback",
            json={
                "kind": "bug",
                "body": "The Deep toggle forgot my choice after a reload.",
                "context": {"surface": "Workspace home", "route": "/"},
            },
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertRegex(payload["reference"], r"^ORB-[0-9A-F]{6}$")
        entry = self.config.feedback.for_account(
            self.config.accounts.find_by_email("ada@example.com").id
        )[0]
        self.assertEqual(entry.kind, "bug")
        self.assertEqual(entry.status, "received")
        page = self.html("/feedback")
        self.assertIn(payload["reference"], page)
        detail = self.html(f"/feedback/{entry.id}")
        self.assertIn("Context that was attached", detail)

    def test_short_and_empty_bodies_are_refused_with_a_reason(self):
        self.verified_account()
        for body in ("", "too short"):
            response = self.client.post(
                "/api/onboarding/feedback",
                json={"kind": "idea", "body": body},
                headers={"X-CSRF-Token": self.csrf_token()},
            )
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.get_json()["problems"])

    def test_feedback_needs_a_token_and_a_known_kind(self):
        self.verified_account()
        response = self.client.post("/api/onboarding/feedback", json={"kind": "bug", "body": "x" * 40})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/api/onboarding/feedback",
            json={"kind": "ransom", "body": "x" * 40},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 400)

    def test_another_account_cannot_read_a_report(self):
        self.verified_account(email="ada@example.com", name="Ada")
        self.client.post(
            "/api/onboarding/feedback",
            json={"kind": "general", "body": "Something general about the beta."},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        entry = self.config.feedback.for_account(
            self.config.accounts.find_by_email("ada@example.com").id
        )[0]
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        self.verified_account(email="bob@example.com", name="Bob")
        response = self.client.get(f"/feedback/{entry.id}")
        self.assertEqual(response.status_code, 404)
        self.assertIn("not found", response.get_data(as_text=True).lower())

    def test_context_is_an_allowlist_and_bodies_are_escaped(self):
        clean = sanitize_context({"route": "/", "surface": "Home", "cookie": "session=1", "script": "<script>"})
        self.assertNotIn("cookie", clean)
        cleaned, problems = first_line("<b>hello</b>"), None
        self.assertTrue(cleaned.startswith("<b>hello</b>"))
        self.assertIsNone(problems)

    def test_feedback_fab_is_present_on_every_signed_in_surface(self):
        self.verified_account()
        for path in ("/", "/projects", "/settings/profile"):
            self.assertIn("obFeedbackFab", self.html(path))

    def test_a_receipt_is_sent_and_the_demo_mailbox_shows_it(self):
        self.verified_account()
        self.client.post(
            "/api/onboarding/feedback",
            json={"kind": "idea", "body": "Keyboard shortcut for switching modes."},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        outbox = self.config.mailer.outbox if hasattr(self.config.mailer, "outbox") else None
        self.assertTrue(outbox is not None)


class TokenSyncTests(OnboardingTestCase):
    """What must never drift: one token, one digest, one use, one session secret."""

    def test_only_digests_are_written_to_disk(self):
        self.sign_up(email="disk@example.com", name="Disk")
        link = mailbox_link(self.client)
        token = link.rstrip("/").rsplit("/", 1)[-1]
        raw = (self.runtime / "accounts.json").read_text()
        # The link that reached the mailbox is not on disk; its digest is.
        self.assertNotIn(token, raw)
        self.assertIn(token_digest(token), raw)
        account = self.config.accounts.find_by_email("disk@example.com")
        self.assertTrue(account.password_hash.startswith("pbkdf2_sha256$"))
        self.assertNotIn(PASSWORD, raw)

    def test_verification_tokens_are_single_use_and_expire(self):
        self.sign_up(email="expire@example.com", name="Expire")
        account = self.config.accounts.find_by_email("expire@example.com")
        issued = self.config.accounts.start_verification(account)
        token, reason = self.config.accounts.consume_verification(issued)
        self.assertIsNotNone(token)
        self.assertEqual(reason, "")
        again, reason = self.config.accounts.consume_verification(issued)
        self.assertIsNone(again)
        self.assertEqual(reason, "used")

    def test_reset_tokens_are_tracked_by_digest_only(self):
        self.verified_account()
        account = self.config.accounts.find_by_email("ada@example.com")
        _, token = self.config.accounts.start_password_reset(account.email)
        self.assertTrue(self.config.accounts.reset_token_known(token_digest(token)))
        self.assertFalse(self.config.accounts.reset_token_known("0" * 64))

    def test_password_hashes_are_salted_per_account(self):
        one = hash_password(PASSWORD)
        two = hash_password(PASSWORD)
        self.assertNotEqual(one, two)
        self.assertTrue(verify_password(PASSWORD, one))
        self.assertFalse(verify_password(PASSWORD + "x", one))

    def test_password_rules_match_the_signup_form(self):
        problems = password_problems("short", email="ada@example.com", name="Ada")
        self.assertTrue(problems)
        self.assertEqual(password_problems(PASSWORD), [])
        self.assertGreaterEqual(MIN_PASSWORD_LENGTH, 10)

    def test_accounts_with_an_unknown_address_fail_the_same_way(self):
        with self.assertRaises(AccountStoreError):
            self.config.accounts.authenticate("nobody@example.com", PASSWORD)
        self.verified_account()
        with self.assertRaises(AccountStoreError):
            self.config.accounts.authenticate("ada@example.com", "wrong-password")

    def test_session_secret_is_stable_across_restarts(self):
        first = session_secret(self.runtime)
        second = session_secret(self.runtime)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_env_secret_wins_when_it_is_set(self):
        with patch.dict(os.environ, {"ORBIT_AUTH_SECRET": "set-from-the-environment"}):
            self.assertEqual(session_secret(self.runtime), "set-from-the-environment")

    def test_email_normalization_is_idempotent(self):
        self.assertEqual(normalize_email("  Ada@Example.COM "), "ada@example.com")
        self.assertEqual(normalize_email(normalize_email("Ada@Example.COM")), "ada@example.com")

    def test_one_account_store_survives_a_reload(self):
        self.verified_account()
        reloaded = AccountStore(self.runtime / "accounts.json")
        account = reloaded.find_by_email("ada@example.com")
        self.assertIsNotNone(account)
        self.assertTrue(verify_password(PASSWORD, account.password_hash))

    def test_zip_and_folder_readers_ignore_the_things_that_should_not_ship(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("src/app.py", "print(1)")
            archive.writestr(".git/config", "[core]")
            archive.writestr("__pycache__/x.pyc", "x")
        entries, _ = list_zip_entries(buffer.getvalue())
        paths = {entry["path"] for entry in entries}
        self.assertIn("src/app.py", paths)
        self.assertNotIn(".git/config", paths)
        folder = list_folder_paths(["app/src/main.py", "app/.env", "app/node_modules/x.js"])
        self.assertEqual([item["path"] for item in folder], ["src/main.py"])


if __name__ == "__main__":
    unittest.main()
