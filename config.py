"""Load local ORBIT configuration without storing credentials in source."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
ENV_FILE = Path(__file__).resolve().parent / ".env"
_PLACEHOLDER_KEYS = {"your-key-here", "your_api_key_here", "replace-me", "changeme"}


@dataclass(frozen=True, slots=True)
class Settings:
    gemini_api_key: str = field(repr=False)
    gemini_model: str = DEFAULT_GEMINI_MODEL


def load_settings() -> Settings:
    """Load the Gemini key/model from the project .env or process environment."""
    load_dotenv(dotenv_path=ENV_FILE, override=False)

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key.casefold() in _PLACEHOLDER_KEYS:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. Copy .env.example to .env and add your key."
        )

    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    if not model:
        model = DEFAULT_GEMINI_MODEL

    return Settings(gemini_api_key=api_key, gemini_model=model)
