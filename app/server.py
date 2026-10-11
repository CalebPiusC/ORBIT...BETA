"""ORBIT web server: routes + templates backed by app.store and streaming.

Two screens from the mockup become real routes:

  * ``/``                       — the chat-first home
  * ``/task/<task_id>``         — the coding-agent task view

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
from collections.abc import Iterable, Iterator
from typing import Any, Callable

from flask import Flask, Response, render_template, request, stream_with_context
from providers.base import ChatMessage as ProvChatMessage, StreamChunk, ToolProposal

from app.actions import PendingActions, ProposalRejected
from app.store import PLACEHOLDER_TITLE, ActionCard, Store, now_label
from app.titles import request_title
from memory.facts import FactStore

log = logging.getLogger("orbit.server")

HOME_SYSTEM_PROMPT = (
    "You are ORBIT, a helpful assistant. Reply clearly and concisely. "
    "Keep answers focused and useful."
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


def _sse(event: str, data: str) -> str:
    # Each logical line of ``data`` becomes its own ``data:`` line so that
    # newlines inside a streamed chunk survive SSE framing. The client joins
    # them back with "\n".
    data_lines = "\n".join(f"data: {line}" for line in data.split("\n"))
    return f"event: {event}\n{data_lines}\n\n"


def _json(payload: Any, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")


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


def create_app(
    provider: Any | None = None,
    store: Store | None = None,
    facts: FactStore | None = None,
    heartbeat: Any | None = None,
    actions: PendingActions | None = None,
) -> Flask:
    """Build the Flask app.

    ``provider`` is the ChatProvider used for streaming. If omitted, the app
    lazily constructs a real ``GeminiProvider`` on first use (which needs
    GEMINI_API_KEY). Tests pass a stubbed provider directly. ``facts`` is the
    background-fact file; it is re-read on every turn.

    ``heartbeat`` is the reminder service. It is deliberately not built here:
    ``run_app`` builds it and starts its scheduler at boot, so importing or
    testing the app never touches runtime/heartbeat.json. Without a heartbeat
    the heartbeat routes answer 503 and home shows no notices.
    """
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "orbit-dev-secret"
    app.config["PROVIDER"] = provider
    app.config["STORE"] = store or Store()
    app.config["FACTS"] = facts if facts is not None else FactStore()
    app.config["HEARTBEAT"] = heartbeat
    # The queue of tool calls the home chat proposed. Without it, Chat mode is
    # plain text, as before. run_app builds it with the real tools.
    app.config["ACTIONS"] = actions

    def _heartbeat_or_none() -> Any | None:
        return app.config["HEARTBEAT"]

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
            prov = GeminiProvider(tool_registry=queue.registry if queue is not None else None)
            app.config["PROVIDER"] = prov
        return prov

    @app.get("/")
    def home() -> str:
        st: Store = app.config["STORE"]
        return render_template(
            "home.html",
            user=st.user,
            greeting=st.greeting(),
            mode=st.mode,
            conversations=st.conversations_grouped(),
            active_conversation_id=st.active_conversation_id,
            connections=st.connections,
            connected_count=st.connected_count(),
            thread=st.home_thread,
            # Once a conversation exists its composer is the fixed bottom bar.
            # Only a fresh, empty conversation centers it.
            docked=st.active_conversation_id is not None,
            heartbeat=_heartbeat_view(),
        )

    @app.get("/task/<task_id>")
    def task(task_id: str) -> str:
        st: Store = app.config["STORE"]
        task_obj = st.get_task(task_id)
        if task_obj is None:
            return render_template("task_not_found.html", task_id=task_id), 404
        # The opening update is a real model call. Re-asking for it on every
        # page load would spend a call per refresh and append a duplicate
        # message to the thread, so it runs only while the task has no Orbit
        # reply yet.
        stream_initial = not any(m.is_orbit for m in task_obj.chat)
        return render_template(
            "task.html",
            user=st.user,
            mode=st.mode,
            task=task_obj,
            stream_initial=stream_initial,
            connections=st.connections,
            connected_count=st.connected_count(),
            conversations=st.conversations_grouped(),
            active_conversation_id=st.active_conversation_id,
        )

    @app.post("/api/mode")
    def set_mode() -> Response:
        st: Store = app.config["STORE"]
        payload = request.get_json(silent=True) or {}
        mode = payload.get("mode")
        try:
            st.set_mode(mode)
        except ValueError as exc:
            return Response(json.dumps({"error": str(exc)}), status=400, mimetype="application/json")
        return Response(json.dumps({"mode": st.mode}), mimetype="application/json")

    @app.post("/api/conversations/new")
    def api_new_conversation() -> Response:
        st: Store = app.config["STORE"]
        st.new_conversation()
        return Response(json.dumps({"active_conversation_id": None}), mimetype="application/json")

    @app.post("/api/chat")
    def api_chat() -> Response:
        st: Store = app.config["STORE"]
        payload = request.get_json(silent=True) or {}
        message = (payload.get("message") or "").strip()
        if not message:
            return Response(json.dumps({"error": "message is required"}), status=400, mimetype="application/json")

        from app.store import ChatMessage as StoreChatMessage

        # The first send in a fresh conversation creates it. Every event of this
        # turn is tied to this id, so switching conversations mid-stream cannot
        # file the reply under the wrong one.
        created = None
        if st.active_conversation_id is None:
            created = st.start_conversation()
        conv_id = st.active_conversation_id
        assert conv_id is not None

        sent_at = now_label()
        st.append_message(conv_id, StoreChatMessage(who="You", time=sent_at, body=message, is_orbit=False))
        # Heartbeat's check-in: a message sent from home counts as checking in.
        hb = _heartbeat_or_none()
        if hb is not None:
            try:
                hb.record_checkin()
            except RuntimeError:
                log.exception("could not record check-in; the chat message was still saved")
        history = _history_from_thread(st.threads[conv_id])

        # Tools are offered in Chat mode only, and only when a queue is attached.
        queue: PendingActions | None = app.config["ACTIONS"]
        use_tools = queue is not None and st.mode == "chat"
        home_prompt = HOME_SYSTEM_PROMPT + (f" {TOOL_GUIDANCE}" if use_tools else "")
        # Read the facts now, once per turn, so an edit lands on the next reply.
        system_prompt = app.config["FACTS"].system_prompt(home_prompt)
        cards: list[ActionCard] = []

        def chunk_source() -> Iterator[StreamChunk | ToolProposal]:
            provider = get_provider()
            if use_tools:
                return provider.reply_stream(history, system_prompt, think=False, allow_tools=True)
            return provider.reply_stream(history, system_prompt, think=False)

        def on_proposal(proposal: ToolProposal) -> tuple[str, str]:
            """Queue the call for approval. Nothing runs here."""
            assert queue is not None
            try:
                action = queue.propose(proposal.name, dict(proposal.arguments))
            except ProposalRejected as exc:
                return "notice", str(exc)
            card = ActionCard(id=action.id, summary=action.summary)
            cards.append(card)
            return "action", json.dumps({"id": card.id, "summary": card.summary, "state": card.state})

        def on_done(answer: str) -> None:
            if answer:
                st.append_message(
                    conv_id, StoreChatMessage(who="Orbit", time=sent_at, body=answer, is_orbit=True)
                )
            # Cards follow the answer they came with, in the order they were proposed.
            for card in cards:
                st.append_message(
                    conv_id,
                    StoreChatMessage(who="Orbit", time=sent_at, body=card.summary, is_orbit=True, action=card),
                )

        def after_done(answer: str) -> tuple[str, str] | None:
            """Name the conversation after its first completed exchange, once."""
            conv = st.get_conversation(conv_id)
            if not answer or conv is None or conv.titled:
                return None
            conv.titled = True  # one attempt only, success or not
            title = request_title(get_provider(), message, answer)
            st.set_title(conv_id, title)
            return "title", json.dumps({"id": conv_id, "title": title})

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
    def api_action_decide(action_id: str, decision: str) -> Response:
        """Approve or decline one proposed tool call. Both outcomes are audited by the registry."""
        st: Store = app.config["STORE"]
        queue: PendingActions | None = app.config["ACTIONS"]
        if queue is None:
            return _json({"error": "tools are not enabled"}, 503)
        if decision not in ("approve", "decline"):
            return _json({"error": "decision must be approve or decline"}, 404)
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

    @app.post("/api/task/<task_id>/stream")
    def api_task_stream(task_id: str) -> Response:
        st: Store = app.config["STORE"]
        task_obj = st.get_task(task_id)
        if task_obj is None:
            return Response(json.dumps({"error": "unknown task"}), status=404, mimetype="application/json")

        payload = request.get_json(silent=True) or {}
        message = (payload.get("message") or "").strip()
        initial = bool(payload.get("initial", False))

        from app.store import ChatMessage as StoreChatMessage

        extra_user: str | None = None
        sent_at = now_label()  # message start time, shown under both bubbles
        if initial:
            extra_user = (
                f"Start the task: {task_obj.title}. {task_obj.description} "
                "Give your opening update."
            )
        else:
            if not message:
                return Response(json.dumps({"error": "message is required"}), status=400, mimetype="application/json")
            st.add_chat(
                task_id,
                StoreChatMessage(who="You", time=sent_at, body=message, is_orbit=False),
            )

        history = _history_from_thread(task_obj.chat, extra_user=extra_user)

        system_prompt = app.config["FACTS"].system_prompt(TASK_SYSTEM_PROMPT)
        # Agent mode names the model being asked ("Asking Titan…"); Chat mode stays unlabeled.
        label_models = st.mode == "agent"

        def chunk_source() -> Iterator[StreamChunk]:
            return get_provider().reply_stream(
                history, system_prompt, think=True, report_attempts=label_models
            )

        def on_done(answer: str) -> None:
            if answer:
                st.add_chat(
                    task_id,
                    StoreChatMessage(who="Orbit", time=sent_at, body=answer, is_orbit=True),
                )

        return _sse_response(chunk_source, on_done)

    @app.post("/api/task/<task_id>/approve")
    def api_approve(task_id: str) -> Response:
        st: Store = app.config["STORE"]
        if st.get_task(task_id) is None:
            return Response(json.dumps({"error": "unknown task"}), status=404, mimetype="application/json")
        payload = request.get_json(silent=True) or {}
        # The gate is explicit: anything other than a literal true is rejected.
        if payload.get("approve") is not True:
            return Response(
                json.dumps({"error": "explicit approval required", "approved": False}),
                status=400,
                mimetype="application/json",
            )
        st.approve_task(task_id)
        from app.store import Activity

        task_obj = st.get_task(task_id)
        assert task_obj is not None
        task_obj.activity.insert(0, Activity(text="You approved the proposed changes.", time="just now"))
        return Response(json.dumps({"approved": True}), mimetype="application/json")

    @app.post("/api/task/<task_id>/apply")
    def api_apply(task_id: str) -> Response:
        st: Store = app.config["STORE"]
        if st.get_task(task_id) is None:
            return Response(json.dumps({"error": "unknown task"}), status=404, mimetype="application/json")
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
        on_done: Callable[[str], None],
        *,
        start_event: tuple[str, str] | None = None,
        after_done: Callable[[str], tuple[str, str] | None] | None = None,
        on_proposal: Callable[[ToolProposal], tuple[str, str]] | None = None,
    ) -> Response:
        @stream_with_context
        def generate() -> Iterator[str]:
            answer_parts: list[str] = []
            saw_thinking = False
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
                    if chunk.kind == "status":
                        # Agent mode only: which model is being asked. Not reply text.
                        yield _sse("status", chunk.text)
                    elif chunk.kind == "thinking":
                        saw_thinking = True
                        yield _sse("thinking", chunk.text)
                    else:
                        yield _sse("answer", chunk.text)
                        answer_parts.append(chunk.text)
            except Exception as exc:  # surface provider/SDK errors to the UI
                failed = True
                yield _sse("error", str(exc))
            if not failed and not answer_parts and not saw_thinking and not saw_proposal:
                # A turn that produced literally nothing looks identical to a
                # hung UI. Say so instead — this is the run_demo stub case and
                # any provider that streams empty chunks.
                yield _sse(
                    "error",
                    "The provider returned an empty reply. If this is run_demo.py "
                    "you are on the stub, not Gemini; with a real key check "
                    "GEMINI_MODEL and GEMINI_API_KEY in .env.",
                )
            answer = "".join(answer_parts)
            # Store the answer and any cards before "done", so a client that has
            # seen "done" can always find its cards in the store (approve works).
            on_done(answer)
            yield _sse("done", "")
            if after_done is not None:
                # Sent after "done", so the reply is never held up by naming.
                extra = after_done(answer)
                if extra is not None:
                    yield _sse(*extra)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return app


def run_app(app: Flask) -> None:
    """Run the Flask development server without exposing the debugger.

    Loopback is the safe default for a local run. Set ``ORBIT_HOST=0.0.0.0``
    explicitly when a container or sandbox preview needs an externally reachable
    bind address. The Flask debugger stays disabled in either case.

    At boot this also attaches the heartbeat (unless the app already has one) and
    starts its scheduler as a daemon thread in this process. Debug mode is off, so
    the reloader never runs a second copy of the thread.
    """
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

    heartbeat = app.config.get("HEARTBEAT")
    if heartbeat is None:
        heartbeat = Heartbeat(settings=load_heartbeat_settings())
        app.config["HEARTBEAT"] = heartbeat
    start_scheduler(heartbeat, interval_seconds=heartbeat.settings.interval_seconds)

    if app.config.get("ACTIONS") is None:
        # Browser tools: the same six tools as the terminal, but the decision
        # comes from the card, not a console prompt.
        gate = DeferredConfirmationGate()
        registry = build_tool_registry(confirmation_gate=gate, heartbeat=heartbeat)
        app.config["ACTIONS"] = PendingActions(registry, gate, audit=AuditLog())

    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run_app(create_app())
