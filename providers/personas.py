"""Display names for model slots. Used only in Agent mode status lines.

A slot is a position in the model chain: slot 0 is the primary model and slot 1
is the fallback. Names are handed out in this fixed order, one per slot, and
carry no meaning beyond their position. Chat mode never shows them.
"""

from __future__ import annotations

SLOT_NAMES: tuple[str, ...] = ("Chidi", "Ada", "Obi", "Chika", "Zara", "Jiden", "Oma")


def slot_name(slot: int) -> str:
    """Return the display name for a model slot (0 = primary, 1 = fallback, ...)."""
    if not 0 <= slot < len(SLOT_NAMES):
        raise ValueError(
            f"No display name is reserved for model slot {slot}. "
            f"Add one to SLOT_NAMES in providers/personas.py first."
        )
    return SLOT_NAMES[slot]
