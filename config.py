"""Load local ORBIT configuration without storing credentials in source."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Model choice lives here and in .env only — never hard-coded in a provider.
# gemini-2.5-flash was retired-by-announcement on 2026-10-20, so it is no
# longer the default. Keep this value in sync with .env.example.
# Checked 2026-10-10. The primary is the model that answered a live "pong"
# check on this machine (about 24.6s). The fallback is tried once, only when
# the primary fails with 503/504 or a transport error, before any tool runs.
# Model ids live here and in .env only; never hard-code one in a provider.
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-3.8-flash"
FALLBACK_ENV_VAR = "GEMINI_FALLBACK_MODEL"
ENV_FILE = Path(__file__).resolve().parent / ".env"
_PLACEHOLDER_KEYS = {"your-key-here", "your_api_key_here", "replace-me", "changeme"}

# Every request to the model gets a deadline. Without one the SDK forwards
# ``timeout=None`` to httpx, and an explicit None does not fall back to the
# client default — it disables the timeout outright, so a firewalled or
# black-holed connection blocks forever with no reply and no error. That is a
# silent failure, and it is the whole reason this setting exists. Raise it for
# slow links, never remove it.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
TIMEOUT_ENV_VAR = "ORBIT_HTTP_TIMEOUT"
_TIMEOUT_ENV_FILE = "ORBIT_HTTP_TIMEOUT in .env"


def read_timeout_seconds(raw: str | None) -> float:
    """Turn ``ORBIT_HTTP_TIMEOUT`` into a deadline in seconds.

    Unset or blank means "use the default". A value that is not a positive,
    finite number is a configuration mistake, not a request to disable the
    deadline, so it is rejected loudly here rather than silently becoming an
    unbounded wait at the network layer.
    """
    if raw is None or not raw.strip():
        return DEFAULT_REQUEST_TIMEOUT_SECONDS

    try:
        value = float(raw.strip())
    except ValueError:
        raise RuntimeError(
            f"{_TIMEOUT_ENV_FILE} must be a number of seconds, got {raw.strip()!r}."
        ) from None

    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(
            f"{_TIMEOUT_ENV_FILE} must be a positive number of seconds, got {raw.strip()!r}. "
            "Zero or negative would disable the deadline and let a request hang forever."
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    gemini_api_key: str = field(repr=False)
    gemini_model: str = DEFAULT_GEMINI_MODEL
    # "" means the fallback is disabled (blank in .env, or equal to the primary).
    gemini_fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS


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

    # Unset means the default fallback; set but blank means "no fallback".
    # A fallback equal to the primary would just repeat the same request.
    fallback_raw = os.getenv(FALLBACK_ENV_VAR)
    fallback = DEFAULT_GEMINI_FALLBACK_MODEL if fallback_raw is None else fallback_raw.strip()
    if fallback == model:
        fallback = ""

    timeout = read_timeout_seconds(os.getenv(TIMEOUT_ENV_VAR))

    return Settings(
        gemini_api_key=api_key,
        gemini_model=model,
        gemini_fallback_model=fallback,
        request_timeout_seconds=timeout,
    )
