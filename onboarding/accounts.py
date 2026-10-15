"""Accounts, sessions, email verification and password recovery.

Design rules this file follows, all of them load-bearing:

* **No secret is ever stored in the clear.** Passwords are PBKDF2-HMAC-SHA256
  with a per-account salt and 210,000 iterations; verification and reset tokens
  are stored as SHA-256 digests, so a leaked state file is not a leaked login.
* **Nothing runs at import.** ``AccountStore`` takes an explicit path and only
  touches the disk when a route asks it to. ``create_app`` never constructs one,
  so importing or testing the app cannot write to ``runtime/``.
* **A token is single-use and time-boxed.** Verification links last 24 hours,
  reset links 60 minutes, and both are consumed on first use.
* **Signing up does not imply repository access.** The GitHub columns here record
  identity only; a repository grant is a separate row a person has to create on
  purpose (see ``onboarding.projects``).
* **Failed attempts are cheap to count and expensive to repeat.** Login and reset
  requests are throttled per email and per client, with the waiting time reported
  rather than a generic refusal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PBKDF2_ITERATIONS = 210_000
VERIFY_TOKEN_TTL = timedelta(hours=24)
RESET_TOKEN_TTL = timedelta(minutes=60)
SESSION_TTL = timedelta(days=30)
SESSION_TTL_SHORT = timedelta(hours=12)
MIN_PASSWORD_LENGTH = 10

# A tiny list on purpose: this blocks the passwords that actually appear at the
# top of every breach corpus, and refuses to pretend a blacklist is a policy.
COMMON_PASSWORDS = {
    "password", "password1", "password123", "1234567890", "qwertyuiop",
    "letmein123", "iloveyou1", "admin12345", "welcome123", "changeme123",
    "orbitorbit", "passw0rd123", "1q2w3e4r5t", "abc12345678",
}

PROVIDERS = ("email", "google", "github", "demo")

# Which preference keys the server will accept, and what shape each one takes.
_LIST_PREFERENCE_KEYS = ("interests", "languages", "custom_languages")
_BOOL_PREFERENCE_KEYS = (
    "use_preferences_for_suggestions",
    "voice_replies",
    "email_product_updates",
    "feedback_replies",
)
_CHOICE_PREFERENCES = {
    "background": {"student", "early", "professional", "lead", "founder", "hobbyist", "other", ""},
    "experience": {"new", "some", "confident", "expert", ""},
    "default_mode": {"chat", "agent"},
    "default_depth": {"quick", "deep"},
}
_TEXT_PREFERENCE_KEYS = ("role", "goal")
MAX_PREFERENCE_ITEMS = 24
MAX_TEXT_PREFERENCE = 160


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def parse_iso(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


# --------------------------------------------------------------------------- #
# Password and token helpers
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check that never raises on a malformed stored value."""
    if not stored or not password:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def password_problems(password: str, *, email: str = "", name: str = "") -> list[str]:
    """Everything wrong with a password, in the order a person would fix it."""
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if password.lower() in COMMON_PASSWORDS:
        problems.append(
            "That password is too predictable — it is one of the first ones an attacker tries."
        )
    local_part = (email or "").split("@")[0].lower()
    if local_part and len(local_part) > 2 and local_part in password.lower():
        problems.append("Don't build it out of your email address.")
    first_name = (name or "").split(" ")[0].lower()
    if first_name and len(first_name) > 2 and first_name in password.lower():
        problems.append("Don't build it out of your own name.")
    return problems


def password_strength(password: str) -> dict[str, Any]:
    """An advisory score for the meter. It never blocks a submit on its own."""
    score = 0
    classes = 0
    for test in (str.islower, str.isupper, str.isdigit, lambda s: not s.isalnum()):
        if any(test(character) for character in password):
            classes += 1
    length = len(password)
    if length >= 8:
        score += 1
    if length >= MIN_PASSWORD_LENGTH:
        score += 1
    if length >= 16:
        score += 1
    score += max(0, classes - 1)
    if password.lower() in COMMON_PASSWORDS:
        score = 0
    score = max(0, min(4, score))
    labels = ("Very weak", "Weak", "Fair", "Good", "Strong")
    return {"score": score, "label": labels[score]}


def valid_email_shape(email: str) -> str:
    """A deliberate shape check, not an RFC 5322 parser.

    Deliverability is decided by the verification email, not by a regex, so this
    only catches the typo a person can see for themselves.
    """
    email = normalize_email(email)
    if not email:
        return "Enter an email address so we can reach you."
    if "@" not in email:
        return "That is not a valid email address — it needs an @ and a domain, like you@example.com."
    local, _, domain = email.rpartition("@")
    if not local or not domain or "." not in domain:
        return "That is not a valid email address — the domain looks incomplete."
    if any(character.isspace() for character in email):
        return "That is not a valid email address — they cannot contain spaces."
    if domain.endswith(".") or domain.startswith(".") or ".." in email:
        return "That is not a valid email address — check it for a typo."
    return ""


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass
class SessionRecord:
    id: str
    created_at: str
    last_seen: str
    expires_at: str
    user_agent: str = ""
    label: str = "Unknown device"
    current: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expired"] = bool(parse_iso(self.expires_at) and parse_iso(self.expires_at) < _now())
        return payload


@dataclass
class Account:
    id: str
    name: str
    email: str
    provider: str = "email"
    provider_subject: str = ""
    password_hash: str = ""
    verified: bool = False
    created_at: str = field(default_factory=_iso)
    last_seen: str = field(default_factory=_iso)
    workspace: str = "Personal workspace"
    verification: dict[str, Any] = field(default_factory=dict)
    reset: dict[str, Any] = field(default_factory=dict)
    sessions: list[SessionRecord] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)
    onboarding: dict[str, Any] = field(default_factory=dict)
    integrations: dict[str, Any] = field(default_factory=dict)
    is_demo: bool = False

    # ----- derived -----
    @property
    def first_name(self) -> str:
        return (self.name or "").strip().split(" ")[0] if (self.name or "").strip() else "there"

    @property
    def initials(self) -> str:
        parts = [part for part in (self.name or "").strip().split(" ") if part]
        if not parts:
            return self.email[:2].upper() if self.email else "OR"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][:1] + parts[-1][:1]).upper()

    @property
    def signed_up_with_provider(self) -> bool:
        return self.provider in ("google", "github")

    @property
    def provider_label(self) -> str:
        return {"google": "Google", "github": "GitHub", "demo": "Demo workspace"}.get(
            self.provider, "Email and password"
        )

    @property
    def linked_providers(self) -> list[str]:
        """Every provider this account can sign in with, primary one first.

        An account created with an email and a password stays an email account
        when Google or GitHub is linked later, so the password keeps working and
        the screens go on saying it does.
        """
        recorded = [str(item) for item in (self.integrations or {}).get("identity_providers", [])]
        known = [self.provider, *recorded]
        return [item for item in dict.fromkeys(known) if item in PROVIDERS]

    def github_repository_access(self) -> bool:
        """True only when a repository grant was made on purpose.

        Signing up with GitHub never sets this. The whole point of the sentence
        on the consent screen is that this stays False until the person asks.
        """
        return bool(self.integrations.get("github_repo_access"))

    def public(self) -> dict[str, Any]:
        """The shape routes and templates are allowed to see (no hashes, no tokens)."""
        return {
            "id": self.id,
            "name": self.name,
            "first_name": self.first_name,
            "initials": self.initials,
            "email": self.email,
            "provider": self.provider,
            "provider_label": self.provider_label,
            "linked_providers": self.linked_providers,
            "verified": self.verified,
            "created_at": self.created_at,
            "workspace": self.workspace,
            "is_demo": self.is_demo,
            "github_repository_access": self.github_repository_access(),
            "preferences": dict(self.preferences),
            "onboarding": dict(self.onboarding),
            "integrations": {k: v for k, v in self.integrations.items() if k != "github_token"},
            "has_password": bool(self.password_hash),
        }

    def export(self) -> dict[str, Any]:
        """Everything ORBIT holds about this account, for the data-export route.

        Deliberately excludes credential material and live tokens: an export is
        meant to be handed to the person, not to widen the blast radius of a
        download sitting in a Downloads folder.
        """
        payload = self.public()
        payload["sessions"] = [session.as_dict() for session in self.sessions]
        payload["exported_at"] = _iso()
        payload["note"] = (
            "Credential material (password hash, verification and reset tokens) "
            "is not included in this export."
        )
        return payload


DEFAULT_ONBOARDING = {
    "personalize_done": False,
    "personalize_skipped": False,
    "welcome_seen": False,
    "tour_seen": False,
    "tour_opt_out": False,
    "dismissed_hints": [],
    "getting_started_dismissed": False,
    "landing_visits": 0,
}

DEFAULT_PREFERENCES = {
    "background": "",
    "experience": "",
    "interests": [],
    "languages": [],
    "custom_languages": [],
    "role": "",
    "goal": "",
    "default_mode": "chat",
    "default_depth": "quick",
    "use_preferences_for_suggestions": True,
    "voice_replies": True,
    "email_product_updates": True,
    "feedback_replies": True,
    "density": "comfortable",
}

DEFAULT_INTEGRATIONS = {
    "github_connected": False,
    "github_login": "",
    "github_repo_access": False,
    "github_scope": "",
    "github_connected_at": "",
    "push_enabled": False,
    "push_topic_hint": "",
}


class AccountStoreError(RuntimeError):
    """Raised for the states a route must turn into a specific sentence."""


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #

class AccountStore:
    """Accounts persisted as one JSON document, written atomically."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        self._by_email: dict[str, str] = {}
        self._loaded = False
        self._throttle: dict[str, list[float]] = {}

    # ----- persistence -----
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            if self.path.exists():
                try:
                    document = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    raise AccountStoreError(
                        f"Account state at {self.path} is unreadable. Move it aside and "
                        "restart to start with a fresh account list."
                    ) from None
                for raw in document.get("accounts", []):
                    account = self._from_dict(raw)
                    self._accounts[account.id] = account
                    self._by_email[account.email] = account.id
            self._loaded = True

    @staticmethod
    def _from_dict(raw: dict[str, Any]) -> Account:
        sessions = [SessionRecord(**item) for item in raw.pop("sessions", [])]
        onboarding = {**DEFAULT_ONBOARDING, **(raw.pop("onboarding", {}) or {})}
        preferences = {**DEFAULT_PREFERENCES, **(raw.pop("preferences", {}) or {})}
        integrations = {**DEFAULT_INTEGRATIONS, **(raw.pop("integrations", {}) or {})}
        known = {field_name for field_name in Account.__dataclass_fields__}
        account = Account(**{key: value for key, value in raw.items() if key in known})
        account.sessions = sessions
        account.onboarding = onboarding
        account.preferences = preferences
        account.integrations = integrations
        return account

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "accounts": [asdict(account) for account in self._accounts.values()]}
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)

    # ----- reads -----
    def get(self, account_id: str | None) -> Account | None:
        if not account_id:
            return None
        self._ensure_loaded()
        return self._accounts.get(account_id)

    def find_by_email(self, email: str) -> Account | None:
        self._ensure_loaded()
        account_id = self._by_email.get(normalize_email(email))
        return self._accounts.get(account_id) if account_id else None

    def count(self) -> int:
        self._ensure_loaded()
        return len(self._accounts)

    def by_provider_subject(self, provider: str, subject: str) -> Account | None:
        if not subject:
            return None
        self._ensure_loaded()
        for account in self._accounts.values():
            if account.provider == provider and account.provider_subject == subject:
                return account
        return None

    # ----- writes -----
    def create_account(
        self,
        *,
        name: str,
        email: str,
        password: str = "",
        provider: str = "email",
        provider_subject: str = "",
        verified: bool = False,
        is_demo: bool = False,
    ) -> Account:
        self._ensure_loaded()
        email = normalize_email(email)
        if provider not in PROVIDERS:
            raise AccountStoreError(f"Unknown sign-up provider: {provider}.")
        with self._lock:
            if email and email in self._by_email:
                raise AccountStoreError(
                    "An account already exists for this email address."
                )
            account = Account(
                id=f"u-{uuid.uuid4().hex[:12]}",
                name=(name or "").strip() or email.split("@")[0],
                email=email,
                provider=provider,
                provider_subject=provider_subject,
                password_hash=hash_password(password) if password else "",
                verified=verified,
                is_demo=is_demo,
                onboarding=dict(DEFAULT_ONBOARDING),
                preferences=dict(DEFAULT_PREFERENCES),
                integrations=dict(DEFAULT_INTEGRATIONS),
            )
            self._accounts[account.id] = account
            if account.email:
                self._by_email[account.email] = account.id
            self._save()
        return account

    def save(self, account: Account) -> None:
        with self._lock:
            self._accounts[account.id] = account
            self._by_email = {
                existing.email: existing.id for existing in self._accounts.values() if existing.email
            }
            account.last_seen = _iso()
            self._save()

    def delete(self, account_id: str) -> bool:
        self._ensure_loaded()
        with self._lock:
            account = self._accounts.pop(account_id, None)
            if account is None:
                return False
            self._by_email.pop(account.email, None)
            self._save()
        return True

    # ----- credentials -----
    def authenticate(self, email: str, password: str) -> Account:
        """Return the account, or raise with the sentence the form should show.

        The message for an unknown email and a wrong password is the same one, so
        the form cannot be used to enumerate who has an account here.
        """
        account = self.find_by_email(email)
        wrong = AccountStoreError("That email and password don’t match an account.")
        if account is None:
            # Spend roughly the same time as a real check so the timing does not
            # answer the question the message refused to.
            hash_password(password or "x")
            raise wrong
        if account.is_demo:
            raise AccountStoreError("The demo workspace doesn't use a password. Open it from the landing page.")
        if not account.password_hash or not verify_password(password, account.password_hash):
            raise wrong
        return account

    def change_password(self, account: Account, current: str, new: str, *, keep_session_id: str = "") -> int:
        """Change the password and end stale logins, keeping the device doing it.

        Returns the number of other sessions that were signed out, because the
        person deserves to be told that happened.
        """
        if account.password_hash and not verify_password(current, account.password_hash):
            raise AccountStoreError("That is not your current password.")
        problems = password_problems(new, email=account.email, name=account.name)
        if problems:
            raise AccountStoreError(problems[0])
        if account.password_hash and verify_password(new, account.password_hash):
            raise AccountStoreError("That is the password you already have.")
        account.password_hash = hash_password(new)
        signed_out = len([s for s in account.sessions if s.id != keep_session_id])
        account.sessions = [session for session in account.sessions if session.id == keep_session_id]
        self.save(account)
        return signed_out

    def change_email(self, account: Account, email: str) -> None:
        email = normalize_email(email)
        problem = valid_email_shape(email)
        if problem:
            raise AccountStoreError(problem)
        existing = self.find_by_email(email)
        if existing is not None and existing.id != account.id:
            raise AccountStoreError("Another account already uses that email address.")
        if email == account.email:
            raise AccountStoreError("That is already your email address.")
        previous = account.email
        account.email = email
        # A different address is a different inbox, so it starts unverified.
        account.verified = account.provider in ("google", "github")
        with self._lock:
            self._by_email.pop(previous, None)
            self._by_email[email] = account.id
        self.save(account)

    # ----- email verification -----
    def start_verification(self, account: Account) -> str:
        """Begin (or restart) verification and return the token for the email."""
        token = new_token()
        account.verification = {
            "digest": token_digest(token),
            "issued_at": _iso(),
            "expires_at": _iso(_now() + VERIFY_TOKEN_TTL),
            "sent_count": int(account.verification.get("sent_count", 0)) + 1,
        }
        self.save(account)
        return token

    def consume_verification(self, token: str) -> tuple[Account | None, str]:
        """Return (account, "") on success or (None, short_reason) on failure."""
        self._ensure_loaded()
        digest = token_digest(token or "")
        for account in self._accounts.values():
            record = account.verification or {}
            if record.get("digest") and hmac.compare_digest(record["digest"], digest):
                if record.get("used_at"):
                    return None, "used"
                expires = parse_iso(record.get("expires_at", ""))
                if expires and expires < _now():
                    account.verification = {}
                    self.save(account)
                    return None, "expired"
                account.verified = True
                account.verification = {
                    "confirmed_at": _iso(),
                    "digest": digest,
                    "used_at": _iso(),
                }
                self.save(account)
                return account, ""
        return None, "unknown"

    def reset_token_known(self, digest: str) -> bool:
        """Whether this reset digest belongs to any account and is still live.

        Used to show "this link expired" *before* a person types a new password,
        rather than after they submit one.
        """
        self._ensure_loaded()
        for account in self._accounts.values():
            record = account.reset or {}
            if record.get("digest") and hmac.compare_digest(record["digest"], digest):
                expires = parse_iso(record.get("expires_at", ""))
                return not (expires and expires < _now())
        return False

    def verification_pending(self, account: Account) -> bool:
        return not account.verified and bool((account.verification or {}).get("digest"))

    # ----- password recovery -----
    def start_password_reset(self, email: str) -> tuple[Account | None, str]:
        """Return (account, token). A missing account is not an error to the caller.

        The route answers identically either way; this is where that decision is
        made so no template has to remember to.
        """
        account = self.find_by_email(email)
        if account is None or account.is_demo:
            return None, ""
        token = new_token()
        account.reset = {
            "digest": token_digest(token),
            "issued_at": _iso(),
            "expires_at": _iso(_now() + RESET_TOKEN_TTL),
        }
        self.save(account)
        return account, token

    def consume_reset(self, token: str, new_password: str) -> tuple[Account | None, str, str]:
        """Return (account, "", "") or (None, short_reason, message)."""
        self._ensure_loaded()
        digest = token_digest(token or "")
        for account in self._accounts.values():
            record = account.reset or {}
            if record.get("digest") and hmac.compare_digest(record["digest"], digest):
                expires = parse_iso(record.get("expires_at", ""))
                if expires and expires < _now():
                    account.reset = {}
                    self.save(account)
                    return None, "expired", "That reset link has expired. Request a new one."
                problems = password_problems(new_password, email=account.email, name=account.name)
                if problems:
                    return None, "invalid_password", problems[0]
                account.password_hash = hash_password(new_password)
                account.reset = {}
                account.sessions = []
                self.save(account)
                return account, "", ""
        return None, "unknown", "That reset link is not valid any more. Request a new one."

    # ----- sessions -----
    def create_session(self, account: Account, *, user_agent: str = "", remember: bool = True) -> SessionRecord:
        ttl = SESSION_TTL if remember else SESSION_TTL_SHORT
        record = SessionRecord(
            id=f"s-{secrets.token_urlsafe(18)}",
            created_at=_iso(),
            last_seen=_iso(),
            expires_at=_iso(_now() + ttl),
            user_agent=(user_agent or "")[:200],
            label=describe_device(user_agent or ""),
        )
        account.sessions.append(record)
        # Keep the list readable: drop expired rows, then cap the tail.
        account.sessions = [
            item for item in account.sessions if not (parse_iso(item.expires_at) and parse_iso(item.expires_at) < _now())
        ][-12:]
        self.save(account)
        return record

    def session_valid(self, session_id: str) -> tuple[Account | None, SessionRecord | None]:
        if not session_id:
            return None, None
        self._ensure_loaded()
        for account in self._accounts.values():
            for session in account.sessions:
                if hmac.compare_digest(session.id, session_id):
                    expires = parse_iso(session.expires_at)
                    if expires and expires < _now():
                        account.sessions.remove(session)
                        self.save(account)
                        return None, None
                    return account, session
        return None, None

    def touch_session(self, account: Account, session_id: str) -> None:
        for session in account.sessions:
            if session.id == session_id:
                session.last_seen = _iso()
                break

    def revoke_session(self, account: Account, session_id: str) -> bool:
        before = len(account.sessions)
        account.sessions = [session for session in account.sessions if session.id != session_id]
        if len(account.sessions) != before:
            self.save(account)
            return True
        return False

    def revoke_others(self, account: Account, keep_id: str) -> int:
        removed = len([s for s in account.sessions if s.id != keep_id])
        account.sessions = [session for session in account.sessions if session.id == keep_id]
        self.save(account)
        return removed

    # ----- preferences, onboarding state, integrations -----
    def update_preferences(self, account: Account, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply only the keys this store understands, with the values it accepts."""
        applied: dict[str, Any] = {}
        for key, value in (changes or {}).items():
            if key in _BOOL_PREFERENCE_KEYS:
                applied[key] = bool(value)
            elif key in _CHOICE_PREFERENCES:
                candidate = str(value)
                if candidate not in _CHOICE_PREFERENCES[key]:
                    raise AccountStoreError(f"'{value}' is not a valid choice for {key}.")
                applied[key] = candidate
            elif key in _LIST_PREFERENCE_KEYS:
                items = value if isinstance(value, (list, tuple)) else [value]
                cleaned: list[str] = []
                for item in items:
                    text = str(item).strip()[:60]
                    if text and text not in cleaned:
                        cleaned.append(text)
                applied[key] = cleaned[:MAX_PREFERENCE_ITEMS]
            elif key in _TEXT_PREFERENCE_KEYS:
                applied[key] = str(value).strip()[:MAX_TEXT_PREFERENCE]
            elif key == "density":
                applied[key] = str(value) if value in ("comfortable", "compact") else "comfortable"
        account.preferences = {**DEFAULT_PREFERENCES, **account.preferences, **applied}
        self.save(account)
        return account.preferences

    def update_onboarding(self, account: Account, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_ONBOARDING)
        applied = {key: value for key, value in (changes or {}).items() if key in allowed}
        if "dismissed_hints" in applied:
            hints = applied["dismissed_hints"] if isinstance(applied["dismissed_hints"], (list, tuple)) else []
            merged = list(dict.fromkeys([*account.onboarding.get("dismissed_hints", []), *[str(h)[:60] for h in hints]]))
            applied["dismissed_hints"] = merged[-40:]
        account.onboarding = {**DEFAULT_ONBOARDING, **account.onboarding, **applied}
        self.save(account)
        return account.onboarding

    def dismiss_hint(self, account: Account, hint_id: str) -> dict[str, Any]:
        return self.update_onboarding(account, {"dismissed_hints": [hint_id]})

    def update_profile(self, account: Account, *, name: str = "", workspace: str = "") -> None:
        if name is not None and name.strip():
            account.name = name.strip()[:80]
        if workspace is not None and workspace.strip():
            account.workspace = workspace.strip()[:80]
        self.save(account)

    def update_integrations(self, account: Account, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_INTEGRATIONS)
        applied = {key: value for key, value in (changes or {}).items() if key in allowed}
        account.integrations = {**DEFAULT_INTEGRATIONS, **account.integrations, **applied}
        self.save(account)
        return account.integrations

    # ----- throttling -----
    def throttle(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds). Counts only the window it names."""
        import time

        now = time.monotonic()
        with self._lock:
            stamps = [stamp for stamp in self._throttle.get(key, []) if now - stamp < window_seconds]
            if len(stamps) >= limit:
                self._throttle[key] = stamps
                return False, int(window_seconds - (now - stamps[0])) + 1
            stamps.append(now)
            self._throttle[key] = stamps
        return True, 0

    def clear_throttle(self, key: str) -> None:
        with self._lock:
            self._throttle.pop(key, None)


def describe_device(user_agent: str) -> str:
    """A short, honest device label from the UA string — never a fingerprint."""
    ua = (user_agent or "").lower()
    if not ua:
        return "Unknown device"
    platform = "Device"
    for needle, label in (
        ("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
        ("macintosh", "Mac"), ("mac os", "Mac"), ("windows", "Windows"),
        ("linux", "Linux"), ("cros", "Chromebook"),
    ):
        if needle in ua:
            platform = label
            break
    browser = ""
    for needle, label in (
        ("edg/", "Edge"), ("opr/", "Opera"), ("chrome/", "Chrome"),
        ("firefox/", "Firefox"), ("safari/", "Safari"),
    ):
        if needle in ua:
            browser = label
            break
    return f"{browser} on {platform}".strip() if browser else platform
