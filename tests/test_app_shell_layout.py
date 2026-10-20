"""The shared app shell's own measurements, pinned on their own.

Kept out of ``tests/test_onboarding.py`` on purpose: that file's
``LayoutPassTests`` is where the sticky-panel layout work also lands, and two
branches adding tests to the same class in the same place merge into a
conflict that says nothing about either change. The shell's numbers belong by
themselves anyway — every screen that extends ``app_shell.html`` inherits them.

Sources: design/ORBIT UI___2/ phone frames (780 wide = 390 at 2x). The
content edges measure 24 there on Dashboard, Projects, Import project,
Agents, Analytics, Settings, Connected apps, Approval review, Reminders,
Onboarding, Landing and Workspace alike.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def template(name: str) -> str:
    return (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")


class AppShellLayoutTests(unittest.TestCase):
    def test_the_phone_gutter_is_the_frames_24px(self) -> None:
        app_shell = template("app_shell.html")
        phone = app_shell[app_shell.index("@media (max-width: 900px)"):]
        self.assertIn("--app-pad-x: 24px;", phone)
        self.assertIn("padding: 0 24px;", phone)

    def test_the_desktop_gutters_still_come_from_the_shell_variables(self) -> None:
        app_shell = template("app_shell.html")
        self.assertIn("--app-pad-y: clamp(24px, 4.4vh, 40px);", app_shell)
        self.assertIn("--app-pad-x: clamp(24px, 2.8vw, 40px);", app_shell)
        self.assertIn(
            "padding: var(--app-pad-y) var(--app-pad-x) var(--app-pad-bottom);",
            app_shell,
        )

    def test_every_app_shell_screen_inherits_it(self) -> None:
        """No screen overrides the phone gutter or pads its own edges."""
        screens = [
            path for path in (ROOT / "app" / "templates").glob("*.html")
            if 'extends "app_shell.html"' in path.read_text(encoding="utf-8")
        ]
        self.assertTrue(screens)
        for path in screens:
            body = path.read_text(encoding="utf-8")
            self.assertNotIn("--app-pad-x", body, path.name)
            self.assertNotIn("--app-pad-y", body, path.name)


if __name__ == "__main__":
    unittest.main()
