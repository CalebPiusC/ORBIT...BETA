"""Accounts, sign-in and the onboarding state behind ORBIT's first-run UX.

This module owns everything the onboarding experience needs that the chat
``Store`` deliberately does not: who is signed in, how they registered, whether
their email is verified, what they answered in the optional personalization,
where they are in the first-run checklist, and the feedback they have sent.

Three honesty rules are enforced here, the same spirit as ``ProjectStore``:

  * Email/password registration is not usable until the verification code is
    confirmed. Google/GitHub sign-in creates an already-verified account
    (provider confirms identity), which is why those paths skip the code.
  * OAuth is a **demo flow** in the beta sandbox: no real request is made to
    Google or GitHub. The routes and templates say exactly that, and the
    account records which provider identity it was created from — nothing is
    faked as a live OAuth integration. GitHub sign-in records identity only;
    repository access is a separate connection that does not exist yet and is
    never implied (``OAUTH_SCOPE_COPY`` is the single source of that wording).
  * Codes are stored so the demo can show them on screen ("no email is sent in
    this beta sandbox — use this code"). That is a labelled demo affordance,
    not a pretend mail server.

Passwords are salted PBKDF2-HMAC-SHA256 (stdlib, no new dependency).
``iterations`` is a constructor argument so tests can keep the real code path
at a fraction of the cost; production keeps the default.

Persistence follows the repo convention: ``JsonStore`` under ``runtime/`` when
a path is given (``run_app`` does that), plain in-memory otherwise, so
importing or testing the app never touches disk.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from memory.jsonstore import JsonStore

DATA_VERSION = 1

Provider = Literal["email", "google", "github"]
PROVIDERS: tuple[str, ...] = ("email", "google", "github")
PROVIDER_LABELS: dict[str, str] = {
    "email": "Email and password",
    "google": "Google",
    "github": "GitHub",
}

# The GitHub/GitSign-in honesty copy, in one place so the consent screen, the
# settings page and the tests all read the same sentence. Signing in with a
# Git provider identifies the person; it never grants ORBIT repository access.
OAUTH_SCOPE_COPY = (
    "Signing in with {provider} tells ORBIT who you are — nothing more. "
    "It does not grant access to any repository. Connecting repositories is a "
    "separate step that always asks first, and it is not built yet."
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_CHARS = 8
MAX_NAME_CHARS = 60
MAX_FEEDBACK_TITLE_CHARS = 120
MAX_FEEDBACK_BODY_CHARS = 2000
CODE_TTL_MINUTES = 10
CODE_MAX_ATTEMPTS = 6
DEFAULT_ITERATIONS = 120_000
CODE_HASH_ITERATIONS = 120_000


def _hash_code(code: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), bytes.fromhex(salt_hex), CODE_HASH_ITERATIONS
    ).hex()

# ----- personalization options: one list per question, shared by the --------
# onboarding wizard, the settings page and the dashboard recommendations.
ROLES: tuple[str, ...] = (
    "Student",
    "Professional developer",
    "Founder / indie builder",
    "Designer",
    "Hobbyist",
    "Something else",
)
EXPERIENCE_LEVELS: tuple[str, ...] = (
    "New to coding",
    "1–2 years",
    "3–5 years",
    "5+ years",
)
INTERESTS: tuple[str, ...] = (
    "Web apps",
    "APIs & backends",
    "Bots & automation",
    "Data & ML",
    "Mobile",
    "Games",
    "Dev tools",
)
LANGUAGES: tuple[str, ...] = (
    "Python",
    "JavaScript",
    "TypeScript",
    "HTML/CSS",
    "Java",
    "C#",
    "Go",
    "Rust",
    "PHP",
    "SQL",
    "Something else",
)

# Interest → the agent specialty that genuinely exists in app/agents.py. The
# dashboard's recommendations are built from this map and nothing else, so a
# suggestion never names a capability the product does not have.
INTEREST_AGENT: dict[str, tuple[str, str]] = {
    "Web apps": ("coding", "Build and change real code with the Coding specialist"),
    "APIs & backends": ("coding", "Shape services and endpoints with the Coding specialist"),
    "Bots & automation": ("coding", "Script workflows with the Coding specialist"),
    "Data & ML": ("research", "Dig into datasets and papers with the Research specialist"),
    "Mobile": ("coding", "Work through app code with the Coding specialist"),
    "Games": ("debug", "Hunt down logic bugs with the Debug specialist"),
    "Dev tools": ("review", "Get a second pair of eyes with the Review specialist"),
}

TOUR_STATES: tuple[str, ...] = ("new", "done", "skipped")

FEEDBACK_KINDS: dict[str, str] = {
    "feature": "Feature suggestion",
    "bug": "Bug report",
    "general": "General feedback",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat(timespec="seconds")


def _empty() -> dict[str, Any]:
    return {"version": DATA_VERSION, "accounts": [], "codes": {}, "feedback": []}


def clean_name(name: str) -> str:
    return " ".join((name or "").split())[:MAX_NAME_CHARS]


def clean_email(email: str) -> str:
    return (email or "").strip().lower()


def email_issues(email: str) -> list[str]:
    """What is wrong with an email address, in words a form can show."""
    if not clean_email(email):
        return ["Enter your email address."]
    if not EMAIL_RE.match(clean_email(email)):
        return ["That doesn't look like a valid email address."]
    return []


def password_issues(password: str) -> list[str]:
    """The password policy, returned as human sentences (the signup form shows
    them verbatim, and the strength meter mirrors the same rules)."""
    issues: list[str] = []
    if len(password or "") < MIN_PASSWORD_CHARS:
        issues.append(f"Use at least {MIN_PASSWORD_CHARS} characters.")
    if password and not re.search(r"[A-Za-z]", password):
        issues.append("Add at least one letter.")
    if password and not re.search(r"[0-9]", password):
        issues.append("Add at least one number.")
    return issues


def _hash_password(password: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        iterations,
    ).hex()


class AccountError(Exception):
    """A registration/sign-in problem the form can render verbatim."""


class AccountStore:
    """The account database: JSON-backed with a path, in-memory without one."""

    def __init__(self, path: str | Path | None = None, *, iterations: int = DEFAULT_ITERATIONS) -> None:
        self._json: JsonStore | None = None if path is None else JsonStore(path, default=_empty)
        self._memory: dict[str, Any] = _empty()
        self.iterations = max(1_000, int(iterations))
        # Demo codes are shown to the user in the current process but are never
        # written to persistent storage. The durable record contains only a
        # salted PBKDF2 hash and expiry.
        self._demo_codes: dict[str, tuple[str, str]] = {}
        self._demo_codes_lock = threading.RLock()
        if self._json is not None:
            self._migrate_legacy_codes()

    def _migrate_legacy_codes(self) -> None:
        """Hash any codes written in clear by older beta versions."""
        assert self._json is not None

        def migrate(data: dict[str, Any]) -> None:
            for entry in data.get("codes", {}).values():
                legacy = entry.pop("code", None)
                if isinstance(legacy, str):
                    salt = secrets.token_hex(16)
                    entry["salt"] = salt
                    entry["token_hash"] = _hash_code(legacy, salt)

        self._json.update(migrate)

    @property
    def persistent(self) -> bool:
        return self._json is not None

    # ----- raw data ---------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if self._json is None:
            return self._memory
        return self._json.read()

    def _update(self, change):
        if self._json is None:
            return change(self._memory)
        return self._json.update(change)

    @staticmethod
    def _find(data: dict[str, Any], account_id: str) -> dict[str, Any] | None:
        for raw in data.get("accounts", []):
            if raw.get("id") == account_id:
                return raw
        return None

    @staticmethod
    def _find_by_email(data: dict[str, Any], email: str) -> dict[str, Any] | None:
        needle = clean_email(email)
        for raw in data.get("accounts", []):
            if raw.get("email") == needle:
                return raw
        return None

    # ----- reads (return detached copies) ------------------------------------
    def by_id(self, account_id: str) -> dict[str, Any] | None:
        if not account_id:
            return None
        raw = self._find(self._read(), account_id)
        return copy.deepcopy(raw) if raw is not None else None

    def by_email(self, email: str) -> dict[str, Any] | None:
        raw = self._find_by_email(self._read(), email)
        return copy.deepcopy(raw) if raw is not None else None

    def exists(self, email: str) -> bool:
        return self._find_by_email(self._read(), email) is not None

    # ----- registration -------------------------------------------------------
    def register_email(self, name: str, email: str, password: str) -> dict[str, Any]:
        """Create an unverified email/password account and its verification code.

        Raises ``AccountError`` with a form-ready sentence when the input is not
        acceptable. Successful creation always starts a fresh verify code; the
        account cannot sign in until ``verify_email`` succeeds.
        """

        def op(data: dict[str, Any]) -> dict[str, Any]:
            display = clean_name(name)
            if not display:
                raise AccountError("Tell us what to call you.")
            errs = email_issues(email)
            if errs:
                raise AccountError(errs[0])
            pw_issues = password_issues(password)
            if pw_issues:
                raise AccountError(pw_issues[0])
            if self._find_by_email(data, email) is not None:
                raise AccountError("An account with this email already exists. Try signing in instead.")
            salt = secrets.token_hex(16)
            account = _new_account(
                name=display,
                email=clean_email(email),
                provider="email",
                verified=False,
                password_hash=_hash_password(password, salt, self.iterations),
                password_salt=salt,
            )
            data["accounts"].append(account)
            self._issue_code(data, account["email"], "verify")
            return copy.deepcopy(account)

        return self._update(op)

    def oauth_signin(self, provider: str, name: str, email: str) -> tuple[dict[str, Any], bool]:
        """Create-or-reuse an account confirmed by a provider identity.

        Returns ``(account, is_new)``. Provider accounts are born verified —
        that is the whole reason OAuth users skip the email code. ``is_new``
        tells the route whether to land on the welcome screen (first time) or
        go straight to the dashboard (returning identity).
        """
        if provider not in ("google", "github"):
            raise AccountError(f"unknown sign-in provider: {provider!r}")

        def op(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            display = clean_name(name) or f"{provider.title()} user"
            errs = email_issues(email)
            if errs:
                raise AccountError("The demo sign-in needs an email address to build a local account.")
            existing = self._find_by_email(data, email)
            if existing is not None:
                existing["last_login_at"] = _utcnow_iso()
                # A provider account stays verified; an email account that
                # later arrives via OAuth is identity-confirmed by the demo
                # sign-in as well.
                existing["email_verified"] = True
                data["codes"].pop(f"verify:{existing['email']}", None)
                return copy.deepcopy(existing), False
            account = _new_account(
                name=display,
                email=clean_email(email),
                provider=provider,
                verified=True,
                password_hash="",
                password_salt="",
            )
            data["accounts"].append(account)
            return copy.deepcopy(account), True

        return self._update(op)

    # ----- verification & reset codes ----------------------------------------
    def _issue_code(self, data: dict[str, Any], email: str, purpose: str) -> str:
        """Mint a code and retain its clear value only in process memory."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        key = f"{purpose}:{clean_email(email)}"
        salt = secrets.token_hex(16)
        expires_at = (_utcnow() + timedelta(minutes=CODE_TTL_MINUTES)).isoformat(timespec="seconds")
        data.setdefault("codes", {})[key] = {
            "token_hash": _hash_code(code, salt),
            "salt": salt,
            "purpose": purpose,
            "email": clean_email(email),
            "expires_at": expires_at,
            "attempts_left": CODE_MAX_ATTEMPTS,
        }
        with self._demo_codes_lock:
            self._demo_codes[key] = (code, expires_at)
        return code

    def _check_code(self, data: dict[str, Any], email: str, purpose: str, code: str) -> str:
        """Consume one attempt. Returns "ok", or the reason the UI shows."""
        key = f"{purpose}:{clean_email(email)}"
        entry = data.setdefault("codes", {}).get(key)
        if entry is None:
            return "no-code"
        try:
            if datetime.fromisoformat(entry["expires_at"]) < _utcnow():
                return "expired"
        except (KeyError, ValueError):
            return "no-code"
        if int(entry.get("attempts_left", 0)) <= 0:
            return "locked"
        entry["attempts_left"] = int(entry["attempts_left"]) - 1
        submitted = re.sub(r"\s", "", code or "")
        salt = entry.get("salt")
        token_hash = entry.get("token_hash")
        if not isinstance(salt, str) or not isinstance(token_hash, str):
            return "no-code"
        candidate = _hash_code(submitted, salt)
        if not hmac.compare_digest(candidate, token_hash):
            return "wrong"
        data["codes"].pop(key, None)
        with self._demo_codes_lock:
            self._demo_codes.pop(key, None)
        return "ok"

    def attempts_left(self, email: str, purpose: str) -> int | None:
        entry = self._read().get("codes", {}).get(f"{purpose}:{clean_email(email)}")
        return None if entry is None else int(entry.get("attempts_left", 0))

    def demo_code(self, email: str, purpose: str) -> str | None:
        """Return the current-process demo code, never a persisted secret."""
        key = f"{purpose}:{clean_email(email)}"
        entry = self._read().get("codes", {}).get(key)
        with self._demo_codes_lock:
            held = self._demo_codes.get(key)
        if entry is None or held is None:
            return None
        try:
            if datetime.fromisoformat(entry["expires_at"]) < _utcnow():
                with self._demo_codes_lock:
                    self._demo_codes.pop(key, None)
                return None
        except (KeyError, ValueError):
            return None
        return held[0]

    def verify_email(self, email: str, code: str) -> str:
        """Confirm the verification code. Returns "ok" or a reason."""

        def op(data: dict[str, Any]) -> str:
            account = self._find_by_email(data, email)
            if account is None:
                return "no-code"
            result = self._check_code(data, email, "verify", code)
            if result == "ok":
                account["email_verified"] = True
            return result

        return self._update(op)

    def resend_code(self, email: str, purpose: str) -> str | None:
        """A fresh code. Returns it, or None when no such account exists."""

        def op(data: dict[str, Any]) -> str | None:
            if self._find_by_email(data, email) is None:
                return None
            return self._issue_code(data, email, purpose)

        return self._update(op)

    def start_reset(self, email: str) -> None:
        """Begin password recovery. Deliberately silent for unknown emails: the
        route always shows the same neutral message so the flow cannot be used
        to discover which addresses have accounts."""

        def op(data: dict[str, Any]) -> None:
            if self._find_by_email(data, email) is not None:
                self._issue_code(data, email, "reset")

        self._update(op)

    def complete_reset(self, email: str, code: str, new_password: str) -> str:
        """Set a new password if the reset code checks out. "ok" or a reason."""

        def op(data: dict[str, Any]) -> str:
            account = self._find_by_email(data, email)
            if account is None:
                return "no-code"
            pw_issues = password_issues(new_password)
            if pw_issues:
                return "weak-password"
            result = self._check_code(data, email, "reset", code)
            if result == "ok":
                salt = secrets.token_hex(16)
                account["password_hash"] = _hash_password(new_password, salt, self.iterations)
                account["password_salt"] = salt
                # A recovered account has proven inbox access; treat the email
                # as verified rather than stranding it on the verify screen.
                account["email_verified"] = True
                account["sessions"] = []  # sign out every past session
            return result

        return self._update(op)

    # ----- passwords & sessions ------------------------------------------------
    def check_password(self, account_id: str, password: str) -> bool:
        account = self.by_id(account_id)
        if account is None or not account.get("password_hash"):
            return False
        candidate = _hash_password(password, account["password_salt"], self.iterations)
        return hmac.compare_digest(candidate, account["password_hash"])

    def set_password(self, account_id: str, new_password: str) -> str:
        def op(data: dict[str, Any]) -> str:
            account = self._find(data, account_id)
            if account is None:
                return "no-account"
            pw_issues = password_issues(new_password)
            if pw_issues:
                return "weak-password"
            salt = secrets.token_hex(16)
            account["password_hash"] = _hash_password(new_password, salt, self.iterations)
            account["password_salt"] = salt
            account["sessions"] = []  # other sessions end; the route re-adds this one
            return "ok"

        return self._update(op)

    def record_login(self, account_id: str) -> None:
        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is not None:
                account["last_login_at"] = _utcnow_iso()

        self._update(op)

    def start_session(self, account_id: str, user_agent: str = "") -> str:
        """Register a browser session and return its id (the Flask session
        cookie stores it). The Settings page lists these; 'sign out everywhere
        else' revokes the others."""
        sid = secrets.token_urlsafe(16)

        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is None:
                return
            account.setdefault("sessions", []).append(
                {
                    "sid": sid,
                    "created_at": _utcnow_iso(),
                    "agent": (user_agent or "")[:140],
                    "current": False,
                }
            )

        self._update(op)
        return sid

    def session_valid(self, account_id: str, sid: str) -> bool:
        account = self.by_id(account_id)
        if account is None:
            return False
        return any(s.get("sid") == sid for s in account.get("sessions", []))

    def end_session(self, account_id: str, sid: str) -> None:
        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is not None:
                account["sessions"] = [s for s in account.get("sessions", []) if s.get("sid") != sid]

        self._update(op)

    def revoke_other_sessions(self, account_id: str, keep_sid: str) -> None:
        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is not None:
                account["sessions"] = [s for s in account.get("sessions", []) if s.get("sid") == keep_sid]

        self._update(op)

    # ----- profile, preferences, onboarding state ------------------------------
    def update_profile(self, account_id: str, **fields: Any) -> None:
        """Patch top-level profile fields (name, workspace) with the same
        cleaning rules registration applies."""

        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is None:
                return
            if "name" in fields:
                display = clean_name(str(fields.get("name") or ""))
                if display:
                    account["name"] = display
            if "workspace" in fields:
                workspace = " ".join(str(fields.get("workspace") or "").split())[:60]
                if workspace:
                    account["workspace"] = workspace

        self._update(op)

    def update_personalization(
        self,
        account_id: str,
        *,
        role: str = "",
        experience: str = "",
        interests: list[str] | None = None,
        languages: list[str] | None = None,
        skipped: bool = False,
    ) -> None:
        """Save personalization answers — or that the person skipped. Either
        way the checklist item resolves: an explicit skip is a decision, not an
        unfinished task. Values outside the known option lists are dropped,
        never stored, so a forged form cannot smuggle labels into the UI."""

        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is None:
                return
            profile = account.setdefault("profile", {})
            if role in ROLES:
                profile["role"] = role
            if experience in EXPERIENCE_LEVELS:
                profile["experience"] = experience
            if interests is not None:
                profile["interests"] = [i for i in interests if i in INTERESTS]
            if languages is not None:
                profile["languages"] = [l for l in languages if l in LANGUAGES]
            onboarding = account.setdefault("onboarding", _new_onboarding())
            if skipped:
                onboarding["personalize_skipped"] = True
            else:
                onboarding["personalized"] = True
                onboarding["personalize_skipped"] = False

        self._update(op)

    def set_pref(self, account_id: str, key: str, value: Any) -> None:
        """One preference flag. Only known keys write — preferences drive UI
        behaviour, so invented keys would be invisible state."""
        allowed = {"default_depth", "beta_updates"}
        if key not in allowed:
            return
        if key == "default_depth" and value not in ("quick", "deep"):
            return

        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is not None:
                account.setdefault("prefs", {})[key] = value

        self._update(op)

    def mark_tour(self, account_id: str, state: str) -> None:
        if state not in ("done", "skipped"):
            return

        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is not None:
                account.setdefault("onboarding", _new_onboarding())["tour"] = state

        self._update(op)

    def reset_tour(self, account_id: str) -> None:
        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is not None:
                account.setdefault("onboarding", _new_onboarding())["tour"] = "new"

        self._update(op)

    def dismiss_checklist(self, account_id: str, dismissed: bool = True) -> None:
        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is not None:
                account.setdefault("onboarding", _new_onboarding())["checklist_dismissed"] = bool(dismissed)

        self._update(op)

    def mark_dashboard_visited(self, account_id: str) -> None:
        """First dashboard render of an account sets the returning-user marker.
        The 'welcome back' state only exists because this was written once."""

        def op(data: dict[str, Any]) -> None:
            account = self._find(data, account_id)
            if account is not None:
                account.setdefault("onboarding", _new_onboarding())["visited_dashboard_at"] = _utcnow_iso()

        self._update(op)

    # ----- feedback -------------------------------------------------------------
    def add_feedback(self, account_id: str, kind: str, title: str, body: str, page: str = "") -> dict[str, Any]:
        """One piece of beta feedback. Kept per account at the top level so a
        future admin view can read across people without touching profiles."""
        if kind not in FEEDBACK_KINDS:
            raise ValueError(f"unknown feedback kind: {kind!r}")
        clean_title = " ".join((title or "").split())[:MAX_FEEDBACK_TITLE_CHARS]
        clean_body = " ".join((body or "").split())[:MAX_FEEDBACK_BODY_CHARS]
        if not clean_title or not clean_body:
            raise ValueError("feedback needs a title and details.")
        entry = {
            "id": f"f-{uuid.uuid4().hex[:10]}",
            "account_id": account_id,
            "kind": kind,
            "title": clean_title,
            "body": clean_body,
            "page": (page or "")[:200],
            "created_at": _utcnow_iso(),
            "status": "new",
        }

        def op(data: dict[str, Any]) -> None:
            data.setdefault("feedback", []).append(entry)

        self._update(op)
        return dict(entry)

    def feedback_for(self, account_id: str) -> list[dict[str, Any]]:
        entries = [
            copy.deepcopy(raw)
            for raw in self._read().get("feedback", [])
            if raw.get("account_id") == account_id
        ]
        entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        return entries

    # ----- deletion ---------------------------------------------------------------
    def delete_account(self, account_id: str) -> None:
        """Remove the account, its codes and its feedback. Workspace content
        (conversations, projects) lives under the shared local Store and is not
        the account's — the Settings copy says exactly that."""
        account = self.by_id(account_id)
        email = account.get("email") if account else None

        def op(data: dict[str, Any]) -> None:
            data["accounts"] = [a for a in data.get("accounts", []) if a.get("id") != account_id]
            data["feedback"] = [f for f in data.get("feedback", []) if f.get("account_id") != account_id]
            if email:
                data["codes"] = {
                    key: value
                    for key, value in data.get("codes", {}).items()
                    if value.get("email") != email
                }

        self._update(op)


def _new_onboarding() -> dict[str, Any]:
    return {
        "personalized": False,
        "personalize_skipped": False,
        "tour": "new",
        "checklist_dismissed": False,
        "visited_dashboard_at": "",
    }


def _new_account(
    *,
    name: str,
    email: str,
    provider: str,
    verified: bool,
    password_hash: str,
    password_salt: str,
) -> dict[str, Any]:
    now = _utcnow_iso()
    return {
        "id": f"u-{uuid.uuid4().hex[:12]}",
        "name": name,
        "email": email,
        "workspace": "Personal space",
        "provider": provider,
        "email_verified": bool(verified),
        "password_hash": password_hash,
        "password_salt": password_salt,
        "created_at": now,
        "last_login_at": now,
        "profile": {"role": "", "experience": "", "interests": [], "languages": []},
        "prefs": {"default_depth": "quick", "beta_updates": True},
        "onboarding": _new_onboarding(),
        "sessions": [],
    }


# ----- view helpers: what templates and routes need per account -------------------

def workspace_view(account: dict[str, Any]) -> dict[str, str]:
    """The sidebar/topbar's `user` object for a signed-in account, shaped like
    ``Store.user`` so the chrome never needs to know which produced it."""
    return {
        "id": account.get("id", ""),
        "name": account.get("name") or "Explorer",
        "workspace": account.get("workspace") or "Personal space",
    }


def checklist_for(account: dict[str, Any], project_count: int) -> list[dict[str, Any]]:
    """The new-user checklist, computed from real state — every item's done? is
    derived, never stored, so the list can never show a step as complete that
    did not happen."""
    onboarding = account.get("onboarding") or _new_onboarding()
    return [
        {
            "key": "verify",
            "label": "Confirm your email" if account.get("provider") == "email" else f"Sign in with {PROVIDER_LABELS.get(account.get('provider', ''), 'a provider')}",
            "done": bool(account.get("email_verified")),
            "href": "/auth/verify" if account.get("provider") == "email" else "",
        },
        {
            "key": "personalize",
            "label": "Personalize your workspace (optional)",
            "done": bool(onboarding.get("personalized")) or bool(onboarding.get("personalize_skipped")),
            "href": "/onboarding",
        },
        {
            "key": "project",
            "label": "Create or import your first project",
            "done": project_count > 0,
            "href": "/create",
        },
        {
            "key": "tour",
            "label": "Take the 60-second tour",
            "done": onboarding.get("tour") in ("done", "skipped"),
            "href": "/dashboard?tour=1",
        },
    ]


def recommendations_for(account: dict[str, Any]) -> list[dict[str, str]]:
    """Personalized next actions. Every card points at a real page; interests
    map to agent specialties that exist, languages shape the describe-idea
    suggestion. With no personalization yet the list stays generic and short."""
    profile = account.get("profile") or {}
    interests = list(profile.get("interests") or [])
    languages = list(profile.get("languages") or [])
    cards: list[dict[str, str]] = []

    for interest in interests:
        agent_id, text = INTEREST_AGENT.get(interest, ("", ""))
        if not agent_id:
            continue
        cards.append(
            {
                "kind": "agent",
                "title": f"{interest}: start with a specialist",
                "text": text + ". Assign it to a project from the Agents page.",
                "href": "/agents",
                "go": "Browse agents",
            }
        )
        break  # one specialist card is enough

    if languages:
        langs = ", ".join(languages[:3])
        cards.append(
            {
                "kind": "describe",
                "title": "Describe your first project",
                "text": f"Tell the Main Agent what you want to build with {langs} — it will help you shape the first tasks.",
                "href": "/create#describe",
                "go": "Describe an idea",
            }
        )

    if not interests and not languages:
        cards.append(
            {
                "kind": "personalize",
                "title": "Personalize ORBIT (optional)",
                "text": "Four quick questions so suggestions match what you build and the tools you use. Skip it whenever you like.",
                "href": "/onboarding",
                "go": "Answer four questions",
            }
        )

    cards.append(
        {
            "kind": "chat",
            "title": "Just talk to Orbit",
            "text": "The home chat is always ready — ask a question, think out loud, or plan something new.",
            "href": "/",
            "go": "Open the chat",
        }
    )
    return cards[:3]


def session_label(agent: str) -> str:
    """A short human label for a stored user-agent string."""
    text = agent or ""
    if not text:
        return "This browser"
    if "Windows" in text:
        platform = "Windows"
    elif "Mac OS" in text or "Macintosh" in text:
        platform = "Mac"
    elif "Linux" in text:
        platform = "Linux"
    elif "Android" in text:
        platform = "Android"
    elif "iPhone" in text or "iPad" in text:
        platform = "iOS"
    else:
        platform = "Unknown device"
    browser = "Browser"
    for marker, label in (("Edg", "Edge"), ("Chrome", "Chrome"), ("Firefox", "Firefox"), ("Safari", "Safari")):
        if marker in text:
            browser = label
            break
    return f"{browser} on {platform}"
