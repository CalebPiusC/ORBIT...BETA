from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.server import DEFAULT_SECRET_KEY, run_app
from app.voice import VoiceDependencyReport

REPO_ROOT = Path(__file__).resolve().parents[1]

# Run in a *fresh* interpreter, because the thing under test happens at import
# time: app/server.py loads the project .env before create_app() reads
# ORBIT_SECRET_KEY. Nothing in-process can re-run a module-level statement, so
# each case below reproduces a real launch instead of reaching into internals.
#
# config.ENV_FILE is pointed at a temporary file *before* app.server is
# imported; app.server does ``from config import ENV_FILE`` at import, so it
# picks the temporary path up exactly as it would pick up the real one. The
# repo's own .env is never read or written by these tests.
_LAUNCH_SCRIPT = """
import json
import pathlib

import config
config.ENV_FILE = pathlib.Path(__ENV_FILE__)

import app.server as server
from flask import Flask

ran = {}


def record_run(self, **options):
    ran.update(options)


Flask.run = record_run

app = server.create_app()
guard = "passed"
try:
    server._require_real_secret_key(app)
except RuntimeError as exc:
    guard = "refused: " + str(exc)

started = False
if guard == "passed":
    server.run_app(app)
    started = bool(ran)

print("RESULT" + json.dumps({
    "secret_is_default": app.secret_key == server.DEFAULT_SECRET_KEY,
    "guard": guard,
    "started": started,
    "host": ran.get("host"),
    "env_file_is_absolute": bool(getattr(server, "ENV_FILE", None))
                            and server.ENV_FILE.is_absolute(),
}))
"""


class FakeHeartbeat:
    """Stands in for the real heartbeat so no test writes runtime/heartbeat.json."""

    class _Settings:
        interval_seconds = 60.0

    def __init__(self) -> None:
        self.settings = self._Settings()


class RecordingFlaskApp:
    def __init__(self) -> None:
        self.run_options: dict[str, object] | None = None
        self.config: dict[str, object] = {"HEARTBEAT": FakeHeartbeat()}
        # A real Flask app always carries a session secret; run_app refuses to
        # serve on the committed default, so the stand-in has a real one too.
        self.secret_key = "test-session-secret-not-the-committed-default"

    def run(self, **kwargs: object) -> None:
        self.run_options = kwargs


class ServerLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        # Every run_app test runs with the scheduler stubbed out: no real thread starts.
        patcher = patch("heartbeat.start_scheduler")
        self.scheduler = patcher.start()
        self.addCleanup(patcher.stop)
        self._preserve_orbit_logger()

    def _preserve_orbit_logger(self) -> None:
        """Hand the "orbit" logger back after each test: run_app configures it once."""
        orbit = logging.getLogger("orbit")
        handlers, level, propagate = list(orbit.handlers), orbit.level, orbit.propagate

        def restore() -> None:
            orbit.handlers[:] = handlers
            orbit.setLevel(level)
            orbit.propagate = propagate

        self.addCleanup(restore)

    def test_a_blank_secret_key_is_treated_as_unset_by_the_factory(self) -> None:
        """setup.sh's untouched .env copy sets ORBIT_SECRET_KEY= (blank).

        The factory must fall back to the committed default so the offline
        suite and a bare render work; the launch guard still refuses that
        default, so nothing serves on it.
        """
        from app.server import create_app

        with patch.dict(os.environ, {"ORBIT_SECRET_KEY": ""}):
            app = create_app()
        self.assertEqual(app.secret_key, DEFAULT_SECRET_KEY)
        guard_app = RecordingFlaskApp()
        guard_app.secret_key = app.secret_key
        with patch.dict(os.environ, {"ORBIT_SECRET_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "ORBIT_SECRET_KEY is not set"):
                run_app(guard_app)  # type: ignore[arg-type]

    def test_run_app_starts_scheduler_for_the_attached_heartbeat(self) -> None:
        app = RecordingFlaskApp()
        heartbeat = app.config["HEARTBEAT"]
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}):
            run_app(app)  # type: ignore[arg-type]

        self.scheduler.assert_called_once_with(heartbeat, interval_seconds=60.0)

    def test_run_app_builds_heartbeat_at_boot_when_none_attached(self) -> None:
        from config import HeartbeatSettings
        from heartbeat import Heartbeat

        app = RecordingFlaskApp()
        app.config["HEARTBEAT"] = None
        settings = HeartbeatSettings(plan_hour=10, checkin_gap_days=2, interval_seconds=60.0,
                                     ntfy_server="https://ntfy.sh", ntfy_topic="")
        with (
            patch("config.load_heartbeat_settings", return_value=settings),
            patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}),
        ):
            run_app(app)  # type: ignore[arg-type]
        built = app.config["HEARTBEAT"]
        self.assertIsInstance(built, Heartbeat)
        self.scheduler.assert_called_once_with(built, interval_seconds=60.0)

    def test_defaults_to_loopback_with_debug_disabled(self) -> None:
        app = RecordingFlaskApp()
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}):
            run_app(app)  # type: ignore[arg-type]

        self.assertEqual(
            app.run_options,
            {"host": "127.0.0.1", "port": 5000, "debug": False},
        )

    def test_preview_can_opt_into_external_bind_without_debugger(self) -> None:
        app = RecordingFlaskApp()
        with patch.dict(os.environ, {"ORBIT_HOST": "0.0.0.0", "PORT": "8123"}):
            run_app(app)  # type: ignore[arg-type]

        self.assertEqual(
            app.run_options,
            {"host": "0.0.0.0", "port": 8123, "debug": False},
        )

    def test_invalid_port_fails_before_starting_server(self) -> None:
        app = RecordingFlaskApp()
        with patch.dict(os.environ, {"ORBIT_HOST": "0.0.0.0", "PORT": "0"}):
            with self.assertRaisesRegex(RuntimeError, "between 1 and 65535"):
                run_app(app)  # type: ignore[arg-type]

        self.assertIsNone(app.run_options)

    def test_run_app_reports_what_the_voice_dependencies_are_at_boot(self) -> None:
        class FakeVoiceService:
            def dependencies(self):
                return VoiceDependencyReport(
                    model="base",
                    device="cpu",
                    compute_type="int8",
                    pyav="18.1.0",
                    ffmpeg_libraries={"libavcodec": "62.28.102"},
                    ffmpeg_program=None,
                    model_cached=False,
                    cache_dir="/tmp/hf-cache",
                )

        app = RecordingFlaskApp()
        app.config["VOICE_SERVICE"] = FakeVoiceService()
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}):
            with self.assertLogs("orbit.server", level="INFO") as captured:
                run_app(app)  # type: ignore[arg-type]

        [record] = [r for r in captured.records if "voice dependencies" in r.getMessage()]
        logged = record.getMessage()
        self.assertIn("pyav=18.1.0", logged)
        self.assertIn("ffmpeg_program=not installed", logged)
        self.assertIn("ffmpeg_program_required=False", logged)
        self.assertIn("model_cached=False", logged)

    def test_run_app_survives_a_voice_service_that_cannot_report(self) -> None:
        class OpaqueVoiceService:
            def dependencies(self):
                raise RuntimeError("no diagnostics here")

        app = RecordingFlaskApp()
        app.config["VOICE_SERVICE"] = OpaqueVoiceService()
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": ""}):
            with self.assertLogs("orbit.server", level="ERROR") as captured:
                run_app(app)  # type: ignore[arg-type]

        self.assertTrue(
            any("voice dependency diagnostics" in r.getMessage() for r in captured.records)
        )

    def test_run_app_refuses_to_start_without_a_session_secret(self) -> None:
        """No secret means forged sessions, so the server must not come up."""
        app = RecordingFlaskApp()
        app.secret_key = None
        with patch.dict(os.environ, {"ORBIT_HOST": "", "PORT": "", "ORBIT_SECRET_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "ORBIT_SECRET_KEY is not set"):
                run_app(app)  # type: ignore[arg-type]

        self.assertIsNone(app.run_options)
        self.scheduler.assert_not_called()

    def test_run_app_refuses_the_committed_default_secret(self) -> None:
        """The fallback is public in this repo; serving on it is forgeable."""
        app = RecordingFlaskApp()
        app.secret_key = DEFAULT_SECRET_KEY
        with patch.dict(
            os.environ,
            {"ORBIT_HOST": "", "PORT": "", "ORBIT_SECRET_KEY": DEFAULT_SECRET_KEY},
        ):
            with self.assertRaisesRegex(RuntimeError, "committed default"):
                run_app(app)  # type: ignore[arg-type]

        self.assertIsNone(app.run_options)
        self.scheduler.assert_not_called()

    def test_run_app_accepts_a_real_secret_from_the_environment(self) -> None:
        app = RecordingFlaskApp()
        app.secret_key = None
        with patch.dict(
            os.environ,
            {"ORBIT_HOST": "", "PORT": "", "ORBIT_SECRET_KEY": "a-real-random-secret"},
        ):
            run_app(app)  # type: ignore[arg-type]

        self.assertEqual(app.run_options, {"host": "127.0.0.1", "port": 5000, "debug": False})

    # ----- the project's .env at launch ------------------------------------
    #
    # The bug these cover: a fresh terminal whose only copy of
    # ORBIT_SECRET_KEY was in .env refused to start, because the first
    # load_dotenv happened inside VoiceService.__init__, after create_app()
    # had already frozen app.secret_key to the committed default.

    def _launch(self, env_file: Path, *, cwd: Path, secret: str | None = None) -> dict:
        """Boot the server in a fresh interpreter and report what happened."""
        script = _LAUNCH_SCRIPT.replace("__ENV_FILE__", repr(str(env_file)))
        home = Path(tempfile.mkdtemp(prefix="orbit-launch-home-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        env = {key: value for key, value in os.environ.items() if key != "ORBIT_SECRET_KEY"}
        if secret is not None:
            env["ORBIT_SECRET_KEY"] = secret
        # A real launch installs SQLite stores; keep that database (and any
        # model cache) inside the throwaway home rather than the real one.
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT")]
        self.assertEqual(len(lines), 1, completed.stdout + completed.stderr)
        payload = json.loads(lines[0][len("RESULT"):])
        payload["output"] = completed.stdout + completed.stderr
        return payload

    def test_a_key_that_lives_only_in_the_env_file_starts_the_server(self) -> None:
        """The reported bug: .env holds the key, the terminal holds nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            secret = "secret-from-the-env-file-4c1d9e"
            env_file.write_text(f"ORBIT_SECRET_KEY={secret}\n", encoding="utf-8")

            result = self._launch(env_file, cwd=REPO_ROOT)

        self.assertFalse(result["secret_is_default"])   # create_app saw the key
        self.assertEqual(result["guard"], "passed")     # the launch gate agrees
        self.assertTrue(result["started"])              # run_app reached app.run()
        self.assertEqual(result["host"], "127.0.0.1")
        # "Never log or print the key": the secret appears in no output at all.
        self.assertNotIn(secret, result["output"])

    def test_a_missing_key_is_still_refused_everywhere(self) -> None:
        """No .env, no variable: the guard must still refuse, not fall back."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._launch(Path(tmp) / "absent.env", cwd=REPO_ROOT)

        self.assertTrue(result["secret_is_default"])
        self.assertTrue(result["guard"].startswith("refused:"))
        self.assertIn("ORBIT_SECRET_KEY is not set", result["guard"])
        self.assertFalse(result["started"])

    def test_the_project_env_is_loaded_from_any_working_directory(self) -> None:
        """config.ENV_FILE is resolved from the checkout, not from the cwd."""
        from config import ENV_FILE

        self.assertTrue(ENV_FILE.is_absolute())
        self.assertEqual(ENV_FILE, REPO_ROOT / ".env")

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("ORBIT_SECRET_KEY=secret-from-elsewhere-9a7b\n", encoding="utf-8")
            elsewhere = Path(tempfile.mkdtemp(prefix="orbit-launch-cwd-"))
            self.addCleanup(lambda: __import__("shutil").rmtree(elsewhere, ignore_errors=True))

            # Launched from a directory that contains no .env of its own.
            result = self._launch(env_file, cwd=elsewhere)

        self.assertTrue(result["env_file_is_absolute"])
        self.assertFalse(result["secret_is_default"])
        self.assertEqual(result["guard"], "passed")
        self.assertTrue(result["started"])

    def test_demo_launcher_uses_shared_safe_runner(self) -> None:
        import run_demo

        # SimpleNamespace, not object(): the launcher sets a per-run session
        # secret on the app, which a bare object() cannot carry.
        fake_app = SimpleNamespace()
        with (
            patch("run_demo.create_app", return_value=fake_app),
            patch("run_demo.run_app") as runner,
            patch("builtins.print"),
        ):
            run_demo.main()

        runner.assert_called_once_with(fake_app)

    def test_demo_launcher_mints_a_secret_instead_of_using_the_default(self) -> None:
        """The stub launcher must not fall back to the committed key."""
        import run_demo

        fake_app = SimpleNamespace()
        with (
            patch("run_demo.create_app", return_value=fake_app),
            patch("run_demo.run_app"),
            patch("builtins.print"),
            patch.dict(os.environ, {"ORBIT_SECRET_KEY": ""}),
        ):
            run_demo.main()

        self.assertNotEqual(fake_app.secret_key, DEFAULT_SECRET_KEY)
        self.assertTrue(fake_app.secret_key)


if __name__ == "__main__":
    unittest.main()
