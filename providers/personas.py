"""Display names for model slots. Used only in Agent mode status lines.

A slot is a position in the model chain: slot 0 is the primary model and slot 1
is the fallback. Each slot has one name, handed out in order and carrying no
meaning beyond its position. Chat mode never shows them.

Names reserved for future model slots, in the order they are handed out:
Io, Ganymede, Callisto, Triton, Luna. They are kept out of SLOT_NAMES until a
slot needs them.
"""

from __future__ import annotations

import os

SLOT_NAMES: tuple[str, ...] = ("Titan", "Europa")

RESERVED_NAMES: tuple[str, ...] = ("Io", "Ganymede", "Callisto", "Triton", "Luna")

# Which configured model answers under which name. Read from the environment at
# call time, so it always reflects the configuration in force. Only these three
# names are assigned today; any other model id is reported by its own id rather
# than being given an invented name.
_OPENROUTER_ENV_VAR = "OPENROUTER_MODEL"
_GEMINI_ENV_VAR = "GEMINI_MODEL"
_GEMINI_FALLBACK_ENV_VAR = "GEMINI_FALLBACK_MODEL"


def model_persona_map() -> dict[str, str]:
    """Map configured model ids to the name used for them in one place: the UI.

    ``Io`` is reserved for the OpenRouter primary, ``Titan`` for the primary
    Gemini model and ``Europa`` for the Gemini fallback. Blank or unset values
    contribute nothing.
    """
    from config import DEFAULT_GEMINI_FALLBACK_MODEL, DEFAULT_GEMINI_MODEL

    mapping: dict[str, str] = {}
    openrouter = os.getenv(_OPENROUTER_ENV_VAR, "").strip()
    if openrouter:
        mapping[openrouter] = RESERVED_NAMES[0]  # Io
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
    """Return the display name for a model slot (0 = primary, 1 = fallback)."""
    if not 0 <= slot < len(SLOT_NAMES):
        raise ValueError(
            f"No display name is assigned to model slot {slot}. Add one to SLOT_NAMES "
            f"in providers/personas.py first (reserved, not yet assigned: {', '.join(RESERVED_NAMES)})."
        )
    return SLOT_NAMES[slot]

