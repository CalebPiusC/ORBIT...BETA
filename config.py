"""Load local ORBIT configuration without storing credentials in source."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Model choice lives here and in .env only — never hard-coded in a provider.
# gemini-2.5-flash was retired-by-announcement on 2026-10-20, so it is no
# longer the default. Keep this value in sync with .env.example.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
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
    # The placeholder is matched by name, not by shape: an untouched .env.example
    # must fail loudly here rather than surface later as a 401 from the API.
    if (
        not api_key
        or api_key.casefold() in _PLACEHOLDER_KEYS
        or api_key.casefold().startswith("your_")
    ):
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. Copy .env.example to .env and add your key."
        )

    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    if not model:
        model = DEFAULT_GEMINI_MODEL

    return Settings(gemini_api_key=api_key, gemini_model=model)
