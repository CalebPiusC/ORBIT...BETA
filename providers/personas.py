"""Display names for configured model slots, shown in Agent mode status lines.

Gemini keeps the first two slots (Titan and Europa). The next three slots are
used by Deep mode's pinned OpenRouter models: Io, Ganymede, and Callisto. Any
other configured model, including OpenRouter's auto-router, keeps its own id.
Chat mode never shows these names or model-attempt status lines.
"""

from __future__ import annotations

import os

SLOT_NAMES: tuple[str, ...] = ("Titan", "Europa", "Io", "Ganymede", "Callisto")

RESERVED_NAMES: tuple[str, ...] = ("Io", "Ganymede", "Callisto", "Triton", "Luna")

_GEMINI_ENV_VAR = "GEMINI_MODEL"
_GEMINI_FALLBACK_ENV_VAR = "GEMINI_FALLBACK_MODEL"


def model_persona_map() -> dict[str, str]:
    """Map configured model ids to their display names.

    Names are position-based, not properties of a model. Io is the first
    pinned OpenRouter model, Ganymede the second, Callisto the third; Titan
    and Europa retain their Gemini primary/fallback assignments.
    """
    from config import (
        DEFAULT_GEMINI_FALLBACK_MODEL,
        DEFAULT_GEMINI_MODEL,
        load_openrouter_settings,
    )

    mapping: dict[str, str] = {}
    for position, model in enumerate(load_openrouter_settings().models, start=2):
        if model:
            mapping[model] = slot_name(position)

    gemini = os.getenv(_GEMINI_ENV_VAR, "").strip() or DEFAULT_GEMINI_MODEL
    fallback = (
        os.getenv(_GEMINI_FALLBACK_ENV_VAR, "").strip() or DEFAULT_GEMINI_FALLBACK_MODEL
    )
    mapping[gemini] = slot_name(0)  # Titan
    if fallback:
        mapping.setdefault(fallback, slot_name(1))  # Europa
    return mapping


def persona_for_model(model: str, mapping: dict[str, str] | None = None) -> str:
    """The name shown for a model id, or the id itself when none is assigned."""
    names = model_persona_map() if mapping is None else mapping
    return names.get(model, model)


def slot_name(slot: int) -> str:
    """Return the display name assigned to a model slot."""
    if not 0 <= slot < len(SLOT_NAMES):
        reserved_offset = max(0, len(SLOT_NAMES) - 2)
        still_reserved = RESERVED_NAMES[reserved_offset:]
        detail = ", ".join(still_reserved) or "none"
        raise ValueError(
            f"No display name is assigned to model slot {slot}. Add one to SLOT_NAMES "
            f"in providers/personas.py first (still reserved: {detail})."
        )
    return SLOT_NAMES[slot]
