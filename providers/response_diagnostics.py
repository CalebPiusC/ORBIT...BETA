"""Safe structured diagnostics for Gemini responses that contain no text."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name, default)
    except (AttributeError, TypeError, ValueError):
        return default


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    raw = getattr(value, "value", None)
    return str(raw if raw is not None else value)


def _ratings(value: Any) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for rating in value or []:
        diagnostics.append(
            {
                "category": _enum_name(_field(rating, "category")),
                "probability": _enum_name(_field(rating, "probability")),
                "blocked": _field(rating, "blocked"),
            }
        )
    return diagnostics


def _blocked_reason(reason: str | None) -> bool:
    return bool(reason and reason not in {"BLOCK_REASON_UNSPECIFIED", "UNSPECIFIED", "0"})


def response_diagnostics(response: Any, *, chunk_index: int) -> dict[str, Any]:
    """Summarize raw response metadata without persisting prompt/reply/tool args."""
    prompt_feedback = _field(response, "prompt_feedback")
    prompt_block_reason = _enum_name(_field(prompt_feedback, "block_reason"))
    prompt_ratings = _ratings(_field(prompt_feedback, "safety_ratings", []))
    candidates_out: list[dict[str, Any]] = []
    candidates = _field(response, "candidates", []) or []

    for index, candidate in enumerate(candidates):
        content = _field(candidate, "content")
        parts = _field(content, "parts", []) or []
        part_types: list[str] = []
        function_call_names: list[str] = []
        text_characters = 0
        has_text = False
        for part in parts:
            text = _field(part, "text")
            if isinstance(text, str) and text:
                has_text = True
                text_characters += len(text)
                part_types.append("text")
            function_call = _field(part, "function_call")
            if function_call is not None:
                part_types.append("function_call")
                name = _field(function_call, "name")
                if isinstance(name, str) and name:
                    function_call_names.append(name)
            if _field(part, "thought") is True:
                part_types.append("thought")
            if not isinstance(text, str) and function_call is None and _field(part, "thought") is not True:
                part_types.append("other")

        finish_reason = _enum_name(_field(candidate, "finish_reason"))
        safety_ratings = _ratings(_field(candidate, "safety_ratings", []))
        safety_blocked = (
            finish_reason == "SAFETY"
            or any(rating.get("blocked") is True for rating in safety_ratings)
        )
        candidates_out.append(
            {
                "index": index,
                "finish_reason": finish_reason,
                "finish_message": _field(candidate, "finish_message"),
                "part_count": len(parts),
                "part_types": part_types,
                "has_text": has_text,
                "text_characters": text_characters,
                "function_call_names": function_call_names,
                "function_call_without_text": bool(function_call_names) and not has_text,
                "safety_blocked": safety_blocked,
                "safety_ratings": safety_ratings,
            }
        )

    usage = _field(response, "usage_metadata")
    usage_summary = None
    if usage is not None:
        usage_summary = {
            key: _field(usage, key)
            for key in (
                "prompt_token_count",
                "candidates_token_count",
                "total_token_count",
                "thoughts_token_count",
            )
            if _field(usage, key) is not None
        }

    return {
        "chunk_index": chunk_index,
        "response_id": _field(response, "response_id"),
        "model_version": _field(response, "model_version"),
        "prompt_block_reason": prompt_block_reason,
        "prompt_safety_blocked": _blocked_reason(prompt_block_reason)
        or any(rating.get("blocked") is True for rating in prompt_ratings),
        "prompt_safety_ratings": prompt_ratings,
        "candidate_count": len(candidates_out),
        "candidates": candidates_out,
        "usage": usage_summary,
    }
