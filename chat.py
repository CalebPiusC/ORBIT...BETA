"""Interactive chat with Gemini, through the same gated provider as the UI.

One-shot on purpose at first; that made it feel broken next to the web chat, so
it keeps the connection open now. Conversation history carries across turns, a
provider failure ends the turn and not the session, and the loop itself is
testable via ``run_session`` (the real Gemini call is still a manual check).

``--check`` walks config -> network -> model and names the first layer that
fails. It exists because the worst failure this program had was silence: with
no request deadline, a blocked connection waits forever and prints nothing, so
"it does nothing" and "it is thinking" looked identical.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import time
from collections.abc import Callable
from typing import Any

from config import Settings, load_settings
from providers.base import ChatMessage
from providers.gemini import GeminiProvider
from tools import build_tool_registry

SYSTEM_PROMPT = (
    "You are ORBIT, a helpful assistant. Reply clearly and concisely. "
    "Use write_note only when the user explicitly asks you to save a note, "
    "and pass the exact one-line note text they requested. Never claim it was "
    "saved unless the tool reports success. If the person declines, do not retry."
)

# History is capped so a long session cannot grow the request without limit.
MAX_MESSAGES = 20

HELP = "Enter sends. /help this list · /reset clears history · /quit exits."

# Printed before the program blocks on the model, so a long wait is never
# indistinguishable from a hang. Ctrl-C during the wait cancels the turn only.
WAIT_TEMPLATE = "waiting on {model} — Ctrl-C cancels this turn"

# --check diagnostics.
CHECK_PROMPT = "Reply with the single word: pong"
GEMINI_HOST = "generativelanguage.googleapis.com"
GEMINI_PORT = 443
# The network probe is a TCP connect, so it gets its own shorter ceiling; the
# real request still uses the configured deadline.
NETWORK_PROBE_TIMEOUT_SECONDS = 10.0
_PROXY_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NETWORK = 3
EXIT_MODEL = 4


def run_session(
    provider: Any,
    *,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    """Drive the prompt/reply loop. Returns a process exit code.

    ``read``/``write`` are injectable so the loop is testable offline, and so a
    non-interactive stdin (EOF) exits cleanly instead of raising.
    """
    history: list[ChatMessage] = []
    write(HELP)

    while True:
        try:
            prompt = read("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            write("")
            return 0

        if not prompt:
            continue
        if prompt in ("/quit", "/exit"):
            return 0
        if prompt == "/help":
            write(HELP)
            continue
        if prompt == "/reset":
            history.clear()
            write("(history cleared)")
            continue

        history.append(ChatMessage(role="user", content=prompt))
        if len(history) > MAX_MESSAGES:
            del history[: len(history) - MAX_MESSAGES]

        model_name = getattr(provider, "model", "the model")
        write(WAIT_TEMPLATE.format(model=model_name))
        try:
            answer = provider.reply(history=history, system_prompt=SYSTEM_PROMPT)
        except KeyboardInterrupt:
            # Cancel the wait, not the conversation: keep the turn queued.
            history.pop()
            write("")
            write("(cancelled — your message is still queued; send it again to retry, or /quit)")
            continue
        except Exception as exc:  # a failed turn must not end the session
            history.pop()  # keep the user's turn so they can simply send it again
            write(f"ORBIT: [error] {type(exc).__name__}: {exc}")
            write("(your message is still queued — send it again to retry)")
            continue

        history.append(ChatMessage(role="assistant", content=answer))
        used = getattr(provider, "last_model_used", None)
        if used and used != getattr(provider, "model", None):
            write(f"(answered by fallback {used}; primary {provider.model} failed)")
        write(f"\nORBIT: {answer}")


def mask_key(key: str) -> str:
    """Reduce a credential to its first four characters, for display only."""
    if not key:
        return "(empty)"
    return f"{key[:4]}…"


def _redact(text: str, key: str) -> str:
    """Last line of defence: never let a full key reach the terminal.

    Provider errors do not normally echo the credential, but the guarantee
    asked for is "no full key in any output", and a guarantee that depends on
    an upstream library's good manners is not one.
    """
    if key and len(key) >= 8 and key in text:
        return text.replace(key, mask_key(key))
    return text


def _tcp_probe(host: str, port: int, timeout: float) -> float:
    """Open a TCP connection and report how long it took. Raises on failure."""
    started = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    return time.monotonic() - started


def _proxy_note() -> str | None:
    """Name the proxy env var in use, if any.

    httpx honours these; a raw TCP probe does not. So when a proxy is set, a
    direct connect failing tells you nothing about whether the model is
    reachable, and the probe has to say so instead of crying firewall.
    """
    for name in _PROXY_ENV_VARS:
        if os.getenv(name):
            return f"{name} is set"
    return None


# Google's own status text for the same two cases, in case the numeric code is
# not on the exception.
_OVERLOADED_RE = re.compile(r"\b(503|504)\b|\bUNAVAILABLE\b|\bDEADLINE_EXCEEDED\b")


def _model_hint(exc: BaseException, elapsed: float, timeout: float) -> str:
    """Turn a model-layer failure into the next thing to try."""
    name = type(exc).__name__
    detail = str(exc)
    # 503/504 mean Google answered and said it is slow or overloaded. Check this
    # before the deadline test: a 504 that arrives near 60s is Google's answer,
    # not a blocked connection.
    if _OVERLOADED_RE.search(detail) or getattr(exc, "code", None) in (503, 504):
        return (
            "Google's server is slow or overloaded (503/504). ORBIT already tried the "
            "fallback model if one is set, so wait a few minutes and try again. "
            "This is not a key or firewall problem."
        )
    # A failure that lands at or beyond the deadline means the request never
    # came back at all: the connection is being blocked or black-holed.
    if elapsed >= timeout * 0.9:
        return (
            "the request never came back — this is a blocked or firewalled connection, "
            "not a key problem. If your network needs a proxy, set HTTPS_PROXY (httpx "
            "reads it) and re-run."
        )
    if "401" in detail or "403" in detail or "Unauthenticated" in name or "PermissionDenied" in name:
        return "the key was rejected or is restricted. Check the key is valid and enabled for this API."
    if "404" in detail or "NotFound" in name or "not found" in detail.casefold():
        return (
            "this model id is not available to your key. Compare GEMINI_MODEL in .env with "
            "the current list on ai.google.dev, and update config.py and .env.example together."
        )
    # The TCP probe can succeed while something in between still breaks TLS, so
    # a connect-level failure after a "reachable" network is still a network
    # answer, not a credential one.
    if any(
        marker in name or marker in detail
        for marker in ("ConnectError", "ConnectTimeout", "ReadError", "SSL", "TLS", "EOF", "ProxyError")
    ):
        return (
            "the connection was broken before the API answered — usually a proxy or TLS "
            "interception. If your network needs a proxy, set HTTPS_PROXY (httpx reads it) "
            "and re-run. This is not a key problem."
        )
    return "the API was reached but the call failed. The line above is the provider's own error."


def run_check(
    *,
    write: Callable[[str], None] = print,
    settings: Settings | None = None,
    connect: Callable[[str, int, float], float] | None = None,
    provider: Any | None = None,
) -> int:
    """Walk config -> network -> model and name the first layer that fails.

    Never raises: every layer is wrapped, so a broken setup prints which layer
    broke rather than a traceback. The credential is masked everywhere.
    """
    write("ORBIT · setup check (config -> network -> model)")

    # 1. config — is there a usable key and a deadline at all?
    try:
        resolved = settings if settings is not None else load_settings()
    except Exception as exc:
        write(f"[config] FAIL — {type(exc).__name__}: {exc}")
        write("        fix: cp .env.example .env, then set GEMINI_API_KEY to your own key.")
        return EXIT_CONFIG
    key = resolved.gemini_api_key
    write(
        f"[config] ok — key {mask_key(key)} (masked), "
        f"model {resolved.gemini_model}, fallback {resolved.gemini_fallback_model or 'none'}, "
        f"timeout {resolved.request_timeout_seconds:g}s"
    )

    # 2. network — can this machine open a socket to the API endpoint?
    probe_timeout = min(resolved.request_timeout_seconds, NETWORK_PROBE_TIMEOUT_SECONDS)
    proxy = _proxy_note()
    if proxy:
        write(f"[network] skipped — {proxy}; a direct TCP probe would not use it, so the model call below decides.")
    else:
        connect_fn = connect or _tcp_probe
        try:
            elapsed = connect_fn(GEMINI_HOST, GEMINI_PORT, probe_timeout)
        except Exception as exc:
            write(f"[network] FAIL after {probe_timeout:g}s — {type(exc).__name__}: {exc}")
            write(
                f"          could not open a TCP connection to {GEMINI_HOST}:{GEMINI_PORT}. "
                "Firewall, DNS, or offline. If your network requires a proxy, set HTTPS_PROXY "
                "(httpx reads it) and re-run."
            )
            return EXIT_NETWORK
        write(f"[network] ok — connected to {GEMINI_HOST}:{GEMINI_PORT} in {elapsed:.1f}s")

    # 3. model — does a real reply come back, within the deadline?
    try:
        target = provider if provider is not None else GeminiProvider()
    except Exception as exc:
        write(f"[model] FAIL — could not build the provider: {_redact(str(exc), key)}")
        return EXIT_MODEL

    started = time.monotonic()
    try:
        reply = target.reply(
            [ChatMessage(role="user", content="ping")], system_prompt=CHECK_PROMPT
        )
    except KeyboardInterrupt:
        write("")
        write("[model] cancelled.")
        return EXIT_MODEL
    except Exception as exc:
        elapsed = time.monotonic() - started
        write(f"[model] FAIL after {elapsed:.1f}s — {_redact(str(exc), key)}")
        write(f"          {_model_hint(exc, elapsed, resolved.request_timeout_seconds)}")
        return EXIT_MODEL

    elapsed = time.monotonic() - started
    preview = " ".join(str(reply).split())[:80]
    answered_by = getattr(target, "last_model_used", None) or getattr(target, "model", "?")
    write(f"[model] ok after {elapsed:.1f}s — answered by {answered_by} — replied: {preview}")
    write("All three layers passed. If the REPL still seems to hang, the wait is the model, not your setup.")
    return EXIT_OK


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat.py",
        description="Talk to ORBIT in the terminal, or check the setup without talking at all.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="walk config -> network -> model, print which layer fails, and exit. Makes no tool calls.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Parse first, so an unknown flag is a usage message rather than a REPL
    # that silently swallowed it.
    args = build_arg_parser().parse_args(argv)

    if args.check:
        try:
            return run_check()
        except Exception as exc:  # the check must not itself become a traceback
            print(f"Check failed before it could start: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_CONFIG

    try:
        provider = GeminiProvider(tool_registry=build_tool_registry())
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"ORBIT · {provider.model} · chat mode (tools are gated by the confirmation prompt)")
    return run_session(provider)


if __name__ == "__main__":
    raise SystemExit(main())
