"""Display names for model slots. Used only in Agent mode status lines.

A slot is a position in the model chain: slot 0 is the primary model and slot 1
is the fallback. Each slot has one name, handed out in order and carrying no
meaning beyond its position. Chat mode never shows them.

Names reserved for future model slots, not assigned yet: Obi, Kemi, Tobi, Amina.
They are kept out of SLOT_NAMES until a slot needs them.
"""

from __future__ import annotations

SLOT_NAMES: tuple[str, ...] = ("Chidi", "Ada")

RESERVED_NAMES: tuple[str, ...] = ("Obi", "Kemi", "Tobi", "Amina")


def slot_name(slot: int) -> str:
    """Return the display name for a model slot (0 = primary, 1 = fallback)."""
    if not 0 <= slot < len(SLOT_NAMES):
        raise ValueError(
            f"No display name is assigned to model slot {slot}. Add one to SLOT_NAMES "
            f"in providers/personas.py first (reserved, not yet assigned: {', '.join(RESERVED_NAMES)})."
        )
    return SLOT_NAMES[slot]
