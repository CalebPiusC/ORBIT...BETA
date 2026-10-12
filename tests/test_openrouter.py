from __future__ import annotations

import json
import unittest
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import httpx

from providers.base import ChatMessage, StreamChunk, ToolProposal
from providers.gemini import GeminiProvider
from providers.openrouter import OpenRouterClient, OpenRouterDelta, OpenRouterError

FIRST = "vendor/first:free"
SECOND = "vendor/second:free"
THIRD = "vendor/third:free"
AUTO = "router-test"
GEMINI = "gemini-test-model"
HISTORY = [ChatMessage(role="user", content="Please help.")]


def sse(payload: Mapping[str, Any]) -> str:
    return "data: " + json.dumps(payload)


class FakeHttpResponse:
    def __init__(self, lines: Sequence[str], status_code: int = 200) -> None:
        self.lines = list(lines)
        self.status_code = status_code

    def iter_lines(self):
        yield from self.lines


class FakeHttpStream:
    def __init__(self, response: FakeHttpResponse) -> None:
        self.response = response

    def __enter__(self) -> FakeHttpResponse:
        return self.response

    def __exit__(self, *_args: object) -> None:
        return None


class FakeHttpClient:
    def __init__(self, response: FakeHttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> FakeHttpStream:
        self.calls.append({"method": method, "url": url, **kwargs})
        return FakeHttpStream(self.response)


class OpenRouterClientTests(unittest.TestCase):
    def test_sends_openai_compatible_stream_request_and_parses_text(self) -> None:
        http = FakeHttpClient(
            FakeHttpResponse(
                [
                    ": keep-alive",
                    sse({"choices": [{"delta": {"role": "assistant"}}]}),
                    sse({"choices": [{"delta": {"content": "Hello "}}]}),
                    sse({"choices": [{"delta": {"content": "there."}}]}),
                    "data: [DONE]",
                ]
            )
        )
        client = OpenRouterClient("local-test-key", timeout_seconds=12.5, http_client=http)

        output = list(
            client.stream(
                model=FIRST,
                messages=[{"role": "system", "content": "Be concise."}],
                max_tokens=4096,
            )
        )

        self.assertEqual([item.text for item in output], ["Hello ", "there."])
        call = http.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["json"]["model"], FIRST)
        self.assertTrue(call["json"]["stream"])
        self.assertEqual(call["json"]["max_tokens"], 4096)
        self.assertEqual(call["timeout"], 12.5)
        self.assertEqual(call["headers"]["Authorization"], "Bearer local-test-key")

    def test_sends_tool_declarations_and_streams_tool_call_deltas(self) -> None:
        http = FakeHttpClient(
            FakeHttpResponse(
                [
                    sse(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call-1",
                                                "function": {"name": "write_note", "arguments": "{}"},
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    ),
                    "data: [DONE]",
                ]
            )
        )
        tool = {
            "type": "function",
            "function": {"name": "write_note", "parameters": {"type": "object"}},
        }
        client = OpenRouterClient("local-test-key", timeout_seconds=2, http_client=http)

        output = list(
            client.stream(
                model=FIRST,
                messages=[{"role": "user", "content": "Save this."}],
                max_tokens=100,
                tools=[tool],
            )
        )

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0].tool_calls[0]["id"], "call-1")
        self.assertEqual(http.calls[0]["json"]["tools"], [tool])

    def test_http_errors_are_safe_and_carry_the_status(self) -> None:
        http = FakeHttpClient(FakeHttpResponse([], status_code=503))
        client = OpenRouterClient("never-print-this-key", timeout_seconds=1, http_client=http)

        with self.assertRaises(OpenRouterError) as caught:
            list(
                client.stream(
                    model=FIRST,
                    messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=32,
                )
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("never-print-this-key", str(caught.exception))


class ScriptedOpenRouter:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = {model: list(items) if isinstance(items, list) else items for model, items in outcomes.items()}
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any):
        model = kwargs["model"]
        self.calls.append(kwargs)
        outcome = self.outcomes[model]
        if isinstance(outcome, BaseException):
            raise outcome

        def chunks():
            assert isinstance(outcome, list)
            for item in outcome:
                if isinstance(item, BaseException):
                    raise item
                yield item if isinstance(item, OpenRouterDelta) else OpenRouterDelta(text=str(item))

        return chunks()


class ScriptedGemini:
    def __init__(self, text: str = "Gemini last resort.") -> None:
        self.calls: list[dict[str, Any]] = []
        self.text = text

    def generate_content_stream(self, **kwargs: Any):
        self.calls.append(kwargs)
        return iter([SimpleNamespace(text=self.text, parts=[])])


class DeepOpenRouterTests(unittest.TestCase):
    def provider(self, openrouter: ScriptedOpenRouter, gemini: ScriptedGemini | None = None, **kwargs: Any):
        gemini_models = gemini or ScriptedGemini()
        provider = GeminiProvider(
            client=SimpleNamespace(models=gemini_models),
            model=GEMINI,
            fallback_model="",
            audit_log=None,
            openrouter_api_key="local-test-key",
            openrouter_models=(FIRST, SECOND, THIRD),
            openrouter_fallback_model=AUTO,
            openrouter_client=openrouter,
            **kwargs,
        )
        return provider, gemini_models

    @staticmethod
    def stream(provider: GeminiProvider, **kwargs: Any) -> list[StreamChunk | ToolProposal]:
        return list(provider.reply_stream(HISTORY, "Be helpful.", thinking_level="deep", **kwargs))

    def test_pinned_models_then_auto_router_are_tried_in_order(self) -> None:
        openrouter = ScriptedOpenRouter(
            {
                FIRST: OpenRouterError("busy", status_code=503),
                SECOND: OpenRouterError("busy", status_code=429),
                THIRD: OpenRouterError("busy", status_code=503),
                AUTO: ["Auto-router answer."],
            }
        )
        provider, gemini = self.provider(openrouter)

        output = self.stream(provider, report_attempts=True)

        self.assertEqual([call["model"] for call in openrouter.calls], [FIRST, SECOND, THIRD, AUTO])
        self.assertEqual(gemini.calls, [])
        self.assertEqual(
            [chunk.text for chunk in output if chunk.kind == "status"],
            [
                "Asking Io…",
                "Io didn't answer (OpenRouter returned 503). Asking Ganymede…",
                "Ganymede didn't answer (OpenRouter returned 429). Asking Callisto…",
                "Callisto didn't answer (OpenRouter returned 503). Asking router-test…",
            ],
        )
        self.assertEqual("".join(chunk.text for chunk in output if chunk.kind == "answer"), "Auto-router answer.")
        self.assertEqual(provider.last_model_used, AUTO)
        self.assertEqual(provider.last_attempts, ["503", "429", "503"])

    def test_first_pinned_model_success_skips_later_fallbacks(self) -> None:
        openrouter = ScriptedOpenRouter(
            {FIRST: ["Deep ", "answer."], SECOND: ["unused"], THIRD: ["unused"], AUTO: ["unused"]}
        )
        provider, gemini = self.provider(openrouter)

        output = self.stream(provider, report_attempts=True)

        self.assertEqual([call["model"] for call in openrouter.calls], [FIRST])
        self.assertEqual(gemini.calls, [])
        self.assertEqual(provider.last_model_used, FIRST)
        self.assertEqual([chunk.text for chunk in output if chunk.kind == "status"], ["Asking Io…"])
        self.assertEqual("".join(chunk.text for chunk in output if chunk.kind == "answer"), "Deep answer.")

    def test_openrouter_exhaustion_falls_back_to_gemini_europa(self) -> None:
        openrouter = ScriptedOpenRouter(
            {
                FIRST: OpenRouterError("busy", status_code=503),
                SECOND: OpenRouterError("busy", status_code=503),
                THIRD: OpenRouterError("busy", status_code=503),
                AUTO: OpenRouterError("busy", status_code=503),
            }
        )
        gemini = ScriptedGemini("Europa answer.")
        provider, gemini = self.provider(openrouter, gemini)

        output = self.stream(provider, report_attempts=True)

        self.assertEqual([call["model"] for call in openrouter.calls], [FIRST, SECOND, THIRD, AUTO])
        self.assertEqual([call["model"] for call in gemini.calls], [GEMINI])
        self.assertEqual(gemini.calls[0]["config"].max_output_tokens, 4096)
        self.assertEqual(
            [chunk.text for chunk in output if chunk.kind == "status"][-1],
            "router-test didn't answer (OpenRouter returned 503). Asking Europa…",
        )
        self.assertEqual("".join(chunk.text for chunk in output if chunk.kind == "answer"), "Europa answer.")
        self.assertEqual(provider.last_model_used, GEMINI)

    def test_network_failure_after_output_does_not_restart_or_fallback(self) -> None:
        openrouter = ScriptedOpenRouter(
            {
                FIRST: ["partial", httpx.ReadError("connection dropped")],
                SECOND: ["must not run"],
                THIRD: ["must not run"],
                AUTO: ["must not run"],
            }
        )
        provider, gemini = self.provider(openrouter)
        iterator = provider.reply_stream(HISTORY, "Be helpful.", thinking_level="deep")

        self.assertEqual(next(iterator), StreamChunk(kind="answer", text="partial"))
        with self.assertRaises(httpx.ReadError):
            next(iterator)
        self.assertEqual([call["model"] for call in openrouter.calls], [FIRST])
        self.assertEqual(gemini.calls, [])

    def test_deep_tool_call_is_returned_as_a_proposal_not_executed(self) -> None:
        class Registry:
            definitions = (
                SimpleNamespace(
                    name="write_note",
                    description="Save a note.",
                    parameters={"type": "object", "properties": {"content": {"type": "string"}}},
                ),
            )

            def __init__(self) -> None:
                self.rejected: list[str] = []

            def reject_call(self, name, *_args, **_kwargs):
                self.rejected.append(name)

        registry = Registry()
        openrouter = ScriptedOpenRouter(
            {
                FIRST: [
                    OpenRouterDelta(
                        tool_calls=(
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "write_note", "arguments": '{"content":'},
                            },
                        )
                    ),
                    OpenRouterDelta(
                        tool_calls=(
                            {"index": 0, "function": {"arguments": '"exact note"}'}},
                        )
                    ),
                ]
            }
        )
        provider, _gemini = self.provider(openrouter, tool_registry=registry)

        output = self.stream(provider, allow_tools=True)

        proposal = next(item for item in output if isinstance(item, ToolProposal))
        self.assertEqual(proposal.name, "write_note")
        self.assertEqual(proposal.arguments, {"content": "exact note"})
        self.assertEqual(registry.rejected, [])
        sent_tools = openrouter.calls[0]["tools"]
        self.assertEqual(sent_tools[0]["function"]["name"], "write_note")


if __name__ == "__main__":
    unittest.main()
