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
from werkzeug.exceptions import RequestEntityTooLarge

from app.actions import PendingActions, ProposalRejected
from app.followups import followups_for
from app.starters import WELCOME_STARTERS
from app.store import PLACEHOLDER_TITLE, ActionCard, Activity, Store, now_label
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
from tools.registry import AuditLog

log = logging.getLogger("orbit.server")

MAX_VOICE_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_SPEECH_TEXT_CHARS = 12_000
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
    voice_service: Any | None = None,
    model_audit: AuditLog | None = None,
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
    """
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "orbit-dev-secret"
    app.config["MAX_CONTENT_LENGTH"] = MAX_VOICE_UPLOAD_BYTES
    app.config["PROVIDER"] = provider
    app.config["STORE"] = store or Store()
    app.config["FACTS"] = facts if facts is not None else FactStore()
    app.config["HEARTBEAT"] = heartbeat
    app.config["VOICE_SERVICE"] = voice_service if voice_service is not None else VoiceService()
    app.config["MODEL_AUDIT"] = model_audit if model_audit is not None else AuditLog()
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

    @app.get("/")
    def home() -> str:
        st: Store = app.config["STORE"]
        return render_template(
            "home.html",
            user=st.user,
            greeting=st.greeting(),
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
        st: Store = app.config["STORE"]
        payload = request.get_json(silent=True) or {}
        request_text = payload.get("request")
        request_text = request_text.strip() if isinstance(request_text, str) else ""
        if not request_text:
            return _json({"error": "a task needs a request to work from."}, 400)
        task = st.create_task(request_text)
        return _json({"id": task.id, "title": task.title})

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

    @app.post("/api/thinking-level")
    def set_thinking_level() -> Response:
        st: Store = app.config["STORE"]
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
        try:
            transcript = app.config["VOICE_SERVICE"].transcribe(audio, extension=extension)
        except VoiceServiceUnavailable:
            log.exception("voice transcription service unavailable")
            return _json({"error": "Voice transcription is unavailable right now."}, 503)
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)
        except Exception:
            log.exception("voice transcription failed")
            return _json({"error": "Could not transcribe that recording. Please try again."}, 500)
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
        except VoiceServiceUnavailable:
            log.exception("voice synthesis service unavailable")
            return _json({"error": "Spoken replies are unavailable right now."}, 503)
        except StopIteration:
            return _json({"error": "The speech service returned no audio."}, 502)
        except Exception:
            log.exception("voice synthesis failed before audio started")
            return _json({"error": "Could not generate spoken audio. Please try again."}, 502)

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
        st: Store = app.config["STORE"]
        st.new_conversation()
        return Response(json.dumps({"active_conversation_id": None}), mimetype="application/json")

    @app.post("/api/chat")
    def api_chat() -> Response:
        st: Store = app.config["STORE"]
        payload = request.get_json(silent=True) or {}
        regenerate = payload.get("regenerate") is True
        message = payload.get("message")
        message = message.strip() if isinstance(message, str) else ""
        from app.store import ChatMessage as StoreChatMessage

        created = None
        sent_at = now_label()
        if regenerate:
            conv_id = st.active_conversation_id
            thread = st.threads.get(conv_id or "", [])
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
            # Heartbeat's check-in: a user message counts, a retry does not.
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
        thinking_level = st.thinking_level
        home_prompt = HOME_SYSTEM_PROMPT + (f" {TOOL_GUIDANCE}" if use_tools else "")
        # Read the facts now, once per turn, so an edit lands on the next reply.
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
                action = queue.propose(proposal.name, dict(proposal.arguments))
            except ProposalRejected as exc:
                return "notice", str(exc)
            card = ActionCard(id=action.id, summary=action.summary)
            cards.append(card)
            return "action", json.dumps({"id": card.id, "summary": card.summary, "state": card.state})

        def on_done(answer: str) -> None:
            if answer.strip():
                st.append_message(
                    conv_id, StoreChatMessage(who="Orbit", time=sent_at, body=answer, is_orbit=True)
                )
            # Cards follow the answer they came with, in the order they were proposed.
            for card in cards:
                st.append_message(
                    conv_id,
                    StoreChatMessage(who="Orbit", time=sent_at, body=card.summary, is_orbit=True, action=card),
                )

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
        message = payload.get("message")
        message = message.strip() if isinstance(message, str) else ""
        initial = bool(payload.get("initial", False))
        regenerate = payload.get("regenerate") is True

        from app.store import ChatMessage as StoreChatMessage

        extra_user: str | None = None
        sent_at = now_label()  # message start time, shown under both bubbles
        if regenerate:
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
            st.add_chat(
                task_id,
                StoreChatMessage(who="You", time=sent_at, body=message, is_orbit=False),
            )

        history = _history_from_thread(task_obj.chat, extra_user=extra_user)

        system_prompt = with_current_time(app.config["FACTS"].system_prompt(TASK_SYSTEM_PROMPT))
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

        def on_done(answer: str) -> None:
            if answer.strip():
                st.add_chat(
                    task_id,
                    StoreChatMessage(who="Orbit", time=sent_at, body=answer, is_orbit=True),
                )

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
            on_done(answer)
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
