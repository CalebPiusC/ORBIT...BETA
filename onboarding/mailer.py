"""Sending verification and reset email — with the seam left visible.

There is no transactional email provider wired into this build, and pretending
otherwise would be the worst kind of placeholder: a person stuck on "check your
inbox" forever with nothing in it.

So the seam is explicit. ``Mailer`` is the protocol; the deployment's
configuration decides which implementation is real:

* ``SmtpMailer`` — used when ``ORBIT_SMTP_HOST`` and friends are set. It sends
  real mail over SMTP and records only the fact that it sent, never the token.
* ``ConsoleMailer`` — the default. It writes the message to the console, to
  ``runtime/outbox/``, and returns the link so the interface can offer it as a
  clearly-labeled **demo mailbox** panel on the very screen that says "check your
  inbox". The panel names itself as a stand-in; it never claims an email was sent.

The verification *link* is the only sensitive thing in an email, and it is never
written to the audit log by any path here.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol


@dataclass
class Message:
    to: str
    subject: str
    body: str
    kind: str  # verification | reset | welcome | feedback-receipt
    link: str = ""
    sent_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    transport: str = "console"

    def as_dict(self) -> dict[str, Any]:
        return {
            "to": self.to,
            "subject": self.subject,
            "body": self.body,
            "kind": self.kind,
            "link": self.link,
            "sent_at": self.sent_at,
            "transport": self.transport,
        }


class Mailer(Protocol):
    name: str
    delivers: bool

    def send(self, message: Message) -> Message: ...


class ConsoleMailer:
    """Writes the message where the operator and the demo panel can read it."""

    name = "Console mailbox"
    delivers = False

    def __init__(self, outbox: str | Path) -> None:
        self.outbox = Path(outbox)
        self.recent: list[Message] = []

    def send(self, message: Message) -> Message:
        message.transport = "console"
        self.recent = [item for item in self.recent if item.to != message.to][-8:] + [message]
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            path = self.outbox / f"{message.kind}-{message.to.replace('@', '_at_')}.txt"
            path.write_text(f"To: {message.to}\nSubject: {message.subject}\n\n{message.body}\n", encoding="utf-8")
        except OSError:
            # A read-only filesystem must not break sign-up. The link is still
            # available to the route that asked for it.
            pass
        return message


class SmtpMailer:
    """Real mail, used only when the deployment configured it."""

    name = "SMTP"
    delivers = True

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        sender: str = "",
        use_tls: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender or username or "orbit@localhost"
        self.use_tls = use_tls

    def send(self, message: Message) -> Message:
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.body)
        if self.use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(self.host, self.port, timeout=20) as server:
                server.starttls(context=context)
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(email)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=20) as server:
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(email)
        message.transport = "smtp"
        return message


def build_mailer(runtime_dir: str | Path) -> Mailer:
    """Pick the transport from configuration, and say which one it picked."""
    host = (os.environ.get("ORBIT_SMTP_HOST") or "").strip()
    if not host:
        return ConsoleMailer(Path(runtime_dir) / "outbox")
    return SmtpMailer(
        host=host,
        port=int((os.environ.get("ORBIT_SMTP_PORT") or "587").strip() or 587),
        username=(os.environ.get("ORBIT_SMTP_USER") or "").strip(),
        password=(os.environ.get("ORBIT_SMTP_PASSWORD") or "").strip(),
        sender=(os.environ.get("ORBIT_SMTP_FROM") or "").strip(),
        use_tls=(os.environ.get("ORBIT_SMTP_TLS") or "1").strip() not in ("0", "false", "no"),
    )


# --------------------------------------------------------------------------- #
# Message bodies
# --------------------------------------------------------------------------- #

def verification_message(*, to: str, name: str, link: str, product: str = "ORBIT") -> Message:
    return Message(
        to=to,
        kind="verification",
        subject=f"Confirm your email for {product}",
        link=link,
        body=(
            f"Hi {name},\n\n"
            f"Confirm this address to finish setting up your {product} account:\n\n"
            f"  {link}\n\n"
            "The link works once and expires in 24 hours. Until it is used, your "
            "workspace stays closed — that is on purpose, so an address someone "
            "typed by accident never becomes an account.\n\n"
            "If you did not sign up, you can ignore this message. Nothing was "
            "created and nothing will be.\n"
        ),
    )


def reset_message(*, to: str, name: str, link: str, product: str = "ORBIT") -> Message:
    return Message(
        to=to,
        kind="reset",
        subject=f"Reset your {product} password",
        link=link,
        body=(
            f"Hi {name},\n\n"
            f"Set a new {product} password with this link:\n\n"
            f"  {link}\n\n"
            "It works once and expires in 60 minutes. Resetting your password "
            "signs out every other device.\n\n"
            "If you did not ask for this, no action is needed — your password has "
            "not changed.\n"
        ),
    )


def change_email_message(*, to: str, name: str, link: str, product: str = "ORBIT") -> Message:
    return Message(
        to=to,
        kind="verification",
        subject=f"Confirm your new {product} email address",
        link=link,
        body=(
            f"Hi {name},\n\n"
            f"Confirm this address as your new {product} login:\n\n"
            f"  {link}\n\n"
            "Until it is confirmed, your workspace stays locked — a new address is "
            "a new inbox, and we confirm it the same way as the first one.\n"
        ),
    )


def feedback_receipt(*, to: str, name: str, kind: str, reference: str, product: str = "ORBIT") -> Message:
    return Message(
        to=to,
        kind="feedback-receipt",
        subject=f"We got your {kind} — {reference}",
        body=(
            f"Hi {name},\n\n"
            f"Your {kind} reached the {product} team. Reference {reference}.\n\n"
            "During beta a person reads these. Status changes are not automated "
            "yet — if we need anything from you, we will reply to this address.\n\n"
            "Thank you for taking the time.\n"
        ),
    )


def outbox_view(mailer: Mailer, limit: int = 5) -> list[Message]:
    """What the demo-mailbox panel shows, newest first."""
    return list(reversed(getattr(mailer, "recent", [])[-limit:]))
