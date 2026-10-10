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
from collections.abc import Iterable, Iterator
from typing import Any, Callable

from flask import Flask, Response, render_template, request, stream_with_context
from providers.base import ChatMessage as ProvChatMessage, StreamChunk

from app.store import Store

HOME_SYSTEM_PROMPT = (
    "You are ORBIT, a helpful assistant. Reply clearly and concisely. "
    "Keep answers focused and useful."
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


def _history_from_thread(
    messages: Iterable[Any], extra_user: str | None = None
) -> list[ProvChatMessage]:
    history: list[ProvChatMessage] = []
    for msg in messages:
        role = "assistant" if getattr(msg, "is_orbit", False) else "user"
        history.append(ProvChatMessage(role=role, content=msg.body))
    if extra_user:
        history.append(ProvChatMessage(role="user", content=extra_user))
    return history


def create_app(provider: Any | None = None, store: Store | None = None) -> Flask:
    """Build the Flask app.

    ``provider`` is the ChatProvider used for streaming. If omitted, the app
    lazily constructs a real ``GeminiProvider`` on first use (which needs
    GEMINI_API_KEY). Tests pass a stubbed provider directly.
    """
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "orbit-dev-secret"
    app.config["PROVIDER"] = provider
    app.config["STORE"] = store or Store()

    def get_provider() -> Any:
        prov = app.config["PROVIDER"]
        if prov is None:
            from providers.gemini import GeminiProvider

            prov = GeminiProvider()
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

    @app.post("/api/chat")
    def api_chat() -> Response:
        st: Store = app.config["STORE"]
        payload = request.get_json(silent=True) or {}
        message = (payload.get("message") or "").strip()
        if not message:
            return Response(json.dumps({"error": "message is required"}), status=400, mimetype="application/json")

        from app.store import ChatMessage as StoreChatMessage

        st.home_thread.append(StoreChatMessage(who="You", time="now", body=message, is_orbit=False))
        history = _history_from_thread(st.home_thread)

        def chunk_source() -> Iterator[StreamChunk]:
            return get_provider().reply_stream(history, HOME_SYSTEM_PROMPT, think=False)

        def on_done(answer: str) -> None:
            if answer:
                st.home_thread.append(
                    StoreChatMessage(who="Orbit", time="now", body=answer, is_orbit=True)
                )

        return _sse_response(chunk_source, on_done)

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
                StoreChatMessage(who="You", time="now", body=message, is_orbit=False),
            )

        history = _history_from_thread(task_obj.chat, extra_user=extra_user)

        def chunk_source() -> Iterator[StreamChunk]:
            return get_provider().reply_stream(history, TASK_SYSTEM_PROMPT, think=True)

        def on_done(answer: str) -> None:
            if answer:
                st.add_chat(
                    task_id,
                    StoreChatMessage(who="Orbit", time="now", body=answer, is_orbit=True),
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
        chunk_source: Callable[[], Iterator[StreamChunk]],
        on_done: Callable[[str], None],
    ) -> Response:
        @stream_with_context
        def generate() -> Iterator[str]:
            answer_parts: list[str] = []
            saw_thinking = False
            failed = False
            try:
                for chunk in chunk_source():
                    if chunk.kind == "thinking":
                        saw_thinking = True
                        yield _sse("thinking", chunk.text)
                    else:
                        yield _sse("answer", chunk.text)
                        answer_parts.append(chunk.text)
            except Exception as exc:  # surface provider/SDK errors to the UI
                failed = True
                yield _sse("error", str(exc))
            if not failed and not answer_parts and not saw_thinking:
                # A turn that produced literally nothing looks identical to a
                # hung UI. Say so instead — this is the run_demo stub case and
                # any provider that streams empty chunks.
                yield _sse(
                    "error",
                    "The provider returned an empty reply. If this is run_demo.py "
                    "you are on the stub, not Gemini; with a real key check "
                    "GEMINI_MODEL and GEMINI_API_KEY in .env.",
                )
            yield _sse("done", "")
            on_done("".join(answer_parts))

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


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000, debug=True)
