#!/usr/bin/env python3
"""Scan staged Git blobs for common credential formats before a commit."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Final

# Match recognizable token formats, never print the matched bytes. The Google
# AQ. variant is included alongside the familiar AIza-prefixed API keys.
SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[bytes]], ...]] = (
    (
        "Google AI API key (AIza)",
        re.compile(rb"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    (
        "Google API key (AQ.)",
        re.compile(rb"(?<![A-Za-z0-9_-])AQ\.[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"),
    ),
    (
        "Google OAuth client secret",
        re.compile(rb"(?<![A-Za-z0-9_-])GOCSPX-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    (
        "Anthropic API key",
        re.compile(rb"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    (
        "OpenAI API key",
        re.compile(rb"(?<![A-Za-z0-9_-])sk-(?:proj-)?(?!ant-)[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
    (
        "Stripe live secret key",
        re.compile(rb"(?<![A-Za-z0-9_])(?:sk|rk)_live_[A-Za-z0-9]{16,}(?![A-Za-z0-9_])"),
    ),
    (
        "GitHub personal access token",
        re.compile(rb"(?<![A-Za-z0-9_])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])"),
    ),
    (
        "AWS access key ID",
        re.compile(rb"(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    (
        "Slack token",
        re.compile(rb"(?<![A-Za-z0-9_-])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])"),
    ),
    (
        "private key block",
        re.compile(
            rb"-{5}BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-{5}"
            rb"|-{5}BEGIN PGP PRIVATE KEY BLOCK-{5}"
        ),
    ),
)


def _git_output(repo_root: str, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout


def _staged_blobs(repo_root: str) -> list[tuple[bytes, bytes]]:
    """Return (path, blob oid) pairs for added/changed staged files."""
    changed = _git_output(
        repo_root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMRT",
        "-z",
        "--",
    )
    changed_paths = [path for path in changed.split(b"\0") if path]
    if not changed_paths:
        return []

    index = _git_output(repo_root, "ls-files", "--stage", "-z")
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in index.split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, oid, stage = header.split()
        if stage == b"0":
            entries[path] = (mode, oid)

    return [
        (path, entries[path][1])
        for path in changed_paths
        if path in entries and entries[path][0] != b"160000"
    ]


def scan_staged_content(repo_root: str) -> list[tuple[str, str]]:
    """Return safe (path, pattern label) findings from staged Git blobs."""
    findings: list[tuple[str, str]] = []
    for raw_path, oid in _staged_blobs(repo_root):
        content = subprocess.run(
            ["git", "cat-file", "blob", oid.decode("ascii")],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        path = os.fsdecode(raw_path)
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                findings.append((path, label))
    return findings


def main() -> int:
    try:
        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        findings = scan_staged_content(repo_root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Secret scan could not inspect the Git index: {exc}", file=sys.stderr)
        return 2

    if findings:
        print("Secret scan failed: credential-shaped content is staged.", file=sys.stderr)
        for path, label in findings:
            # repr keeps unusual path characters from being interpreted by a terminal.
            print(f"  {path!r}: {label}", file=sys.stderr)
        print("Remove the credential from the staged content and rotate it if it was real.", file=sys.stderr)
        return 1

    print("Staged-content secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
