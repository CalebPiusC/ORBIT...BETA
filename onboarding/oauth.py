"""Google and GitHub sign-in, written so the scope question cannot be dodged.

The requirement that shapes this module: **authenticating with GitHub must not
imply repository access.** That is enforced in three places, not one sentence of
copy:

1. The scope strings below ask for identity only — GitHub gets ``read:user`` and
   ``user:email``; Google gets ``openid email profile``. ``repo`` appears nowhere
   in ``IDENTITY_SCOPES``.
2. A repository grant is a *different* operation with its own scope constant
   (``REPOSITORY_SCOPES``), its own consent screen, and its own column in
   ``account.integrations`` that sign-in never touches.
3. The consent screen between the button and the redirect says which scopes are
   requested, in words, and offers "not now" as a first-class choice.

``SimulatedOAuth`` exists because this build has no client id to point at. It is
never silently substituted: the routes only use it when ``ORBIT_OAUTH_DEMO`` is
on, and every screen it touches is labeled as a simulation. An unconfigured
provider that is *not* in demo mode returns a configuration error instead of a
fake success — the failure mode this repo keeps having to dig itself out of.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol

IDENTITY_SCOPES = {
    "google": "openid email profile",
    "github": "read:user user:email",
}

REPOSITORY_SCOPES = {
    "github": "public_repo",
}

# What each provider's identity scope means, said plainly for the consent screen.
SCOPE_EXPLANATIONS = {
    "google": (
        ("email", "The address on your Google account, so ORBIT can match it to a login."),
        ("profile", "Your name and profile picture, so the workspace can greet you by name."),
    ),
    "github": (
        ("read:user", "Your public profile: display name, username, avatar."),
        ("user:email", "Your verified email addresses, so ORBIT can reach you."),
    ),
}

REPOSITORY_EXPLANATION = (
    "Reading repository contents and file listings. Granted separately, from "
    "Settings → Integrations, and revocable on its own."
)

NOT_GRANTED_BY_SIGNIN = (
    "Repository access is not part of this. ORBIT will not see, list, clone, or "
    "read any of your repositories unless you grant it yourself, later."
)


@dataclass
class OAuthIdentity:
    provider: str
    subject: str
    email: str
    name: str
    username: str = ""
    avatar_url: str = ""
    email_verified: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class OAuthError(RuntimeError):
    """Carries the sentence a person should read, not a stack trace."""


class OAuthProvider(Protocol):
    slug: str
    label: str
    configured: bool
    simulate: bool

    def authorize_url(self, *, state: str, code_challenge: str = "") -> str: ...
    def exchange(self, *, code: str, code_verifier: str = "", state: str = "") -> OAuthIdentity: ...


# --------------------------------------------------------------------------- #
# Shared PKCE helpers (Google supports it; GitHub ignores the extra parameter)
# --------------------------------------------------------------------------- #

def new_state() -> str:
    return secrets.token_urlsafe(24)


def new_verifier() -> str:
    return secrets.token_urlsafe(48)[:96]


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- #
# Real providers
# --------------------------------------------------------------------------- #

class GoogleOAuth:
    slug = "google"
    label = "Google"
    authorize_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(self, *, client_id: str = "", client_secret: str = "", timeout: float = 20.0) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.redirect_uri = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def simulate(self) -> bool:
        return False

    def authorize_url(self, *, state: str, code_challenge: str = "") -> str:
        if not self.configured:
            raise OAuthError(
                "Google sign-in is not configured on this deployment. "
                "Set ORBIT_GOOGLE_CLIENT_ID and ORBIT_GOOGLE_CLIENT_SECRET, or "
                "create an account with your email instead."
            )
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": IDENTITY_SCOPES["google"],
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{self.authorize_endpoint}?{urllib.parse.urlencode(params)}"

    def exchange(self, *, code: str, code_verifier: str = "", state: str = "") -> OAuthIdentity:
        import httpx

        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        with httpx.Client(timeout=self.timeout) as client:
            token_response = client.post(self.token_endpoint, data=data, headers={"Accept": "application/json"})
            if token_response.status_code >= 400:
                raise OAuthError(_provider_message("Google", token_response, "sign you in"))
            access_token = token_response.json().get("access_token", "")
            if not access_token:
                raise OAuthError("Google accepted the request but returned no access token.")
            profile_response = client.get(self.userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"})
            if profile_response.status_code >= 400:
                raise OAuthError(_provider_message("Google", profile_response, "read your profile"))
            profile = profile_response.json()

        email = str(profile.get("email", "")).strip().lower()
        if not email:
            raise OAuthError("Google did not share an email address, so there is nothing to match this login to.")
        return OAuthIdentity(
            provider="google",
            subject=str(profile.get("sub", "")) or f"google:{email}",
            email=email,
            name=str(profile.get("name") or email.split("@")[0]),
            username=str(profile.get("given_name", "")),
            avatar_url=str(profile.get("picture", "")),
            email_verified=bool(profile.get("email_verified", False)),
            raw={"scopes": IDENTITY_SCOPES["google"]},
        )


class GitHubOAuth:
    slug = "github"
    label = "GitHub"
    authorize_endpoint = "https://github.com/login/oauth/authorize"
    token_endpoint = "https://github.com/login/oauth/access_token"
    user_endpoint = "https://api.github.com/user"
    emails_endpoint = "https://api.github.com/user/emails"

    def __init__(self, *, client_id: str = "", client_secret: str = "", timeout: float = 20.0) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.redirect_uri = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def simulate(self) -> bool:
        return False

    def authorize_url(self, *, state: str, code_challenge: str = "") -> str:
        if not self.configured:
            raise OAuthError(
                "GitHub sign-in is not configured on this deployment. "
                "Set ORBIT_GITHUB_CLIENT_ID and ORBIT_GITHUB_CLIENT_SECRET, or "
                "create an account with your email instead."
            )
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            # Identity only. `repo` is deliberately absent — see the module docstring.
            "scope": IDENTITY_SCOPES["github"],
            "state": state,
            "allow_signup": "true",
        }
        return f"{self.authorize_endpoint}?{urllib.parse.urlencode(params)}"

    def exchange(self, *, code: str, code_verifier: str = "", state: str = "") -> OAuthIdentity:
        import httpx

        with httpx.Client(timeout=self.timeout) as client:
            token_response = client.post(
                self.token_endpoint,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            if token_response.status_code >= 400:
                raise OAuthError(_provider_message("GitHub", token_response, "sign you in"))
            access_token = str(token_response.json().get("access_token", ""))
            if not access_token:
                raise OAuthError(
                    "GitHub accepted the request but returned no access token. "
                    "Check that the OAuth app's callback URL matches this deployment."
                )
            headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
            profile_response = client.get(self.user_endpoint, headers=headers)
            if profile_response.status_code >= 400:
                raise OAuthError(_provider_message("GitHub", profile_response, "read your profile"))
            profile = profile_response.json()
            email = str(profile.get("email") or "").strip().lower()
            if not email:
                emails_response = client.get(self.emails_endpoint, headers=headers)
                if emails_response.status_code < 400:
                    for item in emails_response.json() or []:
                        if item.get("primary") and item.get("verified"):
                            email = str(item.get("email", "")).strip().lower()
                            break

        if not email:
            raise OAuthError(
                "GitHub did not return a verified email address. Add one to your "
                "GitHub account, or sign up with your email instead."
            )
        return OAuthIdentity(
            provider="github",
            subject=str(profile.get("id", "")) or f"github:{profile.get('login', email)}",
            email=email,
            name=str(profile.get("name") or profile.get("login") or email.split("@")[0]),
            username=str(profile.get("login", "")),
            avatar_url=str(profile.get("avatar_url", "")),
            email_verified=True,
            raw={"scopes": IDENTITY_SCOPES["github"], "repositories_granted": False},
        )


# --------------------------------------------------------------------------- #
# Simulated provider (demo builds only, and always labeled as such)
# --------------------------------------------------------------------------- #

class SimulatedOAuth:
    """Stands in for a provider that has no client id in this build.

    It cannot be reached by accident: the routes only select it when
    ``ORBIT_OAUTH_DEMO=1``, and ``simulate`` is True so every screen it appears on
    can say so.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.label = {"google": "Google", "github": "GitHub"}.get(slug, slug.title())

    @property
    def configured(self) -> bool:
        return True

    @property
    def simulate(self) -> bool:
        return True

    def authorize_url(self, *, state: str, code_challenge: str = "") -> str:
        return f"/oauth/{self.slug}/simulate?state={urllib.parse.quote(state)}"

    def exchange(self, *, code: str, code_verifier: str = "", state: str = "") -> OAuthIdentity:
        # ``code`` carries the person's choice from the simulated consent screen:
        # "allow" or "deny" plus the address they confirmed.
        payload: dict[str, str] = {}
        try:
            decoded = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)).decode("utf-8")
            payload = json.loads(decoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise OAuthError("That simulated consent response could not be read. Start again.") from None
        if payload.get("decision") != "allow":
            raise OAuthError("The consent screen was declined, so nothing was shared and no account was created.")
        email = str(payload.get("email", "")).strip().lower()
        if not email:
            raise OAuthError("Enter the email address to use for this simulated account.")
        return OAuthIdentity(
            provider=self.slug,
            subject="sim-" + hashlib.sha256(f"{self.slug}:{email}".encode()).hexdigest()[:16],
            email=email,
            name=str(payload.get("name") or email.split("@")[0]),
            username=str(payload.get("username") or email.split("@")[0]),
            email_verified=True,
            raw={
                "scopes": IDENTITY_SCOPES.get(self.slug, ""),
                "simulated": True,
                "repositories_granted": False,
            },
        )


def encode_simulated_decision(*, decision: str, email: str, name: str = "", username: str = "") -> str:
    payload = json.dumps({"decision": decision, "email": email, "name": name, "username": username})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

class OAuthRegistry:
    """The providers this deployment can actually use, plus what is missing."""

    def __init__(self, providers: dict[str, OAuthProvider], *, demo_enabled: bool = False, public_url: str = "") -> None:
        self.providers = providers
        self.demo_enabled = demo_enabled
        self.public_url = public_url
        if public_url:
            for provider in providers.values():
                if isinstance(provider, (GoogleOAuth, GitHubOAuth)):
                    provider.redirect_uri = f"{public_url.rstrip('/')}/oauth/{provider.slug}/callback"

    def get(self, slug: str) -> OAuthProvider | None:
        return self.providers.get(slug)

    def available(self, slug: str) -> bool:
        provider = self.providers.get(slug)
        return bool(provider and provider.configured)

    def simulated(self, slug: str) -> bool:
        provider = self.providers.get(slug)
        return bool(provider and provider.simulate)

    def status_line(self, slug: str) -> str:
        provider = self.providers.get(slug)
        if provider is None:
            return "Not available"
        if provider.simulate:
            return "Simulated on this build"
        return "Ready" if provider.configured else "Not configured here"

    def any_simulated(self) -> bool:
        return any(provider.simulate for provider in self.providers.values())


def build_registry(*, public_url: str = "") -> OAuthRegistry:
    demo_enabled = (os.environ.get("ORBIT_OAUTH_DEMO") or "").strip() in ("1", "true", "yes", "on")
    google: OAuthProvider = GoogleOAuth(
        client_id=(os.environ.get("ORBIT_GOOGLE_CLIENT_ID") or "").strip(),
        client_secret=(os.environ.get("ORBIT_GOOGLE_CLIENT_SECRET") or "").strip(),
    )
    github: OAuthProvider = GitHubOAuth(
        client_id=(os.environ.get("ORBIT_GITHUB_CLIENT_ID") or "").strip(),
        client_secret=(os.environ.get("ORBIT_GITHUB_CLIENT_SECRET") or "").strip(),
    )
    if demo_enabled:
        if not google.configured:
            google = SimulatedOAuth("google")
        if not github.configured:
            github = SimulatedOAuth("github")
    return OAuthRegistry({"google": google, "github": github}, demo_enabled=demo_enabled, public_url=public_url)


def _provider_message(provider: str, response: Any, attempt: str) -> str:
    """A provider error, said in terms of what the person can do next."""
    detail = ""
    try:
        payload = response.json()
        detail = str(payload.get("error_description") or payload.get("error") or payload.get("message") or "")
    except (ValueError, AttributeError):
        detail = ""
    hints = {
        400: "The request was rejected — usually a callback URL that does not match the app's settings.",
        401: "The client secret was rejected.",
        403: "The provider refused this request.",
        429: "The provider is rate-limiting this deployment. Try again in a minute.",
    }
    hint = hints.get(int(getattr(response, "status_code", 0)), "The provider returned an error.")
    tail = f" ({detail})" if detail else ""
    return f"{provider} could not {attempt}{tail}. {hint}"
