"""Browser tool calls: queue-then-approve, the deferred gate, streaming proposals, and save_plan.

Offline throughout. The model is a fake client that yields real google-genai response
objects, so the SDK types and the streaming code are both exercised. Every path points
at a temp directory; nothing touches notes.txt, runtime/, or audit.log.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.genai import types

from app.actions import PendingActions, ProposalRejected, summarize
from app.server import create_app
from app.store import DEFAULT_USER_ID, Store
from config import HeartbeatSettings
from heartbeat.schedule import local_time, utc_now
from heartbeat.service import KIND_PLAN, Heartbeat
from memory.facts import FactStore
from memory.plans import PlanStore
from providers.base import ChatMessage, StreamChunk, ToolProposal
from providers.gemini import GeminiProvider
from tests.test_app import parse_sse
from tools import build_tool_registry
from tools.confirmation import DeferredConfirmationGate, ProposedAction
from tools.registry import AuditLog

LAGOS = ZoneInfo("Africa/Lagos")
TOOL_NAMES = {"write_note", "remember_fact", "forget_fact", "save_plan", "pause_heartbeat", "resume_heartbeat"}


def audit_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class Paths(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.notes = self.root / "notes.txt"
        self.facts = self.root / "facts.txt"
        self.plans = self.root / "plans.json"
        self.heartbeat_state = self.root / "heartbeat.json"
        self.audit = self.root / "audit.log"

    def registry_and_queue(self) -> tuple[Any, PendingActions, DeferredConfirmationGate]:
        gate = DeferredConfirmationGate()
        registry = build_tool_registry(
            confirmation_gate=gate,
            audit_log_path=self.audit,
            notes_path=self.notes,
            facts_path=self.facts,
            plans_path=self.plans,
            heartbeat_path=self.heartbeat_state,
            heartbeat=self.heartbeat(),
        )
        return registry, PendingActions(registry, gate, audit=AuditLog(self.audit)), gate

    def heartbeat(self) -> Heartbeat:
        return Heartbeat(
            settings=HeartbeatSettings(
                plan_hour=10,
                checkin_gap_days=2,
                interval_seconds=60.0,
                ntfy_topic="",
                ntfy_server="https://ntfy.sh",
            ),
            state_path=self.heartbeat_state,
            plans=PlanStore(self.plans),
            audit=AuditLog(self.audit),
        )


class DeferredGateTests(unittest.TestCase):
    def test_no_recorded_decision_declines(self) -> None:
        gate = DeferredConfirmationGate()
        action = ProposedAction("write_note", "d", {"content": "x"})
        self.assertFalse(gate.confirm(action))

    def test_decision_applies_only_to_the_matching_action(self) -> None:
        gate = DeferredConfirmationGate()
        with gate.decision("write_note", {"content": "x"}, approved=True):
            self.assertTrue(gate.confirm(ProposedAction("write_note", "d", {"content": "x"})))
            self.assertFalse(gate.confirm(ProposedAction("write_note", "d", {"content": "y"})))
            self.assertFalse(gate.confirm(ProposedAction("remember_fact", "d", {"content": "x"})))

    def test_decision_is_cleared_after_the_dispatch(self) -> None:
        gate = DeferredConfirmationGate()
        with gate.decision("write_note", {"content": "x"}, approved=True):
            pass
        self.assertFalse(gate.confirm(ProposedAction("write_note", "d", {"content": "x"})))

    def test_declined_decision_declines(self) -> None:
        gate = DeferredConfirmationGate()
        with gate.decision("write_note", {"content": "x"}, approved=False):
            self.assertFalse(gate.confirm(ProposedAction("write_note", "d", {"content": "x"})))


class ValidationTests(Paths):
    def test_validate_reports_unknown_tools_and_bad_arguments(self) -> None:
        registry, _queue, _gate = self.registry_and_queue()
        self.assertEqual(registry.validate("no_such_tool", {})[0], "unknown_tool")
        self.assertEqual(registry.validate("save_plan", {})[0], "invalid_arguments")
        self.assertEqual(registry.validate("save_plan", ["not", "a", "dict"])[0], "invalid_arguments")
        self.assertIsNone(registry.validate("save_plan", {"plan": "ok"}))

    def test_browser_registry_has_the_six_chat_tools(self) -> None:
        registry, _queue, _gate = self.registry_and_queue()
        self.assertEqual({d.name for d in registry.definitions}, TOOL_NAMES)


class ProposalQueueTests(Paths):
    def test_invalid_proposal_is_rejected_and_audited_without_queueing(self) -> None:
        _registry, queue, _gate = self.registry_and_queue()
        with self.assertRaises(ProposalRejected):
            queue.propose("save_plan", {})
        rows = audit_rows(self.audit)
        self.assertEqual([r["execution"] for r in rows], ["invalid_arguments"])
        self.assertEqual(rows[0]["confirmation"], "declined")

    def test_proposal_runs_nothing_until_approved(self) -> None:
        _registry, queue, _gate = self.registry_and_queue()
        action = queue.propose("write_note", {"content": "Buy milk"})
        self.assertFalse(self.notes.exists())
        self.assertEqual(action.summary, "Add this line to your notes: \u201cBuy milk\u201d")
        outcome = queue.decide(action.id, approved=True)
        self.assertEqual(outcome.execution, "succeeded")
        self.assertIn("Buy milk", self.notes.read_text(encoding="utf-8"))

    def test_approval_is_audited_as_confirmed_and_succeeded(self) -> None:
        _registry, queue, _gate = self.registry_and_queue()
        action = queue.propose("write_note", {"content": "Audit me"})
        queue.decide(action.id, approved=True)
        events = [(r.get("event"), r.get("tool_name"), r.get("confirmation"), r.get("execution")) for r in audit_rows(self.audit)]
        self.assertEqual(
            events,
            [("tool_proposed", "write_note", None, None), (None, "write_note", "confirmed", "succeeded")],
        )

    def test_decline_runs_nothing_and_is_audited(self) -> None:
        _registry, queue, _gate = self.registry_and_queue()
        action = queue.propose("write_note", {"content": "Never saved"})
        outcome = queue.decide(action.id, approved=False)
        self.assertEqual(outcome.confirmation, "declined")
        self.assertFalse(self.notes.exists())
        last = audit_rows(self.audit)[-1]
        self.assertEqual((last["tool_name"], last["confirmation"], last["execution"]), ("write_note", "declined", "not_run"))

    def test_a_decision_can_be_made_only_once(self) -> None:
        _registry, queue, _gate = self.registry_and_queue()
        action = queue.propose("write_note", {"content": "Once"})
        queue.decide(action.id, approved=True)
        with self.assertRaises(KeyError):
            queue.decide(action.id, approved=True)
        self.assertEqual(self.notes.read_text(encoding="utf-8").count("Once"), 1)

    def test_summaries_say_what_will_run(self) -> None:
        self.assertIn("Remember this about you", summarize("remember_fact", {"fact": "Plays bass."}))
        self.assertIn("Forget saved facts", summarize("forget_fact", {"fact": "Plays bass."}))
        self.assertIn("Save today", summarize("save_plan", {"plan": "Ship it"}))
        self.assertIn("Pause reminders", summarize("pause_heartbeat", {}))
        self.assertIn("Resume reminders", summarize("resume_heartbeat", {}))


class FakeStreamClient:
    """Stands in for genai.Client. Records each request and yields the scripted responses lazily."""

    def __init__(self, responses: list[types.GenerateContentResponse], consumed: list[int]) -> None:
        self._responses = responses
        self._consumed = consumed
        self.requests: list[dict[str, Any]] = []
        self.models = self

    def generate_content_stream(self, **kwargs: Any):
        self.requests.append(kwargs)
        for index, response in enumerate(self._responses):
            self._consumed.append(index)
            yield response


def text_response(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))]
    )


def call_response(*calls: tuple[str, dict[str, Any]]) -> types.GenerateContentResponse:
    parts = [types.Part.from_function_call(name=name, args=args) for name, args in calls]
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=parts))])


class GeminiStreamProposalTests(Paths):
    def provider(self, responses: list[types.GenerateContentResponse], consumed: list[int]) -> tuple[GeminiProvider, FakeStreamClient, Any]:
        registry, _queue, _gate = self.registry_and_queue()
        client = FakeStreamClient(responses, consumed)
        return GeminiProvider(client=client, tool_registry=registry, fallback_model=""), client, registry

    def test_tools_are_declared_in_chat_mode_and_one_call_ends_the_stream(self) -> None:
        consumed: list[int] = []
        provider, client, registry = self.provider(
            [
                text_response("Sure, I'll save that. "),
                call_response(("write_note", {"content": "first"}), ("remember_fact", {"fact": "second"})),
                text_response("this must never be read"),
            ],
            consumed,
        )
        chunks = list(
            provider.reply_stream([ChatMessage(role="user", content="note it")], "You are ORBIT.", think=False, allow_tools=True)
        )
        self.assertEqual(chunks[0], StreamChunk(kind="answer", text="Sure, I'll save that. "))
        self.assertEqual(chunks[1], ToolProposal(name="write_note", arguments={"content": "first"}))
        self.assertEqual(len(chunks), 2)
        # The third response was never requested: the stream stopped at the proposal.
        self.assertEqual(consumed, [0, 1])
        # Nothing ran while streaming.
        self.assertFalse(self.notes.exists())
        # The extra call in the same reply is refused and audited.
        extra = [r for r in audit_rows(self.audit) if r.get("tool_name") == "remember_fact"]
        self.assertEqual([(r["confirmation"], r["execution"]) for r in extra], [("declined", "tool_call_limit")])
        # The request offered all six tools to the model.
        config = client.requests[0]["config"]
        names = {d.name for d in config.tools[0].function_declarations}
        self.assertEqual(names, TOOL_NAMES)
        self.assertTrue(config.automatic_function_calling.disable)

    def test_plain_stream_without_allow_tools_never_proposes(self) -> None:
        consumed: list[int] = []
        provider, client, _registry = self.provider([text_response("Just text.")], consumed)
        chunks = list(provider.reply_stream([ChatMessage(role="user", content="hi")], "You are ORBIT.", think=False))
        self.assertEqual(chunks, [StreamChunk(kind="answer", text="Just text.")])
        self.assertNotIn("tools", client.requests[0]["config"].model_dump(exclude_none=True))

    def test_allow_tools_with_thinking_is_refused(self) -> None:
        provider, _client, _registry = self.provider([text_response("x")], [])
        with self.assertRaises(ValueError):
            list(provider.reply_stream([ChatMessage(role="user", content="hi")], "p", think=True, allow_tools=True))


class SavePlanEndToEndTests(Paths):
    """The heartbeat's missing-plan notice depends on the plan that approval saves."""

    def now_today_at(self, hour: int) -> datetime:
        return datetime.now(LAGOS).replace(hour=hour, minute=0, second=0, microsecond=0)

    def test_approved_save_plan_stops_the_missing_plan_notice(self) -> None:
        _registry, queue, _gate = self.registry_and_queue()
        at_eleven = self.now_today_at(11)

        before = self.heartbeat().tick(at_eleven)
        self.assertEqual([n["kind"] for n in before], [KIND_PLAN])

        # A fresh state file: the next attempt is a new day's check, with a plan now saved.
        self.heartbeat_state.unlink()
        action = queue.propose("save_plan", {"plan": "Finish the release notes"})
        outcome = queue.decide(action.id, approved=True)
        self.assertEqual(outcome.execution, "succeeded")
        saved = json.loads(self.plans.read_text(encoding="utf-8"))
        self.assertEqual([p["text"] for p in saved], ["Finish the release notes"])
        self.assertEqual(saved[0]["date"], local_time(utc_now()).date().isoformat())

        self.assertEqual(self.heartbeat().tick(at_eleven), [])

    def test_declined_save_plan_leaves_the_notice_in_place(self) -> None:
        _registry, queue, _gate = self.registry_and_queue()
        action = queue.propose("save_plan", {"plan": "Never saved"})
        queue.decide(action.id, approved=False)
        self.assertFalse(self.plans.exists())
        fired = self.heartbeat().tick(self.now_today_at(11))
        self.assertEqual([n["kind"] for n in fired], [KIND_PLAN])


class ScriptedProvider:
    """A stand-in model: each call streams the next scripted items, then the stream ends."""

    def __init__(self, scripts: list[list[Any]]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []
        self.histories: list[list[ChatMessage]] = []

    def reply(self, history, system_prompt: str) -> str:  # pragma: no cover - chat streams
        raise AssertionError("the home chat must stream")

    def title(self, user_text: str, answer: str) -> str:
        return "Plan saving"

    def reply_stream(self, history, system_prompt: str, *, think: bool = False, report_attempts: bool = False, allow_tools: bool = False, thinking_level: str = "quick"):
        self.calls.append({"allow_tools": allow_tools, "system_prompt": system_prompt})
        self.histories.append(list(history))
        script = self.scripts.pop(0) if self.scripts else []
        yield from script


class ChatRouteToolTests(Paths):
    def setUp(self) -> None:
        super().setUp()
        self.registry, self.queue, _gate = self.registry_and_queue()

    def client(self, provider: ScriptedProvider, *, tools: bool = True):
        app = create_app(
            provider=provider,
            store=Store(),
            facts=FactStore(self.facts),
            actions=self.queue if tools else None,
        )
        return app.test_client(), app

    def chat(self, client, text: str) -> list[tuple[str, str]]:
        response = client.post("/api/chat", json={"message": text})
        self.assertEqual(response.status_code, 200)
        return parse_sse(response.get_data(as_text=True))

    def test_chat_turn_queues_one_proposal_and_stops_after_it(self) -> None:
        provider = ScriptedProvider([[StreamChunk(kind="answer", text="I can save that."), ToolProposal("save_plan", {"plan": "Ship the release"})]])
        client, _app = self.client(provider)
        events = self.chat(client, "Save my plan: ship the release")

        names = [name for name, _ in events]
        self.assertIn("action", names)
        self.assertEqual(names.count("answer"), 1)
        self.assertEqual(len(provider.calls), 1)  # single pass: no second model call
        self.assertTrue(provider.calls[0]["allow_tools"])
        card = json.loads(next(data for name, data in events if name == "action"))
        self.assertEqual(card["state"], "pending")
        self.assertFalse(self.plans.exists())

        page = client.get("/").get_data(as_text=True)
        self.assertIn(f'data-action-id="{card["id"]}"', page)
        self.assertIn("Approve", page)
        self.assertIn("Save today", page)

    def test_card_is_stored_before_done_so_an_early_approve_finds_it(self) -> None:
        provider = ScriptedProvider([[ToolProposal("save_plan", {"plan": "Early"})]])
        client, app = self.client(provider)
        response = client.post("/api/chat", json={"message": "Plan"}, buffered=False)
        seen = b""
        for piece in response.response:
            seen += piece
            if b"event: done" in seen:
                break
        # The client has seen "done" and has not read anything after it.
        cards = [m.action for m in app.config["STORE"].for_account(DEFAULT_USER_ID).home_thread if m.action is not None]
        response.close()
        self.assertEqual([c.state for c in cards], ["pending"])

    def test_approve_runs_the_tool_and_the_card_records_it(self) -> None:
        provider = ScriptedProvider([[ToolProposal("save_plan", {"plan": "Ship the release"})]])
        client, _app = self.client(provider)
        events = self.chat(client, "Save my plan")
        card_id = json.loads(next(data for name, data in events if name == "action"))["id"]

        response = client.post(f"/api/actions/{card_id}/approve", json={})
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["state"], "approved")
        self.assertIn("Saved the plan", body["result"])
        self.assertEqual(json.loads(self.plans.read_text(encoding="utf-8"))[0]["text"], "Ship the release")

        page = client.get("/").get_data(as_text=True)
        self.assertIn('data-state="approved"', page)
        self.assertIn("Approved", page)

        # A second decision on the same card is refused.
        again = client.post(f"/api/actions/{card_id}/approve", json={})
        self.assertEqual(again.status_code, 404)

    def test_decline_runs_nothing_and_says_so(self) -> None:
        provider = ScriptedProvider([[ToolProposal("write_note", {"content": "No thanks"})]])
        client, _app = self.client(provider)
        card_id = json.loads(next(d for n, d in self.chat(client, "Note this") if n == "action"))["id"]

        body = client.post(f"/api/actions/{card_id}/decline", json={}).get_json()
        self.assertEqual(body["state"], "declined")
        self.assertFalse(self.notes.exists())
        self.assertEqual(audit_rows(self.audit)[-1]["confirmation"], "declined")

    def test_next_turn_tells_the_model_how_the_proposal_ended(self) -> None:
        provider = ScriptedProvider(
            [
                [ToolProposal("save_plan", {"plan": "Ship it"})],
                [StreamChunk(kind="answer", text="Noted.")],
            ]
        )
        client, _app = self.client(provider)
        card_id = json.loads(next(d for n, d in self.chat(client, "Plan") if n == "action"))["id"]
        client.post(f"/api/actions/{card_id}/approve", json={})
        self.chat(client, "Thanks")
        second_turn = [m.content for m in provider.histories[1]]
        self.assertTrue(any("[approved: Saved the plan" in text for text in second_turn), second_turn)

    def test_unknown_tool_is_refused_with_a_notice_not_a_card(self) -> None:
        provider = ScriptedProvider([[ToolProposal("delete_everything", {})]])
        client, _app = self.client(provider)
        events = self.chat(client, "Do it")
        self.assertNotIn("action", [n for n, _ in events])
        notice = next(d for n, d in events if n == "notice")
        self.assertIn("not registered", notice)
        self.assertNotIn("error", [n for n, _ in events])
        self.assertEqual(audit_rows(self.audit)[-1]["execution"], "unknown_tool")

    def test_agent_mode_offers_no_tools(self) -> None:
        provider = ScriptedProvider([[StreamChunk(kind="answer", text="Plain reply.")]])
        client, app = self.client(provider)
        app.config["STORE"].for_account(DEFAULT_USER_ID).set_mode("agent")
        events = self.chat(client, "Save my plan")
        self.assertFalse(provider.calls[0]["allow_tools"])
        self.assertNotIn("action", [n for n, _ in events])

    def test_without_a_queue_the_chat_is_plain_text(self) -> None:
        provider = ScriptedProvider([[StreamChunk(kind="answer", text="Plain.")]])
        client, _app = self.client(provider, tools=False)
        self.chat(client, "Hello")
        self.assertFalse(provider.calls[0]["allow_tools"])

    def test_decision_route_refuses_when_tools_are_off(self) -> None:
        client, _app = self.client(ScriptedProvider([]), tools=False)
        self.assertEqual(client.post("/api/actions/act-x/approve", json={}).status_code, 503)

    def test_unknown_decision_word_is_a_404(self) -> None:
        client, _app = self.client(ScriptedProvider([]))
        self.assertEqual(client.post("/api/actions/act-x/maybe", json={}).status_code, 404)


class BootWiringTests(Paths):
    def test_run_app_attaches_the_browser_queue_and_starts_the_scheduler(self) -> None:
        from unittest import mock

        from app.server import run_app

        # run_app configures a console handler on the "orbit" logger; hand the
        # logger back untouched so later tests keep logging as they did before.
        orbit_logger = logging.getLogger("orbit")
        handlers, level, propagate = (
            list(orbit_logger.handlers),
            orbit_logger.level,
            orbit_logger.propagate,
        )

        def restore() -> None:
            orbit_logger.handlers[:] = handlers
            orbit_logger.setLevel(level)
            orbit_logger.propagate = propagate

        self.addCleanup(restore)

        app = create_app(provider=ScriptedProvider([]), store=Store(), heartbeat=self.heartbeat())
        # run_app refuses to serve on the committed default session secret; a
        # real launch gets one from ORBIT_SECRET_KEY, this test supplies one.
        app.secret_key = "test-session-secret-not-the-committed-default"
        self.assertIsNone(app.config["ACTIONS"])
        with mock.patch("heartbeat.start_scheduler") as scheduler, mock.patch.object(app, "run") as run:
            run_app(app)
        scheduler.assert_called_once()
        run.assert_called_once()
        queue = app.config["ACTIONS"]
        self.assertIsInstance(queue, PendingActions)
        self.assertEqual({d.name for d in queue.registry.definitions}, TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
