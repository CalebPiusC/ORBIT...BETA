"""Load local ORBIT configuration without storing credentials in source."""

from __future__ import annotations

import math
import re
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
OPENROUTER_API_KEY_ENV_VAR = "OPENROUTER_API_KEY"
OPENROUTER_MODEL_ENV_VARS = (
    "OPENROUTER_MODEL_1",
    "OPENROUTER_MODEL_2",
    "OPENROUTER_MODEL_3",
)
OPENROUTER_FALLBACK_MODEL_ENV_VAR = "OPENROUTER_FALLBACK_MODEL"
ENV_FILE = Path(__file__).resolve().parent / ".env"
_PLACEHOLDER_KEYS = {
    "your-key-here",
    "your_api_key_here",
    "your_openrouter_api_key_here",
    "replace-me",
    "changeme",
}

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
class OpenRouterSettings:
    """Optional OpenRouter configuration used only by Deep mode."""

    api_key: str = field(default="", repr=False)
    models: tuple[str, ...] = ()
    fallback_model: str = ""


@dataclass(frozen=True, slots=True)
class Settings:
    gemini_api_key: str = field(repr=False)
    gemini_model: str = DEFAULT_GEMINI_MODEL
    # "" means the fallback is disabled (blank in .env, or equal to the primary).
    gemini_fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    openrouter_api_key: str = field(default="", repr=False)
    openrouter_models: tuple[str, ...] = ()
    openrouter_fallback_model: str = ""


def _read_openrouter_settings() -> OpenRouterSettings:
    """Read optional OpenRouter credentials and ordered model ids."""
    api_key = os.getenv(OPENROUTER_API_KEY_ENV_VAR, "").strip()
    if (
        api_key.casefold() in _PLACEHOLDER_KEYS
        or api_key.casefold().startswith("your_")
    ):
        api_key = ""

    # Keep empty entries so model position stays tied to persona slot (for
    # example, OPENROUTER_MODEL_2 remains Ganymede if slot 1 is left blank).
    models = tuple(os.getenv(name, "").strip() for name in OPENROUTER_MODEL_ENV_VARS)
    configured_models = {model for model in models if model}

    fallback = os.getenv(OPENROUTER_FALLBACK_MODEL_ENV_VAR, "").strip()
    if fallback in configured_models:
        fallback = ""

    return OpenRouterSettings(
        api_key=api_key,
        models=models,
        fallback_model=fallback,
    )


def load_openrouter_settings() -> OpenRouterSettings:
    """Load optional OpenRouter configuration without requiring a Gemini key."""
    load_dotenv(dotenv_path=ENV_FILE, override=False)
    return _read_openrouter_settings()


def load_settings() -> Settings:
    """Load model keys and configuration from the project .env or environment."""
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
    openrouter = _read_openrouter_settings()

    return Settings(
        gemini_api_key=api_key,
        gemini_model=model,
        gemini_fallback_model=fallback,
        request_timeout_seconds=timeout,
        openrouter_api_key=openrouter.api_key,
        openrouter_models=openrouter.models,
        openrouter_fallback_model=openrouter.fallback_model,
    )


# Heartbeat (Phase 4b). Both checks are local-time rules in Africa/Lagos, and
# quiet hours (22:00 to 08:00 local) suppress automatic notices.
HEARTBEAT_TIMEZONE = "Africa/Lagos"
DEFAULT_HEARTBEAT_PLAN_HOUR = 10
DEFAULT_HEARTBEAT_CHECKIN_GAP_DAYS = 2
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60
DEFAULT_NTFY_SERVER = "https://ntfy.sh"


@dataclass(frozen=True, slots=True)
class HeartbeatSettings:
    plan_hour: int = DEFAULT_HEARTBEAT_PLAN_HOUR
    checkin_gap_days: int = DEFAULT_HEARTBEAT_CHECKIN_GAP_DAYS
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    # "" means phone push is off. The topic is a secret-ish name: keep it in .env.
    ntfy_topic: str = field(default="", repr=False)
    ntfy_server: str = DEFAULT_NTFY_SERVER


def _int_setting(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise RuntimeError(f"{name} in .env must be a whole number, got {raw.strip()!r}.") from None
    if not low <= value <= high:
        raise RuntimeError(f"{name} in .env must be between {low} and {high}, got {value}.")
    return value


def load_heartbeat_settings() -> HeartbeatSettings:
    """Read the heartbeat settings from .env or the environment, validated loudly."""
    load_dotenv(dotenv_path=ENV_FILE, override=False)

    # 08:00 to 21:59 only: a plan hour inside quiet hours could never fire,
    # because notices are held back until the quiet window ends.
    plan_hour = _int_setting("HEARTBEAT_PLAN_HOUR", DEFAULT_HEARTBEAT_PLAN_HOUR, 8, 21)
    gap_days = _int_setting("HEARTBEAT_CHECKIN_GAP_DAYS", DEFAULT_HEARTBEAT_CHECKIN_GAP_DAYS, 1, 365)
    interval = float(_int_setting("HEARTBEAT_INTERVAL_SECONDS", int(DEFAULT_HEARTBEAT_INTERVAL_SECONDS), 5, 3600))

    topic = os.getenv("NTFY_TOPIC", "").strip()
    if topic and not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", topic):
        raise RuntimeError(
            "NTFY_TOPIC in .env may use only letters, digits, '-' and '_' (4 to 64 characters)."
        )
    server = os.getenv("NTFY_SERVER", DEFAULT_NTFY_SERVER).strip().rstrip("/") or DEFAULT_NTFY_SERVER
    if not server.startswith(("https://", "http://")):
        raise RuntimeError("NTFY_SERVER in .env must start with https:// (or http:// for a local server).")

    return HeartbeatSettings(
        plan_hour=plan_hour,
        checkin_gap_days=gap_days,
        interval_seconds=interval,
        ntfy_topic=topic,
        ntfy_server=server,
    )
