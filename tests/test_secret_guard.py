from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class SecretHookTests(unittest.TestCase):
    def test_hook_allows_safe_content_and_rejects_google_key_shapes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orbit-secret-hook-test-") as temp_dir:
            repo = Path(temp_dir)
            self.assertEqual(run_git("init", "-q", cwd=repo).returncode, 0)
            (repo / ".githooks").mkdir()
            shutil.copy2(ROOT / ".gitignore", repo / ".gitignore")
            shutil.copy2(ROOT / ".env.example", repo / ".env.example")
            shutil.copy2(ROOT / "check_secrets.py", repo / "check_secrets.py")
            shutil.copy2(ROOT / ".githooks" / "pre-commit", repo / ".githooks" / "pre-commit")
            for key, value in (
                ("core.hooksPath", ".githooks"),
                ("user.name", "ORBIT test"),
                ("user.email", "orbit-test@example.invalid"),
                ("commit.gpgsign", "false"),
            ):
                self.assertEqual(run_git("config", key, value, cwd=repo).returncode, 0)

            (repo / "safe.txt").write_text("No credentials here.\n", encoding="utf-8")
            staged = run_git("add", ".", cwd=repo)
            self.assertEqual(staged.returncode, 0, staged.stderr)
            safe_commit = run_git("commit", "-m", "safe fixture", cwd=repo)
            self.assertEqual(safe_commit.returncode, 0, safe_commit.stdout + safe_commit.stderr)

            # Construct synthetic format-shaped values at runtime so no key-like
            # string is stored in this test or ever committed to this repository.
            google_key = "AIza" + "A" * 35
            google_aq_key = "AQ" + "." + "B" * 32
            (repo / ".env").write_text(
                f"GEMINI_API_KEY={google_key}\nOTHER_KEY={google_aq_key}\n",
                encoding="utf-8",
            )
            force_add = run_git("add", "-f", ".env", cwd=repo)
            self.assertEqual(force_add.returncode, 0, force_add.stderr)
            rejected = run_git("commit", "-m", "must be rejected", cwd=repo)

            output = rejected.stdout + rejected.stderr
            self.assertNotEqual(rejected.returncode, 0, "credential-shaped content was committed")
            self.assertIn("Google AI API key (AIza)", output)
            self.assertIn("Google API key (AQ.)", output)
            self.assertNotIn(google_key, output)
            self.assertNotIn(google_aq_key, output)


if __name__ == "__main__":
    unittest.main()
