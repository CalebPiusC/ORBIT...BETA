"""ORBIT's onboarding layer: landing, accounts, welcome, projects, settings, feedback.

This package is a layer, not a fork. It owns the surfaces a person meets *before*
and *around* the workspace — the marketing page, sign-up, verification, the
welcome screen, project entry points, settings and beta feedback — and it plugs
into the existing app through one function, :func:`attach`.

What it deliberately does not do:

* It does not touch the chat, task, analytics or approval code paths. The
  dashboard reads their state and links into their routes; the Agent task screen
  is still the one in ``app/templates/task.html``.
* It does not write anything to disk at import time. ``build_onboarding()`` is
  called by whoever runs the app (``run_app``), which is what keeps
  ``runtime/accounts.json`` out of the way of the test suite.
* It does not fake a provider. Where Google, GitHub, or email delivery is
  unconfigured, the screens say so — see :mod:`onboarding.oauth` and
  :mod:`onboarding.mailer`.

Wiring, in full::

    from onboarding import attach, build_onboarding
    attach(app, build_onboarding(runtime_dir="runtime"))

After that, ``app.config["STORE_RESOLVER"]`` returns the signed-in account's
workspace, and every non-public route requires a verified account.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Callable

from onboarding.accounts import Account, AccountStore
from onboarding.blueprint import OnboardingConfig, attach as attach_blueprint
from onboarding.feedback import FeedbackStore
from onboarding.mailer import build_mailer
from onboarding.oauth import build_registry
from onboarding.projects import ProjectStore, PublishingProjectStore

__all__ = [
    "AccountStore",
    "FeedbackStore",
    "ProjectStore",
    "OnboardingConfig",
    "attach",
    "build_onboarding",
    "make_store_factory",
    "sync_workspace_projects",
]


def make_store_factory(*, demo_account_id: str | None = None) -> Callable[[Account], Any]:
    """One workspace per account, built on first use and kept for the process.

    Conversations live in memory today (see README: persistence is a later phase),
    so the cache is per process and a restart starts fresh. Accounts and projects
    are on disk, which is what makes "come back tomorrow" work.
    """
    from app.store import Store, User

    stores: dict[str, Any] = {}

    def factory(account: Account) -> Any:
        existing = stores.get(account.id)
        if existing is not None:
            return existing
        if account.is_demo or account.id == demo_account_id:
            store = Store()
            store.user = User(name=account.name, workspace=account.workspace, id=account.id)
        else:
            store = Store.blank(
                user=User(name=account.name, workspace=account.workspace, id=account.id)
            )
            # The account's own AI preferences decide where the workspace opens.
            try:
                if account.preferences.get("default_mode") in ("chat", "agent"):
                    store.set_mode(account.preferences["default_mode"])
                if account.preferences.get("default_depth") in ("quick", "deep"):
                    store.set_thinking_level(account.preferences["default_depth"])
            except ValueError:
                pass
        stores[account.id] = store
        return store

    def drop(account_id: str) -> None:
        """Forget one account's workspace — used when an account is deleted."""
        stores.pop(account_id, None)

    factory.drop = drop  # type: ignore[attr-defined]
    factory.stores = stores  # type: ignore[attr-defined]
    return factory


def sync_workspace_projects(app: Any) -> int:
    """Publish this layer's project ledger into the store ``app`` is using now.

    ``create_app`` attaches this layer before ``run_app`` swaps the in-memory
    project store for the file-backed one, so the attach-time reconciliation
    lands in a store that is about to be replaced. Re-running it once the real
    store is in place is what keeps every project on both screens.
    """
    config: OnboardingConfig | None = app.config.get("ONBOARDING")
    if config is None:
        return 0
    store = config.projects
    if isinstance(store, PublishingProjectStore):
        return store.sync_all()
    return 0


def session_secret(runtime_dir: str | Path) -> str:
    """The key that signs session cookies.

    ``ORBIT_AUTH_SECRET`` wins if it is set. Otherwise a random key is generated
    once and kept in ``runtime/session.key`` (owner-readable), because a session
    cookie signed with a value that lives in the source tree is a session cookie
    anyone can mint. Deleting that file signs everybody out — which is the honest
    trade-off, and better than the alternative.
    """
    configured = (os.environ.get("ORBIT_AUTH_SECRET") or "").strip()
    if configured:
        return configured
    path = Path(runtime_dir) / "session.key"
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if len(existing) >= 32:
                return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        path.write_text(generated, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return generated
    except OSError:
        # A read-only filesystem still has to work. Sessions become per-process,
        # so a restart signs everyone out rather than failing to boot.
        return secrets.token_urlsafe(48)


def build_onboarding(
    *,
    runtime_dir: str | Path = "runtime",
    audit: Any | None = None,
    public_url: str = "",
    build_label: str = "beta",
    demo_enabled: bool = True,
    store_factory: Callable[[Account], Any] | None = None,
) -> OnboardingConfig:
    """Construct the stores, the mailer and the OAuth registry, and return a config."""
    directory = Path(runtime_dir)
    registry = build_registry(public_url=public_url or _guess_public_url())
    config = OnboardingConfig(
        accounts=AccountStore(directory / "accounts.json"),
        # Its own ledger, not the workspace's: the workspace keeps what the
        # Projects screens read (runtime/projects.json), and this layer keeps the
        # fuller import records. Two schemas in one file is a crash waiting to
        # happen, which is exactly what it did.
        projects=ProjectStore(directory / "onboarding-projects.json"),
        feedback=FeedbackStore(directory / "feedback.json"),
        mailer=build_mailer(directory),
        oauth=registry,
        store_factory=store_factory or make_store_factory(),
        audit=audit,
        public_url=public_url or _guess_public_url(),
        build_label=build_label,
        demo_enabled=demo_enabled,
        runtime_dir=str(directory),
    )
    config.seed_demo = lambda account: seed_demo_workspace(config, account)
    return config


# --------------------------------------------------------------------------- #
# The demo workspace
# --------------------------------------------------------------------------- #

DEMO_PROJECTS = (
    {
        "name": "whatsapp-ordering-bot",
        "kind": "github",
        "description": "Imported from GitHub (caleb/whatsapp-ordering-bot) — file listing only.",
        "primary_language": "Python",
        "remote": {"repo": "whatsapp-ordering-bot", "owner": "caleb", "branch": "main", "contents_copied": False},
        "files": [
            {"path": "bot.py", "size": 8123, "kind": "file"},
            {"path": "orders.py", "size": 6120, "kind": "file"},
            {"path": "vendor/summary.py", "size": 2140, "kind": "file"},
            {"path": "tests/test_orders.py", "size": 3312, "kind": "file"},
            {"path": "README.md", "size": 1180, "kind": "file"},
            {"path": "requirements.txt", "size": 96, "kind": "file"},
        ],
    },
    {
        "name": "portfolio-site",
        "kind": "zip",
        "description": "ZIP archive — the file listing was read; the archive itself was not kept.",
        "primary_language": "TypeScript",
        "remote": {"archive": "portfolio-site.zip", "contents_copied": False},
        "files": [
            {"path": "app/page.tsx", "size": 4210, "kind": "file"},
            {"path": "app/layout.tsx", "size": 1580, "kind": "file"},
            {"path": "components/Hero.tsx", "size": 3020, "kind": "file"},
            {"path": "package.json", "size": 720, "kind": "file"},
        ],
    },
    {
        "name": "SEN106 study planner",
        "kind": "orbit",
        "description": "Guided setup · data",
        "primary_language": "Python",
        "template": "data",
        "options": ["tests", "readme"],
        "plan": {
            "generated_by": "local planner",
            "template": "data",
            "language": "Python",
            "status": "planned",
            "note": "Planned, not created. Ask ORBIT in Chat mode to write any of these.",
            "files": [
                {"path": "README.md", "why": "Says what this is and how to run it.", "kind": "doc"},
                {"path": "analysis/load.py", "why": "Starting point for a data project in Python.", "kind": "code"},
                {"path": "analysis/report.py", "why": "Starting point for a data project in Python.", "kind": "code"},
                {"path": "tests/test_smoke.py", "why": "One failing test, so 'make it pass' is the first real task.", "kind": "test"},
            ],
        },
        "files": [
            {"path": "README.md", "size": 0, "kind": "planned"},
            {"path": "analysis/load.py", "size": 0, "kind": "planned"},
            {"path": "analysis/report.py", "size": 0, "kind": "planned"},
            {"path": "tests/test_smoke.py", "size": 0, "kind": "planned"},
        ],
    },
)


def seed_demo_workspace(config: OnboardingConfig, account: Account) -> None:
    """Give the shared demo account real sample projects, once.

    These are records, not clones: names, kinds, and file listings, exactly like
    every other import path in this build produces. The demo is what the landing
    page's "Explore" button opens, so somebody can see the *has-projects* shape of
    the dashboard without inventing an account first.
    """
    if config.projects.count_for_account(account.id) > 0:
        return
    for sample in DEMO_PROJECTS:
        project = config.projects.create(
            account_id=account.id,
            name=sample["name"],
            kind=sample["kind"],
            description=sample.get("description", ""),
            primary_language=sample.get("primary_language", ""),
            template=sample.get("template", ""),
            options=list(sample.get("options", [])),
            files=list(sample.get("files", [])),
            plan=dict(sample.get("plan", {})),
            remote=dict(sample.get("remote", {})),
        )
        if sample["kind"] == "orbit":
            project.notes = (
                "Seeded for the demo. In a real project this plan would be the one you "
                "approved, and these files would still not exist until you asked for them."
            )
            config.projects.update(project)


def _guess_public_url() -> str:
    explicit = (os.environ.get("ORBIT_PUBLIC_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = (os.environ.get("PORT") or "5000").strip()
    host = (os.environ.get("ORBIT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def attach(app: Any, config: OnboardingConfig) -> None:
    """Register the onboarding blueprint, the gate, and the session key."""
    if not app.config.get("SECRET_KEY") or app.secret_key == "orbit-dev-secret":
        app.secret_key = session_secret(config.runtime_dir)
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    # Secure cookies require HTTPS; turning it on unconditionally is what breaks a
    # local preview, so it follows the deployment rather than our good intentions.
    if (config.public_url or "").startswith("https://"):
        app.config["SESSION_COOKIE_SECURE"] = True
    # The workspace's project store is what the Projects overview, the rail and
    # the project page read, so every project this layer records is published
    # into it under the same id (see PublishingProjectStore).
    config.projects = PublishingProjectStore(config.projects, app)  # type: ignore[assignment]
    # Bring the workspace's store up to date with whatever this layer wrote
    # before the two were joined: one list, not two that disagree.
    config.projects.sync_all()
    attach_blueprint(app, config)
