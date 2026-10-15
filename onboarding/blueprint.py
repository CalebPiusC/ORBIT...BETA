"""The onboarding surface: landing, accounts, welcome, projects, settings, feedback.

Where the pieces live:

* This module owns **routes and the gate**. It is the only place that knows a
  request might not have an account yet.
* ``onboarding.accounts`` / ``projects`` / ``feedback`` own the data.
* ``onboarding.mailer`` and ``onboarding.oauth`` are seams that either do the
  real thing or say plainly that they are not configured on this deployment.

Three behaviors are worth naming because they are decisions, not accidents:

1. **The gate is on the app, not on a template.** ``attach()`` installs a
   ``before_request`` that requires a verified account for every non-public
   endpoint, including ``/`` and ``/task/<id>``. A page cannot be reached by
   forgetting to guard it. API paths get a JSON 401 instead of a redirect, so a
   chat turn that outlives its session reports the reason instead of following a
   redirect into an HTML page.
2. **Email/password accounts wait; Google and GitHub accounts don't.** That is
   the product rule from the brief, so it is enforced in one place
   (``_requires_verification``) rather than in each screen.
3. **Nothing states something it cannot know.** Where a provider is unconfigured
   the screen says so; where a plan came from the local planner instead of a
   model, it says that; where the mailbox is a console stand-in, the panel is
   labeled as one.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlparse

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from onboarding import content, mailer as mailer_module, oauth as oauth_module, projects as projects_module
from onboarding.accounts import (
    Account,
    AccountStore,
    AccountStoreError,
    normalize_email,
    password_problems,
    password_strength,
    token_digest,
    valid_email_shape,
)
from onboarding.feedback import FeedbackStore, build_context, sanitize_context
from onboarding.projects import ProjectStore

# Endpoints reachable without an account. Everything else is gated.
PUBLIC_ENDPOINTS = {
    "onboarding.landing",
    "onboarding.demo_enter",
    "onboarding.signup",
    "onboarding.login",
    "onboarding.logout",
    "onboarding.verify",
    "onboarding.verify_token",
    "onboarding.verify_resend",
    "onboarding.verify_change_email",
    "onboarding.forgot_password",
    "onboarding.reset_password",
    "onboarding.oauth_start",
    "onboarding.oauth_callback",
    "onboarding.oauth_simulate",
    "onboarding.oauth_simulate_decide",
    "onboarding.static",
    "static",
}

# Reachable *while* an email/password account is still unverified.
PENDING_ALLOWED_ENDPOINTS = {
    "onboarding.verify",
    "onboarding.verify_token",
    "onboarding.verify_resend",
    "onboarding.verify_change_email",
    "onboarding.logout",
}

SETTINGS_SECTIONS = (
    ("profile", "Profile", "◐", "Your name, what you are building, and your working hours."),
    ("account", "Account", "⌘", "Email address, password, and where you are signed in."),
    ("preferences", "Development preferences", "⌗", "Background, experience, interests, languages."),
    ("ai", "AI assistance", "✦", "How ORBIT answers, and what it is allowed to do on its own."),
    ("integrations", "Integrations", "◉", "GitHub today. Nothing is faked as connected."),
    ("notifications", "Notifications", "◔", "Reminders, quiet hours, and what may email you."),
    ("privacy", "Privacy & data", "🔒", "What is stored, what is logged, export and deletion."),
    ("security", "Security", "⚿", "Password, active sessions, and what is not built yet."),
    ("help", "Help & beta", "?", "Replay the tour, reset hints, and talk to us."),
)


@dataclass
class OnboardingConfig:
    """Everything the blueprint needs, injected by whoever wires the app up."""

    accounts: AccountStore
    projects: ProjectStore
    feedback: FeedbackStore
    mailer: Any
    oauth: Any
    store_factory: Callable[[Account], Any] | None = None
    audit: Any | None = None
    public_url: str = ""
    build_label: str = "beta"
    demo_enabled: bool = True
    runtime_dir: str = "runtime"
    seed_demo: Callable[[Account], None] | None = None


bp = Blueprint(
    "onboarding",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/onboarding-static",
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def config() -> OnboardingConfig:
    return current_app.config["ONBOARDING"]


def accounts() -> AccountStore:
    return config().accounts


def project_store() -> ProjectStore:
    return config().projects


def feedback_store() -> FeedbackStore:
    return config().feedback


def current_account() -> Account | None:
    """The signed-in account, or None. Reads the session, never the database for guests."""
    account_id = session.get("account_id")
    if not account_id:
        return None
    account = accounts().get(account_id)
    if account is None:
        session.clear()
        return None
    session_id = session.get("session_id", "")
    resolved, record = accounts().session_valid(session_id)
    if resolved is None or resolved.id != account.id:
        # The session row is gone (revoked here, or expired). End it cleanly.
        session.clear()
        return None
    accounts().touch_session(account, session_id)
    return account


def is_pending(account: Account | None) -> bool:
    return bool(account is not None and not account.verified)


def _requires_verification(account: Account) -> bool:
    """Email/password accounts must confirm the address. Provider accounts don't.

    A provider that has itself verified the address (or vouched for it) is
    treated as deliverable proof, which is why Google and GitHub sign-ups land
    straight in the workspace.
    """
    return not account.verified


def sign_in(account: Account, *, remember: bool = True, extra: dict[str, Any] | None = None) -> None:
    record = accounts().create_session(
        account, user_agent=request.headers.get("User-Agent", ""), remember=remember
    )
    session.clear()
    session["account_id"] = account.id
    session["session_id"] = record.id
    session.permanent = bool(remember)
    session["csrf"] = secrets.token_urlsafe(24)
    if extra:
        session.update(extra)
    _audit("signed_in", account=account, provider=account.provider)


def sign_out() -> None:
    account = current_account()
    session_id = session.get("session_id", "")
    if account is not None and session_id:
        accounts().revoke_session(account, session_id)
        _audit("signed_out", account=account)
    session.clear()


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
    return token


def _csrf_ok() -> bool:
    expected = session.get("csrf", "")
    supplied = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
        or (request.get_json(silent=True) or {}).get("csrf_token")
        or ""
    )
    return bool(expected and supplied and hmac.compare_digest(str(expected), str(supplied)))


def _audit(event: str, *, account: Account | None = None, **fields: Any) -> None:
    """One JSON line per account event — ids and timings, never secrets.

    Deliberately never receives a token, a password, or an email body: the audit
    log is a record of what happened, not a second copy of the credentials.
    """
    audit = config().audit
    if audit is None:
        return
    payload = dict(fields)
    if account is not None:
        payload["account_id"] = account.id
        payload["provider"] = account.provider
    try:
        # record_event, not record: record() is the tool-call signature.
        audit.record_event(event, **payload)
    except Exception:  # pragma: no cover - auditing must never break a request
        current_app.logger.exception("could not write %s to the audit log", event)


def _safe_next(raw: str | None, fallback: str) -> str:
    """Only ever redirect to a path on this site."""
    candidate = (raw or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback
    return candidate


def _form_problems(
    *,
    name: str = "",
    email: str = "",
    password: str = "",
    confirm: str = "",
    needs_name: bool = True,
) -> dict[str, str]:
    """Field-keyed errors, so the template can put each one beside its input."""
    errors: dict[str, str] = {}
    if needs_name and not (name or "").strip():
        errors["name"] = "Tell ORBIT what to call you."
    if not email and not password and not confirm and not errors:
        return errors
    problem = valid_email_shape(email)
    if problem:
        errors["email"] = problem
    for issue in password_problems(password, email=email, name=name):
        errors.setdefault("password", issue)
        break
    if confirm is not None and password != confirm:
        errors["confirm"] = "Those two passwords don't match."
    return errors


def _store_for(account: Account) -> Any:
    factory = config().store_factory
    return factory(account) if factory is not None else None


def _client_key() -> str:
    return request.remote_addr or "unknown"


def _mailbox_panel(email: str = "") -> list[dict[str, Any]]:
    """Outbox messages for the demo-mailbox panel — only this address's.

    Showing another person's verification link would be a real vulnerability, not
    a convenience, so this filters by the address the screen is already naming.
    """
    mailer = config().mailer
    if getattr(mailer, "delivers", True):
        return []
    wanted = normalize_email(email)
    items = []
    for message in reversed(getattr(mailer, "recent", [])[-12:]):
        if wanted and normalize_email(message.to) != wanted:
            continue
        items.append(message.as_dict())
    return items


def _outbox_visible() -> bool:
    return not getattr(config().mailer, "delivers", True)


def _now_label() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")


# --------------------------------------------------------------------------- #
# Context for every onboarding page
# --------------------------------------------------------------------------- #

@bp.context_processor
def inject_shell_context() -> dict[str, Any]:
    account = current_account()
    store = _store_for(account) if account is not None else None
    heartbeat = current_app.config.get("HEARTBEAT")
    heartbeat_view = None
    if heartbeat is not None:
        try:
            heartbeat_view = heartbeat.snapshot()
        except Exception:  # pragma: no cover - a corrupt file must not kill a page
            heartbeat_view = None
    conversations = store.conversations_grouped() if store is not None else {}
    return {
        "account": account,
        "account_public": account.public() if account is not None else None,
        "user": getattr(store, "user", None)
        or SimpleNamespace(name=account.name if account else "", workspace="", id=""),
        "connections": getattr(store, "connections", []),
        "connected_count": store.connected_count() if store is not None else 0,
        "conversations": conversations,
        "active_conversation_id": getattr(store, "active_conversation_id", None),
        "heartbeat": heartbeat_view,
        "csrf_token": csrf_token,
        "content": content,
        "settings_sections": SETTINGS_SECTIONS,
        "workspace_store": store,
        "oauth_registry": config().oauth,
        "mailer_delivers": getattr(config().mailer, "delivers", True),
        "outbox_visible": _outbox_visible(),
        "build_label": config().build_label,
        "now_label": _now_label(),
    }


# --------------------------------------------------------------------------- #
# Tour and shell context
# --------------------------------------------------------------------------- #

# Where each tour stop points. The ids exist in app/templates/home.html and in the
# sidebar; a selector that misses is skipped by the tour engine rather than pointing
# at nothing.
TOUR_ANCHORS = {
    "modeToggle": "#modeToggle",
    "depthToggle": "#depthToggle",
    "composerDock": "#composerDock",
    "connectedTools": "#connectedTools",
    "sidebar": "#sidebar",
}

SURFACE_LABELS = {
    "home": "Workspace home",
    "task": "Agent task",
    "analytics": "Analytics",
}


def tour_steps_payload() -> list[dict[str, str]]:
    return [
        {
            "id": step["id"],
            "title": step["title"],
            "body": step["body"],
            "selector": TOUR_ANCHORS.get(step["anchor"], "#" + step["anchor"]),
        }
        for step in content.TOUR_STEPS
    ]


def feedback_context_for(account: Account) -> dict[str, str]:
    """The context block the drawer shows before anything is sent.

    Server side fills what it knows (route, screen, browser, build); the browser adds
    its own viewport size when the drawer opens, because the server cannot know it.
    """
    endpoint = (request.endpoint or "").split(".")[-1]
    surface = SURFACE_LABELS.get(endpoint, "Workspace")
    if endpoint == "settings_profile" or (request.path or "").startswith("/settings"):
        surface = "Settings"
    elif (request.path or "").startswith("/projects"):
        surface = "Projects"
    elif (request.path or "").startswith("/feedback"):
        surface = "Feedback"
    return {
        "surface": surface,
        "route": request.path,
        "project": request.args.get("project", ""),
        "browser": feedback_module_browser(request.headers.get("User-Agent", "")),
        "build": config().build_label,
    }


def feedback_module_browser(user_agent: str) -> str:
    from onboarding.feedback import _browser_from

    return _browser_from(user_agent)


def home_context_for_store(store: Any) -> dict[str, Any]:
    """Extra context for ``GET /`` when the onboarding layer is active."""
    account = current_account()
    if account is None:
        return {}
    cfg = config()
    projects = cfg.projects.for_account(account.id)
    conversations = store.conversations_for(store.user.id) if store is not None else []
    state = projects_module.dashboard_state(
        account,
        projects=projects,
        conversations=conversations,
        store=store,
        github_connected=bool(account.integrations.get("github_connected")),
    )
    return {
        "dashboard": state,
        "project_count": len(projects),
        "tour_requested": request.args.get("tour") == "1",
        "tour_offered": (
            request.endpoint == "home"
            and (
                request.args.get("tour") == "1"
                or not (account.onboarding.get("tour_seen") or account.onboarding.get("tour_opt_out"))
            )
        ),
        "page_styles": True,
    }


# --------------------------------------------------------------------------- #
# Marketing
# --------------------------------------------------------------------------- #

@bp.get("/landing")
def landing():
    account = current_account()
    plan_tier = "beta"
    # /deep/link -> /landing?next=/deep/link -> signup or login -> /deep/link
    next_url = _safe_next(request.args.get("next"), "")
    return render_template(
        "landing.html",
        page="landing",
        next_url=next_url if next_url not in ("", "/") else "",
        signed_in=account is not None,
        oauth_ready={
            "google": config().oauth.available("google"),
            "github": config().oauth.available("github"),
        },
        oauth_status={
            "google": config().oauth.status_line("google"),
            "github": config().oauth.status_line("github"),
        },
        simulated_oauth=config().oauth.any_simulated(),
        plan_tier=plan_tier,
        account_count=accounts().count(),
    )


@bp.get("/demo")
def demo_enter():
    """One click into a workspace that already has sample work in it.

    It only ever touches the shared demo account. A real account cannot be
    reached this way, which is what makes a GET acceptable here.
    """
    if not config().demo_enabled:
        flash("The demo workspace is switched off on this deployment.", "warning")
        return redirect(url_for("onboarding.landing"))
    demo_email = "demo@orbit.local"
    account = accounts().find_by_email(demo_email)
    if account is None:
        account = accounts().create_account(
            name="Caleb",
            email=demo_email,
            provider="demo",
            verified=True,
            is_demo=True,
        )
        account.integrations.update({"github_connected": True, "github_login": "caleb", "github_scope": "read:user user:email"})
        account.preferences.update({"languages": ["python", "javascript"], "interests": ["web-apps", "apis"], "experience": "confident"})
        accounts().save(account)
    if config().seed_demo is not None:
        config().seed_demo(account)
    sign_in(account, remember=False, extra={"demo": True})
    _audit("demo_entered", account=account)
    return redirect("/")


@bp.post("/demo/exit")
def demo_exit():
    sign_out()
    flash("You left the demo workspace. Nothing there was saved to an account.", "info")
    return redirect(url_for("onboarding.landing"))


# --------------------------------------------------------------------------- #
# Sign-up, sign-in, verification, recovery
# --------------------------------------------------------------------------- #

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_account() is not None:
        return redirect("/")
    values = {"name": "", "email": ""}
    errors: dict[str, str] = {}
    next_url = _safe_next(request.args.get("next") or request.form.get("next"), "")

    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Please try again.", "error")
            return redirect(url_for("onboarding.signup"))
        values = {
            "name": (request.form.get("name") or "").strip(),
            "email": normalize_email(request.form.get("email") or ""),
            "workspace": (request.form.get("workspace") or "").strip(),
        }
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        errors = _form_problems(
            name=values["name"], email=values["email"], password=password, confirm=confirm
        )
        allowed, retry_after = accounts().throttle(
            f"signup:{_client_key()}", limit=8, window_seconds=900
        )
        if not allowed:
            errors["_form"] = f"Too many attempts from this network. Try again in {retry_after} seconds."
        if request.form.get("terms") != "on":
            errors["terms"] = "Please accept the beta terms to continue."

        if not errors:
            try:
                existing = accounts().find_by_email(values["email"])
                if existing is not None:
                    errors["email"] = (
                        "An account already uses that address. Sign in instead — or "
                        "reset the password if it is not yours any more."
                    )
                else:
                    account = accounts().create_account(
                        name=values["name"], email=values["email"], password=password
                    )
                    if values.get("workspace"):
                        account.workspace = values["workspace"]
                        accounts().save(account)
                    token = accounts().start_verification(account)
                    link = url_for("onboarding.verify_token", token=token, _external=True)
                    config().mailer.send(
                        mailer_module.verification_message(
                            to=account.email, name=account.first_name, link=link
                        )
                    )
                    _audit("account_created", account=account, verification="sent")
                    session.clear()
                    session["pending_account_id"] = account.id
                    session["csrf"] = secrets.token_urlsafe(24)
                    if next_url not in ("", "/"):
                        # Survives the confirmation step, so the link they
                        # followed before signing up is still honoured after it.
                        session["next_after_verify"] = next_url
                    flash(
                        f"Account created. We sent a confirmation link to {account.email}.",
                        "success",
                    )
                    return redirect(url_for("onboarding.verify"))
            except AccountStoreError as error:
                errors["email"] = str(error)

        if errors:
            _audit("signup_failed", reason=sorted(errors))

    return render_template(
        "signup.html",
        page="signup",
        values=values,
        errors=errors,
        next_url=next_url,
        min_password_length=content.__dict__.get("MIN_PASSWORD_LENGTH", 10),
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_account() is not None:
        return redirect("/")
    values = {"email": normalize_email(request.args.get("email") or "")}
    errors: dict[str, str] = {}
    next_url = _safe_next(request.args.get("next") or request.form.get("next"), "/")

    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Please try again.", "error")
            return redirect(url_for("onboarding.login"))
        values = {"email": normalize_email(request.form.get("email") or "")}
        password = request.form.get("password") or ""
        remember = request.form.get("remember") == "on"
        next_url = _safe_next(request.form.get("next"), "/")
        email_key = values["email"] or "blank"
        allowed, retry_after = accounts().throttle(
            f"login:{email_key}:{_client_key()}", limit=6, window_seconds=600
        )
        if not allowed:
            errors["_form"] = (
                f"Too many sign-in attempts for this address. Try again in {retry_after} seconds, "
                "or reset the password."
            )
        else:
            try:
                account = accounts().authenticate(values["email"], password)
            except AccountStoreError as error:
                errors["_form"] = str(error)
                _audit("signin_failed", reason="credentials")
            else:
                accounts().clear_throttle(f"login:{email_key}:{_client_key()}")
                if _requires_verification(account):
                    # A correct password for an unconfirmed address is a state we
                    # can fix, so say which one it is instead of "wrong password".
                    if not accounts().verification_pending(account):
                        accounts().start_verification(account)
                    session.clear()
                    session["pending_account_id"] = account.id
                    session["csrf"] = secrets.token_urlsafe(24)
                    _audit("signin_blocked_unverified", account=account)
                    flash("Your email address still needs confirming. We can send a new link.", "warning")
                    return redirect(url_for("onboarding.verify"))
                sign_in(account, remember=remember)
                _audit("sign_in", account=account, method="password")
                return redirect(next_url)

        if errors:
            _audit("signin_failed", reason="throttled" if "_form" in errors else "validation")

    return render_template(
        "login.html",
        page="login",
        values=values,
        errors=errors,
        next_url=next_url,
        just_reset=request.args.get("reset") == "1",
        just_signed_out=request.args.get("signedout") == "1",
    )


@bp.post("/logout")
def logout():
    if not _csrf_ok():
        flash("That sign-out request expired. Try again.", "error")
        return redirect("/")
    sign_out()
    return redirect(url_for("onboarding.login", signedout="1"))


# ----- verification -----

def _pending_account() -> Account | None:
    account_id = session.get("pending_account_id")
    if not account_id:
        return None
    account = accounts().get(account_id)
    if account is None or account.verified:
        return None
    return account


@bp.get("/verify")
def verify():
    account = _pending_account()
    state = request.args.get("state", "")
    if account is None:
        # Either already verified (success screen) or nothing pending at all.
        if request.args.get("done") == "1":
            return render_template("verify.html", page="verify", mode="done", email="")
        return render_template("verify.html", page="verify", mode="nothing", email="")
    mode = {
        "resent": "sent",
        "expired": "expired",
        "changed": "changed",
    }.get(state, "pending")
    return render_template(
        "verify.html",
        page="verify",
        mode=mode,
        email=account.email,
        name=account.first_name,
        outbox=_mailbox_panel(account.email),
        resend_wait=_resend_wait(account),
        provider=account.provider,
    )


def _resend_wait(account: Account) -> int:
    """Seconds until another confirmation email may be sent. A real cooldown, not a prop."""
    issued = (account.verification or {}).get("issued_at", "")
    if not issued:
        return 0
    try:
        moment = datetime.fromisoformat(issued)
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - moment).total_seconds()
    return max(0, int(60 - elapsed))


@bp.post("/verify/resend")
def verify_resend():
    if not _csrf_ok():
        flash("That request expired. Try again.", "error")
        return redirect(url_for("onboarding.verify"))
    account = _pending_account()
    if account is None:
        flash("There is nothing waiting to be confirmed.", "info")
        return redirect(url_for("onboarding.verify"))
    wait = _resend_wait(account)
    if wait > 0:
        flash(f"One request is already in flight. You can resend in {wait} seconds.", "info")
        return redirect(url_for("onboarding.verify"))
    allowed, retry_after = accounts().throttle(
        f"resend:{account.id}", limit=5, window_seconds=900
    )
    if not allowed:
        flash(f"That is a lot of requests. Try again in {retry_after} seconds.", "warning")
        return redirect(url_for("onboarding.verify"))
    token = accounts().start_verification(account)
    link = url_for("onboarding.verify_token", token=token, _external=True)
    config().mailer.send(
        mailer_module.verification_message(to=account.email, name=account.first_name, link=link)
    )
    _audit("verification_resent", account=account)
    flash(f"Sent again to {account.email}.", "success")
    return redirect(url_for("onboarding.verify", state="resent"))


@bp.post("/verify/change-email")
def verify_change_email():
    account = _pending_account()
    if account is None or not _csrf_ok():
        return redirect(url_for("onboarding.verify"))
    new_email = normalize_email(request.form.get("email") or "")
    problem = valid_email_shape(new_email)
    if problem:
        flash(problem, "error")
        return redirect(url_for("onboarding.verify"))
    try:
        accounts().change_email(account, new_email)
    except AccountStoreError as error:
        flash(str(error), "error")
        return redirect(url_for("onboarding.verify"))
    token = accounts().start_verification(account)
    link = url_for("onboarding.verify_token", token=token, _external=True)
    config().mailer.send(
        mailer_module.verification_message(to=account.email, name=account.first_name, link=link)
    )
    _audit("verification_email_changed", account=account)
    flash(f"Address updated. A new link is on its way to {account.email}.", "success")
    return redirect(url_for("onboarding.verify", state="changed"))


@bp.get("/verify/<token>")
def verify_token(token: str):
    account, reason = accounts().consume_verification(token)
    if account is None:
        if reason == "expired":
            pending = None
            flash("That confirmation link expired. Send yourself a new one.", "warning")
        else:
            pending = None
            flash("That confirmation link is not valid any more. Sign in to get a fresh one.", "error")
        # Keep the pending account handy so "resend" works after an expiry.
        return render_template(
            "verify.html",
            page="verify",
            mode="expired" if reason == "expired" else "invalid",
            email="",
            outbox=[],
            resend_wait=0,
            provider="",
        )
    session.pop("pending_account_id", None)
    session["account_id"] = account.id
    session["csrf"] = secrets.token_urlsafe(24)
    session["session_id"] = ""
    record = accounts().create_session(
        account, user_agent=request.headers.get("User-Agent", ""), remember=True
    )
    session["session_id"] = record.id
    _audit("email_verified", account=account)
    flash("Email confirmed. Welcome in.", "success")
    target = _safe_next(session.pop("next_after_verify", ""), "")
    if target:
        return redirect(target)
    return redirect(url_for("onboarding.welcome", fresh="1"))


# ----- password recovery -----

@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Ask for a reset link, answering the same way whether the address exists.

    A POST never renders; it redirects and the next GET renders, so a refresh
    re-reads the page instead of sending a second email.
    """
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.forgot_password"))
        email = normalize_email(request.form.get("email") or "")
        problem = valid_email_shape(email)
        if problem:
            # A malformed address is worth saying out loud; a *known* address is not.
            flash(problem, "error")
            return redirect(url_for("onboarding.forgot_password"))
        allowed, retry_after = accounts().throttle(
            f"reset:{email}:{_client_key()}", limit=4, window_seconds=900
        )
        if not allowed:
            flash(f"Too many reset requests. Try again in {retry_after} seconds.", "warning")
        else:
            account, token = accounts().start_password_reset(email)
            if account is not None:
                link = url_for("onboarding.reset_password", token=token, _external=True)
                config().mailer.send(
                    mailer_module.reset_message(to=account.email, name=account.first_name, link=link)
                )
                _audit("password_reset_requested", account=account)
            else:
                _audit("password_reset_requested", found=False)
        session["reset_email"] = email
        return redirect(url_for("onboarding.forgot_password", sent="1"))

    submitted = request.args.get("sent") == "1"
    wanted = normalize_email(session.pop("reset_email", "")) if submitted else ""
    return render_template(
        "forgot_password.html",
        page="forgot",
        submitted=submitted,
        outbox=[message for message in _mailbox_panel(wanted) if message["kind"] == "reset"]
        if wanted
        else [],
    )


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    errors: dict[str, str] = {}
    # Check the link up front so an expired one shows its own screen rather than a
    # password form that will fail on submit.
    known = accounts().reset_token_known(token_digest(token))

    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.reset_password", token=token))
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if password != confirm:
            errors["confirm"] = "Those two passwords don't match."
        account, reason, message = accounts().consume_reset(token, password)
        if account is None and not errors:
            errors["_form"] = message
            if reason in ("expired", "unknown"):
                return render_template(
                    "reset_password.html", page="reset", token=token, errors=errors, known=False
                )
        if account is not None:
            _audit("password_reset", account=account)
            flash("Password updated. Sign in with the new one.", "success")
            return redirect(url_for("onboarding.login", reset="1", email=account.email))

    return render_template(
        "reset_password.html",
        page="reset",
        token=token,
        errors=errors,
        known=known,
    )


# ----- OAuth -----

@bp.post("/oauth/<slug>/start")
def oauth_start(slug: str):
    provider = config().oauth.get(slug)
    if provider is None:
        flash("That sign-in method is not available.", "error")
        return redirect(url_for("onboarding.login"))
    if not _csrf_ok():
        flash("That request expired. Try again.", "error")
        return redirect(url_for("onboarding.login"))
    state = oauth_module.new_state()
    verifier = oauth_module.new_verifier()
    session["oauth_state"] = state
    session["oauth_verifier"] = verifier
    session["oauth_next"] = _safe_next(request.form.get("next"), "/")
    _audit("oauth_started", provider=slug, simulated=provider.simulate)
    try:
        url = provider.authorize_url(state=state, code_challenge=oauth_module.challenge_for(verifier))
    except oauth_module.OAuthError as error:
        flash(str(error), "error")
        return redirect(url_for("onboarding.login"))
    return redirect(url)


@bp.get("/oauth/<slug>/callback")
def oauth_callback(slug: str):
    provider = config().oauth.get(slug)
    if provider is None:
        flash("That sign-in method is not available.", "error")
        return redirect(url_for("onboarding.login"))
    if request.args.get("error"):
        detail = request.args.get("error_description") or request.args.get("error")
        flash(f"{provider.label} declined the request: {detail}", "error")
        return redirect(url_for("onboarding.login"))
    state = request.args.get("state", "")
    expected = session.pop("oauth_state", "")
    verifier = session.pop("oauth_verifier", "")
    next_url = session.pop("oauth_next", "/")
    if not state or not expected or not hmac.compare_digest(state, expected):
        _audit("oauth_failed", provider=slug, reason="state")
        flash(
            "That sign-in attempt could not be verified, so it was stopped. "
            "Start again from this page.",
            "error",
        )
        return redirect(url_for("onboarding.login"))
    code = request.args.get("code", "")
    if not code:
        flash("The provider sent you back without an authorization code. Start again.", "error")
        return redirect(url_for("onboarding.login"))
    try:
        identity = provider.exchange(code=code, code_verifier=verifier, state=state)
    except oauth_module.OAuthError as error:
        _audit("oauth_failed", provider=slug, reason="exchange")
        flash(str(error), "error")
        return redirect(url_for("onboarding.login"))
    except Exception as error:  # transport errors: say the layer that failed
        _audit("oauth_failed", provider=slug, reason=type(error).__name__)
        flash(
            f"Could not reach {provider.label} to finish signing you in "
            f"({type(error).__name__}). Check the connection and try again.",
            "error",
        )
        return redirect(url_for("onboarding.login"))
    return _complete_oauth(identity, provider=provider, next_url=next_url)


@bp.post("/oauth/<slug>/simulate/decision")
def oauth_simulate_decide(slug: str):
    """The simulated consent screen's decision handler (demo builds only)."""
    if not config().oauth.demo_enabled:
        flash("Simulated sign-in is switched off on this deployment.", "error")
        return redirect(url_for("onboarding.login"))
    if not _csrf_ok():
        flash("That request expired. Try again.", "error")
        return redirect(url_for("onboarding.login"))
    email = normalize_email(request.form.get("email") or "")
    name = (request.form.get("name") or "").strip()
    decision = "allow" if request.form.get("decision") == "allow" else "deny"
    state = request.form.get("state", "")
    expected = session.get("oauth_state", "")
    if not state or not expected or not hmac.compare_digest(state, expected):
        flash("That consent screen expired. Start the sign-in again.", "error")
        return redirect(url_for("onboarding.login"))
    if decision == "deny":
        _audit("oauth_declined", provider=slug)
        flash("Nothing was shared and no account was created.", "info")
        return redirect(url_for("onboarding.login"))
    if valid_email_shape(email):
        flash(valid_email_shape(email), "error")
        return redirect(url_for("onboarding.oauth_simulate", state=state))
    code = oauth_module.encode_simulated_decision(decision="allow", email=email, name=name)
    return redirect(url_for("onboarding.oauth_callback", slug=slug, code=code, state=state))


@bp.get("/oauth/<slug>/simulate")
def oauth_simulate(slug: str):
    provider = config().oauth.get(slug)
    if provider is None or not provider.simulate:
        return redirect(url_for("onboarding.login"))
    state = request.args.get("state", "")
    return render_template(
        "oauth_consent.html",
        page="consent",
        provider=provider,
        slug=slug,
        state=state,
        scopes=oauth_module.SCOPE_EXPLANATIONS.get(slug, ()),
        scope_string=oauth_module.IDENTITY_SCOPES.get(slug, ""),
        repository_explanation=oauth_module.REPOSITORY_EXPLANATION,
        not_granted=oauth_module.NOT_GRANTED_BY_SIGNIN,
    )


def _complete_oauth(identity: Any, *, provider: Any, next_url: str = "/"):
    account = accounts().by_provider_subject(identity.provider, identity.subject)
    created = False
    linked = False
    if account is None:
        account = accounts().find_by_email(identity.email)
        if account is not None:
            linked = True
    if account is None:
        try:
            account = accounts().create_account(
                name=identity.name,
                email=identity.email,
                provider=identity.provider,
                provider_subject=identity.subject,
                verified=bool(identity.email_verified or True),
            )
            created = True
        except AccountStoreError as error:
            flash(str(error), "error")
            return redirect(url_for("onboarding.login"))
    # A provider account records the connection for identity, and nothing more:
    # repository access stays False until it is granted on purpose.
    integrations = {"github_connected": identity.provider == "github"}
    if identity.provider == "github":
        integrations = {
            "github_connected": True,
            "github_login": identity.username,
            "github_scope": oauth_module.IDENTITY_SCOPES["github"],
            "github_connected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # Explicitly not granted by signing in. See onboarding.oauth.
            "github_repo_access": account.integrations.get("github_repo_access", False),
        }
    if created:
        account.provider = identity.provider
        account.provider_subject = identity.subject
    # An account that already existed keeps the provider it was created with: an
    # email account still has a working password, and Settings has to go on saying
    # so. The extra identity is recorded below, and the address match above is what
    # signs the same button in next time.
    integrations["identity_providers"] = sorted(
        {*account.integrations.get("identity_providers", []), identity.provider}
    )
    if identity.email_verified:
        account.verified = True
    accounts().update_integrations(account, integrations)
    accounts().save(account)
    _audit(
        "oauth_completed",
        account=account,
        provider=identity.provider,
        created=created,
        linked=linked,
        simulated=bool(identity.raw.get("simulated")),
    )
    if created:
        session.clear()
        sign_in(account, remember=True)
        flash(
            f"Signed up with {provider.label}. No confirmation email is needed for this one.",
            "success",
        )
        if next_url not in ("", "/"):
            return redirect(next_url)
        return redirect(url_for("onboarding.welcome", fresh="1"))
    sign_in(account, remember=True)
    if linked:
        flash(f"Signed in with {provider.label}. Matched to your existing account by email.", "info")
    return redirect(next_url)


# --------------------------------------------------------------------------- #
# Welcome, personalization
# --------------------------------------------------------------------------- #

def _require_account() -> Account | None:
    return current_account()


@bp.get("/onboarding/welcome")
def welcome():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    if not account.verified:
        return redirect(url_for("onboarding.verify"))
    accounts().update_onboarding(account, {"welcome_seen": True})
    related = accounts().find_by_email("demo@orbit.local")
    return render_template(
        "welcome.html",
        page="welcome",
        fresh=request.args.get("fresh") == "1",
        options=content.WELCOME_OPTIONS,
        provider_label=account.provider_label,
        provider=account.provider,
        repo_access=account.github_repository_access(),
        tour_requested=not account.onboarding.get("tour_opt_out", False),
        demo_exists=related is not None and related.id != account.id,
    )


@bp.post("/onboarding/welcome/skip")
def welcome_skip():
    return redirect("/?tour=off")


@bp.route("/onboarding/personalize", methods=["GET", "POST"])
def personalize():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    saved = False
    errors: dict[str, str] = {}
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.personalize"))
        if request.form.get("skip"):
            accounts().update_onboarding(account, {"personalize_skipped": True})
            flash("Skipped. You can answer these any time in Settings → Development preferences.", "info")
            return redirect("/")
        payload = {
            "background": request.form.get("background", ""),
            "experience": request.form.get("experience", ""),
            "interests": request.form.getlist("interests"),
            "languages": request.form.getlist("languages"),
            "custom_languages": [item.strip() for item in request.form.getlist("custom_languages") if item.strip()],
            "role": request.form.get("role", ""),
            "goal": request.form.get("goal", ""),
        }
        try:
            accounts().update_preferences(account, payload)
        except AccountStoreError as error:
            errors["_form"] = str(error)
        else:
            accounts().update_onboarding(account, {"personalize_done": True, "personalize_skipped": False})
            _audit("personalization_saved", account=account)
            flash("Saved. Your suggestions will use this from now on.", "success")
            return redirect("/")
    preferences = account.preferences
    return render_template(
        "personalize.html",
        page="personalize",
        preferences=preferences,
        errors=errors,
        saved=saved,
        language_groups=_grouped(content.LANGUAGES),
    )


def _grouped(items: tuple[dict[str, str], ...]) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for item in items:
        groups.setdefault(str(item.get("group", "Other")), []).append(dict(item))
    return groups


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #

def _require_project(project_id: str) -> Any:
    account = _require_account()
    if account is None:
        return None
    return project_store().owned_by(project_id, account.id)


@bp.get("/projects")
def projects_index():
    account = _require_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    return render_template(
        "projects_index.html",
        page="projects",
        projects=project_store().for_account(account.id),
        entry_points=content.PROJECT_ENTRY_POINTS,
    )


@bp.get("/projects/new")
def projects_new():
    account = _require_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    group = request.args.get("group", "all")
    points = [point for point in content.PROJECT_ENTRY_POINTS if group in ("all", point.group)]
    return render_template(
        "projects_new.html",
        page="projects",
        group=group,
        entry_points=points,
        github_connected=account.integrations.get("github_connected", False),
    )


@bp.route("/projects/blank", methods=["GET", "POST"])
def project_blank():
    account = _require_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    errors: dict[str, str] = {}
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.project_blank"))
        name = (request.form.get("name") or "").strip()
        if not name:
            errors["name"] = "Give it a name — you can change it later."
        else:
            project = project_store().create(
                account_id=account.id,
                name=name,
                kind="orbit",
                description=(request.form.get("description") or "").strip(),
                primary_language=(request.form.get("primary_language") or "").strip(),
            )
            _audit("project_created", account=account, project=project.id, kind="orbit")
            flash(f"“{project.name}” is ready. Nothing was created on your machine.", "success")
            return redirect(url_for("onboarding.project_detail", project_id=project.id))
    return render_template("project_blank.html", page="projects", errors=errors)


@bp.route("/projects/guided", methods=["GET", "POST"])
def project_guided():
    account = _require_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    errors: dict[str, str] = {}
    step = request.args.get("step", "1")
    draft = {
        "name": (request.form.get("name") or "").strip(),
        "template": (request.form.get("template") or "").strip(),
        "primary_language": (request.form.get("primary_language") or "").strip(),
        "options": request.form.getlist("options"),
        "description": (request.form.get("description") or "").strip(),
    }
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.project_guided"))
        if request.form.get("stage") == "review":
            if not draft["name"]:
                errors["name"] = "Give the project a name to finish."
            if not draft["template"]:
                errors["template"] = "Pick the shape closest to what you are building."
            if not errors:
                plan = project_store().guided_plan(
                    template=draft["template"],
                    language=draft["primary_language"],
                    options=draft["options"],
                    name=draft["name"],
                )
                project = project_store().create(
                    account_id=account.id,
                    name=draft["name"],
                    kind="orbit",
                    description=draft["description"] or f"Guided setup · {draft['template']}",
                    primary_language=draft["primary_language"],
                    template=draft["template"],
                    options=draft["options"],
                    files=[{"path": item["path"], "size": 0, "kind": "planned"} for item in plan["files"]],
                    plan=plan,
                )
                _audit("project_created", account=account, project=project.id, kind="guided")
                flash("Plan saved. These files are planned, not written — ask for them in Chat mode.", "success")
                return redirect(url_for("onboarding.project_detail", project_id=project.id))
            step = "1"
    return render_template(
        "project_guided.html",
        page="projects",
        errors=errors,
        step=step,
        draft=draft,
        templates=content.PROJECT_TEMPLATES,
        guided_options=content.GUIDED_OPTIONS,
        language_groups=_grouped(content.LANGUAGES),
        plan=project_store().guided_plan(
            template=draft["template"] or "web",
            language=draft["primary_language"],
            options=draft["options"],
            name=draft["name"] or "your project",
        )
        if request.method == "POST"
        else None,
    )


@bp.route("/projects/idea", methods=["GET", "POST"])
def project_idea():
    account = _require_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    idea = (request.form.get("idea") or "").strip()
    errors: dict[str, str] = {}
    plan: dict[str, Any] | None = None
    source_note = ""

    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.project_idea"))
        if len(idea) < 12:
            errors["idea"] = "A sentence or two, so ORBIT has something to plan from."
        elif request.form.get("stage") == "create":
            # Turning an approved plan into a project does not need a second
            # model call: the plan is already on screen and in the form.
            project = project_store().create(
                account_id=account.id,
                name=(request.form.get("project_name") or "").strip() or _title_from_idea(idea),
                kind="idea",
                idea=idea,
                description="From a described idea.",
                plan=_plan_from_form(request.form),
            )
            _audit("project_created", account=account, project=project.id, kind="idea")
            flash("Project created from your idea. The plan is a checklist, not a promise.", "success")
            return redirect(url_for("onboarding.project_detail", project_id=project.id))
        else:
            plan, source_note = _plan_for_idea(idea, account)

    return render_template(
        "project_idea.html",
        page="projects",
        idea=idea,
        plan=plan,
        source_note=source_note,
        errors=errors,
        suggested_name=_title_from_idea(idea) if idea else "",
        can_model=_can_model(),
    )


def _can_model() -> bool:
    """Whether a real provider is wired in this process."""
    provider = current_app.config.get("PROVIDER")
    if provider is not None:
        return True
    try:
        from config import load_settings  # type: ignore

        return bool(getattr(load_settings(), "gemini_api_key", ""))
    except Exception:
        return False


def _plan_for_idea(idea: str, account: Account) -> tuple[dict[str, Any], str]:
    """Ask the configured model; fall back to the local planner and say so.

    The label on the resulting plan always names which one produced it. A plan
    that looks like model output but came from a template is a quiet lie, and
    this project has had enough of those.
    """
    provider = current_app.config.get("PROVIDER")
    languages = list(account.preferences.get("languages") or [])
    experience = account.preferences.get("experience", "")
    if provider is not None:
        system_prompt = (
            "You are ORBIT's planning assistant. Turn the person's idea into a short, "
            "practical plan. Reply in plain text with exactly these sections and no "
            "preamble: HEADLINE: one line. SUMMARY: two sentences. GOALS: three bullet "
            "lines starting with '- '. MILESTONES: up to four lines of 'Name — detail'. "
            "FIRST TASKS: four bullet lines. RISKS: two bullet lines. Be concrete and "
            "modest: never claim work has been done, and never invent file contents."
        )
        history = [{"role": "user", "content": f"My idea: {idea}\nExperience: {experience or 'unknown'}\nLanguages: {', '.join(languages) or 'unknown'}"}]
        try:
            answer = provider.reply(history, system_prompt)
        except Exception as error:
            plan = project_store().local_idea_plan(idea, experience=experience, languages=languages)
            plan["error"] = f"{type(error).__name__}"
            return plan, (
                "The model could not be reached just now "
                f"({type(error).__name__}), so this outline came from ORBIT's local planner instead."
            )
        stripped = (answer or "").strip()
        if stripped.upper().startswith("STUB:") or not stripped:
            plan = project_store().local_idea_plan(idea, experience=experience, languages=languages)
            return plan, (
                "No real model is wired into this run (the reply was the stub provider), "
                "so this outline came from ORBIT's local planner."
            )
        parsed = _parse_plan(stripped)
        parsed.setdefault("generated_by", "model")
        parsed["note"] = "Review before you rely on it. ORBIT has not created anything yet."
        return parsed, "Planned with your configured model."

    plan = project_store().local_idea_plan(idea, experience=experience, languages=languages)
    return plan, (
        "No model is configured in this run, so this outline came from ORBIT's local "
        "planner — a checklist to react to, not model output."
    )


def _parse_plan(text: str) -> dict[str, Any]:
    """Read the sectioned plain-text reply into the shape the template renders."""

    def section(*names: str) -> str:
        lowered = text.lower()
        for name in names:
            marker = f"{name.lower()}:"
            index = lowered.find(marker)
            if index == -1:
                continue
            rest = text[index + len(marker) :]
            end = len(rest)
            for other in ("headline:", "summary:", "goals:", "milestones:", "first tasks:", "risks:"):
                position = rest.lower().find(other)
                if position != -1:
                    end = min(end, position)
            return rest[:end].strip()
        return ""

    def bullets(block: str) -> list[str]:
        lines = []
        for line in block.splitlines():
            clean = line.strip().lstrip("-*•").strip()
            if clean:
                lines.append(clean[:220])
        return lines

    milestones = []
    for line in bullets(section("milestones")):
        if "—" in line:
            name, _, detail = line.partition("—")
            milestones.append({"name": name.strip(), "detail": detail.strip()})
        elif ":" in line:
            name, _, detail = line.partition(":")
            milestones.append({"name": name.strip(), "detail": detail.strip()})
        else:
            milestones.append({"name": line, "detail": ""})

    plan = {
        "generated_by": "model",
        "headline": section("headline") or "A first pass at your idea",
        "summary": section("summary") or text[:400],
        "goals": bullets(section("goals")),
        "milestones": milestones,
        "first_tasks": bullets(section("first tasks")),
        "risks": bullets(section("risks")),
    }
    if not plan["goals"] and not plan["milestones"]:
        # The sectioned format did not survive; keep the answer rather than lose it.
        plan["summary"] = text[:1200]
    return plan


def _plan_from_form(form: Any) -> dict[str, Any]:
    """Rebuild the plan from the hidden fields so nothing is asked twice.

    The alternative — a second model call to re-generate a plan the person just
    approved — would cost money and could return something different from what
    they read.
    """
    def lines(key: str) -> list[str]:
        return [item.strip() for item in form.getlist(key) if item.strip()]

    milestones = []
    raw_milestones = lines("plan_milestone")
    for index, name in enumerate(raw_milestones):
        details = form.getlist("plan_milestone_detail")
        milestones.append({"name": name, "detail": details[index] if index < len(details) else ""})
    return {
        "generated_by": form.get("plan_source") or "local planner",
        "headline": (form.get("plan_headline") or "").strip(),
        "summary": (form.get("plan_summary") or "").strip(),
        "goals": lines("plan_goal"),
        "milestones": milestones,
        "first_tasks": lines("plan_task"),
        "risks": lines("plan_risk"),
        "status": "approved",
        "note": "Approved by you when the project was created.",
    }


def _title_from_idea(idea: str) -> str:
    text = " ".join((idea or "").split())
    if not text:
        return "New idea"
    words = text.split(" ")[:7]
    title = " ".join(words).rstrip(".,;:")
    if len(text.split(" ")) > 7:
        title += "…"
    return title[:70]


@bp.route("/projects/import/<slug>", methods=["GET", "POST"])
def project_import(slug: str):
    account = _require_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    point = content.ENTRY_POINTS_BY_SLUG.get(slug)
    if point is None or point.group != "import":
        flash("That import route does not exist.", "error")
        return redirect(url_for("onboarding.projects_new"))
    if not point.available:
        return render_template(
            "project_unavailable.html",
            page="projects",
            point=point,
            alternatives=[item for item in content.PROJECT_ENTRY_POINTS if item.group == "import" and item.available],
        )

    errors: dict[str, str] = {}
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.project_import", slug=slug))
        if slug == "github":
            project, errors = _import_github(account)
        elif slug == "zip":
            project, errors = _import_zip(account)
        elif slug == "folder":
            project, errors = _import_folder(account)
        elif slug == "files":
            project, errors = _import_files(account)
        else:
            project, errors = None, {"_form": "That import route is not implemented."}
        if project is not None:
            _audit("project_imported", account=account, project=project.id, kind=project.kind)
            flash(f"Imported “{project.name}”. {project.file_summary} — contents stayed where they are.", "success")
            return redirect(url_for("onboarding.project_detail", project_id=project.id))

    return render_template(
        "project_import.html",
        page="projects",
        slug=slug,
        point=point,
        errors=errors,
        github=account.integrations,
        repo_access=account.github_repository_access(),
        github_ready=bool(account.integrations.get("github_connected")),
        github_oauth=_github_oauth_available(),
    )


def _import_github(account: Account) -> tuple[Any, dict[str, str]]:
    repo = (request.form.get("repo") or "").strip()
    owner = (request.form.get("owner") or "").strip()
    branch = (request.form.get("branch") or "main").strip()
    if not repo:
        return None, {"repo": "Name the repository, for example orbit-demo."}
    # A listing cannot be fetched without a real grant, and inventing one would be
    # the exact lie this codebase refuses. The project records the reference and
    # says plainly that no listing was read.
    project = project_store().import_github(
        account_id=account.id,
        repo=repo,
        owner=owner,
        default_branch=branch,
        tree=[],
        access_mode="token" if request.form.get("token") else "reference",
    )
    project.notes = (
        "Reference only: no file listing was read, because repository access has "
        "not been granted for this workspace."
        if not account.github_repository_access()
        else "Repository access is granted; a listing will be read the next time ORBIT is asked to."
    )
    project_store().update(project)
    return project, {}


def _import_zip(account: Account) -> tuple[Any, dict[str, str]]:
    upload = request.files.get("archive")
    if upload is None or not upload.filename:
        return None, {"archive": "Choose the .zip file you downloaded."}
    if not upload.filename.lower().endswith(".zip"):
        return None, {"archive": "That is not a .zip file. Unzip .tar.gz downloads first."}
    payload = upload.read(projects_module.MAX_ARCHIVE_BYTES + 1)
    files, problem = projects_module.list_zip_entries(payload)
    if problem:
        return None, {"archive": problem}
    del payload  # the archive itself is not kept: only the listing is
    return project_store().import_archive(
        account_id=account.id, archive_name=upload.filename, files=files
    ), {}


def _import_folder(account: Account) -> tuple[Any, dict[str, str]]:
    paths = request.form.getlist("paths")
    if not paths:
        return None, {"paths": "Choose a folder — your browser shares the file names, not the files."}
    files = projects_module.list_folder_paths(paths)
    if not files:
        return None, {"paths": "That folder has no files ORBIT can list."}
    folder = projects_module.folder_name_from_paths(paths) or "Local folder"
    return project_store().import_folder(account_id=account.id, folder=folder, files=files), {}


def _import_files(account: Account) -> tuple[Any, dict[str, str]]:
    uploads = request.files.getlist("files")
    files = projects_module.list_uploaded_files(uploads)
    if not files:
        return None, {"files": "Choose at least one file to list."}
    name = (request.form.get("name") or "").strip() or "Selected files"
    return project_store().import_files(account_id=account.id, name=name, files=files), {}


@bp.get("/projects/<project_id>")
def project_detail(project_id: str):
    account = _require_account()
    project = _require_project(project_id)
    if account is None:
        return redirect(url_for("onboarding.login"))
    if project is None:
        return render_template("project_not_found.html", page="projects", project_id=project_id), 404
    project_store().touch(project)
    store = _store_for(account)
    return render_template(
        "project_detail.html",
        page="projects",
        project=project,
        tasks=getattr(store, "tasks", {}) if store is not None else {},
        form={},
        errors={},
    )


@bp.post("/projects/<project_id>/update")
def project_update(project_id: str):
    account = _require_account()
    project = _require_project(project_id)
    if account is None or project is None:
        return redirect(url_for("onboarding.projects_index"))
    if not _csrf_ok():
        flash("That form expired. Try again.", "error")
        return redirect(url_for("onboarding.project_detail", project_id=project_id))
    changes: dict[str, Any] = {}
    if request.form.get("name"):
        changes["name"] = projects_module.clean_project_name(request.form.get("name"))
    if "description" in request.form:
        changes["description"] = (request.form.get("description") or "").strip()[:280]
    if request.form.get("primary_language"):
        changes["primary_language"] = request.form.get("primary_language", "").strip()[:40]
    if changes:
        project_store().update(project, **changes)
        flash("Project updated.", "success")
    return redirect(url_for("onboarding.project_detail", project_id=project_id))


@bp.post("/projects/<project_id>/archive")
def project_archive(project_id: str):
    account = _require_account()
    project = _require_project(project_id)
    if account is None or project is None:
        return redirect(url_for("onboarding.projects_index"))
    if not _csrf_ok():
        flash("That form expired. Try again.", "error")
        return redirect(url_for("onboarding.project_detail", project_id=project_id))
    project_store().update(project, archived=not project.archived)
    flash("Project archived." if project.archived else "Project restored.", "info")
    return redirect(url_for("onboarding.projects_index"))


@bp.post("/projects/<project_id>/delete")
def project_delete(project_id: str):
    account = _require_account()
    project = _require_project(project_id)
    if account is None or project is None:
        return redirect(url_for("onboarding.projects_index"))
    if not _csrf_ok():
        flash("That form expired. Try again.", "error")
        return redirect(url_for("onboarding.project_detail", project_id=project_id))
    if request.form.get("confirm") != project.name:
        flash(f"To delete this project, type its name exactly: {project.name}", "warning")
        return redirect(url_for("onboarding.project_detail", project_id=project_id))
    project_store().delete(project)
    _audit("project_deleted", account=account, project=project.id)
    flash("Project removed from your workspace.", "info")
    return redirect(url_for("onboarding.projects_index"))


@bp.post("/projects/<project_id>/task")
def project_task(project_id: str):
    """Send a project into the existing Agent mode surface."""
    account = _require_account()
    project = _require_project(project_id)
    if account is None or project is None:
        return redirect(url_for("onboarding.projects_index"))
    if not _csrf_ok():
        flash("That form expired. Try again.", "error")
        return redirect(url_for("onboarding.project_detail", project_id=project_id))
    ask = (request.form.get("ask") or "").strip()
    if not ask:
        flash("Describe what you want done, and the Agent panel will plan from it.", "warning")
        return redirect(url_for("onboarding.project_detail", project_id=project_id))
    store = _store_for(account)
    if store is None:
        flash("No workspace store is attached to this app.", "error")
        return redirect(url_for("onboarding.project_detail", project_id=project_id))
    task = store.create_task(ask)
    task.breadcrumb = f"{project.kind_label} / {project.name}"
    if project.primary_language:
        task.description = f"{task.description}\n\nProject: {project.name} ({project.primary_language})."
    flash("Task created. Approve nothing you have not read.", "success")
    return redirect(f"/task/{task.id}")


@bp.post("/projects/<project_id>/chat")
def project_chat(project_id: str):
    """Open Chat mode with the project as context — no new surface, no invented feature."""
    account = _require_account()
    project = _require_project(project_id)
    if account is None or project is None:
        return redirect(url_for("onboarding.projects_index"))
    if not _csrf_ok():
        flash("That form expired. Try again.", "error")
        return redirect(url_for("onboarding.project_detail", project_id=project_id))
    prompt = (request.form.get("prompt") or f"Let's work on {project.name}.").strip()
    store = _store_for(account)
    if store is not None:
        session["pending_composer_text"] = prompt
    return redirect("/")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def _settings_context(section: str, **extra: Any) -> dict[str, Any]:
    account = current_account()
    meta = next((item for item in SETTINGS_SECTIONS if item[0] == section), None)
    return {
        "page": "settings",
        "section": section,
        "section_meta": {
            "slug": section,
            "label": meta[1] if meta else section.title(),
            "icon": meta[2] if meta else "•",
            "blurb": meta[3] if meta else "",
        },
        "preferences": account.preferences if account else {},
        "onboarding": account.onboarding if account else {},
        "integrations": account.integrations if account else {},
        "language_groups": _grouped(content.LANGUAGES),
        "sessions": account.sessions if account else [],
        "current_session_id": session.get("session_id", ""),
        "backgrounds": content.BACKGROUNDS,
        "experience_levels": content.EXPERIENCE_LEVELS,
        "interests": content.INTERESTS,
        "integration_cards": content.INTEGRATIONS,
        "tour_steps": content.TOUR_STEPS,
        "feedback_count": feedback_store().count_for_account(account.id) if account else 0,
        **extra,
    }


@bp.get("/settings")
def settings_root():
    return redirect(url_for("onboarding.settings_profile"))


@bp.route("/settings/profile", methods=["GET", "POST"])
def settings_profile():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    saved = False
    errors: dict[str, str] = {}
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_profile"))
        name = (request.form.get("name") or "").strip()
        if not name:
            errors["name"] = "A display name is required — it is what ORBIT calls you."
        else:
            accounts().update_profile(account, name=name, workspace=(request.form.get("workspace") or "").strip())
            try:
                accounts().update_preferences(
                    account,
                    {"role": request.form.get("role", ""), "goal": request.form.get("goal", "")},
                )
            except AccountStoreError as error:
                errors["_form"] = str(error)
            if not errors:
                saved = True
                flash("Profile saved.", "success")
    return render_template(
        "settings/profile.html", **_settings_context("profile", saved=saved, errors=errors)
    )


@bp.route("/settings/account", methods=["GET", "POST"])
def settings_account():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    errors: dict[str, str] = {}
    notice = ""
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_account"))
        action = request.form.get("action", "")
        if action == "email":
            new_email = normalize_email(request.form.get("email") or "")
            problem = valid_email_shape(new_email)
            if problem:
                errors["email"] = problem
            else:
                try:
                    accounts().change_email(account, new_email)
                except AccountStoreError as error:
                    errors["email"] = str(error)
                else:
                    if not account.verified:
                        token = accounts().start_verification(account)
                        link = url_for("onboarding.verify_token", token=token, _external=True)
                        config().mailer.send(
                            mailer_module.change_email_message(
                                to=account.email, name=account.first_name, link=link
                            )
                        )
                    _audit("email_changed", account=account)
                    notice = (
                        f"Email updated to {account.email}. Confirm it from the link we just sent."
                        if not account.verified
                        else f"Email updated to {account.email}."
                    )
                    flash(notice, "success")
                    if not account.verified:
                        session["pending_account_id"] = account.id
                        session.pop("account_id", None)
                        session.pop("session_id", None)
                        return redirect(url_for("onboarding.verify", state="changed"))
        elif action == "password":
            try:
                signed_out = accounts().change_password(
                    account,
                    request.form.get("current_password") or "",
                    request.form.get("new_password") or "",
                    keep_session_id=session.get("session_id", ""),
                )
            except AccountStoreError as error:
                errors["password"] = str(error)
            else:
                _audit("password_changed", account=account, others_signed_out=signed_out)
                flash(
                    "Password changed."
                    + (f" {signed_out} other session{'s were' if signed_out != 1 else ' was'} signed out." if signed_out else ""),
                    "success",
                )
                return redirect(url_for("onboarding.settings_account"))
    return render_template(
        "settings/account.html",
        **_settings_context("account", errors=errors, notice=notice, strength=password_strength),
    )


@bp.route("/settings/preferences", methods=["GET", "POST"])
def settings_preferences():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    errors: dict[str, str] = {}
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_preferences"))
        try:
            accounts().update_preferences(
                account,
                {
                    "background": request.form.get("background", ""),
                    "experience": request.form.get("experience", ""),
                    "interests": request.form.getlist("interests"),
                    "languages": request.form.getlist("languages"),
                    "custom_languages": [i.strip() for i in request.form.getlist("custom_languages") if i.strip()],
                    "role": request.form.get("role", ""),
                    "goal": request.form.get("goal", ""),
                    "use_preferences_for_suggestions": request.form.get("use_preferences_for_suggestions") == "on",
                    "density": request.form.get("density", "comfortable"),
                },
            )
        except AccountStoreError as error:
            errors["_form"] = str(error)
        else:
            flash("Development preferences saved.", "success")
            return redirect(url_for("onboarding.settings_preferences"))
    return render_template(
        "settings/preferences.html",
        **_settings_context("preferences", errors=errors),
    )


@bp.route("/settings/ai", methods=["GET", "POST"])
def settings_ai():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    store = _store_for(account)
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_ai"))
        try:
            accounts().update_preferences(
                account,
                {
                    "default_mode": request.form.get("default_mode", "chat"),
                    "default_depth": request.form.get("default_depth", "quick"),
                    "voice_replies": request.form.get("voice_replies") == "on",
                },
            )
        except AccountStoreError as error:
            flash(str(error), "error")
        else:
            if store is not None:
                try:
                    store.set_mode(account.preferences["default_mode"])
                    store.set_thinking_level(account.preferences["default_depth"])
                except ValueError:
                    pass
            flash("AI preferences saved and applied to your next reply.", "success")
            return redirect(url_for("onboarding.settings_ai"))
    return render_template(
        "settings/ai.html",
        **_settings_context(
            "ai",
            store_mode=getattr(store, "mode", "chat"),
            store_depth=getattr(store, "thinking_level", "quick"),
            provider_ready=_can_model(),
        ),
    )


@bp.route("/settings/integrations", methods=["GET", "POST"])
def settings_integrations():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_integrations"))
        action = request.form.get("action", "")
        if action == "grant-repo":
            accounts().update_integrations(
                account,
                {
                    "github_repo_access": True,
                    "github_scope": f"{oauth_module.IDENTITY_SCOPES['github']} {oauth_module.REPOSITORY_SCOPES['github']}",
                },
            )
            _audit("github_repo_access_granted", account=account)
            flash(
                "Repository access recorded as granted. Reading a repository still "
                "happens only when you ask ORBIT to do something with it.",
                "success",
            )
        elif action == "revoke-repo":
            accounts().update_integrations(account, {"github_repo_access": False})
            _audit("github_repo_access_revoked", account=account)
            flash("Repository access revoked. Sign-in still works; repositories are invisible again.", "success")
        elif action == "revoke-all":
            accounts().update_integrations(
                account, {"github_connected": False, "github_repo_access": False, "github_login": "", "github_scope": ""}
            )
            _audit("github_disconnected", account=account)
            flash("GitHub disconnected from this account.", "success")
        elif action in ("connect-gitlab", "connect-bitbucket"):
            flash("That integration is not built yet — it is listed as planned, not as a switch that does nothing.", "warning")
        return redirect(url_for("onboarding.settings_integrations"))
    return render_template(
        "settings/integrations.html",
        **_settings_context(
            "integrations",
            github_oauth=_github_oauth_available(),
            scope_identity=oauth_module.IDENTITY_SCOPES["github"],
            scope_repo=oauth_module.REPOSITORY_SCOPES["github"],
            scope_explanation=oauth_module.SCOPE_EXPLANATIONS["github"],
            repository_explanation=oauth_module.REPOSITORY_EXPLANATION,
            not_granted=oauth_module.NOT_GRANTED_BY_SIGNIN,
        ),
    )


def _github_oauth_available() -> dict[str, Any]:
    registry = config().oauth
    return {
        "available": registry.available("github"),
        "status": registry.status_line("github"),
        "simulated": registry.simulated("github"),
    }


@bp.route("/settings/notifications", methods=["GET", "POST"])
def settings_notifications():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    heartbeat = current_app.config.get("HEARTBEAT")
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_notifications"))
        if request.form.get("action") == "reminders" and heartbeat is not None:
            paused = request.form.get("reminders") != "on"
            try:
                if paused:
                    heartbeat.pause()
                else:
                    heartbeat.resume()
            except Exception:
                current_app.logger.exception("could not change reminder state")
                flash("Reminders could not be changed just now.", "error")
            else:
                flash("Reminders paused." if paused else "Reminders running again.", "success")
        if request.form.get("action") == "email":
            accounts().update_preferences(
                account,
                {
                    "email_product_updates": request.form.get("email_product_updates") == "on",
                    "feedback_replies": request.form.get("feedback_replies") == "on",
                },
            )
            flash("Notification preferences saved.", "success")
        return redirect(url_for("onboarding.settings_notifications"))
    ntfy_topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    return render_template(
        "settings/notifications.html",
        **_settings_context(
            "notifications",
            reminders_paused=bool(getattr(heartbeat, "paused", False)) if heartbeat is not None else False,
            heartbeat_ready=heartbeat is not None,
            plan_hour=int(getattr(getattr(heartbeat, "settings", None), "plan_hour", 10) or 10),
            checkin_gap=int(getattr(getattr(heartbeat, "settings", None), "checkin_gap_days", 2) or 2),
            push_configured=bool(ntfy_topic),
            push_topic_hint=(ntfy_topic[:4] + "…" if ntfy_topic else ""),
        ),
    )


@bp.route("/settings/privacy", methods=["GET", "POST"])
def settings_privacy():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_privacy"))
        if request.form.get("action") == "delete":
            if request.form.get("confirm") != "delete my account":
                flash("Type “delete my account” exactly to confirm.", "warning")
            else:
                account_id = account.id
                for project in project_store().for_account(account_id, include_archived=True):
                    project_store().delete(project)
                accounts().delete(account_id)
                drop = getattr(config().store_factory, "drop", None)
                if callable(drop):
                    drop(account_id)
                session.clear()
                _audit("account_deleted", reason="user_request")
                flash("Your account and its projects are gone. That cannot be undone.", "info")
                return redirect(url_for("onboarding.landing"))
        elif request.form.get("action") == "clear-hints":
            accounts().update_onboarding(account, {"dismissed_hints": []})
            flash("Dismissed hints cleared — the first-use tips can appear again.", "success")
        return redirect(url_for("onboarding.settings_privacy"))

    return render_template(
        "settings/privacy.html",
        **_settings_context(
            "privacy",
            runtime_dir=str(getattr(config().accounts, "path", "")).rsplit("/", 1)[0] or "runtime/",
            project_count=project_store().count_for_account(account.id),
            feedback_count=feedback_store().count_for_account(account.id),
        ),
    )


@bp.get("/settings/privacy/export")
def settings_export():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    payload = account.export()
    payload["projects"] = [
        project.as_dict() for project in project_store().for_account(account.id, include_archived=True)
    ]
    payload["feedback"] = [entry.as_dict() for entry in feedback_store().for_account(account.id)]
    _audit("data_exported", account=account)
    body = __import__("json").dumps(payload, indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="orbit-export-{account.id}.json"',
            "Cache-Control": "no-store",
        },
    )


@bp.route("/settings/security", methods=["GET", "POST"])
def settings_security():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    errors: dict[str, str] = {}
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_security"))
        action = request.form.get("action", "")
        if action == "change-password":
            try:
                signed_out = accounts().change_password(
                    account,
                    request.form.get("current_password") or "",
                    request.form.get("new_password") or "",
                    keep_session_id=session.get("session_id", ""),
                )
            except AccountStoreError as error:
                errors["password"] = str(error)
            else:
                _audit("password_changed", account=account, others_signed_out=signed_out)
                flash(
                    "Password changed."
                    + (f" {signed_out} other session{'s were' if signed_out != 1 else ' was'} signed out." if signed_out else ""),
                    "success",
                )
                return redirect(url_for("onboarding.settings_security"))
        elif action == "sign-out-others":
            removed = accounts().revoke_others(account, session.get("session_id", ""))
            _audit("sessions_revoked", account=account, count=removed)
            flash(f"{removed} other session{'s' if removed != 1 else ''} signed out." if removed else "No other sessions were open.", "success")
            return redirect(url_for("onboarding.settings_security"))
        elif action == "revoke":
            target = request.form.get("session_id", "")
            if accounts().revoke_session(account, target):
                _audit("session_revoked", account=account)
                if target == session.get("session_id"):
                    session.clear()
                    return redirect(url_for("onboarding.login", signedout="1"))
                flash("That session was signed out.", "success")
            return redirect(url_for("onboarding.settings_security"))
    return render_template(
        "settings/security.html",
        **_settings_context("security", errors=errors, strength=password_strength),
    )


@bp.route("/settings/help", methods=["GET", "POST"])
def settings_help():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Try again.", "error")
            return redirect(url_for("onboarding.settings_help"))
        action = request.form.get("action", "")
        if action == "replay-tour":
            accounts().update_onboarding(account, {"tour_seen": False, "tour_opt_out": False})
            return redirect("/?tour=1")
        if action == "reset-hints":
            accounts().update_onboarding(account, {"dismissed_hints": [], "getting_started_dismissed": False})
            flash("First-use tips and the getting-started checklist are back.", "success")
        if action == "hide-tour":
            accounts().update_onboarding(account, {"tour_opt_out": True, "tour_seen": True})
            flash("The tour will not be offered again. You can replay it from here any time.", "info")
        return redirect(url_for("onboarding.settings_help"))
    return render_template("settings/help.html", **_settings_context("help"))


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #

@bp.route("/feedback", methods=["GET", "POST"])
def feedback():
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login", next="/feedback"))
    errors: list[str] = []
    drafted = {"kind": "suggestion", "subject": "", "body": ""}
    if request.method == "POST":
        if not _csrf_ok():
            flash("That form expired. Your text was not sent.", "error")
            return redirect(url_for("onboarding.feedback"))
        drafted = {
            "kind": request.form.get("kind") or "suggestion",
            "subject": request.form.get("subject") or "",
            "body": request.form.get("body") or "",
        }
        entry, errors = feedback_store().submit(
            account_id=account.id,
            kind=drafted["kind"],
            subject=drafted["subject"],
            body=drafted["body"],
            context=_context_from_form_or_request(account),
        )
        if entry is not None:
            _audit("feedback_received", account=account, kind=entry.kind, reference=entry.reference)
            if account.preferences.get("feedback_replies", True) and getattr(config().mailer, "delivers", False):
                config().mailer.send(
                    mailer_module.feedback_receipt(
                        to=account.email,
                        name=account.first_name,
                        kind=entry.kind_label.lower(),
                        reference=entry.reference,
                    )
                )
            flash(f"Received. Reference {entry.reference} — no changes were made to your workspace.", "success")
            return redirect(url_for("onboarding.feedback", sent=entry.id))
    return render_template(
        "feedback.html",
        page="feedback",
        types=content.FEEDBACK_TYPES,
        promise=content.FEEDBACK_PROMISE,
        drafted=drafted,
        errors=errors,
        entries=feedback_store().for_account(account.id),
        context_default=build_context(
            surface="Feedback page",
            route="/feedback",
            account_email=account.email,
            project_name=request.args.get("project", ""),
            user_agent=request.headers.get("User-Agent", ""),
            viewport=request.args.get("viewport", ""),
            build=config().build_label,
        ),
        sent=request.args.get("sent", ""),
    )


@bp.get("/feedback/<entry_id>")
def feedback_detail(entry_id: str):
    account = current_account()
    if account is None:
        return redirect(url_for("onboarding.login"))
    entry = feedback_store().get(entry_id, account.id)
    if entry is None:
        return render_template("feedback_not_found.html", page="feedback", entry_id=entry_id), 404
    return render_template("feedback_detail.html", page="feedback", entry=entry)


def _context_from_form_or_request(account: Account) -> dict[str, Any]:
    raw = request.form.get("context")
    parsed: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
    clean = sanitize_context(parsed)
    clean.setdefault("surface", "Feedback page")
    clean.setdefault("route", "/feedback")
    clean.setdefault("build", config().build_label)
    clean["account"] = account.email
    return clean


# --------------------------------------------------------------------------- #
# JSON endpoints (keep the person where they are)
# --------------------------------------------------------------------------- #

@bp.post("/api/onboarding/tour")
def api_tour():
    account = current_account()
    if account is None:
        return jsonify({"ok": False, "error": "Not signed in."}), 401
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "Stale token."}), 400
    payload = request.get_json(silent=True) or {}
    changes: dict[str, Any] = {}
    if payload.get("seen"):
        changes["tour_seen"] = True
    if payload.get("opt_out"):
        changes["tour_opt_out"] = True
        changes["tour_seen"] = True
    if changes:
        accounts().update_onboarding(account, changes)
        _audit("tour_updated", account=account, **{k: bool(v) for k, v in changes.items()})
    return jsonify({"ok": True, "onboarding": account.onboarding})


@bp.post("/api/onboarding/hints/<hint_id>/dismiss")
def api_dismiss_hint(hint_id: str):
    account = current_account()
    if account is None:
        return jsonify({"ok": False, "error": "Not signed in."}), 401
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "Stale token."}), 400
    accounts().dismiss_hint(account, hint_id)
    return jsonify({"ok": True, "dismissed": hint_id})


@bp.post("/api/onboarding/getting-started")
def api_getting_started():
    account = current_account()
    if account is None:
        return jsonify({"ok": False, "error": "Not signed in."}), 401
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "Stale token."}), 400
    payload = request.get_json(silent=True) or {}
    if payload.get("dismiss"):
        accounts().update_onboarding(account, {"getting_started_dismissed": True})
    return jsonify({"ok": True, "onboarding": account.onboarding})


@bp.post("/api/onboarding/feedback")
def api_feedback():
    """Submit feedback in place: the page never reloads, so no work is lost."""
    account = current_account()
    if account is None:
        return jsonify({"ok": False, "error": "Your session ended. Refresh the page and sign in again."}), 401
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "That form expired. Reopen the panel and try again."}), 400
    payload = request.get_json(silent=True) or {}
    context = sanitize_context(payload.get("context"))
    context.setdefault("surface", "Feedback panel")
    context["account"] = account.email
    entry, problems = feedback_store().submit(
        account_id=account.id,
        kind=str(payload.get("kind") or "general"),
        subject=str(payload.get("subject") or ""),
        body=str(payload.get("body") or ""),
        context=context,
    )
    if entry is None:
        return jsonify({"ok": False, "problems": problems}), 400
    _audit("feedback_received", account=account, kind=entry.kind, reference=entry.reference)
    return jsonify(
        {
            "ok": True,
            "reference": entry.reference,
            "id": entry.id,
            "kind_label": entry.kind_label,
            "status_label": entry.status_label,
            "created_human": entry.created_human,
        }
    )


@bp.post("/api/onboarding/projects/quick")
def api_quick_project():
    """Create a project from a single field, for the dashboard's inline form."""
    account = current_account()
    if account is None:
        return jsonify({"ok": False, "error": "Your session ended. Refresh the page and sign in again."}), 401
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "That form expired. Reload and try again."}), 400
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "problems": ["A name, even a provisional one."]}), 400
    project = project_store().create(
        account_id=account.id,
        name=name,
        kind="orbit",
        description=(payload.get("description") or "").strip()[:280],
    )
    _audit("project_created", account=account, project=project.id, kind="orbit")
    return jsonify({"ok": True, "project": {"id": project.id, "name": project.name}, "url": f"/projects/{project.id}"})


@bp.post("/api/onboarding/preferences")
def api_preferences():
    """Save one preference change without a full page submit."""
    account = current_account()
    if account is None:
        return jsonify({"ok": False, "error": "Not signed in."}), 401
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "Stale token."}), 400
    payload = request.get_json(silent=True) or {}
    # Both shapes are accepted — {"changes": {...}} and a flat {...} — because the
    # settings switches and the personalization screen post from different places.
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        changes = {key: value for key, value in payload.items() if key != "changes"}
    try:
        preferences = accounts().update_preferences(account, changes)
    except AccountStoreError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    return jsonify({"ok": True, "preferences": preferences})


@bp.post("/api/onboarding/seen")
def api_seen():
    """Record that a one-time surface was shown, so it does not show again."""
    account = current_account()
    if account is None:
        return jsonify({"ok": False, "error": "Not signed in."}), 401
    if not _csrf_ok():
        return jsonify({"ok": False, "error": "Stale token."}), 400
    payload = request.get_json(silent=True) or {}
    key = str(payload.get("key") or "")
    if key not in ("welcome_seen", "personalize_done", "personalize_skipped", "tour_seen"):
        return jsonify({"ok": False, "error": "Unknown key."}), 400
    accounts().update_onboarding(account, {key: True})
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def attach(app: Any, onboarding_config: OnboardingConfig) -> None:
    """Register the blueprint, the gate, and the store resolver on a Flask app."""
    app.config["ONBOARDING"] = onboarding_config
    app.register_blueprint(bp)

    @app.before_request
    def _orbit_gate():  # pragma: no cover - exercised through every route test
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("onboarding.static"):
            return None
        account = current_account()
        if account is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Your session ended. Refresh the page and sign in again."}), 401
            if _pending_account() is not None:
                # Signed up, link not opened yet. The workspace is not theirs to
                # read, and the landing page would only ask them to sign in again.
                return redirect(url_for("onboarding.verify"))
            if request.method == "GET" and request.path == "/":
                # Someone who is not signed in and asks for the root has not told
                # us whether they want to buy in or sign in. The landing page says
                # what ORBIT is and carries both calls to action; the login screen
                # is only right when the intent is already known (a deep link).
                return redirect(url_for("onboarding.landing"))
            target = "/login"
            if request.path not in ("/", "/landing"):
                target = f"/login?next={request.full_path.rstrip('?')}"
            return redirect(target)
        if _requires_verification(account) and endpoint not in PENDING_ALLOWED_ENDPOINTS:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Confirm your email address to use the workspace."}), 403
            return redirect(url_for("onboarding.verify"))
        return None

    def _resolve_store() -> Any:
        account = current_account()
        if account is None:
            return None
        return _store_for(account)

    app.config["STORE_RESOLVER"] = _resolve_store
    app.config["ONBOARDING_ENABLED"] = True

    @app.context_processor
    def _onboarding_shell() -> dict[str, Any]:
        """What every workspace template needs once an account exists.

        Registering this at app level is what lets the tour, the feedback drawer and
        the onboarding styles reach the task and analytics screens without editing
        them: layout.html includes the partials, and the partials decide whether
        they have anything to render.
        """
        account = current_account()
        if account is None:
            return {}
        endpoint = (request.endpoint or "").split(".")[-1]
        # The sidebar lists real projects on every workspace screen; the endpoint
        # tells it which one is currently open.
        shell_projects = project_store().for_account(account.id)
        active_project_id = ""
        match = re.search(r"/projects/(p-[0-9a-f]+)", request.path)
        if match:
            active_project_id = match.group(1)
        return {
            "account": account,
            "projects": shell_projects,
            "active_project_id": active_project_id,
            "project_count": len(shell_projects),
            "feedback_count": feedback_store().count_for_account(account.id),
            "account_public": account.public(),
            "tour_offered": endpoint == "home"
            and (
                request.args.get("tour") == "1"
                or not (account.onboarding.get("tour_seen") or account.onboarding.get("tour_opt_out"))
            ),
            "tour_requested": endpoint == "home" and request.args.get("tour") == "1",
            "tour_steps": tour_steps_payload(),
            "feedback_context": feedback_context_for(account),
            "page_styles": True,
            "content": content,
            "csrf_token": csrf_token,
        }
