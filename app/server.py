"""ORBIT web server: routes + templates backed by app.store and streaming.

Two screens from the mockup become real routes:

  * ``/``                       — the chat-first home
  * ``/task/<task_id>``         — the coding-agent task view
  * ``/analytics``              — counts read back out of audit.log

The mockup's two screens were switched by a dev-only toggle; that toggle is
removed here. Navigation between the two is driven by real state (the Chat/Agent
mode and the task id), and every piece of placeholder text in the mockup is
rendered from app.store so it is live data, not static markup.

Streaming: the thinking block and Orbit's own replies are delivered over Server-
Sent Events from ``ChatProvider.reply_stream`` (chunked, not one-shot). The
home chat streams answers; the task view streams thinking-then-answer.

The approval gate from Phase 2 is enforced here, not just drawn: a task's
proposed changes can only be applied/pushed after an explicit approval call, and
``apply_changes`` hard-fails otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from flask import (
    Flask,
    Response,
    g,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from providers.base import ChatMessage as ProvChatMessage, StreamChunk, ToolProposal
from werkzeug.exceptions import RequestEntityTooLarge

from app.accounts import (
    EXPERIENCE_LEVELS,
    FEEDBACK_KINDS,
    INTERESTS,
    LANGUAGES,
    MIN_PASSWORD_CHARS,
    OAUTH_SCOPE_COPY,
    PROVIDER_LABELS,
    ROLES,
    AccountError,
    AccountStore,
    checklist_for,
    email_issues,
    password_issues,
    recommendations_for,
    session_label,
    workspace_view,
)
from app.actions import PendingActions, ProposalRejected
from app.age import human_age
from app.agents import MAIN_AGENT_ID, agent_types, get_agent, search_agents
from app.analytics import RATING_LABELS, build_analytics
from app.followups import followups_for
from app.projectmap import latest_task, map_stages, next_up, progress, row_state
from app.projects import (
    SOURCE_LABELS,
    STATUS_LABELS,
    STATUSES,
    AccountProjects,
    ProjectStore,
)
from app.starters import PROJECT_STARTERS, WELCOME_STARTERS
from app.store import (
    DEFAULT_USER_ID,
    PLACEHOLDER_TITLE,
    AccountView,
    ActionCard,
    Activity,
    Store,
    now_label,
)
from app.summaries import request_summary
from app.taskplan import (
    FAILED,
    IDLE,
    RUNNING,
    SOURCE_ORBIT,
    TaskRun,
    add_files,
    apply_narration,
    files_in,
    finish_run,
    new_run,
)
from app.voice import VoiceService, VoiceServiceUnavailable
from app.titles import request_title
from memory.facts import FactStore
from memory.time_context import with_current_time
from tools.registry import DEFAULT_AUDIT_LOG_PATH, AuditLog

log = logging.getLogger("orbit.server")

# The session-signing secret's fallback. It is committed, so it is public: any
# server that ran on it would let anyone forge a signed-in session cookie.
# ``create_app`` still uses it so a bare launch can render a page, but
# ``run_app`` refuses to serve on it — see ``_require_real_secret_key``.
DEFAULT_SECRET_KEY = "orbit-dev-secret"
SECRET_KEY_ENV_VAR = "ORBIT_SECRET_KEY"

MAX_VOICE_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_SPEECH_TEXT_CHARS = 12_000
MAX_REASON_CHARS = 300
_AUDIO_SUFFIX_BY_MIME = {
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/mp4": ".mp4",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
}

HOME_SYSTEM_PROMPT = (
    "You are ORBIT, a helpful assistant. Reply clearly and concisely. "
    "Keep answers focused and useful."
)

# Added to the system prompt for one turn when someone regenerates a reply. The
# model already samples non-deterministically, but a second identical answer is
# what regeneration looks like when it has failed, so the request is explicit:
# answer the same turn differently rather than restating the same sentence.
REGENERATE_INSTRUCTION = (
    "The person asked for a fresh answer to the same message. Answer it again, "
    "genuinely differently: a different opening, structure or angle. Do not "
    "repeat your previous reply's wording."
)

# Added to the home prompt only when tools are on (Chat mode with a registry).
TOOL_GUIDANCE = (
    "You may ask for at most one tool action in a reply, and only when the person "
    "explicitly asks for it: write_note (save a line to their notes), remember_fact or "
    "forget_fact (save or remove a fact about them), save_plan (save today's plan), or "
    "pause_heartbeat or resume_heartbeat (pause or resume reminders). Nothing runs until "
    "the person approves it on screen. Do not say anything was saved, removed, paused or "
    "resumed until the card shows it was."
)

TASK_SYSTEM_PROMPT = (
    "You are ORBIT's coding agent, working on a specific task in the user's "
    "repository. First narrate your reasoning in plain language (wrapped in "
    "<thinking>…</thinking>), then give the final answer. "
    "IMPORTANT: you must NEVER commit or push changes on your own. Proposed "
    "changes stay in draft until the user explicitly approves them. If the user "
    "asks you to push, tell them you will wait for their explicit approval."
)

# The project workspace's Main Agent conversation. Project-centred by design:
# the context block below is its ground truth, and anything it cannot see it
# must ask about rather than invent.
MAIN_AGENT_PROMPT = (
    "You are orbit's Main Agent for one project — the primary assistant the "
    "person works with inside that project's workspace. Stay centred on the "
    "project: its goal, its code, its tasks, and the decisions around them. "
    "The PROJECT CONTEXT block below is your ground truth about the project; "
    "if something about the project is not in it, ask the person rather than "
    "invent details. Help them understand the project, plan its tasks, and "
    "move it forward. Never claim you changed the project's map, tasks, files "
    "or code — you discuss and propose; changes happen only through the "
    "workspace's own controls with the person's approval."
)


def _project_context(project: Any, tasks: list[Any]) -> str:
    """The Main Agent's ground-truth block, built from stored data only."""
    lines = [
        "PROJECT CONTEXT",
        f"Name: {project.name}",
        f"Status: {STATUS_LABELS.get(project.status, project.status)}",
        f"Source: {SOURCE_LABELS.get(project.source, project.source)}",
        f"Owner's description: {project.description or '(none provided)'}",
        f"AI summary: {project.summary or '(not written yet)'}",
        f"Tasks on the project map ({len(tasks)}):",
    ]
    if tasks:
        for task in tasks[:8]:
            lines.append(f"- {task.title}")
        if len(tasks) > 8:
            lines.append(f"- …and {len(tasks) - 8} more.")
    else:
        lines.append("- (none yet — the map is empty)")
    return "\n".join(lines)


def _sse(event: str, data: str) -> str:
    # Each logical line of ``data`` becomes its own ``data:`` line so that
    # newlines inside a streamed chunk survive SSE framing. The client joins
    # them back with "\n".
    data_lines = "\n".join(f"data: {line}" for line in data.split("\n"))
    return f"event: {event}\n{data_lines}\n\n"


def _json(payload: Any, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")


def _failure_reason(error: BaseException, *, limit: int = MAX_REASON_CHARS) -> str:
    """The short, UI-safe cause of a failed request.

    The browser's status line has room for one sentence, not a stack trace: the
    full exception belongs in the console log and ``audit.log``. Services that
    know a better wording than ``Type: message`` expose it as ``reason``.
    """
    detail = getattr(error, "reason", None) or f"{type(error).__name__}: {error}"
    collapsed = " ".join(str(detail).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def configure_logging() -> None:
    """Give Orbit's own loggers a console handler that keeps the real exception.

    The Flask development server configures no logging, so ``log.exception``
    previously fell through to logging's last-resort handler: that prints only
    the bare message, without a timestamp, level, or logger name, and it drops
    INFO lines such as "the Whisper model loaded and from where" entirely. A
    failure whose cause is invisible on the console is the reason this exists.

    ``ORBIT_LOG_LEVEL`` overrides the level (default INFO). The handler is
    attached once, and only to ``orbit.*`` so third-party libraries keep their
    own settings.
    """
    raw_level = os.environ.get("ORBIT_LOG_LEVEL", "").strip().upper() or "INFO"
    level = getattr(logging, raw_level, None)
    if not isinstance(level, int):
        level = logging.INFO
    logger = logging.getLogger("orbit")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        # Orbit's lines are handled here; they must not also hit a root handler
        # (Flask's debug reloader or a hosting wrapper may add one).
        logger.propagate = False
    logger.setLevel(level)


def _voice_dependency_dict(service: Any) -> dict[str, Any]:
    """The voice service's dependency state, when it can report it. Never raises."""
    dependencies = getattr(service, "dependencies", None)
    if not callable(dependencies):
        return {}
    try:
        report = dependencies()
    except Exception:
        log.exception("could not collect voice dependency diagnostics")
        return {}
    as_dict = getattr(report, "as_dict", None)
    if not callable(as_dict):
        return {}
    try:
        return dict(as_dict())
    except Exception:
        log.exception("could not serialize voice dependency diagnostics")
        return {}


def _outcome_text(result: Any) -> str:
    """The one line shown on a card: a tool's own message, or its error."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("error") or result.get("message") or "")
    return ""


def _history_from_thread(
    messages: Iterable[Any], extra_user: str | None = None
) -> list[ProvChatMessage]:
    history: list[ProvChatMessage] = []
    for msg in messages:
        role = "assistant" if getattr(msg, "is_orbit", False) else "user"
        body = msg.body
        card = getattr(msg, "action", None)
        if card is not None:
            # The model is told how the proposal ended, so it does not claim otherwise.
            outcome = card.state if not card.result else f"{card.state}: {card.result}"
            body = f"{body} [{outcome}]"
        history.append(ProvChatMessage(role=role, content=body))
    if extra_user:
        history.append(ProvChatMessage(role="user", content=extra_user))
    return history


# ----- route ownership registry --------------------------------------------
# Every route that takes an id naming a stored record must either perform an
# ownership check (marked with @ownership_checked, answering 404 — never
# 403 — for another account's record) or appear here with the reason no
# ownership check applies. tests/test_account_ownership.py walks
# app.url_map and fails on any id-taking route in neither set, so adding a
# route forces the decision to be made explicitly.
OWNERSHIP_EXEMPTIONS: dict[str, str] = {
    "static": (
        "Flask's static-file route; the filename is an asset path, not a "
        "stored record."
    ),
    "select_agent": (
        "agent_id names a catalog AgentType (app/agents.py), validated by "
        "get_agent -> 404. The selection itself is stored per account."
    ),
    "api_heartbeat_dismiss": (
        "notice_id belongs to the heartbeat service, which is "
        "process-global and keeps no per-account records."
    ),
    "auth_oauth": (
        "provider names a sign-in provider catalog entry, validated "
        "against OAUTH_DEFAULTS -> 404."
    ),
}


def ownership_checked(*params: str):
    """Mark a view function as ownership-checking the named id params.

    The checks themselves live in the views (scoped store views plus the
    ``_owned_*_or_404`` helpers); this marker is what the url_map guard
    test enumerates. The function is returned unwrapped.
    """

    def mark(func):
        func.__ownership_params__ = params  # type: ignore[attr-defined]
        return func

    return mark


def create_app(
    provider: Any | None = None,
    store: Store | None = None,
    facts: FactStore | None = None,
    heartbeat: Any | None = None,
    actions: PendingActions | None = None,
    voice_service: Any | None = None,
    model_audit: AuditLog | None = None,
    projects: ProjectStore | None = None,
    accounts: AccountStore | None = None,
) -> Flask:
    """Build the Flask app.

    ``provider`` is the ChatProvider used for streaming. If omitted, the app
    lazily constructs a real ``GeminiProvider`` on first use (which needs
    GEMINI_API_KEY). Tests pass a stubbed provider directly. ``facts`` is the
    background-fact file; it is re-read on every turn.

    ``heartbeat`` is the reminder service. It is deliberately not built here:
    ``run_app`` builds it and starts its scheduler at boot, so importing or
    testing the app never touches runtime/heartbeat.json. Without a heartbeat
    the heartbeat routes answer 503 and home shows no notices. Voice providers
    are lazy by default; tests may inject a fake ``voice_service``.

    ``projects`` is the Projects store. The default is in-memory — the app
    factory never writes runtime/projects.json, same as the heartbeat. At boot
    ``run_app`` swaps in the file-backed store so a restart keeps projects.

    ``accounts`` is the onboarding/account store. ``None`` means the launch has
    no account wall at all: every page behaves exactly as the single-user app
    always has (that is what the test suite creates). ``run_app`` attaches a
    file-backed store so the real server gets the full sign-up → verify →
    welcome → dashboard flow.
    """
    app = Flask(__name__, template_folder="templates")
    app.secret_key = os.environ.get(SECRET_KEY_ENV_VAR, DEFAULT_SECRET_KEY)
    app.config["MAX_CONTENT_LENGTH"] = MAX_VOICE_UPLOAD_BYTES
    app.config["PROVIDER"] = provider
    app.config["STORE"] = store or Store()
    app.config["ACCOUNTS"] = accounts
    app.config["PROJECTS"] = projects if projects is not None else ProjectStore()
    app.config["FACTS"] = facts if facts is not None else FactStore()
    app.config["HEARTBEAT"] = heartbeat
    # The audit log is built first so the voice service can record a real model
    # load in it: "did the model actually download and load?" should be
    # answerable from audit.log, not only from a console line.
    audit = model_audit if model_audit is not None else AuditLog()
    app.config["MODEL_AUDIT"] = audit
    app.config["VOICE_SERVICE"] = (
        voice_service if voice_service is not None else VoiceService(audit_log=audit)
    )
    # The queue of tool calls the home chat proposed. Without it, Chat mode is
    # plain text, as before. run_app builds it with the real tools.
    app.config["ACTIONS"] = actions

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error: RequestEntityTooLarge) -> Response:
        return _json({"error": "Recording is too large. Keep voice clips under 25 MB."}, 413)

    def _heartbeat_or_none() -> Any | None:
        return app.config["HEARTBEAT"]

    def _panel_state(task_obj: Any) -> dict[str, Any] | None:
        """The Agent panel's state, or ``None`` when this task has never run.

        The panel is rendered from this and nothing else. A task with no run has
        no state to report, and an invented one would read as progress the agent
        never made.
        """
        run: TaskRun | None = getattr(task_obj, "run", None)
        if run is None:
            return None
        payload: dict[str, Any] = dict(run.as_dict())
        payload["activity"] = [{"text": a.text, "time": a.time} for a in task_obj.activity]
        payload["approved"] = bool(task_obj.approved)
        payload["applied"] = bool(task_obj.applied)
        # A planned-but-not-started run is not yet working on anything.
        payload["running"] = run.status == RUNNING and run.started
        return payload

    def _note(task_obj: Any, text: str) -> None:
        """One real line in the task's activity list, newest first."""
        task_obj.activity.insert(0, Activity(text=text, time=now_label()))

    def _heartbeat_view() -> dict[str, Any] | None:
        hb = _heartbeat_or_none()
        if hb is None:
            return None
        try:
            return hb.snapshot()
        except RuntimeError:
            # A corrupt state file must not take the whole home page down. The
            # error is logged, and the scheduler reports it on every tick.
            log.exception("heartbeat state unreadable; home shows no notices")
            return None

    def get_provider() -> Any:
        prov = app.config["PROVIDER"]
        if prov is None:
            from providers.gemini import GeminiProvider

            queue = app.config["ACTIONS"]
            prov = GeminiProvider(
                tool_registry=queue.registry if queue is not None else None,
                audit_log=app.config["MODEL_AUDIT"],
            )
            app.config["PROVIDER"] = prov
        return prov

    def _record_model_event(event: str, **fields: Any) -> None:
        audit: AuditLog | None = app.config["MODEL_AUDIT"]
        if audit is None:
            return
        try:
            audit.record_model_event(event=event, **fields)
        except Exception:
            # Diagnostics must not turn a provider failure into a broken SSE stream.
            log.exception("could not write model diagnostics to audit.log")

    def _record_event(event_name: str, **fields: Any) -> None:
        """Append one non-model event to audit.log: a message, a rating, a check-in.

        The Analytics view reads back exactly these lines, so a rating or a sent
        message is recorded here rather than in a second store. Fields are ids,
        counts and labels -- never message text.
        """
        audit: AuditLog | None = app.config["MODEL_AUDIT"]
        if audit is None:
            return
        try:
            audit.record_event(event_name, **fields)
        except Exception:
            # Diagnostics must never break the request that produced them.
            log.exception("could not write %s to audit.log", event_name)

    def _record_voice_failure(event_name: str, error: BaseException, **fields: Any) -> None:
        """Write the real cause of a voice failure to audit.log.

        The browser is shown one short line, so without this the actual reason --
        no model downloaded, no route to huggingface.co, no audio decoder -- would
        exist only as a console traceback that scrolls away. Records the exception
        type, message, cause chain and traceback, plus the voice dependencies;
        never the audio, the transcript, or a credential.
        """
        audit: AuditLog | None = app.config["MODEL_AUDIT"]
        if audit is None:
            return
        try:
            audit.record_error(
                event_name,
                error,
                diagnostics=_voice_dependency_dict(app.config["VOICE_SERVICE"]),
                **fields,
            )
        except Exception:
            # A failure to write diagnostics must not replace the failure itself.
            log.exception("could not write voice diagnostics to audit.log")

    # ----- onboarding & account state --------------------------------------
    # The app runs in one of two modes:
    #   * no AccountStore (the default — every test launch): no sign-in wall,
    #     no chrome additions — exactly the single-user app it has always been;
    #   * an AccountStore attached (what run_app does): signed-out visitors
    #     get the marketing landing page and the auth flow, app pages ask for
    #     a sign-in first, and registration lands on the onboarding path.
    ACCOUNTS_PUBLIC_ENDPOINTS = {
        "home", "landing", "static",
        "auth_login", "auth_signup", "auth_logout",
        "auth_verify", "auth_verify_resend",
        "auth_forgot", "auth_reset", "auth_oauth",
    }
    # An unverified account may only reach its verification page (and sign
    # out). Signing up starts a session immediately so the verify screen knows
    # whose email it is confirming.
    ACCOUNTS_PENDING_ENDPOINTS = {
        "static", "auth_verify", "auth_verify_resend", "auth_logout",
    }

    def _accounts() -> AccountStore | None:
        return app.config.get("ACCOUNTS")

    def _signed_in_account() -> dict[str, Any] | None:
        """The account this request's session names (populated by the guard)."""
        account: dict[str, Any] | None = getattr(g, "account", None)
        return account

    def _current_account_id() -> str:
        """The account every store read/write in this request is scoped to.

        The signed-in account when accounts are on; otherwise the seeded
        legacy account (``DEFAULT_USER_ID``) — what the single-user app
        always used, now resolved per request instead of baked into the
        store's method defaults.
        """
        account = _signed_in_account()
        if account is not None:
            return str(account.get("id") or DEFAULT_USER_ID)
        return DEFAULT_USER_ID

    def _scoped_store() -> AccountView:
        return app.config["STORE"].for_account(_current_account_id())

    def _scoped_projects() -> AccountProjects:
        return app.config["PROJECTS"].for_account(_current_account_id())

    # Ownership helpers. Another account's record answers 404 with exactly
    # the unknown-id body/template — never 403, which would confirm the
    # record exists. Each returns (record, None) or (None, 404-response).
    def _owned_project_or_404(project_id: str, *, template: bool = False):
        project = _scoped_projects().get(project_id)
        if project is not None:
            return project, None
        if template:
            return None, (
                render_template("project_not_found.html", project_id=project_id),
                404,
            )
        return None, _json({"error": "unknown project"}, 404)

    def _owned_task_or_404(task_id: str, *, template: bool = False):
        task_obj = _scoped_store().get_task(task_id)
        if task_obj is not None:
            return task_obj, None
        if template:
            return None, (
                render_template("task_not_found.html", task_id=task_id),
                404,
            )
        return None, _json({"error": "unknown task"}, 404)

    def _owned_message_or_404(message_id: str):
        found = _scoped_store().find_message(message_id)
        if found is not None:
            return found, None
        return None, _json({"error": "no message with that id"}, 404)

    @app.before_request
    def _account_guard() -> Response | None:
        store = _accounts()
        if store is None:
            g.account = None
            return None
        account: dict[str, Any] | None = None
        account_id = session.get("account_id", "")
        sid = session.get("sid", "")
        if account_id and sid and store.session_valid(account_id, sid):
            account = store.by_id(account_id)
        if account_id and account is None:
            # The account or session vanished (deleted, password reset, or a
            # "sign out everywhere"): the cookie alone must not keep working.
            session.pop("account_id", None)
            session.pop("sid", None)
        g.account = account
        endpoint = request.endpoint or ""
        if account is None:
            if endpoint in ACCOUNTS_PUBLIC_ENDPOINTS:
                return None
            return redirect(url_for("auth_login", next=request.path))
        if not account.get("email_verified") and endpoint not in ACCOUNTS_PENDING_ENDPOINTS:
            return redirect(url_for("auth_verify"))
        return None

    def _user_view() -> Any:
        """Who the chrome (sidebar, topbar, greetings) says is here: the
        signed-in account when accounts are on, the store's user otherwise.
        Shaped like ``Store.user`` so templates never need to know which."""
        account = _signed_in_account()
        if account is not None:
            return workspace_view(account)
        return app.config["STORE"].user

    def _greeting() -> str:
        account = _signed_in_account()
        if account is None:
            return app.config["STORE"].greeting()
        hour = datetime.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        return f"Good {part}, {account.get('name', '') or 'there'}."

    def _sign_in(account: dict[str, Any]) -> None:
        """Open a tracked browser session for an account and apply the
        preferences it saved (the Security settings page lists these sessions;
        the default response depth follows the account, not the device)."""
        store = _accounts()
        if store is None:
            return
        store.record_login(account["id"])
        sid = store.start_session(account["id"], user_agent=request.headers.get("User-Agent", ""))
        session["account_id"] = account["id"]
        session["sid"] = sid
        g.account = store.by_id(account["id"]) or account
        depth = ((g.account or {}).get("prefs") or {}).get("default_depth", "quick")
        view = app.config["STORE"].for_account(account["id"])
        if depth != view.thinking_level:
            view.set_thinking_level(depth)

    def _sign_out() -> None:
        store = _accounts()
        account_id, sid = session.get("account_id", ""), session.get("sid", "")
        if store is not None and account_id and sid:
            store.end_session(account_id, sid)
        session.pop("account_id", None)
        session.pop("sid", None)
        g.account = None

    def _safe_next(raw: str, default: str = "/dashboard") -> str:
        """Keep ?next= targets on this site — a login link must never become
        an open redirect to somewhere else."""
        text = (raw or "").strip()
        if text.startswith("/") and not text.startswith("//"):
            return text
        return default

    @app.get("/landing")
    def landing() -> str:
        """The marketing page, reachable from inside the app too (footer links).
        Signed-out visits to `/` render this same page — see ``home``."""
        return render_template("landing.html")

    @app.get("/")
    def home() -> str:
        st = _scoped_store()
        if _accounts() is not None and _signed_in_account() is None:
            # The product's front door: signed-out visits get the landing page;
            # the chat home belongs to signed-in sessions.
            return render_template("landing.html")
        return render_template(
            "home.html",
            user=_user_view(),
            greeting=_greeting(),
            mode=st.mode,
            thinking_level=st.thinking_level,
            conversations=st.conversations_grouped(),
            active_conversation_id=st.active_conversation_id,
            active_conversation_title=(
                st.get_conversation(st.active_conversation_id).title
                if st.active_conversation_id is not None
                and st.get_conversation(st.active_conversation_id) is not None
                else PLACEHOLDER_TITLE
            ),
            connections=st.connections,
            connected_count=st.connected_count(),
            thread=st.home_thread,
            # Once a conversation exists its composer is the fixed bottom bar.
            # Only a fresh, empty conversation centers it.
            docked=st.active_conversation_id is not None,
            # The welcome screen belongs to a genuinely empty conversation only.
            # A returning conversation with history gets the thread, never this.
            welcome=st.active_conversation_id is None and not st.home_thread,
            starters=WELCOME_STARTERS,
            followups=followups_for(st.home_thread),
            heartbeat=_heartbeat_view(),
        )

    # ----- Projects and Agents (the ORBIT Projects product) -----

    @app.context_processor
    def _chrome_defaults() -> dict[str, Any]:
        """Template defaults every page needs: the sidebar's RECENT
        PROJECTS list (the three most recently updated, straight from the
        store), plus the signed-in account and whether this launch has
        accounts at all — chrome (feedback widget, settings links) keys off
        those two. A page can override any value explicitly."""
        raw_store = app.config.get("STORE")
        raw_projects = app.config.get("PROJECTS")
        account = _signed_in_account()
        prefs = (account or {}).get("prefs") or {}
        base: dict[str, Any] = {
            "account": account,
            "accounts_enabled": _accounts() is not None,
            "beta_updates_off": bool(account) and not prefs.get("beta_updates", True),
        }
        if raw_store is None or raw_projects is None:
            return {**base, "sidebar_projects": [], "active_project_id": ""}
        recent = raw_projects.for_account(_current_account_id()).list()[:3]
        return {
            **base,
            "sidebar_projects": [{"id": p.id, "name": p.name} for p in recent],
            "active_project_id": "",
        }

    def _sidebar_context() -> dict[str, Any]:
        """What every page that renders the shared sidebar needs."""
        st = _scoped_store()
        return {
            "user": _user_view(),
            "mode": st.mode,
            "thinking_level": st.thinking_level,
            "conversations": st.conversations_grouped(),
            "active_conversation_id": st.active_conversation_id,
            "active_conversation_title": PLACEHOLDER_TITLE,
            "connections": st.connections,
            "connected_count": st.connected_count(),
            "heartbeat": _heartbeat_view(),
        }

    def _date_label(iso: str) -> str:
        """"15 Oct 2026" from the store's UTC timestamp, for card footers."""
        try:
            return datetime.fromisoformat(iso).strftime("%d %b %Y")
        except (TypeError, ValueError):
            return ""

    def _project_card_view(project: Any, st: AccountView) -> dict[str, Any]:
        """One Projects-overview card, per the design frames.

        The description line is the AI summary when the Main Agent has written
        one, and otherwise the description the owner typed — never an invented
        sentence. The footer is real workspace state: the newest task with its
        state, the count of person-marked completes, and how long ago the
        project last changed.
        """
        card = project.card()
        tasks = st.tasks_for_project(project.id)
        done, total = progress(tasks)
        newest = latest_task(tasks)
        card["task_title"] = newest.title if newest is not None else ""
        card["task_state"] = row_state(newest)[1] if newest is not None else ""
        card["tasks_done"] = done
        card["tasks_total"] = total
        card["age"] = human_age(project.updated_at)
        return card

    def _sort_link(base_args: dict[str, str], sort: str) -> str:
        args = {k: v for k, v in base_args.items() if v}
        args["sort"] = sort
        query = "&".join(f"{k}={v}" for k, v in args.items())
        return url_for("projects_page") + (f"?{query}" if query else "")

    SORTS: dict[str, str] = {"recent": "Recent activity", "name": "Name"}

    @app.get("/projects")
    def projects_page() -> str:
        st = _scoped_store()
        project_store = _scoped_projects()
        query = request.args.get("q", "").strip()
        status = request.args.get("status", "").strip() or None
        sort = request.args.get("sort", "").strip() or "recent"
        if sort not in SORTS:
            sort = "recent"
        if status is not None and status not in STATUS_LABELS:
            # A status the filter cannot have produced is ignored, not honoured
            # with an empty grid that looks like lost data.
            status = None
        cards = project_store.list(query=query, status=status)
        if sort == "name":
            cards = sorted(cards, key=lambda p: p.name.lower())
        counts = project_store.counts()
        total = sum(counts.values())
        notice_raw = request.args.get("notice", "").strip()
        notice = None
        if notice_raw == "invalid":
            notice = (
                "A project needs a name (80 characters or fewer). "
                "The description is optional, up to 600 characters."
            )
        base_args = {"q": query, "status": status or ""}
        sort_links = [(key, label, _sort_link(base_args, key)) for key, label in SORTS.items()]
        return render_template(
            "projects.html",
            projects=[_project_card_view(p, st) for p in cards],
            counts=counts,
            total=total,
            building=counts.get("building", 0),
            statuses=[(key, STATUS_LABELS[key]) for key in STATUSES],
            query=query,
            active_status=status,
            active_sort=sort,
            sort_links=sort_links,
            notice=notice,
            projects_active=True,
            agents_active=False,
            **_sidebar_context(),
        )

    @app.get("/create")
    def create_page() -> str:
        """Start a project: the idea form plus the import entry points.

        Import rows render because the design names them, and they sit
        disabled because no repository provider is connected and file import
        is not built — the row says so instead of silently failing later.
        """
        st = _scoped_store()
        project_store = _scoped_projects()
        notice = None
        if request.args.get("notice", "").strip() == "invalid":
            notice = (
                "A project needs a name (80 characters or fewer). "
                "The description is optional, up to 600 characters."
            )
        projects = project_store.list()
        latest = projects[0] if projects else None
        # The continue strip: the project you touched last, its newest task,
        # and when it changed. Rendered only when there is a project at all.
        continue_strip = None
        if latest is not None:
            newest = latest_task(st.tasks_for_project(latest.id))
            continue_strip = {
                "id": latest.id,
                "name": latest.name,
                "task_title": newest.title if newest is not None else "No tasks yet",
                "age": human_age(latest.updated_at),
            }
        return render_template(
            "create.html",
            notice=notice,
            continue_strip=continue_strip,
            repo_sources=[(key, SOURCE_LABELS[key]) for key in ("github", "gitlab", "bitbucket")],
            projects_active=True,
            agents_active=False,
            **_sidebar_context(),
        )

    @app.post("/projects")
    def create_project() -> Response:
        st = _scoped_store()
        project_store = _scoped_projects()
        # Only the idea path creates anything today: source is always "new",
        # because every import entry point renders disabled on purpose. The
        # four modes are the project's entry story, not different storage:
        #   blank    — create directly, open the empty workspace;
        #   guided   — the /create stepper reviewed it; may carry a first task;
        #   describe — an idea the Main Agent will help plan (composer primed);
        #   plan     — same landing as describe, from the classic radio choice.
        mode = (request.form.get("mode") or "blank").strip()
        try:
            project = project_store.create(
                name=request.form.get("name", ""),
                description=request.form.get("description", ""),
                source="new",
            )
        except ValueError:
            _record_event("project_create_rejected", reason="invalid_input")
            return redirect(url_for("create_page", notice="invalid"), code=303)
        if mode not in ("plan", "blank", "guided", "describe"):
            mode = "blank"
        first_task = " ".join((request.form.get("first_task") or "").split())
        if mode == "guided" and first_task:
            # The guided flow's suggested first task is real: it lands on the
            # new project's map before the workspace even opens.
            st.create_task(request=first_task, project_id=project.id)
        _record_event(
            "project_created",
            project_id=project.id,
            source=project.source,
            status=project.status,
            mode=mode,
        )
        # "Describe with the Main Agent" lands in the workspace with the
        # composer primed to brief the agent; the others open the plain
        # workspace (guided carries its plan hint).
        target = url_for("project_workspace", project_id=project.id)
        if mode in ("plan", "describe"):
            target += "?plan=1#agent"
        elif mode == "guided":
            target += "?guided=1"
        return redirect(target, code=303)

    @app.get("/agents")
    def agents_page() -> str:
        st = _scoped_store()
        project_store = _scoped_projects()
        query = request.args.get("q", "").strip()
        selected = None
        if st.selected_agent:
            try:
                selected = get_agent(st.selected_agent)
            except KeyError:
                # A selection pointing nowhere clears itself rather than
                # showing a bar that names a specialty that does not exist.
                st.selected_agent = None
        assign_targets = [{"id": p.id, "name": p.name} for p in project_store.list()]
        return render_template(
            "agents.html",
            agents=search_agents(query),
            query=query,
            total=len(agent_types()),
            selected_agent=selected,
            assign_targets=assign_targets,
            projects_active=False,
            agents_active=True,
            **_sidebar_context(),
        )

    @app.post("/agents/<agent_id>/select")
    def select_agent(agent_id: str) -> Response:
        """Mark a specialty as picked — the selection bar follows."""
        st = _scoped_store()
        try:
            get_agent(agent_id)
        except KeyError:
            return _json({"error": f"unknown agent type: {agent_id!r}"}, 404)
        st.selected_agent = agent_id
        _record_event("agent_specialist_selected", agent_id=agent_id)
        return redirect(url_for("agents_page", q=request.form.get("q", "")) + "#selection", code=303)

    @app.post("/agents/deselect")
    def deselect_agent() -> Response:
        st = _scoped_store()
        st.selected_agent = None
        return redirect(url_for("agents_page"), code=303)

    @app.post("/agents/<agent_id>/assign")
    @ownership_checked("agent_id (catalog)", "project_id (form)")
    def assign_agent(agent_id: str) -> Response:
        """Assign the selected specialty to a project: the conversation is
        created and the workspace opens with that specialist's panel."""
        st = _scoped_store()
        try:
            agent = get_agent(agent_id)
        except KeyError:
            return _json({"error": f"unknown agent type: {agent_id!r}"}, 404)
        project_id = (request.form.get("project_id") or "").strip()
        _, not_owned = _owned_project_or_404(project_id)
        if not_owned is not None:
            return not_owned
        st.project_conversation(project_id, create=True, agent_id=agent.id)
        st.selected_agent = None
        _record_event(
            "agent_specialist_assigned", agent_id=agent.id, project_id=project_id
        )
        return redirect(
            url_for("project_workspace", project_id=project_id, agent=agent.id) + "#agent",
            code=303,
        )

    # ----- the project workspace (map + task details + Main Agent) -----

    @app.get("/projects/<project_id>")
    @ownership_checked("project_id", "task (query)")
    def project_workspace(project_id: str) -> str:
        st = _scoped_store()
        project, not_owned = _owned_project_or_404(project_id, template=True)
        if not_owned is not None:
            return not_owned
        tasks = st.tasks_for_project(project_id)
        # Task Details belongs to a task of THIS project; an id that is not
        # one is ignored rather than shown (the map may only open its own).
        selected_id = request.args.get("task", "").strip()
        selected = st.get_task(selected_id) if selected_id else None
        if selected is not None and selected.project_id != project_id:
            selected = None
        panel = _panel_state(selected) if selected is not None else None
        # Which agent's conversation the rail shows. "main" by default; a
        # specialist id is honoured only when the specialty exists — an id the
        # Agents page could not have produced falls back to the Main Agent.
        agent_id = request.args.get("agent", "").strip() or MAIN_AGENT_ID
        if agent_id != MAIN_AGENT_ID:
            try:
                get_agent(agent_id)
            except KeyError:
                agent_id = MAIN_AGENT_ID
        thread = st.project_thread(project_id, agent_id=agent_id)
        speaker = (
            "Main Agent" if agent_id == MAIN_AGENT_ID else get_agent(agent_id).name
        )
        # The conversations this project carries, so the rail can offer
        # exactly the agents that have one here — nothing speculative, and a
        # conversation whose specialty no longer exists simply drops out.
        rail_agents: list[dict[str, str]] = []
        seen: set[str] = set()
        for conv in st.project_conversations(project_id):
            cid = conv.agent_id or MAIN_AGENT_ID
            if cid in seen:
                continue
            name = (
                "Main Agent"
                if cid == MAIN_AGENT_ID
                else (get_agent(cid).name if cid in {a.id for a in agent_types()} else None)
            )
            if name is None:
                continue
            seen.add(cid)
            rail_agents.append({"id": cid, "name": name})
        if MAIN_AGENT_ID not in seen:
            rail_agents.insert(0, {"id": MAIN_AGENT_ID, "name": "Main Agent"})
        builds_on = (
            st.get_task(selected.builds_on)
            if selected is not None and selected.builds_on
            else None
        )
        done_tasks, total_tasks = progress(tasks)
        stage_names = [s["name"] for s in map_stages(tasks)]
        view = request.args.get("view", "").strip()
        if view not in ("", "overview"):
            view = ""
        return render_template(
            "project.html",
            project=project.card(),
            project_obj=project,
            created_label=_date_label(project.created_at),
            updated_age=human_age(project.updated_at),
            statuses=[(key, STATUS_LABELS[key]) for key in STATUSES],
            map=map_stages(tasks),
            done_tasks=done_tasks,
            total_tasks=total_tasks,
            tasks=tasks,
            pin_tasks=[
                {"id": t.id, "title": t.title, "state_label": row_state(t)[1]}
                for t in tasks
            ],
            stage_names=stage_names,
            selected=selected,
            selected_state=(row_state(selected) if selected is not None else None),
            builds_on=builds_on if builds_on is not None and builds_on.project_id == project_id else None,
            next_task=next_up(tasks, selected) if selected is not None else None,
            panel=panel,
            thread=thread,
            followups=followups_for(thread),
            agent_id=agent_id,
            speaker=speaker,
            rail_agents=rail_agents,
            starters=(
                get_agent(agent_id).starters if agent_id != MAIN_AGENT_ID else PROJECT_STARTERS
            ),
            plan_hint=request.args.get("plan", "") == "1",
            view=view,
            discuss_task=request.args.get("task", "").strip() and selected or None,
            projects_active=True,
            agents_active=False,
            active_project_id=project_id,
            **_sidebar_context(),
        )

    @app.post("/projects/<project_id>/status")
    @ownership_checked("project_id")
    def project_set_status(project_id: str) -> Response:
        project_store = _scoped_projects()
        project, not_owned = _owned_project_or_404(project_id)
        if not_owned is not None:
            return not_owned
        status = (request.form.get("status") or "").strip()
        try:
            updated = project_store.set_status(project_id, status)
        except ValueError:
            return _json({"error": f"unknown status: {status!r}"}, 400)
        if updated is not None and updated.status != project.status:
            _record_event(
                "project_status_changed",
                project_id=project_id,
                from_status=project.status,
                to_status=updated.status,
            )
        return redirect(url_for("project_workspace", project_id=project_id), code=303)

    @app.post("/projects/<project_id>/tasks")
    @ownership_checked("project_id", "builds_on (form)")
    def project_new_task(project_id: str) -> Response:
        st = _scoped_store()
        _, not_owned = _owned_project_or_404(project_id)
        if not_owned is not None:
            return not_owned
        builds_on = (request.form.get("builds_on") or "").strip() or None
        try:
            task = st.create_task(
                request.form.get("request", ""),
                project_id=project_id,
                stage=request.form.get("stage", "") or None,
                builds_on=builds_on,
            )
        except ValueError:
            # An empty request or a builds_on that names no real task: the
            # page re-renders with exactly the form data; nothing half-baked
            # is filed on the map.
            return redirect(
                url_for("project_workspace", project_id=project_id) + "#add-task",
                code=303,
            )
        return redirect(
            url_for("project_workspace", project_id=project_id, task=task.id) + "#map",
            code=303,
        )

    @app.post("/projects/<project_id>/tasks/<task_id>/complete")
    @ownership_checked("project_id", "task_id")
    def project_task_complete(project_id: str, task_id: str) -> Response:
        """Toggle completion on a map task. The person's assertion, audited."""
        st = _scoped_store()
        _, not_owned = _owned_project_or_404(project_id)
        if not_owned is not None:
            return not_owned
        task, not_owned = _owned_task_or_404(task_id)
        if not_owned is not None:
            return not_owned
        assert task is not None
        if task.project_id != project_id:
            return _json({"error": "unknown task"}, 404)
        updated = st.set_done(task_id, done=not task.done)
        assert updated is not None
        _record_event(
            "task_completion",
            task_id=task_id,
            project_id=project_id,
            state="complete" if updated.done else "reopened",
        )
        keep_agent = request.form.get("agent", "").strip() or MAIN_AGENT_ID
        return redirect(
            url_for(
                "project_workspace",
                project_id=project_id,
                task=task_id,
                agent=keep_agent,
            )
            + "#details",
            code=303,
        )

    def _agent_label(agent_id: str) -> str:
        return "Main Agent" if agent_id == MAIN_AGENT_ID else get_agent(agent_id).name

    @app.post("/api/project/<project_id>/stream")
    @ownership_checked("project_id", "task_id (body)")
    def api_project_stream(project_id: str) -> Response:
        """A project conversation, streamed: the Main Agent or an assigned specialty.

        Same seam, gating and audit as the home chat; the differences are the
        project-scoped conversation it appends to and the project context in
        the system prompt. ``agent`` (payload or query) picks the conversation
        and its instructions; ``task_id`` pins the discussion to a map task
        whose real state is joined to the prompt. Tools are not offered here:
        the home chat is where the six gated tools live, and a project
        conversation is not where a note/heartbeat card belongs.
        """
        st = _scoped_store()
        project_store = _scoped_projects()
        project, not_owned = _owned_project_or_404(project_id)
        if not_owned is not None:
            return not_owned
        payload = request.get_json(silent=True) or {}
        message = payload.get("message")
        message = message.strip() if isinstance(message, str) else ""
        if not message:
            return _json({"error": "message is required"}, 400)
        agent_id = (payload.get("agent") or request.args.get("agent") or "").strip()
        agent_id = agent_id or MAIN_AGENT_ID
        if agent_id != MAIN_AGENT_ID:
            try:
                get_agent(agent_id)
            except KeyError:
                return _json({"error": f"unknown agent type: {agent_id!r}"}, 404)
        # Optional task pin: the subject of the discussion. A task of another
        # project — or of another account, which the scoped get already
        # hides — is refused with the same body: the "Now discussing" strip
        # may never name work this account's map cannot show.
        pinned_id = (payload.get("task_id") or "").strip()
        pinned = st.get_task(pinned_id) if pinned_id else None
        if pinned_id and (pinned is None or pinned.project_id != project_id):
            return _json({"error": "task does not belong to this project"}, 404)

        from app.store import ChatMessage as StoreChatMessage

        conv = st.project_conversation(project_id, create=True, agent_id=agent_id)
        assert conv is not None
        conv_id = conv.id
        sent_at = now_label()
        st.append_message(
            conv_id,
            StoreChatMessage(who="You", time=sent_at, body=message, is_orbit=False),
        )
        _record_event(
            "chat_message_received",
            conversation_id=conv_id,
            surface="project",
            project_id=project_id,
            agent_id=agent_id,
            message_id=st.thread(conv_id)[-1].id,
            chars=len(message),
        )
        history = _history_from_thread(st.thread(conv_id))
        tasks = st.tasks_for_project(project_id)
        task_block = ""
        if pinned is not None:
            pin_lines = [
                "TASK THE PERSON IS DISCUSSING RIGHT NOW",
                f"Title: {pinned.title}",
                f"Request: {pinned.description}",
                f"State: {row_state(pinned)[1]}",
            ]
            pin_steps = list(pinned.run.steps) if pinned.run is not None else []
            if pin_steps:
                pin_lines.append("Plan steps so far:")
                pin_lines.extend(
                    f"- [{'x' if step.status == 'done' else ' '}] {step.title}"
                    for step in pin_steps
                )
            task_block = "\n".join(pin_lines) + "\n\n"
        if agent_id == MAIN_AGENT_ID:
            base_prompt = f"{MAIN_AGENT_PROMPT}\n\n{_project_context(project, tasks)}"
        else:
            base_prompt = (
                f"{get_agent(agent_id).instructions}\n\n"
                f"PROJECT YOU ARE ASSIGNED TO\n{_project_context(project, tasks)}"
            )
        if task_block:
            base_prompt = f"{task_block}{base_prompt}"
        system_prompt = with_current_time(app.config["FACTS"].system_prompt(base_prompt))
        # The Chat/Agent mode and Quick/Deep choices are the person's global
        # ones; the workspace does not fork them.
        think = st.mode == "agent"
        thinking_level = st.thinking_level
        first_exchange = not any(m.is_orbit for m in st.thread(conv_id))

        def chunk_source() -> Iterator[StreamChunk]:
            return get_provider().reply_stream(
                history,
                system_prompt,
                think=think,
                report_attempts=think,
                thinking_level=thinking_level,
            )

        def on_done(answer: str) -> str | None:
            stored_id: str | None = None
            if answer.strip():
                stored = StoreChatMessage(
                    who="Orbit",
                    time=now_label(),
                    body=answer,
                    is_orbit=True,
                    speaker=_agent_label(agent_id),
                )
                st.append_message(conv_id, stored)
                stored_id = stored.id
                # The AI summary, once per project, from the first completed
                # exchange — the titles.py pattern. A failed call writes
                # nothing and the card keeps its honest fallback.
                if first_exchange and not project.summary:
                    summary = request_summary(
                        get_provider(),
                        project.name,
                        project.description,
                        message,
                        answer,
                    )
                    if summary:
                        project_store.set_summary(project_id, summary)
                        _record_event(
                            "project_summary_written",
                            project_id=project_id,
                            chars=len(summary),
                        )
            return stored_id

        def after_done(answer: str) -> list[tuple[str, str]] | None:
            if not answer.strip():
                return None
            chips = followups_for(st.thread(conv_id))
            if not chips:
                return None
            return [("followups", json.dumps([chip.as_dict() for chip in chips]))]

        return _sse_response(
            chunk_source,
            on_done,
            after_done=after_done,
        )

    @app.get("/task/<task_id>")
    @ownership_checked("task_id")
    def task(task_id: str) -> str:
        st = _scoped_store()
        task_obj, not_owned = _owned_task_or_404(task_id, template=True)
        if not_owned is not None:
            return not_owned
        assert task_obj is not None
        # The opening update is a real model call. Re-asking for it on every
        # page load would spend a call per refresh and append a duplicate
        # message to the thread, so it runs only while the task has no Orbit
        # reply yet.
        stream_initial = not any(m.is_orbit for m in task_obj.chat)
        if stream_initial and task_obj.run is None:
            # Plan the run now so the panel is complete on the first paint. This
            # builds an object from the request text — it is not a model call,
            # and it does not claim any step has happened.
            task_obj.run = new_run(task_obj.description)
        return render_template(
            "task.html",
            user=st.user,
            mode=st.mode,
            thinking_level=st.thinking_level,
            task=task_obj,
            stream_initial=stream_initial,
            panel=_panel_state(task_obj),
            heartbeat=_heartbeat_view(),
            connections=st.connections,
            connected_count=st.connected_count(),
            conversations=st.conversations_grouped(),
            active_conversation_id=st.active_conversation_id,
        )

    @app.post("/api/tasks")
    def api_new_task() -> Response:
        """Create an Agent task from a request the person typed."""
        st = _scoped_store()
        payload = request.get_json(silent=True) or {}
        request_text = payload.get("request")
        request_text = request_text.strip() if isinstance(request_text, str) else ""
        if not request_text:
            return _json({"error": "a task needs a request to work from."}, 400)
        task = st.create_task(request_text)
        return _json({"id": task.id, "title": task.title})

    @app.post("/api/mode")
    def set_mode() -> Response:
        st = _scoped_store()
        payload = request.get_json(silent=True) or {}
        mode = payload.get("mode")
        try:
            st.set_mode(mode)
        except ValueError as exc:
            return Response(json.dumps({"error": str(exc)}), status=400, mimetype="application/json")
        body: dict[str, Any] = {"mode": st.mode}
        if st.mode == "agent":
            # Agent mode opens a task page. The server names which one —
            # the account's own example task, created on demand — so the
            # frontend never hardcodes an id that may be another
            # account's (the old "/task/T1" 404s for signed-in accounts).
            task_obj = st.agent_task()
            body["task_id"] = task_obj.id
            body["task_url"] = url_for("task", task_id=task_obj.id)
        return Response(json.dumps(body), mimetype="application/json")

    @app.post("/api/thinking-level")
    def set_thinking_level() -> Response:
        st = _scoped_store()
        payload = request.get_json(silent=True) or {}
        try:
            st.set_thinking_level(payload.get("thinking_level"))
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        return _json({"thinking_level": st.thinking_level})

    @app.post("/api/voice/transcribe")
    def api_voice_transcribe() -> Response:
        upload = request.files.get("audio")
        if upload is None:
            return _json({"error": "audio recording is required"}, 400)
        mime_type = (upload.mimetype or "").lower().split(";", 1)[0].strip()
        extension = _AUDIO_SUFFIX_BY_MIME.get(mime_type)
        if extension is None:
            return _json({"error": "unsupported audio format"}, 415)
        audio = upload.read()
        if not audio:
            return _json({"error": "the recording is empty"}, 400)
        failure_fields = {
            "endpoint": "/api/voice/transcribe",
            "mime_type": mime_type,
            "extension": extension,
            "audio_bytes": len(audio),
        }
        try:
            transcript = app.config["VOICE_SERVICE"].transcribe(audio, extension=extension)
        except VoiceServiceUnavailable as exc:
            # The service already logged the traceback next to the cause it found;
            # this adds the request context, and the reason (missing model,
            # unreachable Hub, missing decoder) also goes to the browser.
            log.error("voice transcription unavailable: %s", _failure_reason(exc))
            _record_voice_failure("voice_transcription_unavailable", exc, **failure_fields)
            return _json(
                {
                    "error": "Voice transcription is unavailable right now.",
                    "reason": _failure_reason(exc),
                },
                503,
            )
        except ValueError as exc:
            # The upload itself is unusable (empty, undecodable): the message already
            # names the cause, so this is not an audit-worthy server failure.
            return _json({"error": str(exc)}, 400)
        except Exception as exc:
            log.exception("voice transcription failed")
            _record_voice_failure("voice_transcription_failed", exc, **failure_fields)
            return _json(
                {
                    "error": "Could not transcribe that recording. Please try again.",
                    "reason": _failure_reason(exc),
                },
                500,
            )
        if not transcript:
            return _json({"error": "No speech was detected. Try speaking a little closer to the mic."}, 422)
        return _json({"transcript": transcript})

    @app.post("/api/voice/speech")
    def api_voice_speech() -> Response:
        payload = request.get_json(silent=True) or {}
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _json({"error": "text is required"}, 400)
        text = text.strip()
        if len(text) > MAX_SPEECH_TEXT_CHARS:
            return _json({"error": "That reply is too long to synthesize as one audio clip."}, 413)
        try:
            chunks = iter(app.config["VOICE_SERVICE"].stream_speech(text))
            first_chunk = next(chunks)
        except VoiceServiceUnavailable as exc:
            # Logged in full where the cause was found (see app/voice.py).
            log.error("voice synthesis unavailable: %s", _failure_reason(exc))
            _record_voice_failure(
                "voice_synthesis_unavailable", exc, endpoint="/api/voice/speech"
            )
            return _json(
                {
                    "error": "Spoken replies are unavailable right now.",
                    "reason": _failure_reason(exc),
                },
                503,
            )
        except StopIteration:
            _record_voice_failure(
                "voice_synthesis_failed",
                RuntimeError("the speech service produced no audio (StopIteration)"),
                endpoint="/api/voice/speech",
            )
            return _json({"error": "The speech service returned no audio."}, 502)
        except Exception as exc:
            log.exception("voice synthesis failed before audio started")
            _record_voice_failure(
                "voice_synthesis_failed", exc, endpoint="/api/voice/speech"
            )
            return _json(
                {
                    "error": "Could not generate spoken audio. Please try again.",
                    "reason": _failure_reason(exc),
                },
                502,
            )

        @stream_with_context
        def audio_stream() -> Iterator[bytes]:
            yield first_chunk
            try:
                yield from chunks
            except Exception:
                # HTTP headers and the first MP3 bytes are already on the wire;
                # log a truncated upstream stream rather than leaking details.
                log.exception("voice synthesis stream ended unexpectedly")

        return Response(
            audio_stream(),
            mimetype="audio/mpeg",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/conversations/new")
    def api_new_conversation() -> Response:
        st = _scoped_store()
        st.new_conversation()
        return Response(json.dumps({"active_conversation_id": None}), mimetype="application/json")

    @app.post("/api/chat")
    @ownership_checked("message_id (body)")
    def api_chat() -> Response:
        st = _scoped_store()
        payload = request.get_json(silent=True) or {}
        regenerate = payload.get("regenerate") is True
        message = payload.get("message")
        message = message.strip() if isinstance(message, str) else ""
        from app.store import ChatMessage as StoreChatMessage

        created = None
        sent_at = now_label()
        # Which stored reply this turn rewrites, when regenerating. The by-id
        # path rewrites the message in place; the legacy newest-reply path pops
        # it and lets the new answer take its place.
        in_place_message: Any | None = None
        if regenerate:
            conv_id = st.active_conversation_id
            thread = st.thread(conv_id or "")
            requested_id = payload.get("message_id")
            requested_id = requested_id.strip() if isinstance(requested_id, str) else ""
            if requested_id:
                # Per-message regenerate: the row asked for it by id, so it can be
                # any Orbit reply in the conversation, not only the newest one.
                index = next(
                    (i for i, item in enumerate(thread) if item.id == requested_id), None
                )
                if index is None or not thread[index].is_orbit or thread[index].action is not None:
                    return Response(
                        json.dumps({"error": "no reply with that id to regenerate"}),
                        status=409,
                        mimetype="application/json",
                    )
                previous_user = next(
                    (item for item in reversed(thread[:index]) if not item.is_orbit), None
                )
                if previous_user is None:
                    return Response(
                        json.dumps({"error": "that reply has no user message to retry"}),
                        status=409,
                        mimetype="application/json",
                    )
                # The reply stays where it is and is rewritten in place. Later
                # turns are left alone: replacing one answer is not licence to
                # delete the conversation that followed it.
                in_place_message = thread[index]
                message = previous_user.body
            else:
                if conv_id is None or not thread or not thread[-1].is_orbit or thread[-1].action is not None:
                    return Response(
                        json.dumps({"error": "there is no completed response to regenerate"}),
                        status=409,
                        mimetype="application/json",
                    )
                previous_user = next((item for item in reversed(thread[:-1]) if not item.is_orbit), None)
                if previous_user is None:
                    return Response(
                        json.dumps({"error": "the last response has no user message to retry"}),
                        status=409,
                        mimetype="application/json",
                    )
                # Replace only the latest Orbit reply. The original user message
                # remains in history and no duplicate turn/check-in is created.
                # It is removed here and the new one takes its place.
                thread.pop()
                message = previous_user.body
        else:
            if not message:
                return Response(json.dumps({"error": "message is required"}), status=400, mimetype="application/json")
            # The first send in a fresh conversation creates it. Every event of
            # this turn is tied to this id, so switching conversations mid-stream
            # cannot file the reply under the wrong one.
            if st.active_conversation_id is None:
                created = st.start_conversation()
            conv_id = st.active_conversation_id
            assert conv_id is not None
            st.append_message(
                conv_id,
                StoreChatMessage(who="You", time=sent_at, body=message, is_orbit=False),
            )

        assert conv_id is not None
        if not regenerate:
            # One audit line per accepted message. The Analytics view counts
            # these for "messages per day"; the text itself is never recorded.
            _record_event(
                "chat_message_received",
                conversation_id=conv_id,
                surface="home",
                message_id=st.thread(conv_id)[-1].id if st.thread(conv_id) else None,
                chars=len(message),
            )
            # Heartbeat's check-in: a user message counts, a retry does not.
            hb = _heartbeat_or_none()
            if hb is not None:
                try:
                    hb.record_checkin()
                except RuntimeError:
                    log.exception("could not record check-in; the chat message was still saved")
        # Regenerating a reply the person is looking at, rather than the newest
        # one, rebuilds the same history that produced it: the turns up to and
        # including that reply's user message. Later turns are not sent, so the
        # new answer is not influenced by what followed it on screen.
        if in_place_message is not None:
            regenerated_index = next(
                i for i, item in enumerate(st.thread(conv_id)) if item is in_place_message
            )
            history = _history_from_thread(st.thread(conv_id)[:regenerated_index])
        else:
            history = _history_from_thread(st.thread(conv_id))

        # Tools are offered in Chat mode only, and only when a queue is attached.
        queue: PendingActions | None = app.config["ACTIONS"]
        use_tools = queue is not None and st.mode == "chat"
        thinking_level = st.thinking_level
        # A conversation started from an Agents-page card runs under that
        # card's brief instead of the plain home prompt. Everything else about
        # the turn — tools, gating, history, audit — is identical.
        base_prompt = HOME_SYSTEM_PROMPT
        conv = st.get_conversation(conv_id)
        if conv is not None and conv.agent_id != MAIN_AGENT_ID:
            try:
                base_prompt = get_agent(conv.agent_id).instructions
            except KeyError:
                log.warning(
                    "conversation %s has unknown agent_id %r; using the main prompt",
                    conv_id,
                    conv.agent_id,
                )
        home_prompt = base_prompt + (f" {TOOL_GUIDANCE}" if use_tools else "")
        # Read the facts now, once per turn, so an edit lands on the next reply.
        if regenerate:
            home_prompt = f"{home_prompt} {REGENERATE_INSTRUCTION}"
        system_prompt = with_current_time(app.config["FACTS"].system_prompt(home_prompt))
        cards: list[ActionCard] = []

        def chunk_source() -> Iterator[StreamChunk | ToolProposal]:
            provider = get_provider()
            if use_tools:
                return provider.reply_stream(
                    history,
                    system_prompt,
                    think=False,
                    allow_tools=True,
                    thinking_level=thinking_level,
                )
            return provider.reply_stream(
                history,
                system_prompt,
                think=False,
                thinking_level=thinking_level,
            )

        def on_proposal(proposal: ToolProposal) -> tuple[str, str]:
            """Queue the call for approval. Nothing runs here."""
            assert queue is not None
            try:
                action = queue.propose(
                    proposal.name, dict(proposal.arguments), account_id=st.account_id
                )
            except ProposalRejected as exc:
                return "notice", str(exc)
            card = ActionCard(id=action.id, summary=action.summary)
            cards.append(card)
            return "action", json.dumps({"id": card.id, "summary": card.summary, "state": card.state})

        def on_done(answer: str) -> str | None:
            """Store the reply (in place when regenerating) and return its id."""
            stored_id: str | None = None
            if answer.strip():
                if in_place_message is not None:
                    # Rewritten where it stands, so the screen and the store agree
                    # and the reply keeps its position in the conversation.
                    st.replace_message(conv_id, in_place_message.id, answer)
                    stored_id = in_place_message.id
                else:
                    stored = StoreChatMessage(
                        who="Orbit", time=sent_at, body=answer, is_orbit=True
                    )
                    st.append_message(conv_id, stored)
                    stored_id = stored.id
            # Cards follow the answer they came with, in the order they were proposed.
            for card in cards:
                st.append_message(
                    conv_id,
                    StoreChatMessage(who="Orbit", time=sent_at, body=card.summary, is_orbit=True, action=card),
                )
            return stored_id

        def after_done(answer: str) -> list[tuple[str, str]] | None:
            """Name the conversation, then offer follow-ups this reply supports.

            Both events are sent after "done" so neither can hold up the reply.
            Follow-ups come from the reply that was just stored (see
            app.followups): a chip is only offered for something really in it,
            and an empty or failed turn offers none.
            """
            events: list[tuple[str, str]] = []
            conv = st.get_conversation(conv_id)
            if answer.strip() and conv is not None and not conv.titled:
                conv.titled = True  # one attempt only, success or not
                title = request_title(get_provider(), message, answer)
                st.set_title(conv_id, title)
                events.append(("title", json.dumps({"id": conv_id, "title": title})))
            if answer.strip():
                chips = followups_for(st.home_thread)
                if chips:
                    events.append(("followups", json.dumps([chip.as_dict() for chip in chips])))
            return events or None

        start_event = None
        if created is not None:
            start_event = ("conversation", json.dumps({"id": conv_id, "title": PLACEHOLDER_TITLE}))
        return _sse_response(
            chunk_source,
            on_done,
            start_event=start_event,
            after_done=after_done,
            on_proposal=on_proposal if use_tools else None,
        )

    @app.get("/api/heartbeat")
    def api_heartbeat() -> Response:
        hb = _heartbeat_or_none()
        if hb is None:
            return _json({"error": "heartbeat is not running"}, 503)
        return _json(hb.snapshot())

    @app.post("/api/heartbeat/pause")
    def api_heartbeat_pause() -> Response:
        hb = _heartbeat_or_none()
        if hb is None:
            return _json({"error": "heartbeat is not running"}, 503)
        payload = request.get_json(silent=True) or {}
        # Strict, like the approval gate: only a literal true or false is accepted.
        paused = payload.get("paused")
        if not isinstance(paused, bool):
            return _json({"error": "paused must be true or false"}, 400)
        return _json({"paused": hb.set_paused(paused, source="settings")})

    @app.post("/api/heartbeat/notices/<notice_id>/dismiss")
    def api_heartbeat_dismiss(notice_id: str) -> Response:
        hb = _heartbeat_or_none()
        if hb is None:
            return _json({"error": "heartbeat is not running"}, 503)
        if not hb.dismiss(notice_id, source="card"):
            return _json({"error": "no open notice with that id"}, 404)
        return _json({"dismissed": True})

    @app.post("/api/heartbeat/check")
    def api_heartbeat_check() -> Response:
        """Run both checks now, because the person asked. Quiet hours do not apply."""
        hb = _heartbeat_or_none()
        if hb is None:
            return _json({"error": "heartbeat is not running"}, 503)
        result = hb.check_now()
        return _json({"status": result["status"], "fired": [n["id"] for n in result["fired"]]})

    @app.post("/api/actions/<action_id>/<decision>")
    @ownership_checked("action_id")
    def api_action_decide(action_id: str, decision: str) -> Response:
        """Approve or decline one proposed tool call. Both outcomes are audited by the registry."""
        st = _scoped_store()
        queue: PendingActions | None = app.config["ACTIONS"]
        if queue is None:
            return _json({"error": "tools are not enabled"}, 503)
        if decision not in ("approve", "decline"):
            return _json({"error": "decision must be approve or decline"}, 404)
        # Ownership before execution: only the account whose conversation
        # proposed the call may decide it. A foreign id answers like a spent
        # one and is NOT consumed — the proposal stays its owner's to decide.
        owner = queue.owner_of(action_id)
        if owner and owner != st.account_id:
            return _json({"error": "This request is no longer pending. Nothing ran."}, 404)
        try:
            outcome = queue.decide(action_id, approved=decision == "approve")
        except KeyError:
            return _json({"error": "This request is no longer pending. Nothing ran."}, 404)
        if decision == "decline":
            state, text = "declined", "Declined. Nothing was executed."
        else:
            text = _outcome_text(outcome.result)
            state = "approved" if outcome.execution == "succeeded" else "failed"
        st.resolve_action(action_id, state, text)
        return _json({"state": state, "result": text})

    @app.post("/api/messages/<message_id>/rating")
    @ownership_checked("message_id")
    def api_message_rating(message_id: str) -> Response:
        """Record one thumbs up/down against one message. No form, no follow-up.

        One binary line in audit.log per accepted click: message id, rating and
        the log's own timestamp. The client does not re-send the rating that is
        already showing, so switching from up to down leaves one line for each.
        """
        st = _scoped_store()
        payload = request.get_json(silent=True) or {}
        rating = payload.get("rating")
        if rating not in RATING_LABELS:
            return _json({"error": "rating must be 'up' or 'down'"}, 400)
        found, not_owned = _owned_message_or_404(message_id)
        if not_owned is not None:
            return not_owned
        assert found is not None
        owner_id, message = found
        _record_event(
            "message_rated",
            message_id=message_id,
            rating=rating,
            # Which surface and which conversation/task it belongs to, so a rating
            # can be traced back to the exchange it is about.
            surface="task" if st.get_task(owner_id) is not None else "home",
            owner_id=owner_id,
            reply_chars=len(message.body or ""),
        )
        return _json({"message_id": message_id, "rating": rating})

    @app.get("/analytics")
    def analytics() -> str:
        """The Analytics view: audit.log read back, nothing tracked separately."""
        st = _scoped_store()
        audit: AuditLog | None = app.config["MODEL_AUDIT"]
        # The same file the rest of the app logs to, so the view cannot drift
        # onto a different path from the records it counts.
        log_path = audit.path if audit is not None else DEFAULT_AUDIT_LOG_PATH
        report = build_analytics(log_path)
        return render_template(
            "analytics.html",
            report=report,
            user=st.user,
            mode=st.mode,
            thinking_level=st.thinking_level,
            conversations=st.conversations_grouped(),
            active_conversation_id=st.active_conversation_id,
            active_conversation_title=PLACEHOLDER_TITLE,
            connections=st.connections,
            connected_count=st.connected_count(),
            heartbeat=_heartbeat_view(),
        )

    @app.post("/api/task/<task_id>/stream")
    @ownership_checked("task_id")
    def api_task_stream(task_id: str) -> Response:
        st = _scoped_store()
        task_obj, not_owned = _owned_task_or_404(task_id)
        if not_owned is not None:
            return not_owned
        assert task_obj is not None

        payload = request.get_json(silent=True) or {}
        message = payload.get("message")
        message = message.strip() if isinstance(message, str) else ""
        initial = bool(payload.get("initial", False))
        regenerate = payload.get("regenerate") is True

        from app.store import ChatMessage as StoreChatMessage

        extra_user: str | None = None
        sent_at = now_label()  # message start time, shown under both bubbles
        in_place_message: Any | None = None
        # Where the retry's history stops, when the reply is not the newest one.
        history_end: int | None = None
        if regenerate:
            requested_id = payload.get("message_id")
            requested_id = requested_id.strip() if isinstance(requested_id, str) else ""
            if requested_id:
                # Per-message regenerate in the task chat: the reply named by the
                # row is rewritten where it stands, and the turns before it are
                # the history this retry is built from.
                index = next(
                    (i for i, item in enumerate(task_obj.chat) if item.id == requested_id), None
                )
                if index is None or not task_obj.chat[index].is_orbit:
                    return Response(
                        json.dumps({"error": "no task reply with that id to regenerate"}),
                        status=409,
                        mimetype="application/json",
                    )
                in_place_message = task_obj.chat[index]
                history_end = index
                if index == 0 and task_obj.last_turn_initial:
                    # The opening turn was sent as a prompt, never stored as a
                    # message, so it has to come back as an extra user turn.
                    extra_user = task_obj.last_turn_prompt
                if not extra_user and (index == 0 or task_obj.chat[index - 1].is_orbit):
                    return Response(
                        json.dumps({"error": "that task reply has no user message to retry"}),
                        status=409,
                        mimetype="application/json",
                    )
            else:
                if initial or not task_obj.chat or not task_obj.chat[-1].is_orbit:
                    return Response(
                        json.dumps({"error": "there is no completed task response to regenerate"}),
                        status=409,
                        mimetype="application/json",
                    )
                if task_obj.last_turn_initial:
                    extra_user = task_obj.last_turn_prompt
                    if not extra_user:
                        return Response(
                            json.dumps({"error": "the opening prompt is unavailable"}),
                            status=409,
                            mimetype="application/json",
                        )
                elif len(task_obj.chat) < 2 or task_obj.chat[-2].is_orbit:
                    return Response(
                        json.dumps({"error": "the last task response has no user message to retry"}),
                        status=409,
                        mimetype="application/json",
                    )
                task_obj.chat.pop()
        elif initial:
            extra_user = (
                f"Start the task: {task_obj.title}. {task_obj.description} "
                "Give your opening update."
            )
            task_obj.last_turn_prompt = extra_user
            task_obj.last_turn_initial = True
        else:
            if not message:
                return Response(json.dumps({"error": "message is required"}), status=400, mimetype="application/json")
            task_obj.last_turn_prompt = message
            task_obj.last_turn_initial = False
            stored_user = StoreChatMessage(who="You", time=sent_at, body=message, is_orbit=False)
            st.add_chat(task_id, stored_user)
            _record_event(
                "chat_message_received",
                conversation_id=task_id,
                surface="task",
                message_id=stored_user.id,
                chars=len(message),
            )

        history = _history_from_thread(
            task_obj.chat if history_end is None else task_obj.chat[:history_end],
            extra_user=extra_user,
        )

        task_prompt = TASK_SYSTEM_PROMPT
        if regenerate:
            task_prompt = f"{task_prompt} {REGENERATE_INSTRUCTION}"
        system_prompt = with_current_time(app.config["FACTS"].system_prompt(task_prompt))
        # Agent mode names the model being asked ("Asking Titan…"); Chat mode stays unlabeled.
        label_models = st.mode == "agent"
        thinking_level = st.thinking_level

        # The plan is built from the request text the first time a task runs and
        # then carried across turns, so steering the task does not re-plan it.
        run = task_obj.run or new_run(task_obj.description)
        task_obj.run = run
        run.status = RUNNING
        run.started = True
        run.operation = "Thinking…"

        def chunk_source() -> Iterator[StreamChunk]:
            return get_provider().reply_stream(
                history,
                system_prompt,
                think=True,
                report_attempts=label_models,
                thinking_level=thinking_level,
            )

        def on_done(answer: str) -> str | None:
            """Store the reply (in place when regenerating) and return its id."""
            if not answer.strip():
                return None
            if in_place_message is not None:
                st.replace_message(task_id, in_place_message.id, answer)
                return in_place_message.id
            stored = StoreChatMessage(who="Orbit", time=sent_at, body=answer, is_orbit=True)
            st.add_chat(task_id, stored)
            return stored.id

        def on_chunk(chunk: StreamChunk) -> list[tuple[str, str]]:
            """Fold one streamed chunk into the panel, and say so only on change.

            Nothing here is invented: a step moves because Orbit's narration
            mentioned it (app.taskplan.apply_narration), a file appears because
            the request or Orbit named it, and the operation line is whatever the
            stream is actually doing.
            """
            before = json.dumps(_panel_state(task_obj), sort_keys=True)
            if chunk.kind == "status":
                # Agent mode only: "Asking Titan…" / a fallback line.
                run.operation = chunk.text
                _note(task_obj, chunk.text)
            elif chunk.kind == "thinking":
                if apply_narration(run.steps, chunk.text):
                    active = next((s for s in run.steps if s.status == "active"), None)
                    if active is not None and active.evidence:
                        _note(task_obj, f"{active.title} — “{active.evidence}”")
                add_files(run, files_in(chunk.text), SOURCE_ORBIT)
                run.operation = "Working through it…"
            elif chunk.kind == "answer":
                add_files(run, files_in(chunk.text), SOURCE_ORBIT)
                run.operation = "Writing your answer…"
            after = json.dumps(_panel_state(task_obj), sort_keys=True)
            return [("task_state", after)] if after != before else []

        def on_stream_end(answer: str, failed: bool) -> list[tuple[str, str]]:
            """The run's final state, after the last chunk and before "done"."""
            if failed or not answer.strip():
                run.status = FAILED
                run.operation = "Stopped before finishing"
                _note(task_obj, "This turn ended without an answer.")
            else:
                finish_run(run.steps)
                run.status = IDLE
                run.operation = "Waiting for your input"
                _note(task_obj, "Turn finished · waiting for your input.")
            return [("task_state", json.dumps(_panel_state(task_obj), sort_keys=True))]

        return _sse_response(
            chunk_source,
            on_done,
            start_event=("task_state", json.dumps(_panel_state(task_obj), sort_keys=True)),
            on_chunk=on_chunk,
            on_stream_end=on_stream_end,
        )

    @app.post("/api/task/<task_id>/approve")
    @ownership_checked("task_id")
    def api_approve(task_id: str) -> Response:
        st = _scoped_store()
        task_obj, not_owned = _owned_task_or_404(task_id)
        if not_owned is not None:
            return not_owned
        assert task_obj is not None
        payload = request.get_json(silent=True) or {}
        # The gate is explicit: anything other than a literal true is rejected.
        if payload.get("approve") is not True:
            return Response(
                json.dumps({"error": "explicit approval required", "approved": False}),
                status=400,
                mimetype="application/json",
            )
        st.approve_task(task_id)
        task_obj.activity.insert(0, Activity(text="You approved the proposed changes.", time="just now"))
        return Response(json.dumps({"approved": True}), mimetype="application/json")

    @app.post("/api/task/<task_id>/apply")
    @ownership_checked("task_id")
    def api_apply(task_id: str) -> Response:
        st = _scoped_store()
        _, not_owned = _owned_task_or_404(task_id)
        if not_owned is not None:
            return not_owned
        try:
            st.apply_changes(task_id)
        except RuntimeError as exc:
            # The approval gate: no commit/push without explicit approval.
            return Response(
                json.dumps({"error": str(exc), "approved": False}),
                status=403,
                mimetype="application/json",
            )
        return Response(json.dumps({"applied": True, "approved": True}), mimetype="application/json")

    def _sse_response(
        chunk_source: Callable[[], Iterator[StreamChunk | ToolProposal]],
        on_done: Callable[[str], str | None],
        *,
        start_event: tuple[str, str] | None = None,
        after_done: Callable[[str], list[tuple[str, str]] | None] | None = None,
        on_proposal: Callable[[ToolProposal], tuple[str, str]] | None = None,
        on_chunk: Callable[[StreamChunk], list[tuple[str, str]]] | None = None,
        on_stream_end: Callable[[str, bool], list[tuple[str, str]]] | None = None,
    ) -> Response:
        @stream_with_context
        def generate() -> Iterator[str]:
            answer_parts: list[str] = []
            saw_proposal = False
            failed = False
            if start_event is not None:
                yield _sse(*start_event)
            try:
                for chunk in chunk_source():
                    if isinstance(chunk, ToolProposal):
                        saw_proposal = True
                        if on_proposal is None:
                            yield _sse("error", "This reply asked for a tool, but tools are off here.")
                        else:
                            yield _sse(*on_proposal(chunk))
                        continue
                    if on_chunk is not None:
                        # Panel state derived from this chunk, emitted only when
                        # the chunk actually changed it.
                        for event_name, payload in on_chunk(chunk):
                            yield _sse(event_name, payload)
                    if chunk.kind == "status":
                        # Agent mode only: which model is being asked. Not reply text.
                        yield _sse("status", chunk.text)
                    elif chunk.kind == "thinking":
                        yield _sse("thinking", chunk.text)
                    else:
                        yield _sse("answer", chunk.text)
                        answer_parts.append(chunk.text)
            except Exception as exc:  # surface provider/SDK errors to the UI
                failed = True
                provider = app.config.get("PROVIDER")
                _record_model_event(
                    "model_stream_failed",
                    provider=type(provider).__name__ if provider is not None else "unknown",
                    model=getattr(provider, "last_model_used", None)
                    or getattr(provider, "model", None),
                    reason=type(exc).__name__,
                    stream=True,
                )
                yield _sse("error", str(exc))
            answer = "".join(answer_parts)
            if not failed and not answer.strip() and not saw_proposal:
                # Always audit this user-visible fallback—even when the
                # provider itself has no model-level diagnostics (for example,
                # the demo stub or a provider that yielded only empty chunks).
                provider = app.config.get("PROVIDER")
                _record_model_event(
                    "model_returned_nothing",
                    provider=type(provider).__name__ if provider is not None else "unknown",
                    model=getattr(provider, "last_model_used", None)
                    or getattr(provider, "model", None),
                    reason="empty_stream",
                    stream=True,
                    function_call_proposal_seen=saw_proposal,
                )
                # A turn that produced literally nothing looks identical to a
                # hung UI. Say so instead—this is the run_demo stub case and
                # any provider that streams empty chunks.
                yield _sse(
                    "error",
                    "The provider returned an empty reply. If this is run_demo.py "
                    "you are on the stub, not Gemini; with a real key check "
                    "GEMINI_MODEL and GEMINI_API_KEY in .env.",
                )
            if on_stream_end is not None:
                # The run's settled state, before "done": a client that has seen
                # "done" already knows how the run ended.
                for event_name, payload in on_stream_end(answer, failed):
                    yield _sse(event_name, payload)
            # Store the answer and any cards before "done", so a client that has
            # seen "done" can always find its cards in the store (approve works).
            stored_id = on_done(answer)
            if stored_id:
                # The stored reply's id, so the row the client built can carry the
                # id its actions are recorded against -- including after a
                # regeneration, which rewrites that same message.
                yield _sse("message", json.dumps({"id": stored_id}))
            yield _sse("done", "")
            if after_done is not None:
                # Sent after "done", so the reply is never held up by naming.
                for event_name, payload in after_done(answer) or ():
                    yield _sse(event_name, payload)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ======================================================================
    # Onboarding, accounts & feedback
    #
    # Everything below is dormant until run_app attaches an AccountStore —
    # with no store the pages render a "not this launch" notice and the guard
    # above never fires, which is what keeps the whole pre-existing suite
    # green. The flows themselves are real: real password hashing, real codes
    # with expiry and attempt limits, real sessions, and a real feedback
    # registry. The only demo seam is OAuth, and it is labelled on the page.
    # ======================================================================

    def _accounts_off(template: str, **context: Any) -> str:
        """An auth page rendered in a no-accounts launch (tests, old demos):
        the layout is still reviewable, the form is visibly non-functional."""
        return render_template(template, accounts_off=True, **context)

    def _require_account() -> dict[str, Any] | None:
        """The verified account for onboarding routes, or None to bail out.
        With no store there is never an account; with one, the request guard
        has already handled redirecting guests and unverified sessions."""
        return _signed_in_account()

    # ----- registration & sign-in ----------------------------------------

    @app.route("/auth/signup", methods=["GET", "POST"])
    def auth_signup() -> str | Response:
        store = _accounts()
        if store is None:
            return _accounts_off("auth_signup.html")
        if request.method == "GET":
            if (_acct := _signed_in_account()) is not None and _acct.get("email_verified"):
                return redirect(url_for("dashboard"))
            return render_template(
                "auth_signup.html",
                error="", values={}, min_password=MIN_PASSWORD_CHARS, next=_safe_next(request.args.get("next", ""), ""),
            )
        name = request.form.get("name", "")
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        values = {"name": name, "email": email}
        error = ""
        if password != confirm:
            error = "The two passwords don't match. Type them again."
        else:
            try:
                account = store.register_email(name, email, password)
            except AccountError as exc:
                error = str(exc)
            else:
                _sign_in(account)
                _record_event("account_registered", account_id=account["id"], provider="email")
                return redirect(url_for("auth_verify", welcome="1"), code=303)
        return render_template(
            "auth_signup.html",
            error=error, values=values, min_password=MIN_PASSWORD_CHARS,
            next=_safe_next(request.form.get("next", ""), ""),
        ), 422

    @app.route("/auth/login", methods=["GET", "POST"])
    def auth_login() -> str | Response:
        store = _accounts()
        if store is None:
            return _accounts_off("auth_login.html")
        notice = ""
        if request.args.get("reset") == "1":
            notice = "Your password is updated. Sign in with the new one."
        elif request.args.get("signedout") == "1":
            notice = "You're signed out. See you soon."
        elif request.args.get("deleted") == "1":
            notice = "Your account is deleted. The local demo workspace on this machine is untouched."
        if request.method == "GET":
            if (_acct := _signed_in_account()) is not None and _acct.get("email_verified"):
                return redirect(_safe_next(request.args.get("next", "")))
            return render_template(
                "auth_login.html", error="", notice=notice, values={},
                next=_safe_next(request.args.get("next", ""), ""),
            )
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        next_target = _safe_next(request.form.get("next", ""), "")
        account = store.by_email(email)
        error = ""
        if account is None:
            error = "We couldn't find an account for that email. Check the spelling, or create an account below."
        elif not store.check_password(account["id"], password):
            error = "That password doesn't match this account. Try again or recover it below."
        if error:
            return render_template(
                "auth_login.html", error=error, notice="",
                values={"email": email}, next=next_target,
            ), 422
        _sign_in(account)
        _record_event("account_signed_in", account_id=account["id"], provider=account.get("provider", "email"))
        if not account.get("email_verified"):
            # Correct credentials, email unconfirmed: into the pending lane.
            return redirect(url_for("auth_verify"), code=303)
        return redirect(next_target or url_for("dashboard"), code=303)

    @app.get("/auth/logout")
    def auth_logout() -> Response:
        _sign_out()
        if _accounts() is None:
            return redirect(url_for("home"), code=303)
        return redirect(url_for("auth_login", signedout="1"), code=303)

    # ----- email verification ----------------------------------------------

    VERIFY_REASONS = {
        "wrong": "That code doesn't match. Check the six digits and try again.",
        "expired": "That code has expired — we've issued a fresh one below.",
        "locked": "Too many tries on that code. Request a new one below.",
        "no-code": "There's no active code for this account. Request a new one below.",
    }

    @app.route("/auth/verify", methods=["GET", "POST"])
    def auth_verify() -> str | Response:
        store = _accounts()
        if store is None:
            return _accounts_off("auth_verify.html")
        account = _signed_in_account()
        if account is None:
            return redirect(url_for("auth_login"))
        if account.get("email_verified"):
            return redirect(url_for("welcome_page"))
        email = account.get("email", "")
        context: dict[str, Any] = {
            "email": email,
            "demo_code": store.demo_code(email, "verify"),
            "error": "",
            "notice": "",
            "attempts_left": store.attempts_left(email, "verify"),
        }
        if request.args.get("resent") == "1":
            context["notice"] = "A new code is ready below."
        if request.args.get("welcome") == "1":
            context["notice"] = "Your account is created — one quick check to confirm this email is yours."
        if request.method == "GET":
            return render_template("auth_verify.html", **context)
        result = store.verify_email(email, request.form.get("code", ""))
        if result == "ok":
            _record_event("account_email_verified", account_id=account["id"])
            _record_event("account_onboarding_started", account_id=account["id"], path="email")
            return redirect(url_for("welcome_page"), code=303)
        if result == "expired":
            store.resend_code(email, "verify")
        context["error"] = VERIFY_REASONS.get(result, VERIFY_REASONS["wrong"])
        context["demo_code"] = store.demo_code(email, "verify")
        context["attempts_left"] = store.attempts_left(email, "verify")
        return render_template("auth_verify.html", **context), 422

    @app.post("/auth/verify/resend")
    def auth_verify_resend() -> Response:
        store = _accounts()
        if store is None:
            return redirect(url_for("home"), code=303)
        account = _signed_in_account()
        if account is None:
            return redirect(url_for("auth_login"), code=303)
        store.resend_code(account.get("email", ""), "verify")
        _record_event("account_verify_code_resent", account_id=account["id"])
        return redirect(url_for("auth_verify", resent="1"), code=303)

    # ----- password recovery ---------------------------------------------------

    @app.route("/auth/forgot", methods=["GET", "POST"])
    def auth_forgot() -> str | Response:
        store = _accounts()
        if store is None:
            return _accounts_off("auth_forgot.html")
        if request.method == "GET":
            return render_template("auth_forgot.html", errors=[])
        email = request.form.get("email", "")
        if email_issues(email):
            return render_template("auth_forgot.html", errors=email_issues(email), values={"email": email}), 422
        session["reset_email"] = email
        # Silent by design for unknown addresses: the next page says the same
        # thing either way, so this flow cannot reveal who has an account.
        store.start_reset(email)
        _record_event("account_reset_requested")
        return redirect(url_for("auth_reset"), code=303)

    @app.route("/auth/reset", methods=["GET", "POST"])
    def auth_reset() -> str | Response:
        store = _accounts()
        if store is None:
            return _accounts_off("auth_reset.html")
        email = session.get("reset_email", "")
        if not email:
            return redirect(url_for("auth_forgot"))
        context: dict[str, Any] = {
            "email": email,
            "demo_code": store.demo_code(email, "reset"),
            "error": "",
            "attempts_left": store.attempts_left(email, "reset"),
        }
        if request.method == "GET":
            return render_template("auth_reset.html", **context)
        error = ""
        if request.form.get("password", "") != request.form.get("confirm", ""):
            error = "The two passwords don't match. Type them again."
        else:
            result = store.complete_reset(email, request.form.get("code", ""), request.form.get("password", ""))
            if result == "ok":
                session.pop("reset_email", None)
                _record_event("account_password_reset")
                return redirect(url_for("auth_login", reset="1"), code=303)
            if result == "weak-password":
                error = password_issues(request.form.get("password", ""))[0] or "Choose a stronger password."
            elif result == "expired":
                store.resend_code(email, "reset")
                error = "That code has expired — a fresh one is shown below."
            else:
                error = VERIFY_REASONS.get(result, VERIFY_REASONS["wrong"])
        context["error"] = error
        context["demo_code"] = store.demo_code(email, "reset")
        context["attempts_left"] = store.attempts_left(email, "reset")
        return render_template("auth_reset.html", **context), 422

    # ----- provider sign-in (an honest demo flow) ------------------------------

    OAUTH_DEFAULTS = {
        "google": {"name": "Ada Osei", "email": "ada.osei@example.dev"},
        "github": {"name": "Ada Osei", "email": "ada-osei@users.demo.orbit.local"},
    }

    @app.route("/auth/oauth/<provider>", methods=["GET", "POST"])
    def auth_oauth(provider: str) -> str | Response:
        store = _accounts()
        if provider not in OAUTH_DEFAULTS:
            return render_template("project_not_found.html", project_id=provider), 404
        if store is None:
            return _accounts_off("auth_oauth.html", provider=provider, provider_label=PROVIDER_LABELS[provider])
        if request.method == "GET":
            return render_template(
                "auth_oauth.html",
                provider=provider,
                provider_label=PROVIDER_LABELS[provider],
                defaults=OAUTH_DEFAULTS[provider],
                scope_note=OAUTH_SCOPE_COPY.format(provider=PROVIDER_LABELS[provider]),
                error="",
            )
        name = request.form.get("name", "")
        email = request.form.get("email", "")
        try:
            account, is_new = store.oauth_signin(provider, name, email)
        except AccountError as exc:
            return render_template(
                "auth_oauth.html",
                provider=provider,
                provider_label=PROVIDER_LABELS[provider],
                defaults={"name": name, "email": email},
                scope_note=OAUTH_SCOPE_COPY.format(provider=PROVIDER_LABELS[provider]),
                error=str(exc),
            ), 422
        _sign_in(account)
        _record_event("account_signed_in", account_id=account["id"], provider=provider, new_account=is_new)
        if is_new:
            _record_event("account_registered", account_id=account["id"], provider=provider)
            return redirect(url_for("welcome_page"), code=303)
        return redirect(url_for("dashboard"), code=303)

    # ----- welcome & personalization --------------------------------------------

    @app.get("/welcome")
    def welcome_page() -> str | Response:
        """The post-registration front door. Four honest starting options and
        nothing mandatory — 'Skip' is a first-class citizen here."""
        account = _require_account()
        if account is None:
            return redirect(url_for("home"))
        onboarding = account.get("onboarding") or {}
        return render_template(
            "welcome.html",
            first_name=(account.get("name", "") or "there").split(" ")[0],
            tour_available=onboarding.get("tour", "new") == "new",
            personalize_pending=not onboarding.get("personalized") and not onboarding.get("personalize_skipped"),
        )

    @app.route("/onboarding", methods=["GET", "POST"])
    def personalize() -> str | Response:
        """The optional four-question setup. Skippable by design: a skip is
        saved as a real answer ('asked, declined') so the checklist can close
        the item instead of nagging forever."""
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        if request.method == "POST":
            if request.form.get("skip") == "1":
                store.update_personalization(account["id"], skipped=True)
                _record_event("account_personalization_skipped", account_id=account["id"])
                return redirect(url_for("dashboard"), code=303)
            store.update_personalization(
                account["id"],
                role=request.form.get("role", ""),
                experience=request.form.get("experience", ""),
                interests=request.form.getlist("interests"),
                languages=request.form.getlist("languages"),
            )
            _record_event("account_personalized", account_id=account["id"])
            return redirect(url_for("dashboard", personalized="1"), code=303)
        profile = account.get("profile") or {}
        return render_template(
            "personalize.html",
            roles=ROLES,
            experiences=EXPERIENCE_LEVELS,
            interests=INTERESTS,
            languages=LANGUAGES,
            profile=profile,
            standalone=True,  # the wizard shell; /settings edits the same answers inline
        )

    # ----- the adaptive dashboard ------------------------------------------------

    @app.get("/dashboard")
    def dashboard() -> str | Response:
        """One dashboard, three honest states:

        * new — no projects yet: the checklist and the three start paths carry
          the page; recommendations come from personalization answers only;
        * active — projects exist: the work itself leads, with the continue
          strip and per-project cards;
        * returning — the account has been here before: the greeting and the
          jump-back-in strip switch to resuming rather than introducing.
        """
        st = _scoped_store()
        project_store = _scoped_projects()
        store = _accounts()
        account = _signed_in_account()
        projects = project_store.list()
        if account is not None and store is not None:
            onboarding = account.get("onboarding") or {}
            returning = bool(onboarding.get("visited_dashboard_at"))
            store.mark_dashboard_visited(account["id"])
            checklist = checklist_for(account, len(projects))
            checklist_done = all(item["done"] for item in checklist)
            checklist_hidden = bool(onboarding.get("checklist_dismissed"))
            recommendations = recommendations_for(account)
            name = account.get("name", "")
        else:
            # No account store (plain demo launch): render the generic new-user
            # state against the store's user so the page is still reviewable.
            onboarding = {}
            returning = False
            checklist = []
            checklist_done = True
            checklist_hidden = True
            recommendations = recommendations_for({"profile": {}})
            name = st.user.name
        latest_proj = projects[0] if projects else None
        continue_strip = None
        if latest_proj is not None:
            newest = latest_task(st.tasks_for_project(latest_proj.id))
            continue_strip = {
                "id": latest_proj.id,
                "name": latest_proj.name,
                "task_title": newest.title if newest is not None else "No tasks yet",
                "age": human_age(latest_proj.updated_at),
            }
        notice = ""
        if request.args.get("personalized") == "1":
            notice = "Preferences saved — your suggestions below already reflect them."
        tour_autostart = (
            account is not None
            and (account.get("onboarding") or {}).get("tour", "new") == "new"
            and request.args.get("tour") == "1"
        )
        hour = datetime.now().hour
        daypart = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        return render_template(
            "dashboard.html",
            first_name=(name or "there").split(" ")[0],
            daypart=daypart,
            state="new" if not projects else "active",
            returning=returning,
            projects=[_project_card_view(p, st) for p in projects[:6]],
            project_count=len(projects),
            checklist=checklist,
            checklist_done=checklist_done,
            checklist_hidden=checklist_hidden,
            show_checklist=(account is not None and not checklist_done and not checklist_hidden),
            recommendations=recommendations,
            continue_strip=continue_strip,
            notice=notice,
            tour_autostart=tour_autostart,
            tour_state=(account.get("onboarding") or {}).get("tour", "new") if account else "done",
            dashboard_active=True,
            projects_active=False,
            agents_active=False,
            **_sidebar_context(),
        )

    # ----- profile & settings ------------------------------------------------------

    SETTINGS_TABS = (
        ("profile", "Profile"),
        ("preferences", "Preferences"),
        ("integrations", "Integrations"),
        ("notifications", "Notifications"),
        ("security", "Privacy & security"),
        ("account", "Account"),
    )

    @app.get("/settings")
    def settings_page() -> str | Response:
        account = _require_account()
        if account is None:
            return redirect(url_for("home"))
        saved = request.args.get("saved", "").strip()
        error = request.args.get("error", "").strip()
        sid = session.get("sid", "")
        sessions = []
        for entry in (account.get("sessions") or []):
            sessions.append(
                {
                    "label": session_label(entry.get("agent", "")),
                    "created_at": (entry.get("created_at") or "")[:10],
                    "current": bool(sid) and entry.get("sid") == sid,
                }
            )
        saved_labels = {
            "profile": "Profile saved.",
            "preferences": "Preferences saved.",
            "notifications": "Notification preferences saved.",
            "password": "Password updated — other sessions were signed out.",
            "sessions": "Everywhere else is signed out.",
        }
        error_labels = {
            "password-current": "That current password doesn't match — nothing was changed.",
            "password-weak": f"Use at least {MIN_PASSWORD_CHARS} characters, with a letter and a number.",
            "password-mismatch": "The two new passwords don't match.",
            "delete-confirm": "Type your email address exactly to confirm deletion.",
            "name": "A name is required.",
        }
        return render_template(
            "settings.html",
            tabs=SETTINGS_TABS,
            active_tab=request.args.get("tab", "profile").strip() or "profile",
            saved_message=saved_labels.get(saved, ""),
            error_message=error_labels.get(error, ""),
            roles=ROLES,
            experiences=EXPERIENCE_LEVELS,
            interests=INTERESTS,
            languages=LANGUAGES,
            profile=account.get("profile") or {},
            prefs=account.get("prefs") or {},
            provider_label=PROVIDER_LABELS.get(account.get("provider", "email"), "Email and password"),
            has_password=bool(account.get("password_hash")),
            sessions=sessions,
            scope_note_github=OAUTH_SCOPE_COPY.format(provider="GitHub"),
            settings_active=True,
            projects_active=False,
            agents_active=False,
            **_sidebar_context(),
        )

    def _settings_redirect(saved: str = "", error: str = "", tab: str = "") -> Response:
        args: dict[str, str] = {}
        if saved:
            args["saved"] = saved
        if error:
            args["error"] = error
        if tab:
            args["tab"] = tab
        return redirect(url_for("settings_page", **args) + f"#{tab or 'profile'}", code=303)

    @app.post("/settings/profile")
    def settings_profile() -> Response:
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        name = request.form.get("name", "").strip()
        if not name:
            return _settings_redirect(error="name", tab="profile")
        store.update_profile(account["id"], name=name, workspace=request.form.get("workspace", ""))
        return _settings_redirect(saved="profile", tab="profile")

    @app.post("/settings/preferences")
    def settings_preferences() -> Response:
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        store.update_personalization(
            account["id"],
            role=request.form.get("role", ""),
            experience=request.form.get("experience", ""),
            interests=request.form.getlist("interests"),
            languages=request.form.getlist("languages"),
        )
        depth = request.form.get("default_depth", "quick")
        store.set_pref(account["id"], "default_depth", depth)
        st = _scoped_store()
        if depth in ("quick", "deep") and depth != st.thinking_level:
            st.set_thinking_level(depth)
        return _settings_redirect(saved="preferences", tab="preferences")

    @app.post("/settings/notifications")
    def settings_notifications() -> Response:
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        # An unchecked checkbox posts nothing; False is a real answer here.
        store.set_pref(account["id"], "beta_updates", request.form.get("beta_updates") == "on")
        return _settings_redirect(saved="notifications", tab="notifications")

    @app.post("/settings/password")
    def settings_password() -> Response:
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        if new != confirm:
            return _settings_redirect(error="password-mismatch", tab="security")
        if account.get("password_hash") and not store.check_password(account["id"], current):
            return _settings_redirect(error="password-current", tab="security")
        if store.set_password(account["id"], new) != "ok":
            return _settings_redirect(error="password-weak", tab="security")
        # set_password cleared every session; re-open only this browser's.
        sid = store.start_session(account["id"], user_agent=request.headers.get("User-Agent", ""))
        session["sid"] = sid
        _record_event("account_password_changed", account_id=account["id"])
        return _settings_redirect(saved="password", tab="security")

    @app.post("/settings/sessions/revoke-others")
    def settings_revoke_sessions() -> Response:
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        store.revoke_other_sessions(account["id"], session.get("sid", ""))
        _record_event("account_sessions_revoked", account_id=account["id"])
        return _settings_redirect(saved="sessions", tab="security")

    @app.post("/settings/tour-replay")
    def settings_tour_replay() -> Response:
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        store.reset_tour(account["id"])
        return redirect(url_for("dashboard", tour="1"), code=303)

    @app.post("/settings/delete")
    def settings_delete() -> Response:
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        if request.form.get("confirm", "").strip().lower() != account.get("email", "").lower():
            return _settings_redirect(error="delete-confirm", tab="account")
        _record_event("account_deleted", account_id=account["id"])
        account_id, sid = account["id"], session.get("sid", "")
        store.delete_account(account_id)
        session.pop("account_id", None)
        session.pop("sid", None)
        g.account = None
        _ = sid
        return redirect(url_for("auth_login", deleted="1"), code=303)

    # ----- beta feedback -------------------------------------------------------------

    @app.route("/feedback", methods=["GET", "POST"])
    def feedback_page() -> str | Response:
        """The dedicated feedback space. The same form also lives in the
        floating dialog (``_feedback_widget.html``) so reporting never means
        leaving the page you're on — submissions from both land here."""
        store = _accounts()
        account = _require_account()
        if store is None or account is None:
            return redirect(url_for("home"))
        if request.method == "POST":
            kind = request.form.get("kind", "")
            title = request.form.get("title", "")
            body = request.form.get("body", "")
            try:
                entry = store.add_feedback(account["id"], kind, title, body, page=request.form.get("page", "/feedback"))
            except ValueError as exc:
                return render_template(
                    "feedback.html",
                    kinds=FEEDBACK_KINDS,
                    error=str(exc),
                    values={"kind": kind, "title": title, "body": body},
                    entries=store.feedback_for(account["id"]),
                    sent=False,
                    projects_active=False,
                    agents_active=False,
                    **_sidebar_context(),
                ), 422
            _record_event("beta_feedback_submitted", account_id=account["id"], kind=kind, feedback_id=entry["id"])
            return redirect(url_for("feedback_page", sent="1"), code=303)
        return render_template(
            "feedback.html",
            kinds=FEEDBACK_KINDS,
            error="",
            values={},
            entries=store.feedback_for(account["id"]),
            sent=request.args.get("sent") == "1",
            projects_active=False,
            agents_active=False,
            **_sidebar_context(),
        )

    @app.post("/api/feedback")
    def api_feedback() -> Response:
        """The floating dialog's submit path: JSON in, JSON out, no navigation,
        so a report never costs the work on screen."""
        store = _accounts()
        account = _signed_in_account()
        if store is None:
            return _json({"error": "Feedback needs an accounts-enabled launch."}, 503)
        if account is None:
            return _json({"error": "Sign in to send feedback."}, 401)
        payload = request.get_json(silent=True) or {}
        try:
            entry = store.add_feedback(
                account["id"],
                str(payload.get("kind", "")),
                str(payload.get("title", "")),
                str(payload.get("body", "")),
                page=str(payload.get("page", "")),
            )
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        _record_event("beta_feedback_submitted", account_id=account["id"], kind=entry["kind"], feedback_id=entry["id"], via="widget")
        return _json({"ok": True, "id": entry["id"]})

    @app.post("/api/onboarding/tour")
    def api_onboarding_tour() -> Response:
        """How the tour ended, recorded once so it never nags again."""
        store = _accounts()
        account = _signed_in_account()
        if store is None:
            return _json({"error": "No account store on this launch."}, 503)
        if account is None:
            return _json({"error": "Sign in first."}, 401)
        state = str((request.get_json(silent=True) or {}).get("state", ""))
        if state not in ("done", "skipped"):
            return _json({"error": "state must be 'done' or 'skipped'."}, 400)
        store.mark_tour(account["id"], state)
        _record_event("account_tour_marked", account_id=account["id"], state=state)
        return _json({"ok": True, "state": state})

    @app.post("/api/onboarding/checklist")
    def api_onboarding_checklist() -> Response:
        """Dismiss (or restore) the new-user checklist — the person's call."""
        store = _accounts()
        account = _signed_in_account()
        if store is None:
            return _json({"error": "No account store on this launch."}, 503)
        if account is None:
            return _json({"error": "Sign in first."}, 401)
        dismissed = bool((request.get_json(silent=True) or {}).get("dismissed", True))
        store.dismiss_checklist(account["id"], dismissed)
        return _json({"ok": True, "dismissed": dismissed})

    return app


def _require_real_secret_key(app: Any) -> None:
    """Refuse to serve on a session secret everybody can read.

    The secret signs the session cookie. The committed fallback is public in
    this repository, so a server running on it would let anyone who has read
    the source forge a signed-in session for any account. That is why this is a
    boot failure and not a warning: a launch that quietly served on a known key
    would look exactly like a working one.

    What is checked is the secret the app will *actually* sign with, because
    that is the one that matters. ``create_app`` derives it from
    ``ORBIT_SECRET_KEY``, so an unset or default variable is caught here; a
    launcher that supplies its own per-run secret (``run_demo.py``) passes, as
    it should. ``ORBIT_SECRET_KEY`` set to the committed default is rejected on
    its own, since that is a choice someone made on purpose.
    """
    configured = os.environ.get(SECRET_KEY_ENV_VAR, "").strip()
    if configured == DEFAULT_SECRET_KEY:
        raise RuntimeError(
            f"{SECRET_KEY_ENV_VAR} is set to the committed default "
            f"({DEFAULT_SECRET_KEY!r}), which is public in this repository. "
            "Set it to a long random value, for example:  "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    app_secret = getattr(app, "secret_key", None)
    effective = (
        app_secret.strip()
        if isinstance(app_secret, str) and app_secret.strip()
        else configured
    )
    if not effective or effective == DEFAULT_SECRET_KEY:
        raise RuntimeError(
            f"{SECRET_KEY_ENV_VAR} is not set. Sessions would be signed with a "
            "secret that is public in this repository, so anyone could forge a "
            "signed-in session. Set it in .env to a long random value, for "
            "example:  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )


def run_app(app: Flask) -> None:
    """Run the Flask development server without exposing the debugger.

    Refuses to start unless ``ORBIT_SECRET_KEY`` is set to something other than
    the committed default -- see ``_require_real_secret_key``. This runs before
    anything else in here, so a bad launch touches no runtime state and starts
    no scheduler thread.

    Loopback is the safe default for a local run. Set ``ORBIT_HOST=0.0.0.0``
    explicitly when a container or sandbox preview needs an externally reachable
    bind address. The Flask debugger stays disabled in either case.

    At boot this also attaches the heartbeat (unless the app already has one) and
    starts its scheduler as a daemon thread in this process. Debug mode is off, so
    the reloader never runs a second copy of the thread. Console logging is
    configured here -- see ``configure_logging`` for why a bare ``log.exception``
    was not enough -- and the voice dependencies are logged once, so a recording
    that fails later can be read against what the machine actually had at boot.
    """
    _require_real_secret_key(app)

    host = os.environ.get("ORBIT_HOST", "").strip() or "127.0.0.1"
    raw_port = os.environ.get("PORT", "").strip() or "5000"
    try:
        port = int(raw_port)
    except ValueError:
        raise RuntimeError(
            f"PORT must be an integer between 1 and 65535, got {raw_port!r}."
        ) from None
    if not 1 <= port <= 65535:
        raise RuntimeError(f"PORT must be between 1 and 65535, got {port}.")

    from config import load_heartbeat_settings
    from heartbeat import Heartbeat, start_scheduler
    from tools import build_tool_registry
    from tools.confirmation import DeferredConfirmationGate
    from tools.registry import AuditLog

    configure_logging()

    voice_service = app.config.get("VOICE_SERVICE")
    voice_report = _voice_dependency_dict(voice_service) if voice_service is not None else {}
    if voice_report:
        log.info(
            "voice dependencies: faster-whisper=%s ctranslate2=%s pyav=%s "
            "ffmpeg_program=%s ffmpeg_program_required=%s model=%s model_cached=%s source=%s",
            voice_report.get("faster_whisper") or "MISSING",
            voice_report.get("ctranslate2") or "MISSING",
            voice_report.get("pyav") or "MISSING",
            voice_report.get("ffmpeg_program") or "not installed",
            voice_report.get("ffmpeg_program_required"),
            voice_report.get("model"),
            voice_report.get("model_cached"),
            voice_report.get("model_source"),
        )

    heartbeat = app.config.get("HEARTBEAT")
    if heartbeat is None:
        heartbeat = Heartbeat(settings=load_heartbeat_settings())
        app.config["HEARTBEAT"] = heartbeat
    start_scheduler(heartbeat, interval_seconds=heartbeat.settings.interval_seconds)

    # Projects persist across restarts — the same rule the heartbeat state
    # follows: create_app stays in-memory so tests never touch runtime/, and
    # the file-backed store only exists once the real server boots.
    project_store = app.config.get("PROJECTS")
    if project_store is not None and not project_store.persistent:
        app.config["PROJECTS"] = ProjectStore(
            Path(os.environ.get("ORBIT_PROJECTS_PATH", "runtime/projects.json"))
        )

    # Accounts persist the same way: attaching a store here is what turns the
    # whole onboarding flow (landing → sign-up → verify → welcome → dashboard)
    # on for the real server. Tests create the app without one and never see
    # the sign-in wall.
    accounts = app.config.get("ACCOUNTS")
    if accounts is None:
        from app.accounts import AccountStore

        app.config["ACCOUNTS"] = AccountStore(
            Path(os.environ.get("ORBIT_ACCOUNTS_PATH", "runtime/accounts.json"))
        )

    if app.config.get("ACTIONS") is None:
        # Browser tools: the same six tools as the terminal, but the decision
        # comes from the card, not a console prompt.
        gate = DeferredConfirmationGate()
        registry = build_tool_registry(confirmation_gate=gate, heartbeat=heartbeat)
        app.config["ACTIONS"] = PendingActions(registry, gate, audit=AuditLog())

    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run_app(create_app())
