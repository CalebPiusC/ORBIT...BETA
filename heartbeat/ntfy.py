"""Phone push through ntfy.sh: one plain HTTP POST per notice.

The topic is the only secret: anyone who knows it can read the notices. It
lives in .env and is never written to the audit log or an error message.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

DEFAULT_TIMEOUT_SECONDS = 5.0


class PushError(RuntimeError):
    """A push did not reach ntfy. The message names the failure type, never the topic."""


def push(
    topic: str,
    *,
    server: str,
    title: str,
    message: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> None:
    if not topic:
        raise PushError("no topic is configured")
    if not title.isascii():
        raise ValueError("ntfy titles travel in an HTTP header and must be ASCII.")
    url = f"{server.rstrip('/')}/{quote(topic, safe='')}"
    headers = {"Title": title, "Tags": "bell"}
    try:
        if client is not None:
            response = client.post(url, content=message.encode("utf-8"), headers=headers, timeout=timeout)
        else:
            response = httpx.post(url, content=message.encode("utf-8"), headers=headers, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Only the type is kept: the URL, and so the topic, must not leak into logs.
        raise PushError(f"ntfy push failed: {type(exc).__name__}") from None
